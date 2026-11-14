//! API integration tests.
//!
//! Each test calls `spawn_server()`, which creates a fresh PostgreSQL database
//! on a host Postgres and starts an FMS HTTP server bound to a random port.
//! Tests are fully isolated: every test gets its own in-memory state and its
//! own database, so they may run in parallel without interference.
//!
//! Prerequisites: a Postgres reachable at $FMS_TEST_DATABASE_URL (default
//! postgres://postgres:postgres@127.0.0.1:5432). `make test-fms` starts an
//! ephemeral local cluster for this; no external services are otherwise needed.

use std::sync::atomic::{AtomicBool, AtomicU32, Ordering};
use std::sync::Arc;

use chrono::Duration as ChronoDuration;
use chrono::Utc;
use reqwest::StatusCode;
use sqlx::postgres::PgPoolOptions;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpListener;
use uuid::Uuid;

use fms::api;
use fms::config::Config;
use fms::db::run_migrations;
use fms::log_queue::{LogQueues, QUEUE_DEPTH};
use fms::state::{AppState, AtgReading, EventKind, JobStatus, JobValidationError, Session};

// ---------------------------------------------------------------------------
// Host Postgres (each test creates a fresh database on it)
// ---------------------------------------------------------------------------

/// Base connection URL (no database name) for the host Postgres. Defaults to the
/// local ephemeral cluster started by `make test-fms`; override with
/// $FMS_TEST_DATABASE_URL (e.g. a CI-provided Postgres).
fn pg_base_url() -> String {
    std::env::var("FMS_TEST_DATABASE_URL")
        .unwrap_or_else(|_| "postgres://postgres:postgres@127.0.0.1:5432".to_string())
}

// ---------------------------------------------------------------------------
// Argon2 admin hash (computed once)
// ---------------------------------------------------------------------------

const TEST_ADMIN_PASSWORD: &str = "Admin1234!";

/// The vessel `create_job` names, and the holder `enrol_card` enrols to, so an
/// enrolled card satisfies the vessel RFID gate of a created dispense job.
const TEST_VESSEL: &str = "MV Harrier";

static ADMIN_HASH: std::sync::OnceLock<String> = std::sync::OnceLock::new();

fn admin_hash() -> &'static str {
    ADMIN_HASH.get_or_init(|| {
        use argon2::password_hash::{rand_core::OsRng, PasswordHasher, SaltString};
        use argon2::Argon2;
        let salt = SaltString::generate(&mut OsRng);
        Argon2::default()
            .hash_password(TEST_ADMIN_PASSWORD.as_bytes(), &salt)
            .expect("argon2 hash failed")
            .to_string()
    })
}

// ---------------------------------------------------------------------------
// Per-test server
// ---------------------------------------------------------------------------

struct TestServer {
    pub base_url: String,
    pub state: AppState,
    // Keeps the channel receiver alive so event sends don't immediately error,
    // and lets a test read back what the handlers emitted.
    pub log_queues: LogQueues,
    /// The ATG console the server's on-demand reads reach, reporting 35 000 L
    /// until a test changes it. A dispense's cavitation checks take a fresh
    /// reading only (fms.md §5.1), so a test that needs none takes it down.
    pub atg: FakeAtg,
}

impl TestServer {
    pub fn client(&self) -> reqwest::Client {
        reqwest::Client::builder()
            .redirect(reqwest::redirect::Policy::none())
            .build()
            .unwrap()
    }

    /// POST /api/v1/auth/login as admin; panics if login fails.
    pub async fn admin_login(&self) -> String {
        let resp = self
            .client()
            .post(format!("{}/api/v1/auth/login", self.base_url))
            .json(&serde_json::json!({
                "username": "fms-admin",
                "password": TEST_ADMIN_PASSWORD,
            }))
            .send()
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK, "admin login failed");
        resp.json::<serde_json::Value>().await.unwrap()["session_token"]
            .as_str()
            .unwrap()
            .to_owned()
    }

    /// Insert an operator session directly, bypassing the directory bind and the
    /// RFID challenge; returns the bearer token.
    ///
    /// Writes both halves a real login writes (`fms.md` §4): the in-memory
    /// session and its `sessions` row. A job records the session it was created
    /// under as a foreign key to that row (`fms.md` §8.3), so a session held only
    /// in memory is one no real login could produce.
    pub async fn inject_operator_session(&self) -> String {
        let token = Uuid::new_v4().to_string();
        let session = Session {
            session_id: Uuid::new_v4(),
            username: "test-operator".to_owned(),
            role: "operator".to_owned(),
            expires_at: Utc::now() + ChronoDuration::seconds(3600),
        };
        sqlx::query("INSERT INTO sessions (session_id, username, role) VALUES ($1, $2, $3)")
            .bind(session.session_id)
            .bind(&session.username)
            .bind(&session.role)
            .execute(&self.state.db)
            .await
            .expect("inject sessions row failed");
        self.state
            .sessions
            .write()
            .await
            .insert(token.clone(), session);
        token
    }

    /// The `session_id` behind a bearer token, from the in-memory session map.
    pub async fn session_id_of(&self, token: &str) -> Uuid {
        self.state
            .sessions
            .read()
            .await
            .get(token)
            .expect("no session for token")
            .session_id
    }

    /// Drain the domain-event queue and return the events of one kind, in the
    /// order they were emitted. Events of other kinds are discarded.
    pub fn events_of(&self, kind: EventKind) -> Vec<fms::state::LogEvent> {
        self.log_queues
            .events
            .drain()
            .into_iter()
            .filter(|e| e.kind == kind)
            .collect()
    }

    /// `jobs.session_id` for a job, read from the database.
    pub async fn job_session_id(&self, job_id: &str) -> Uuid {
        sqlx::query_scalar::<_, Uuid>("SELECT session_id FROM jobs WHERE job_id = $1")
            .bind(Uuid::parse_str(job_id).unwrap())
            .fetch_one(&self.state.db)
            .await
            .expect("reading jobs.session_id failed")
    }

    /// Enrol a card to `TEST_VESSEL` via the API; returns the card_id UUID string.
    pub async fn enrol_card(&self, admin_token: &str, card_number: i32) -> String {
        self.enrol_card_for(admin_token, card_number, TEST_VESSEL)
            .await
    }

    /// Enrol a card to `holder` via the API; returns the card_id UUID string.
    pub async fn enrol_card_for(
        &self,
        admin_token: &str,
        card_number: i32,
        holder: &str,
    ) -> String {
        let resp = self
            .client()
            .post(format!("{}/api/v1/cards", self.base_url))
            .bearer_auth(admin_token)
            .json(&serde_json::json!({
                "card_number": card_number,
                "label": holder,
            }))
            .send()
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::CREATED);
        resp.json::<serde_json::Value>().await.unwrap()["card_id"]
            .as_str()
            .unwrap()
            .to_owned()
    }

    /// `rfid_validated` on the current job.
    pub async fn current_job_rfid_validated(&self, token: &str) -> bool {
        self.client()
            .get(format!("{}/api/v1/jobs/current", self.base_url))
            .bearer_auth(token)
            .send()
            .await
            .unwrap()
            .json::<serde_json::Value>()
            .await
            .unwrap()["rfid_validated"]
            .as_bool()
            .unwrap()
    }

    /// The current job's document, asserting a `200`.
    pub async fn current_job(&self, token: &str) -> serde_json::Value {
        let resp = self
            .client()
            .get(format!("{}/api/v1/jobs/current", self.base_url))
            .bearer_auth(token)
            .send()
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        resp.json::<serde_json::Value>().await.unwrap()
    }

    /// Create a job; returns job_id.
    pub async fn create_job(&self, token: &str, job_type: &str) -> String {
        let resp = self
            .client()
            .post(format!("{}/api/v1/jobs", self.base_url))
            .bearer_auth(token)
            .json(&serde_json::json!({
                "type": job_type,
                "counterparty": TEST_VESSEL,
                "grade": "MGO",
            }))
            .send()
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::CREATED, "create_job failed");
        resp.json::<serde_json::Value>().await.unwrap()["job_id"]
            .as_str()
            .unwrap()
            .to_owned()
    }

    /// Insert a completed dispense (metering) transaction row directly, exactly
    /// as the Modbus background task would on auto-stop. Returns its id.
    pub async fn seed_dispense_transaction(&self, job_id: Uuid) -> Uuid {
        let tx_id = Uuid::new_v4();
        sqlx::query(
            "INSERT INTO transactions
               (transaction_id, job_id, operator, counterparty, grade,
                preset_volume_l, start_totaliser_pulses, end_totaliser_pulses,
                volume_dispensed_l, opened_at, closed_at, created_at)
             VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9, now(), now(), now())",
        )
        .bind(tx_id)
        .bind(job_id)
        .bind("test-operator")
        .bind("MV Harrier")
        .bind("MGO")
        .bind(500.0_f64)
        .bind(0_i32)
        .bind(50_000_i32)
        .bind(500.0_f64)
        .execute(&self.state.db)
        .await
        .expect("seed dispense transaction failed");
        tx_id
    }
}

fn test_config(listen_port: u16) -> Config {
    use std::time::Duration;
    Config {
        database_url: String::new(), // pool is built separately and passed to AppState
        listen_port,
        tls_cert_path: String::new(),
        tls_key_path: String::new(),
        admin_username: "fms-admin".to_owned(),
        admin_password_hash: admin_hash().to_owned(),
        ldap_uri: "ldap://127.0.0.1:9389".to_owned(), // no LDAP in unit tests
        ldap_base_dn: "dc=test,dc=local".to_owned(),
        seed_cards: Vec::new(),
        atg_host: "127.0.0.1".to_owned(),
        atg_port: 10001,
        atg_poll_interval: Duration::from_secs(3600), // do not poll in tests
        dispenser_host: "127.0.0.1".to_owned(),
        dispenser_port: 502,
        modbus_poll_interval: Duration::from_secs(3600),
        vessel_rfid_host: "127.0.0.1".to_owned(),
        vessel_rfid_tcp_port: 9001,
        // Non-existent targets: these tests assert that operations never block
        // on log delivery (fms.md §5.4), so the pusher must fail in the background.
        vmagent_url: "http://127.0.0.1:9999/api/v1/import".to_owned(),
        vlagent_jsonline_url: "http://127.0.0.1:9999/insert/jsonline".to_owned(),
        vlagent_syslog_host: "127.0.0.1".to_owned(),
        vlagent_syslog_port: 9999,
        session_timeout: std::time::Duration::from_secs(3600),
        tank_capacity_l: 50_000.0,
        cavitation_threshold_pct: 10.0, // threshold = 5 000 L
        k_factor: 100.0,
        grade: "MGO".to_owned(),
        density_kg_per_l: 0.840,
    }
}

async fn fresh_db_pool() -> sqlx::PgPool {
    let base = pg_base_url();

    // Create a fresh, uniquely named database for this test.
    let admin_url = format!("{base}/postgres");
    let admin_pool = PgPoolOptions::new()
        .max_connections(1)
        .connect(&admin_url)
        .await
        .unwrap_or_else(|e| {
            panic!(
                "failed to connect to Postgres at {admin_url}: {e}\n\
                 These integration tests require a running Postgres. Run them via \
                 `make test-fms` (starts an ephemeral local cluster), set \
                 $FMS_TEST_DATABASE_URL to point at your own, or run the \
                 database-free unit tests with `make test-fms-unit`."
            )
        });
    let db_name = format!("fms_test_{}", Uuid::new_v4().to_string().replace('-', ""));
    sqlx::query(&format!("CREATE DATABASE \"{db_name}\""))
        .execute(&admin_pool)
        .await
        .expect("failed to create test database");

    // Connect to the fresh database and apply migrations.
    let db_url = format!("{base}/{db_name}");
    let pool = PgPoolOptions::new()
        .max_connections(5)
        .connect(&db_url)
        .await
        .expect("failed to connect to test database");
    run_migrations(&pool).await.expect("migrations failed");
    pool
}

async fn spawn_server() -> TestServer {
    let pool = fresh_db_pool().await;
    // The poller does not run in these tests, so the console serves only the
    // on-demand reads.
    let atg = spawn_fake_atg("000000", false).await;

    // Bind to a random port; the OS assigns the actual port number.
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let listen_port = listener.local_addr().unwrap().port();

    let log_queues = LogQueues::new(QUEUE_DEPTH);
    let mut config = test_config(listen_port);
    config.atg_port = atg.port;
    let state = AppState::new(pool, Arc::new(config), log_queues.clone());

    let router = api::router(state.clone());
    tokio::spawn(async move {
        axum::serve(
            listener,
            router.into_make_service_with_connect_info::<std::net::SocketAddr>(),
        )
        .await
        .expect("test server failed");
    });

    TestServer {
        base_url: format!("http://127.0.0.1:{listen_port}"),
        state,
        log_queues,
        atg,
    }
}

// ===========================================================================
// Auth and middleware
// ===========================================================================

/// A path no route matches is answered by the router fallback, not by axum's
/// built-in empty-bodied 404. The JSON error body is the observable proof the
/// fallback ran: endpoint probing must reach a handler so that it is recorded in
/// the journal, rather than being rejected silently by the router itself.
#[tokio::test]
async fn unknown_route_returns_404_with_json_error_body() {
    let s = spawn_server().await;
    let resp = s
        .client()
        .get(format!("{}/api/v1/does-not-exist", s.base_url))
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::NOT_FOUND);
    let body = resp.json::<serde_json::Value>().await.unwrap();
    assert_eq!(body["error"], "not found");
}

/// An unmatched path under an unrelated prefix is treated identically: the
/// fallback is the router's, not a per-prefix one.
#[tokio::test]
async fn unknown_route_outside_api_prefix_returns_404_with_json_error_body() {
    let s = spawn_server().await;
    let resp = s
        .client()
        .post(format!("{}/admin", s.base_url))
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::NOT_FOUND);
    let body = resp.json::<serde_json::Value>().await.unwrap();
    assert_eq!(body["error"], "not found");
}

/// A known path addressed with a verb it does not serve is answered by the
/// method-not-allowed fallback. Axum's built-in 405 has an empty body and
/// reaches no handler, so verb probing against a real endpoint — which confirms
/// to the caller that the endpoint exists — would otherwise be unrecorded. Both
/// routes used here are public, so the response is the method rejection rather
/// than an auth rejection from a route-group middleware.
#[tokio::test]
async fn known_path_wrong_method_returns_405_with_json_error_body() {
    let s = spawn_server().await;
    let resp = s
        .client()
        .delete(format!("{}/api/v1/health", s.base_url))
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::METHOD_NOT_ALLOWED);
    let body = resp.json::<serde_json::Value>().await.unwrap();
    assert_eq!(body["error"], "method not allowed");
}

#[tokio::test]
async fn post_only_route_addressed_with_get_returns_405_with_json_error_body() {
    let s = spawn_server().await;
    let resp = s
        .client()
        .get(format!("{}/api/v1/auth/login", s.base_url))
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::METHOD_NOT_ALLOWED);
    let body = resp.json::<serde_json::Value>().await.unwrap();
    assert_eq!(body["error"], "method not allowed");
}

/// The fallback sits outside the route-group auth middleware, so an unknown path
/// is reported as unknown even without a token. Were it to drift behind
/// `require_auth`, probing would return 401 and be recorded as an authentication
/// failure, misattributing the activity in the journal.
#[tokio::test]
async fn unknown_route_without_token_returns_404_not_401() {
    let s = spawn_server().await;
    let resp = s
        .client()
        .get(format!("{}/api/v1/jobs/current/nonexistent", s.base_url))
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::NOT_FOUND);
    let body = resp.json::<serde_json::Value>().await.unwrap();
    assert_eq!(body["error"], "not found");
}

#[tokio::test]
async fn health_get_returns_200() {
    let s = spawn_server().await;
    let resp = s
        .client()
        .get(format!("{}/api/v1/health", s.base_url))
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let body = resp.json::<serde_json::Value>().await.unwrap();
    assert_eq!(body["status"], "ok");
    assert!(body["version"].is_string());
}

#[tokio::test]
async fn admin_login_correct_password_returns_token_and_role() {
    let s = spawn_server().await;
    let resp = s
        .client()
        .post(format!("{}/api/v1/auth/login", s.base_url))
        .json(&serde_json::json!({
            "username": "fms-admin",
            "password": TEST_ADMIN_PASSWORD,
        }))
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let body = resp.json::<serde_json::Value>().await.unwrap();
    assert!(body["session_token"].is_string());
    assert_eq!(body["role"], "admin");
}

#[tokio::test]
async fn admin_login_wrong_password_returns_401() {
    let s = spawn_server().await;
    let resp = s
        .client()
        .post(format!("{}/api/v1/auth/login", s.base_url))
        .json(&serde_json::json!({
            "username": "fms-admin",
            "password": "wrongpassword",
        }))
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::UNAUTHORIZED);
}

/// An unreachable directory is not a rejected password, and the response must
/// not reveal which of the two occurred: both are 401, so a client cannot use
/// the difference to discover which accounts exist (fms.md §4). The test
/// configuration points `ldap_uri` at a port nothing listens on, so this
/// exercises the transport-failure path rather than a bind rejection.
///
/// The two are distinguished in the log instead, which is where a certificate,
/// trust-store or routing fault against corp-it has to be visible — it is
/// otherwise indistinguishable from every operator mistyping their password.
#[tokio::test]
async fn operator_login_with_unreachable_directory_returns_401() {
    let s = spawn_server().await;
    let resp = s
        .client()
        .post(format!("{}/api/v1/auth/login", s.base_url))
        .json(&serde_json::json!({
            "username": "jrenard",
            "password": "irrelevant-the-directory-is-unreachable",
            "rfid_card_number": 1000002,
        }))
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::UNAUTHORIZED);
}

#[tokio::test]
async fn protected_route_without_bearer_returns_401() {
    let s = spawn_server().await;
    let resp = s
        .client()
        .get(format!("{}/api/v1/jobs", s.base_url))
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::UNAUTHORIZED);
}

#[tokio::test]
async fn protected_route_with_garbage_token_returns_401() {
    let s = spawn_server().await;
    let resp = s
        .client()
        .get(format!("{}/api/v1/jobs", s.base_url))
        .bearer_auth("not-a-real-token")
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::UNAUTHORIZED);
}

#[tokio::test]
async fn expired_session_on_protected_route_returns_401() {
    let s = spawn_server().await;
    // Inject a session whose expiry is already in the past.
    let token = Uuid::new_v4().to_string();
    s.state.sessions.write().await.insert(
        token.clone(),
        Session {
            session_id: Uuid::new_v4(),
            username: "fms-admin".to_owned(),
            role: "admin".to_owned(),
            expires_at: Utc::now() - ChronoDuration::seconds(1),
        },
    );
    let resp = s
        .client()
        .get(format!("{}/api/v1/jobs", s.base_url))
        .bearer_auth(&token)
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::UNAUTHORIZED);
}

#[tokio::test]
async fn logout_invalidates_session() {
    let s = spawn_server().await;
    let token = s.admin_login().await;

    // Token works before logout.
    let before = s
        .client()
        .get(format!("{}/api/v1/jobs", s.base_url))
        .bearer_auth(&token)
        .send()
        .await
        .unwrap();
    assert_eq!(before.status(), StatusCode::OK);

    // Logout.
    s.client()
        .delete(format!("{}/api/v1/auth/logout", s.base_url))
        .bearer_auth(&token)
        .send()
        .await
        .unwrap();

    // Same token must now be rejected.
    let after = s
        .client()
        .get(format!("{}/api/v1/jobs", s.base_url))
        .bearer_auth(&token)
        .send()
        .await
        .unwrap();
    assert_eq!(after.status(), StatusCode::UNAUTHORIZED);
}

#[tokio::test]
async fn operator_token_on_admin_route_returns_403() {
    let s = spawn_server().await;
    let op_token = s.inject_operator_session().await;
    let resp = s
        .client()
        .post(format!("{}/api/v1/cards", s.base_url))
        .bearer_auth(&op_token)
        .json(&serde_json::json!({
            "card_number": 12345,
            "label": "test",
        }))
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::FORBIDDEN);
}

// ===========================================================================
// Card table
// ===========================================================================

#[tokio::test]
async fn enrol_card_returns_201_with_card_id() {
    let s = spawn_server().await;
    let token = s.admin_login().await;
    let resp = s
        .client()
        .post(format!("{}/api/v1/cards", s.base_url))
        .bearer_auth(&token)
        .json(&serde_json::json!({"card_number": 67826, "label": "MV Harrier"}))
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::CREATED);
    let body = resp.json::<serde_json::Value>().await.unwrap();
    assert!(
        Uuid::parse_str(body["card_id"].as_str().unwrap()).is_ok(),
        "card_id must be a UUID"
    );
}

#[tokio::test]
async fn enrolled_card_appears_in_list() {
    let s = spawn_server().await;
    let token = s.admin_login().await;
    s.enrol_card(&token, 67826).await;

    let resp = s
        .client()
        .get(format!("{}/api/v1/cards", s.base_url))
        .bearer_auth(&token)
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let cards = resp.json::<serde_json::Value>().await.unwrap();
    let found = cards
        .as_array()
        .unwrap()
        .iter()
        .any(|c| c["card_number"] == 67826);
    assert!(found, "enrolled card must appear in list");
}

#[tokio::test]
async fn revoke_removes_card_from_list() {
    let s = spawn_server().await;
    let token = s.admin_login().await;
    let card_id = s.enrol_card(&token, 67826).await;

    let revoke = s
        .client()
        .delete(format!("{}/api/v1/cards/{card_id}", s.base_url))
        .bearer_auth(&token)
        .send()
        .await
        .unwrap();
    assert_eq!(revoke.status(), StatusCode::NO_CONTENT);

    let list = s
        .client()
        .get(format!("{}/api/v1/cards", s.base_url))
        .bearer_auth(&token)
        .send()
        .await
        .unwrap()
        .json::<serde_json::Value>()
        .await
        .unwrap();
    let found = list
        .as_array()
        .unwrap()
        .iter()
        .any(|c| c["card_number"] == 67826);
    assert!(!found, "revoked card must not appear in active list");
}

#[tokio::test]
async fn revoke_unknown_card_id_returns_404() {
    let s = spawn_server().await;
    let token = s.admin_login().await;
    let resp = s
        .client()
        .delete(format!("{}/api/v1/cards/{}", s.base_url, Uuid::new_v4()))
        .bearer_auth(&token)
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::NOT_FOUND);
}

#[tokio::test]
async fn revoked_card_fails_rfid_validation() {
    let s = spawn_server().await;
    let token = s.admin_login().await;

    let card_id = s.enrol_card(&token, 67826).await;
    let job_id = s.create_job(&token, "dispense").await;

    // Revoke the card.
    s.client()
        .delete(format!("{}/api/v1/cards/{card_id}", s.base_url))
        .bearer_auth(&token)
        .send()
        .await
        .unwrap();

    // Attempt RFID validation with the revoked card.
    let resp = s
        .client()
        .post(format!("{}/api/v1/jobs/{job_id}/rfid", s.base_url))
        .bearer_auth(&token)
        .json(&serde_json::json!({"card_number": 67826}))
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::FORBIDDEN);
}

// ===========================================================================
// Jobs — dispense path
// ===========================================================================

#[tokio::test]
async fn create_dispense_job_returns_201() {
    let s = spawn_server().await;
    let token = s.admin_login().await;
    let resp = s
        .client()
        .post(format!("{}/api/v1/jobs", s.base_url))
        .bearer_auth(&token)
        .json(&serde_json::json!({
            "type": "dispense",
            "counterparty": "MV Harrier",
            "grade": "MGO",
        }))
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::CREATED);
    let body = resp.json::<serde_json::Value>().await.unwrap();
    assert!(Uuid::parse_str(body["job_id"].as_str().unwrap()).is_ok());
    assert_eq!(body["type"], "dispense");
    assert_eq!(body["status"], "active");
}

/// The tank holds one grade, `FMS_GRADE` (fms.md §7.4): a job for any other is
/// refused before any other check, and no job is created. JET-A1 is aviation
/// fuel, which the tank does not hold.
#[tokio::test]
async fn a_job_for_another_grade_is_refused() {
    let s = spawn_server().await;
    let token = s.admin_login().await;
    for job_type in ["dispense", "fill"] {
        let resp = s
            .client()
            .post(format!("{}/api/v1/jobs", s.base_url))
            .bearer_auth(&token)
            .json(&serde_json::json!({
                "type": job_type,
                "counterparty": "MV Harrier",
                "grade": "JET-A1",
            }))
            .send()
            .await
            .unwrap();
        assert_eq!(
            resp.status(),
            StatusCode::UNPROCESSABLE_ENTITY,
            "{job_type}"
        );
    }
    let jobs: i64 = sqlx::query_scalar("SELECT count(*) FROM jobs")
        .fetch_one(&s.state.db)
        .await
        .unwrap();
    assert_eq!(jobs, 0, "a refused job must leave no row");
}

// ---------------------------------------------------------------------------
// jobs.session_id (fms.md §8.3)
//
// A job records the authenticated session it was created under. The column's
// NOT NULL and foreign key establish only that *a* real session is recorded;
// that it is *the* session whose token created the job is a property of the
// handler, and is what these tests pin. The audit joins every bunkering event
// to its LOGIN through this value (hist.md §4.2), so a wrong session passes
// both constraints and falsifies the join.
// ---------------------------------------------------------------------------

#[tokio::test]
async fn job_records_the_session_it_was_created_under() {
    let s = spawn_server().await;
    // A real login: the sessions row is written by the login handler itself.
    let token = s.admin_login().await;
    let job_id = s.create_job(&token, "dispense").await;

    let expected = s.session_id_of(&token).await;
    assert_eq!(s.job_session_id(&job_id).await, expected);

    let logged_in: i64 = sqlx::query_scalar("SELECT count(*) FROM sessions WHERE session_id = $1")
        .bind(expected)
        .fetch_one(&s.state.db)
        .await
        .unwrap();
    assert_eq!(logged_in, 1, "the recorded session has no login row");
}

#[tokio::test]
async fn job_records_the_creating_session_not_the_most_recent_login() {
    let s = spawn_server().await;
    // Two live sessions: a session is not ended by a later login (fms.md §4).
    let first = s.inject_operator_session().await;
    let second = s.inject_operator_session().await;
    assert_ne!(
        s.session_id_of(&first).await,
        s.session_id_of(&second).await
    );

    let job_id = s.create_job(&first, "dispense").await;

    assert_eq!(
        s.job_session_id(&job_id).await,
        s.session_id_of(&first).await,
        "the job must record the session whose token created it, not the latest login"
    );
}

/// The schema refuses a job whose session no login produced, and a job with no
/// session. Checked by SQLSTATE so that a missing column (42703) cannot pass
/// for a constraint.
#[tokio::test]
async fn jobs_refuse_an_unknown_or_missing_session() {
    let s = spawn_server().await;
    let insert = |session: Option<Uuid>| {
        sqlx::query(
            "INSERT INTO jobs (type, counterparty, grade, operator, session_id)
             VALUES ('dispense', 'MV Harrier', 'MGO', 'test-operator', $1)",
        )
        .bind(session)
        .execute(&s.state.db)
    };

    let unknown = insert(Some(Uuid::new_v4())).await.unwrap_err();
    assert_eq!(
        unknown
            .as_database_error()
            .and_then(|e| e.code())
            .as_deref(),
        Some("23503"),
        "expected a foreign key violation, got: {unknown}"
    );

    let missing = insert(None).await.unwrap_err();
    assert_eq!(
        missing
            .as_database_error()
            .and_then(|e| e.code())
            .as_deref(),
        Some("23502"),
        "expected a not-null violation, got: {missing}"
    );
}

/// fms.md §8.4: `transactions` carries `temperature_c`, `density_kg_per_l` and
/// `temp_corrected_volume_l`, all nullable — null on BDN-linkage rows, and the
/// temperature on a dispense whose close had no ATG reading. The seeded row
/// writes none of them, so all three read back null.
#[tokio::test]
async fn transactions_carry_nullable_measurement_columns() {
    let s = spawn_server().await;
    let token = s.inject_operator_session().await;
    let job_id = Uuid::parse_str(&s.create_job(&token, "dispense").await).unwrap();
    let tx_id = s.seed_dispense_transaction(job_id).await;

    let row: (Option<f64>, Option<f64>, Option<f64>) = sqlx::query_as(
        "SELECT temperature_c, density_kg_per_l, temp_corrected_volume_l
         FROM transactions WHERE transaction_id = $1",
    )
    .bind(tx_id)
    .fetch_one(&s.state.db)
    .await
    .expect("reading the measurement columns failed");
    assert_eq!(row, (None, None, None));
}

// ---------------------------------------------------------------------------
// Bunkering event envelope (hist.md §4.2)
//
// Every bunkering event carries the same six fields, so that any one of them
// can be attributed to its job, operator and authorising session without a
// second lookup. The values describe the *job*, not the request that happened
// to trigger the event: a BDN linked under a later session still carries the
// session the job was created under.
//
// The dispense `transaction_closed` is emitted by the Modbus poll loop, which
// these tests do not drive (the poll interval is 3600 s and there is no fake
// dispenser); it is covered by the shared envelope builder's unit test and by
// the end-to-end check of the deployed log server.
// ---------------------------------------------------------------------------

/// The road tanker's start time used by `fill_close_body`: one and a half
/// minutes before midnight at a month end, the case the transcribed start
/// exists for (sim.md §8.3).
const TICKET_COMMENCED_AT: &str = "2026-01-31T23:58:30Z";

/// A complete fill-close request (fms.md §7): every field the delivery ticket
/// carries, including the tanker's own start time and measurements.
fn fill_close_body() -> serde_json::Value {
    serde_json::json!({
        "ticket_number": "TKT-001",
        "delivery_commenced_at": TICKET_COMMENCED_AT,
        "delivered_volume_l": 12_500.0,
        "temperature_c": 12.0,
        "density_kg_per_l": 0.845,
        "temp_corrected_volume_l": 12_538.0,
        "supplier": "Fuel Depot Alpha",
        "tanker_registration": "AB12 CDE",
    })
}

/// The six envelope fields of hist.md §4.2, with the values `create_job` and
/// `inject_operator_session` produce.
fn assert_envelope(event: &fms::state::LogEvent, job_id: &str, job_type: &str, session_id: Uuid) {
    let expected = [
        ("job_id", serde_json::json!(job_id)),
        ("type", serde_json::json!(job_type)),
        ("operator", serde_json::json!("test-operator")),
        ("session_id", serde_json::json!(session_id)),
        ("counterparty", serde_json::json!(TEST_VESSEL)),
        ("grade", serde_json::json!("MGO")),
    ];
    for (field, value) in expected {
        assert_eq!(
            event.fields.get(field),
            Some(&value),
            "{:?}: envelope field `{field}`",
            event.kind
        );
    }
}

#[tokio::test]
async fn job_opened_carries_the_envelope() {
    let s = spawn_server().await;
    let token = s.inject_operator_session().await;
    let job_id = s.create_job(&token, "dispense").await;

    let events = s.events_of(EventKind::JobOpened);
    assert_eq!(events.len(), 1);
    assert_envelope(
        &events[0],
        &job_id,
        "dispense",
        s.session_id_of(&token).await,
    );
}

#[tokio::test]
async fn transaction_open_carries_the_envelope() {
    let s = spawn_server().await;
    let admin = s.admin_login().await;
    s.enrol_card(&admin, 67826).await;
    let token = s.inject_operator_session().await;
    let job_id = s.create_job(&token, "dispense").await;

    s.client()
        .post(format!("{}/api/v1/jobs/{job_id}/rfid", s.base_url))
        .bearer_auth(&token)
        .json(&serde_json::json!({"card_number": 67826}))
        .send()
        .await
        .unwrap();
    let resp = s
        .client()
        .post(format!("{}/api/v1/jobs/{job_id}/start", s.base_url))
        .bearer_auth(&token)
        .json(&serde_json::json!({"preset_volume": 500.0}))
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);

    let events = s.events_of(EventKind::TransactionOpen);
    assert_eq!(events.len(), 1);
    assert_envelope(
        &events[0],
        &job_id,
        "dispense",
        s.session_id_of(&token).await,
    );
}

/// Starting a dispense records the transfer's start on the job, and it is the
/// same instant `transaction_open` publishes as `opened_at`. The dispense close
/// (Modbus task) reads it from the job, so the two events agree on when the
/// transfer began — and neither uses job creation (fms.md §8.4, sim.md §8.3).
#[tokio::test]
async fn dispense_start_is_recorded_on_the_job_and_published_by_transaction_open() {
    let s = spawn_server().await;
    let admin = s.admin_login().await;
    s.enrol_card(&admin, 67826).await;
    let token = s.inject_operator_session().await;
    let job_id = s.create_job(&token, "dispense").await;

    s.client()
        .post(format!("{}/api/v1/jobs/{job_id}/rfid", s.base_url))
        .bearer_auth(&token)
        .json(&serde_json::json!({"card_number": 67826}))
        .send()
        .await
        .unwrap();
    let resp = s
        .client()
        .post(format!("{}/api/v1/jobs/{job_id}/start", s.base_url))
        .bearer_auth(&token)
        .json(&serde_json::json!({"preset_volume": 500.0}))
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);

    let (created, started) = {
        let guard = s.state.current_job.read().await;
        let job = guard.as_ref().expect("the dispense is the live job");
        (
            job.created_at(),
            job.tx_opened_at()
                .expect("starting a dispense records its start"),
        )
    };
    assert!(
        started >= created,
        "the transfer cannot start before its job"
    );

    let events = s.events_of(EventKind::TransactionOpen);
    assert_eq!(events.len(), 1);
    assert_eq!(events[0].fields["opened_at"], serde_json::json!(started));
}

#[tokio::test]
async fn fill_transaction_closed_carries_the_envelope() {
    let s = spawn_server().await;
    let token = s.inject_operator_session().await;
    let job_id = s.create_job(&token, "fill").await;

    let resp = s
        .client()
        .post(format!("{}/api/v1/jobs/{job_id}/close", s.base_url))
        .bearer_auth(&token)
        .json(&fill_close_body())
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::CREATED);

    let events = s.events_of(EventKind::TransactionClosed);
    assert_eq!(events.len(), 1);
    assert_envelope(&events[0], &job_id, "fill", s.session_id_of(&token).await);
}

/// The BDN is linked from the durable ledger, so the envelope must come from the
/// `jobs` row: here the job is no longer the live one, and the BDN is entered
/// under a different session from the one that created the job.
#[tokio::test]
async fn bdn_linked_carries_the_envelope_of_the_job_not_the_request() {
    let s = spawn_server().await;
    let creator = s.inject_operator_session().await;
    let job_id = s.create_job(&creator, "dispense").await;
    s.seed_dispense_transaction(Uuid::parse_str(&job_id).unwrap())
        .await;
    *s.state.current_job.write().await = None;

    let later = s.inject_operator_session().await;
    let resp = s
        .client()
        .post(format!("{}/api/v1/jobs/{job_id}/bdn", s.base_url))
        .bearer_auth(&later)
        .json(&serde_json::json!({ "bdn_number": "BDN-2024-001" }))
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::CREATED);

    let events = s.events_of(EventKind::BdnLinked);
    assert_eq!(events.len(), 1);
    assert_envelope(
        &events[0],
        &job_id,
        "dispense",
        s.session_id_of(&creator).await,
    );
}

#[tokio::test]
async fn second_concurrent_job_returns_409() {
    let s = spawn_server().await;
    let token = s.admin_login().await;
    s.create_job(&token, "dispense").await;

    let resp = s
        .client()
        .post(format!("{}/api/v1/jobs", s.base_url))
        .bearer_auth(&token)
        .json(&serde_json::json!({
            "type": "dispense",
            "counterparty": "MV Harrier",
            "grade": "MGO",
        }))
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::CONFLICT);
}

/// The current job carries the dispense start: `null` until the dispense starts,
/// then the instant `transaction_open` publishes as `opened_at`, at its
/// precision. The dock operator writes it on the BDN as the time of
/// commencement (fms.md §7.4, narrative.md §2.2).
#[tokio::test]
async fn current_job_carries_the_dispense_start() {
    let s = spawn_server().await;
    let admin = s.admin_login().await;
    s.enrol_card(&admin, 67826).await;
    let token = s.inject_operator_session().await;
    let job_id = s.create_job(&token, "dispense").await;

    let before = s.current_job(&token).await;
    assert_eq!(
        before.get("opened_at"),
        Some(&serde_json::Value::Null),
        "a job not yet started reports opened_at as null: {before}"
    );

    s.client()
        .post(format!("{}/api/v1/jobs/{job_id}/rfid", s.base_url))
        .bearer_auth(&token)
        .json(&serde_json::json!({"card_number": 67826}))
        .send()
        .await
        .unwrap();
    let resp = s
        .client()
        .post(format!("{}/api/v1/jobs/{job_id}/start", s.base_url))
        .bearer_auth(&token)
        .json(&serde_json::json!({"preset_volume": 500.0}))
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);

    let events = s.events_of(EventKind::TransactionOpen);
    assert_eq!(events.len(), 1);
    assert_eq!(
        s.current_job(&token).await["opened_at"],
        events[0].fields["opened_at"]
    );
}

/// A fill's `opened_at` is transcribed from the delivery ticket at close, so the
/// live fill job reports none (fms.md §7.4).
#[tokio::test]
async fn current_fill_job_reports_no_dispense_start() {
    let s = spawn_server().await;
    let token = s.admin_login().await;
    s.create_job(&token, "fill").await;

    let body = s.current_job(&token).await;
    assert_eq!(
        body.get("opened_at"),
        Some(&serde_json::Value::Null),
        "{body}"
    );
}

#[tokio::test]
async fn current_status_returns_current_job() {
    let s = spawn_server().await;
    let token = s.admin_login().await;
    let job_id = s.create_job(&token, "dispense").await;

    let resp = s
        .client()
        .get(format!("{}/api/v1/jobs/current", s.base_url))
        .bearer_auth(&token)
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let body = resp.json::<serde_json::Value>().await.unwrap();
    assert_eq!(body["job_id"], job_id);
    assert_eq!(body["type"], "dispense");
    assert_eq!(body["status"], "active");
}

#[tokio::test]
async fn current_status_returns_404_when_no_current_job() {
    let s = spawn_server().await;
    let token = s.admin_login().await;
    let resp = s
        .client()
        .get(format!("{}/api/v1/jobs/current", s.base_url))
        .bearer_auth(&token)
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::NOT_FOUND);
}

#[tokio::test]
async fn cancel_job_returns_204() {
    let s = spawn_server().await;
    let token = s.admin_login().await;
    let job_id = s.create_job(&token, "dispense").await;

    let resp = s
        .client()
        .delete(format!("{}/api/v1/jobs/{job_id}", s.base_url))
        .bearer_auth(&token)
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::NO_CONTENT);

    // Job should now report status = cancelled.
    let body = s
        .client()
        .get(format!("{}/api/v1/jobs/current", s.base_url))
        .bearer_auth(&token)
        .send()
        .await
        .unwrap()
        .json::<serde_json::Value>()
        .await
        .unwrap();
    assert_eq!(body["status"], "cancelled");
}

/// `DELETE /api/v1/jobs/{job_id}` with `token`.
async fn cancel_job(s: &TestServer, token: &str, job_id: &str) -> reqwest::Response {
    s.client()
        .delete(format!("{}/api/v1/jobs/{job_id}", s.base_url))
        .bearer_auth(token)
        .send()
        .await
        .unwrap()
}

/// The `job_cancellations` rows of a job: who cancelled it, under which
/// session, and when (fms.md §8.5).
async fn cancellations(s: &TestServer, job_id: &str) -> Vec<(String, Uuid, chrono::DateTime<Utc>)> {
    sqlx::query_as::<_, (String, Uuid, chrono::DateTime<Utc>)>(
        "SELECT operator, session_id, cancelled_at FROM job_cancellations WHERE job_id = $1",
    )
    .bind(Uuid::parse_str(job_id).unwrap())
    .fetch_all(&s.state.db)
    .await
    .expect("reading job_cancellations failed")
}

/// A cancel is recorded in `job_cancellations` under the user and session that
/// made it, which need not be the job's creator's, and `job_cancelled` carries
/// the job's envelope beside them (fms.md §7.4, §8.5; hist.md §4.2).
#[tokio::test]
async fn a_cancel_is_recorded_and_published_under_the_cancelling_session() {
    let s = spawn_server().await;
    let creator = s.inject_operator_session().await;
    let job_id = s.create_job(&creator, "dispense").await;
    let admin = s.admin_login().await;
    let admin_session = s.session_id_of(&admin).await;

    let resp = cancel_job(&s, &admin, &job_id).await;
    assert_eq!(resp.status(), StatusCode::NO_CONTENT);

    let rows = cancellations(&s, &job_id).await;
    assert_eq!(rows.len(), 1, "one cancellation row");
    let (operator, session_id, cancelled_at) = &rows[0];
    assert_eq!(operator, "fms-admin");
    assert_eq!(*session_id, admin_session);

    let events = s.events_of(EventKind::JobCancelled);
    assert_eq!(events.len(), 1);
    // The envelope describes the job's creation, not the cancel.
    assert_envelope(
        &events[0],
        &job_id,
        "dispense",
        s.session_id_of(&creator).await,
    );
    assert_eq!(
        events[0].fields["cancelled_by"],
        serde_json::json!("fms-admin")
    );
    assert_eq!(
        events[0].fields["cancelled_session_id"],
        serde_json::json!(admin_session)
    );
    // The event carries the row's instant, which the database holds to the
    // microsecond.
    let published: chrono::DateTime<Utc> =
        serde_json::from_value(events[0].fields["cancelled_at"].clone()).unwrap();
    let skew = (published - *cancelled_at).num_nanoseconds().unwrap().abs();
    assert!(
        skew < 1_000,
        "event {published} and row {cancelled_at} disagree"
    );
}

/// A cancel whose response was lost is sent again and finds the job already
/// cancelled: it answers 204 and records nothing further (fms.md §7.4).
#[tokio::test]
async fn a_repeated_cancel_answers_204_and_records_nothing_further() {
    let s = spawn_server().await;
    let token = s.inject_operator_session().await;
    let job_id = s.create_job(&token, "dispense").await;

    for _ in 0..2 {
        let resp = cancel_job(&s, &token, &job_id).await;
        assert_eq!(resp.status(), StatusCode::NO_CONTENT);
    }
    assert_eq!(cancellations(&s, &job_id).await.len(), 1);
    assert_eq!(s.events_of(EventKind::JobCancelled).len(), 1);
    assert_eq!(s.current_job(&token).await["status"], "cancelled");
}

/// Only an active job can be cancelled. A running dispense, one awaiting its
/// BDN and a closed job answer 409 and keep their status, with no record and no
/// event (fms.md §7.4).
#[tokio::test]
async fn only_an_active_job_can_be_cancelled() {
    for (status, reported) in [
        (JobStatus::Running, "running"),
        (JobStatus::AwaitingBdn, "awaiting_bdn"),
        (JobStatus::Closed, "closed"),
    ] {
        let s = spawn_server().await;
        let token = s.inject_operator_session().await;
        let job_id = s.create_job(&token, "dispense").await;
        s.state
            .current_job
            .write()
            .await
            .as_mut()
            .expect("the job is the live one")
            .set_status(status);

        let resp = cancel_job(&s, &token, &job_id).await;
        assert_eq!(resp.status(), StatusCode::CONFLICT, "{reported}");
        assert_eq!(s.current_job(&token).await["status"], reported);
        assert!(cancellations(&s, &job_id).await.is_empty(), "{reported}");
        assert!(
            s.events_of(EventKind::JobCancelled).is_empty(),
            "{reported}"
        );
    }
}

/// A cancel resolves against the live job only (fms.md §7.4).
#[tokio::test]
async fn a_cancel_of_a_job_that_is_not_current_returns_404() {
    let s = spawn_server().await;
    let token = s.inject_operator_session().await;
    s.create_job(&token, "dispense").await;

    let resp = cancel_job(&s, &token, &Uuid::new_v4().to_string()).await;
    assert_eq!(resp.status(), StatusCode::NOT_FOUND);
    assert!(s.events_of(EventKind::JobCancelled).is_empty());
}

/// The dock operator's path through a refused start (digital.md §4.2): the
/// refused job stays active and holds the single job slot until it is
/// cancelled, after which the next job is created.
#[tokio::test]
async fn a_refused_start_holds_the_slot_until_the_job_is_cancelled() {
    let s = spawn_server().await;
    let (token, job_id) = startable_dispense(&s).await;
    s.atg.set_volume(5_000.0);
    let resp = start_dispense(&s, &token, &job_id, 100.0).await;
    assert_eq!(resp.status(), StatusCode::UNPROCESSABLE_ENTITY);
    s.atg.set_volume(35_000.0);

    let blocked = s
        .client()
        .post(format!("{}/api/v1/jobs", s.base_url))
        .bearer_auth(&token)
        .json(&serde_json::json!({
            "type": "dispense",
            "counterparty": TEST_VESSEL,
            "grade": "MGO",
        }))
        .send()
        .await
        .unwrap();
    assert_eq!(blocked.status(), StatusCode::CONFLICT);

    let resp = cancel_job(&s, &token, &job_id).await;
    assert_eq!(resp.status(), StatusCode::NO_CONTENT);
    s.create_job(&token, "dispense").await;
}

/// `job_cancellations` is append-only, as `jobs` is (fms.md §8.7).
#[tokio::test]
async fn job_cancellations_is_append_only() {
    let s = spawn_server().await;
    let token = s.inject_operator_session().await;
    let job_id = s.create_job(&token, "dispense").await;
    let resp = cancel_job(&s, &token, &job_id).await;
    assert_eq!(resp.status(), StatusCode::NO_CONTENT);

    for statement in [
        "UPDATE job_cancellations SET operator = 'someone else'",
        "DELETE FROM job_cancellations",
    ] {
        assert!(
            sqlx::query(statement).execute(&s.state.db).await.is_err(),
            "{statement} succeeded on an append-only table"
        );
    }
    assert_eq!(cancellations(&s, &job_id).await[0].0, "test-operator");
}

#[tokio::test]
async fn validate_rfid_with_enrolled_card_returns_200() {
    let s = spawn_server().await;
    let token = s.admin_login().await;
    s.enrol_card(&token, 67826).await;
    let job_id = s.create_job(&token, "dispense").await;

    let resp = s
        .client()
        .post(format!("{}/api/v1/jobs/{job_id}/rfid", s.base_url))
        .bearer_auth(&token)
        .json(&serde_json::json!({"card_number": 67826}))
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);

    // rfid_validated must now be true.
    let body = s
        .client()
        .get(format!("{}/api/v1/jobs/current", s.base_url))
        .bearer_auth(&token)
        .send()
        .await
        .unwrap()
        .json::<serde_json::Value>()
        .await
        .unwrap();
    assert_eq!(body["rfid_validated"], true);
}

#[tokio::test]
async fn validate_rfid_with_inactive_card_returns_403() {
    let s = spawn_server().await;
    let token = s.admin_login().await;
    let job_id = s.create_job(&token, "dispense").await;

    let resp = s
        .client()
        .post(format!("{}/api/v1/jobs/{job_id}/rfid", s.base_url))
        .bearer_auth(&token)
        .json(&serde_json::json!({"card_number": 99999}))
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::FORBIDDEN);
}

/// An active card enrolled to a vessel other than the one the job names does
/// not satisfy the vessel RFID gate.
#[tokio::test]
async fn validate_rfid_with_another_vessels_card_returns_403() {
    let s = spawn_server().await;
    let token = s.admin_login().await;
    s.enrol_card_for(&token, 67827, "Tug Bravo").await;
    let job_id = s.create_job(&token, "dispense").await;

    let resp = s
        .client()
        .post(format!("{}/api/v1/jobs/{job_id}/rfid", s.base_url))
        .bearer_auth(&token)
        .json(&serde_json::json!({"card_number": 67827}))
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::FORBIDDEN);
    assert!(!s.current_job_rfid_validated(&token).await);
}

/// An active operator's card does not satisfy the vessel RFID gate.
#[tokio::test]
async fn validate_rfid_with_operator_card_returns_403() {
    let s = spawn_server().await;
    let token = s.admin_login().await;
    s.enrol_card_for(&token, 1000002, "jrenard").await;
    let job_id = s.create_job(&token, "dispense").await;

    let resp = s
        .client()
        .post(format!("{}/api/v1/jobs/{job_id}/rfid", s.base_url))
        .bearer_auth(&token)
        .json(&serde_json::json!({"card_number": 1000002}))
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::FORBIDDEN);
    assert!(!s.current_job_rfid_validated(&token).await);
}

#[tokio::test]
async fn start_dispense_without_rfid_validated_returns_409() {
    let s = spawn_server().await;
    let token = s.admin_login().await;
    let job_id = s.create_job(&token, "dispense").await;

    let resp = s
        .client()
        .post(format!("{}/api/v1/jobs/{job_id}/start", s.base_url))
        .bearer_auth(&token)
        .json(&serde_json::json!({"preset_volume": 500.0}))
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::CONFLICT);
}

#[tokio::test]
async fn start_dispense_with_rfid_validated_returns_200() {
    let s = spawn_server().await;
    let token = s.admin_login().await;
    s.enrol_card(&token, 67826).await;
    let job_id = s.create_job(&token, "dispense").await;

    s.client()
        .post(format!("{}/api/v1/jobs/{job_id}/rfid", s.base_url))
        .bearer_auth(&token)
        .json(&serde_json::json!({"card_number": 67826}))
        .send()
        .await
        .unwrap();

    let resp = s
        .client()
        .post(format!("{}/api/v1/jobs/{job_id}/start", s.base_url))
        .bearer_auth(&token)
        .json(&serde_json::json!({"preset_volume": 500.0}))
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);

    // Job must now be in running state.
    let body = s
        .client()
        .get(format!("{}/api/v1/jobs/current", s.base_url))
        .bearer_auth(&token)
        .send()
        .await
        .unwrap()
        .json::<serde_json::Value>()
        .await
        .unwrap();
    assert_eq!(body["status"], "running");
}

#[tokio::test]
async fn start_dispense_with_preset_above_register_ceiling_returns_422() {
    let s = spawn_server().await;
    let token = s.admin_login().await;
    s.enrol_card(&token, 67826).await;
    let job_id = s.create_job(&token, "dispense").await;

    s.client()
        .post(format!("{}/api/v1/jobs/{job_id}/rfid", s.base_url))
        .bearer_auth(&token)
        .json(&serde_json::json!({"card_number": 67826}))
        .send()
        .await
        .unwrap();

    // 7000 L exceeds the dispenser's 16-bit "unsigned div10" register ceiling
    // (6553.5 L); the FMS must reject it rather than truncate.
    let resp = s
        .client()
        .post(format!("{}/api/v1/jobs/{job_id}/start", s.base_url))
        .bearer_auth(&token)
        .json(&serde_json::json!({"preset_volume": 7000.0}))
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::UNPROCESSABLE_ENTITY);
}

// ===========================================================================
// Jobs — fill path
// ===========================================================================

#[tokio::test]
async fn create_fill_job_returns_201() {
    let s = spawn_server().await;
    let token = s.admin_login().await;
    let resp = s
        .client()
        .post(format!("{}/api/v1/jobs", s.base_url))
        .bearer_auth(&token)
        .json(&serde_json::json!({
            "type": "fill",
            "counterparty": "Fuel Depot Alpha",
            "grade": "MGO",
        }))
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::CREATED);
    let body = resp.json::<serde_json::Value>().await.unwrap();
    assert_eq!(body["type"], "fill");
    assert_eq!(body["status"], "active");
}

#[tokio::test]
async fn close_fill_returns_201_with_transaction_id() {
    let s = spawn_server().await;
    let token = s.admin_login().await;
    let job_id = s.create_job(&token, "fill").await;

    let resp = s
        .client()
        .post(format!("{}/api/v1/jobs/{job_id}/close", s.base_url))
        .bearer_auth(&token)
        .json(&fill_close_body())
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::CREATED);
    let body = resp.json::<serde_json::Value>().await.unwrap();
    assert!(
        Uuid::parse_str(body["transaction_id"].as_str().unwrap()).is_ok(),
        "transaction_id must be a UUID"
    );
}

#[tokio::test]
async fn close_fill_wrong_job_id_returns_404() {
    let s = spawn_server().await;
    let token = s.admin_login().await;
    let resp = s
        .client()
        .post(format!(
            "{}/api/v1/jobs/{}/close",
            s.base_url,
            Uuid::new_v4()
        ))
        .bearer_auth(&token)
        .json(&fill_close_body())
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::NOT_FOUND);
}

/// The fill row holds the tanker's own figures as transcribed (fms.md §7,
/// §8.4) — the FMS records them, it does not compute them.
#[tokio::test]
async fn close_fill_records_the_transcribed_measurements() {
    let s = spawn_server().await;
    let token = s.inject_operator_session().await;
    let job_id = Uuid::parse_str(&s.create_job(&token, "fill").await).unwrap();

    let resp = s
        .client()
        .post(format!("{}/api/v1/jobs/{job_id}/close", s.base_url))
        .bearer_auth(&token)
        .json(&fill_close_body())
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::CREATED);

    let row: (Option<f64>, Option<f64>, Option<f64>) = sqlx::query_as(
        "SELECT temperature_c, density_kg_per_l, temp_corrected_volume_l
         FROM transactions WHERE job_id = $1",
    )
    .bind(job_id)
    .fetch_one(&s.state.db)
    .await
    .unwrap();
    assert_eq!(row, (Some(12.0), Some(0.845), Some(12_538.0)));
}

/// A fill's `opened_at` is the start time transcribed from the ticket, not the
/// creation of the fill job: the paper ticket and the record must assign the
/// transfer to an audit window by the same instant (sim.md §8.3), and the FMS
/// does not observe the tanker's pump. Both the row and the event carry it.
#[tokio::test]
async fn close_fill_opened_at_is_the_transcribed_start_not_job_creation() {
    let s = spawn_server().await;
    let token = s.inject_operator_session().await;
    let job_id = Uuid::parse_str(&s.create_job(&token, "fill").await).unwrap();

    let resp = s
        .client()
        .post(format!("{}/api/v1/jobs/{job_id}/close", s.base_url))
        .bearer_auth(&token)
        .json(&fill_close_body())
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::CREATED);

    let commenced = chrono::DateTime::parse_from_rfc3339(TICKET_COMMENCED_AT)
        .unwrap()
        .with_timezone(&Utc);
    let opened_at: chrono::DateTime<Utc> =
        sqlx::query_scalar("SELECT opened_at FROM transactions WHERE job_id = $1")
            .bind(job_id)
            .fetch_one(&s.state.db)
            .await
            .unwrap();
    assert_eq!(opened_at, commenced, "the row's opened_at");

    let events = s.events_of(EventKind::TransactionClosed);
    assert_eq!(events.len(), 1);
    assert_eq!(
        events[0].fields["opened_at"],
        serde_json::json!(commenced),
        "the event's opened_at"
    );
}

/// The start time is required: without it the transfer cannot be assigned to
/// an audit window. The request is refused and the job stays open.
#[tokio::test]
async fn close_fill_without_a_start_time_is_refused() {
    let s = spawn_server().await;
    let token = s.inject_operator_session().await;
    let job_id = Uuid::parse_str(&s.create_job(&token, "fill").await).unwrap();

    let mut body = fill_close_body();
    body.as_object_mut()
        .unwrap()
        .remove("delivery_commenced_at");
    let resp = s
        .client()
        .post(format!("{}/api/v1/jobs/{job_id}/close", s.base_url))
        .bearer_auth(&token)
        .json(&body)
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::UNPROCESSABLE_ENTITY);

    let rows: i64 = sqlx::query_scalar("SELECT count(*) FROM transactions WHERE job_id = $1")
        .bind(job_id)
        .fetch_one(&s.state.db)
        .await
        .unwrap();
    assert_eq!(rows, 0, "a refused close must write no transaction row");
}

/// The fill `transaction_closed` carries exactly hist.md §4.2's fields: the
/// envelope plus the fill list, including the three measurement fields that
/// mirror the paper ticket. Exact, so an omitted field and an unspecified one
/// both fail.
#[tokio::test]
async fn fill_transaction_closed_carries_exactly_the_specified_fields() {
    let s = spawn_server().await;
    let token = s.inject_operator_session().await;
    let job_id = s.create_job(&token, "fill").await;

    let resp = s
        .client()
        .post(format!("{}/api/v1/jobs/{job_id}/close", s.base_url))
        .bearer_auth(&token)
        .json(&fill_close_body())
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::CREATED);

    let events = s.events_of(EventKind::TransactionClosed);
    assert_eq!(events.len(), 1);
    let fields = &events[0].fields;

    let mut actual: Vec<&str> = fields.keys().map(String::as_str).collect();
    actual.sort_unstable();
    let mut expected = vec![
        // envelope
        "job_id",
        "type",
        "operator",
        "session_id",
        "counterparty",
        "grade",
        // hist.md §4.2, transaction_closed (fill)
        "transaction_id",
        "ticket_number",
        "delivered_volume_l",
        "temperature_c",
        "density_kg_per_l",
        "temp_corrected_volume_l",
        "supplier",
        "tanker_registration",
        "atg_volume_at_open_l",
        "atg_volume_at_close_l",
        "opened_at",
        "closed_at",
    ];
    expected.sort_unstable();
    assert_eq!(actual, expected);

    assert_eq!(fields["temperature_c"], serde_json::json!(12.0));
    assert_eq!(fields["density_kg_per_l"], serde_json::json!(0.845));
    assert_eq!(
        fields["temp_corrected_volume_l"],
        serde_json::json!(12_538.0)
    );
}

// ===========================================================================
// BDN linkage (dispense)
// ===========================================================================

/// Regression: a BDN must link to a dispense transaction resolved from the
/// durable ledger, even when that job is no longer the live current_job (the
/// situation that was impossible before this work — e.g. after an FMS restart).
#[tokio::test]
async fn bdn_link_succeeds_when_job_not_current() {
    let s = spawn_server().await;
    let token = s.admin_login().await;
    let job_id = Uuid::parse_str(&s.create_job(&token, "dispense").await).unwrap();
    s.seed_dispense_transaction(job_id).await;

    // The job is no longer the live one (e.g. the FMS restarted).
    *s.state.current_job.write().await = None;

    let resp = s
        .client()
        .post(format!("{}/api/v1/jobs/{job_id}/bdn", s.base_url))
        .bearer_auth(&token)
        .json(&serde_json::json!({ "bdn_number": "BDN-2024-001" }))
        .send()
        .await
        .unwrap();
    assert_eq!(
        resp.status(),
        StatusCode::CREATED,
        "BDN must link from the ledger even when the job is not current"
    );
    let body = resp.json::<serde_json::Value>().await.unwrap();
    assert!(Uuid::parse_str(body["transaction_id"].as_str().unwrap()).is_ok());
}

/// Entering the BDN closes the awaiting-BDN dispense job (narrative §279).
#[tokio::test]
async fn bdn_link_closes_awaiting_bdn_job() {
    let s = spawn_server().await;
    let token = s.admin_login().await;
    let job_id = Uuid::parse_str(&s.create_job(&token, "dispense").await).unwrap();
    s.seed_dispense_transaction(job_id).await;
    // Put the live job into the awaiting-BDN state, as the Modbus task would.
    s.state
        .current_job
        .write()
        .await
        .as_mut()
        .unwrap()
        .set_status(JobStatus::AwaitingBdn);

    let resp = s
        .client()
        .post(format!("{}/api/v1/jobs/{job_id}/bdn", s.base_url))
        .bearer_auth(&token)
        .json(&serde_json::json!({ "bdn_number": "BDN-2024-002" }))
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::CREATED);

    let body = s
        .client()
        .get(format!("{}/api/v1/jobs/current", s.base_url))
        .bearer_auth(&token)
        .send()
        .await
        .unwrap()
        .json::<serde_json::Value>()
        .await
        .unwrap();
    assert_eq!(
        body["status"], "closed",
        "linking the BDN must close the job"
    );
}

/// A BDN is a marine-fuel-delivery document — valid only for a dispense.
#[tokio::test]
async fn bdn_link_on_fill_returns_422() {
    let s = spawn_server().await;
    let token = s.admin_login().await;
    let job_id = s.create_job(&token, "fill").await;
    let resp = s
        .client()
        .post(format!("{}/api/v1/jobs/{job_id}/bdn", s.base_url))
        .bearer_auth(&token)
        .json(&serde_json::json!({ "bdn_number": "BDN-X" }))
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::UNPROCESSABLE_ENTITY);
}

#[tokio::test]
async fn bdn_link_unknown_job_returns_404() {
    let s = spawn_server().await;
    let token = s.admin_login().await;
    let resp = s
        .client()
        .post(format!("{}/api/v1/jobs/{}/bdn", s.base_url, Uuid::new_v4()))
        .bearer_auth(&token)
        .json(&serde_json::json!({ "bdn_number": "BDN-X" }))
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::NOT_FOUND);
}

/// No metering transaction yet (dispense not complete) → cannot link a BDN.
#[tokio::test]
async fn bdn_link_before_dispense_complete_returns_409() {
    let s = spawn_server().await;
    let token = s.admin_login().await;
    let job_id = s.create_job(&token, "dispense").await;
    let resp = s
        .client()
        .post(format!("{}/api/v1/jobs/{job_id}/bdn", s.base_url))
        .bearer_auth(&token)
        .json(&serde_json::json!({ "bdn_number": "BDN-X" }))
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::CONFLICT);
}

#[tokio::test]
async fn bdn_duplicate_link_returns_409() {
    let s = spawn_server().await;
    let token = s.admin_login().await;
    let job_id = Uuid::parse_str(&s.create_job(&token, "dispense").await).unwrap();
    s.seed_dispense_transaction(job_id).await;

    let first = s
        .client()
        .post(format!("{}/api/v1/jobs/{job_id}/bdn", s.base_url))
        .bearer_auth(&token)
        .json(&serde_json::json!({ "bdn_number": "BDN-1" }))
        .send()
        .await
        .unwrap();
    assert_eq!(first.status(), StatusCode::CREATED);

    let second = s
        .client()
        .post(format!("{}/api/v1/jobs/{job_id}/bdn", s.base_url))
        .bearer_auth(&token)
        .json(&serde_json::json!({ "bdn_number": "BDN-2" }))
        .send()
        .await
        .unwrap();
    assert_eq!(second.status(), StatusCode::CONFLICT);
}

/// Integration view of the §279 invariant: a new job is refused while a
/// dispense awaits its BDN.
#[tokio::test]
async fn create_blocked_while_awaiting_bdn_returns_409() {
    let s = spawn_server().await;
    let token = s.admin_login().await;
    s.create_job(&token, "dispense").await;
    s.state
        .current_job
        .write()
        .await
        .as_mut()
        .unwrap()
        .set_status(JobStatus::AwaitingBdn);

    let resp = s
        .client()
        .post(format!("{}/api/v1/jobs", s.base_url))
        .bearer_auth(&token)
        .json(&serde_json::json!({
            "type": "fill",
            "counterparty": "Fuel Depot Alpha",
            "grade": "MGO",
        }))
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::CONFLICT);
}

// ===========================================================================
// Cavitation guard
// ===========================================================================

#[tokio::test]
async fn dispense_job_rejected_when_tank_below_cavitation_threshold() {
    let s = spawn_server().await;
    let token = s.admin_login().await;

    // Tank at 4 000 L; threshold is 10 % of 50 000 L = 5 000 L. The cached
    // reading is healthy, and the check must not use it (fms.md §5.1).
    *s.state.last_atg.write().await = Some(sample_atg_reading());
    s.atg.set_volume(4_000.0);

    let resp = s
        .client()
        .post(format!("{}/api/v1/jobs", s.base_url))
        .bearer_auth(&token)
        .json(&serde_json::json!({
            "type": "dispense",
            "counterparty": "MV Harrier",
            "grade": "MGO",
        }))
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::UNPROCESSABLE_ENTITY);
    // The caller decides on the code, not the description (fms.md §7).
    let body = resp.json::<serde_json::Value>().await.unwrap();
    assert_eq!(body["code"], "cavitation_threshold");
}

// ===========================================================================
// ATG poller (Veeder-Root TLS) — full poll cycle against a fake console
//
// Drives `fms::background::atg::poll_once` against a fake ATG console emitting
// spec-correct computer-format frames (tls-protocol.md), and asserts the shared
// reading, the persisted `atg_readings` row, the emitted log events, and the
// checksum-reject path. The pure parsing/interpretation is unit-tested in
// `src/background/atg.rs`; this exercises the I/O shell end-to-end.
// ===========================================================================

/// IEEE-754 single as eight upper-case big-endian hex characters.
fn encode_float(value: f32) -> String {
    value
        .to_be_bytes()
        .iter()
        .map(|b| format!("{b:02X}"))
        .collect()
}

/// i201 body for a tank holding `tc_volume` litres (volume and TC volume both
/// `tc_volume`, ullage the 50 000 L capacity remainder, level 5 000 mm, water 0,
/// temperature 15 °C, water volume 0). The default test volume is 35 000 L.
fn atg_i201_body(tc_volume: f32) -> String {
    let fields = [
        tc_volume,
        tc_volume,
        50_000.0 - tc_volume,
        5_000.0,
        0.0,
        15.0,
        0.0,
    ];
    let mut b = String::from("01D000007");
    for f in fields {
        b.push_str(&encode_float(f));
    }
    b
}

/// Frame an `echo` + `body` into a complete computer-format response with a
/// valid two's-complement checksum. When `corrupt` is set, a body byte is
/// flipped after the checksum is computed, so the frame fails verification.
fn atg_frame(echo: &str, body: &str, corrupt: bool) -> Vec<u8> {
    let mut m = vec![0x01u8];
    m.extend_from_slice(echo.as_bytes());
    m.extend_from_slice(b"2501010000"); // YYMMDDHHmm
    m.extend_from_slice(body.as_bytes());
    m.extend_from_slice(b"&&");
    let sum = m
        .iter()
        .fold(0u16, |acc, &b| acc.wrapping_add(u16::from(b)));
    let checksum = 0u16.wrapping_sub(sum);
    m.extend_from_slice(format!("{checksum:04X}").as_bytes());
    m.push(0x03);
    if corrupt {
        m[17] ^= 0x01; // corrupt the first body byte
    }
    m
}

/// Controllable ATG console. When unavailable, it closes connections without
/// replying so that reads fail immediately. `atg.rs` tests timeouts.
#[derive(Clone)]
struct FakeAtg {
    pub port: u16,
    tc_volume: Arc<AtomicU32>, // f32 bit pattern
    down: Arc<AtomicBool>,
    connections: Arc<AtomicU32>,
}

impl FakeAtg {
    fn set_volume(&self, tc_volume_l: f32) {
        self.tc_volume
            .store(tc_volume_l.to_bits(), Ordering::SeqCst);
    }

    fn set_down(&self, down: bool) {
        self.down.store(down, Ordering::SeqCst);
    }

    /// The connections the console has accepted, answered or not. Each
    /// on-demand read opens one, so an unchanged count shows that the gauge
    /// was not read.
    fn connections(&self) -> u32 {
        self.connections.load(Ordering::SeqCst)
    }
}

/// Spawn a fake ATG console on an ephemeral port. It answers i201/i205/i101 on
/// each persistent connection. `i101_body` selects the system-status body;
/// `corrupt_i201` corrupts the inventory frame's checksum. Reports a 35 000 L
/// tank until the returned handle changes it.
async fn spawn_fake_atg(i101_body: &'static str, corrupt_i201: bool) -> FakeAtg {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();
    let handle = FakeAtg {
        port,
        tc_volume: Arc::new(AtomicU32::new(35_000.0_f32.to_bits())),
        down: Arc::new(AtomicBool::new(false)),
        connections: Arc::new(AtomicU32::new(0)),
    };
    let console = handle.clone();
    tokio::spawn(async move {
        loop {
            let Ok((mut stream, _)) = listener.accept().await else {
                return;
            };
            // Counted before any reply or close, so a read the FMS has
            // completed or failed is already counted.
            console.connections.fetch_add(1, Ordering::SeqCst);
            if console.down.load(Ordering::SeqCst) {
                continue; // drop the connection unanswered
            }
            let console = console.clone();
            tokio::spawn(async move {
                loop {
                    let mut soh = [0u8; 1];
                    if stream.read_exact(&mut soh).await.is_err() {
                        return;
                    }
                    if soh[0] != 0x01 {
                        continue;
                    }
                    let mut code = [0u8; 6];
                    if stream.read_exact(&mut code).await.is_err() {
                        return;
                    }
                    let tc_volume = f32::from_bits(console.tc_volume.load(Ordering::SeqCst));
                    let response = match &code[1..4] {
                        b"201" => atg_frame("i20100", &atg_i201_body(tc_volume), corrupt_i201),
                        b"205" => atg_frame("i20500", "0100", false),
                        b"101" => atg_frame("i10100", i101_body, false),
                        _ => b"\x019999FF1B\x03".to_vec(),
                    };
                    if stream.write_all(&response).await.is_err() {
                        return;
                    }
                }
            });
        }
    });
    handle
}

/// Build an `AppState` whose config points the ATG poller at `atg_port`, plus
/// the event receiver, without standing up the HTTP server.
async fn atg_state(atg_port: u16) -> (AppState, Arc<Config>, LogQueues) {
    let pool = fresh_db_pool().await;
    let mut config = test_config(0);
    config.atg_port = atg_port;
    let config = Arc::new(config);
    let log_queues = LogQueues::new(QUEUE_DEPTH);
    let state = AppState::new(pool, config.clone(), log_queues.clone());
    (state, config, log_queues)
}

/// ATG readings and alarms are the metrics class (log_queue::EventClass), so
/// they queue there rather than with the domain events.
fn drain_events(queues: &LogQueues) -> Vec<fms::state::LogEvent> {
    queues.metrics.drain()
}

async fn atg_reading_count(state: &AppState) -> i64 {
    sqlx::query_scalar::<_, i64>("SELECT COUNT(*) FROM atg_readings")
        .fetch_one(&state.db)
        .await
        .unwrap()
}

#[tokio::test]
async fn atg_poll_cycle_persists_reading_and_emits_event() {
    let atg = spawn_fake_atg("000000", false).await;
    let (state, config, queues) = atg_state(atg.port).await;

    let mut prev = "000000".to_owned();
    fms::background::atg::poll_once(&state, &config, &mut prev)
        .await
        .expect("poll_once");

    let reading = state
        .last_atg
        .read()
        .await
        .clone()
        .expect("a reading should be published");
    assert!((reading.volume_l() - 35_000.0).abs() < 1.0);
    assert!((reading.ullage_l() - 15_000.0).abs() < 1.0);
    assert!((reading.level_mm() - 5_000.0).abs() < 1.0);
    assert!((reading.temperature_c() - 15.0).abs() < 1.0);
    assert!(!reading.bund_alarm_active());
    assert_eq!(reading.alarm_flags(), "000000");

    assert_eq!(atg_reading_count(&state).await, 1);

    let events = drain_events(&queues);
    assert_eq!(
        events
            .iter()
            .filter(|e| e.kind == EventKind::AtgReading)
            .count(),
        1
    );
    assert_eq!(
        events
            .iter()
            .filter(|e| e.kind == EventKind::AtgAlarm)
            .count(),
        0
    );
}

#[tokio::test]
async fn atg_poll_cycle_emits_bund_alarm_on_rising_edge() {
    let atg = spawn_fake_atg("030301", false).await;
    let (state, config, queues) = atg_state(atg.port).await;

    // Previous cycle had no alarm — this cycle latches it.
    let mut prev = "000000".to_owned();
    fms::background::atg::poll_once(&state, &config, &mut prev)
        .await
        .expect("poll_once");

    let reading = state.last_atg.read().await.clone().expect("reading");
    assert!(reading.bund_alarm_active());
    assert_eq!(reading.alarm_flags(), "030301");
    assert_eq!(prev, "030301"); // carried forward for edge detection

    let events = drain_events(&queues);
    let alarms: Vec<_> = events
        .iter()
        .filter(|e| e.kind == EventKind::AtgAlarm)
        .collect();
    assert_eq!(alarms.len(), 1);
    assert_eq!(alarms[0].fields["code"], "030301");
    assert_eq!(alarms[0].fields["alarm"], "bund_leak");
}

#[tokio::test]
async fn atg_poll_cycle_rejects_corrupt_frame() {
    let atg = spawn_fake_atg("000000", true).await;
    let (state, config, queues) = atg_state(atg.port).await;

    let mut prev = "000000".to_owned();
    // A corrupt frame is rejected wholesale: poll_once returns Ok (it warns and
    // skips), but nothing is published, persisted, or emitted.
    fms::background::atg::poll_once(&state, &config, &mut prev)
        .await
        .expect("poll_once returns Ok even when the cycle is rejected");

    assert!(state.last_atg.read().await.is_none());
    assert_eq!(atg_reading_count(&state).await, 0);
    assert!(drain_events(&queues).is_empty());
    assert_eq!(prev, "000000"); // unchanged
}

fn atg_reading_recorded_at(volume_l: f64, recorded_at: chrono::DateTime<Utc>) -> AtgReading {
    AtgReading::new(
        volume_l,
        50_000.0 - volume_l,
        5_000.0,
        15.0,
        "000000".to_owned(),
        false,
        recorded_at,
    )
    .expect("valid ATG reading")
}

#[tokio::test]
async fn dispense_close_uses_a_fresh_reading_or_recent_cached_reading() {
    let atg = spawn_fake_atg("000000", false).await;
    let pool = PgPoolOptions::new()
        .connect_lazy("postgres://unused@127.0.0.1:1/unused")
        .unwrap();
    let mut config = test_config(0);
    config.atg_port = atg.port;
    let state = AppState::new(pool, Arc::new(config), LogQueues::new(QUEUE_DEPTH));

    atg.set_volume(31_500.0);
    *state.last_atg.write().await = Some(atg_reading_recorded_at(9_999.0, Utc::now()));
    assert_eq!(
        fms::background::atg::read_tc_volume_or_cached(&state, "dispense close").await,
        Some(31_500.0)
    );

    atg.set_down(true);
    // The configured hourly poll gives a two-hour cache limit.
    for (age_secs, expected) in [
        (Some(2 * 3_600 - 60), Some(28_000.0)),
        (Some(2 * 3_600 + 60), None),
        (Some(-60), None),
        (None, None),
    ] {
        *state.last_atg.write().await = age_secs.map(|age| {
            atg_reading_recorded_at(28_000.0, Utc::now() - ChronoDuration::seconds(age))
        });
        assert_eq!(
            fms::background::atg::read_tc_volume_or_cached(&state, "dispense close").await,
            expected,
            "cache age {age_secs:?}"
        );
    }
}

// ===========================================================================
// Transaction totaliser — GET /api/v1/jobs/current/totaliser
//
// The HMI polls this endpoint throughout a dispense to drive the operator's
// progress display (hmi.md §8 maps GET /api/jobs/current/dispenser/totaliser
// onto it). The Modbus read that feeds the totaliser is exercised by the Python
// integration suite; these tests pin the HTTP response contract the HMI depends
// on — field names, values, and the dispensing→complete status translation.
// ===========================================================================

#[tokio::test]
async fn totaliser_returns_dispensed_and_preset_while_running() {
    let s = spawn_server().await;
    let token = s.admin_login().await;
    let job_id = s.create_job(&token, "dispense").await;

    // Put the job into the Running state with a partial dispense, exactly as the
    // Modbus background task would while metering.
    {
        let mut guard = s.state.current_job.write().await;
        let job = guard.as_mut().unwrap();
        job.set_status(JobStatus::Running);
        job.set_preset_volume_l(500.0).unwrap();
        job.update_current_dispensed_l(250.0).unwrap();
    }

    let resp = s
        .client()
        .get(format!("{}/api/v1/jobs/current/totaliser", s.base_url))
        .bearer_auth(&token)
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let body = resp.json::<serde_json::Value>().await.unwrap();
    assert_eq!(body["job_id"], job_id);
    assert_eq!(body["dispensed_litres"], 250.0);
    assert_eq!(body["preset_litres"], 500.0);
    assert_eq!(body["status"], "dispensing");
}

#[tokio::test]
async fn totaliser_reports_complete_after_dispense() {
    let s = spawn_server().await;
    let token = s.admin_login().await;
    s.create_job(&token, "dispense").await;

    // After auto-stop the job awaits its BDN; the totaliser must read "complete".
    {
        let mut guard = s.state.current_job.write().await;
        let job = guard.as_mut().unwrap();
        job.set_status(JobStatus::AwaitingBdn);
        job.set_preset_volume_l(500.0).unwrap();
        job.update_current_dispensed_l(500.0).unwrap();
    }

    let resp = s
        .client()
        .get(format!("{}/api/v1/jobs/current/totaliser", s.base_url))
        .bearer_auth(&token)
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let body = resp.json::<serde_json::Value>().await.unwrap();
    assert_eq!(body["dispensed_litres"], 500.0);
    assert_eq!(body["status"], "complete");
}

#[tokio::test]
async fn totaliser_returns_404_when_no_current_job() {
    let s = spawn_server().await;
    let token = s.admin_login().await;
    let resp = s
        .client()
        .get(format!("{}/api/v1/jobs/current/totaliser", s.base_url))
        .bearer_auth(&token)
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::NOT_FOUND);
}

#[tokio::test]
async fn totaliser_returns_404_before_dispense_starts() {
    let s = spawn_server().await;
    let token = s.admin_login().await;
    // A freshly created job is Active, not yet metering: no totaliser to report.
    s.create_job(&token, "dispense").await;

    let resp = s
        .client()
        .get(format!("{}/api/v1/jobs/current/totaliser", s.base_url))
        .bearer_auth(&token)
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::NOT_FOUND);
}

// ===========================================================================
// Tank level — GET /api/v1/tanks/{tank_id}/level
//
// The HMI fetches the operator's tank gauge from this endpoint (hmi.md §8 maps
// GET /api/tanks/{id}/level onto it). It serves the last ATG reading ingested by
// the poller. These tests pin the HTTP response contract the HMI depends on: the
// field set, the single addressable tank id, and the no-reading-yet behaviour.
// ===========================================================================

/// Canonical ATG reading for the tank-level tests.
fn sample_atg_reading() -> AtgReading {
    AtgReading::new(
        35_000.0,
        15_000.0,
        5_000.0,
        15.0,
        "000000".to_owned(),
        false,
        Utc::now(),
    )
    .unwrap()
}

#[tokio::test]
async fn tank_level_returns_last_atg_reading() {
    let s = spawn_server().await;
    let token = s.admin_login().await;
    *s.state.last_atg.write().await = Some(sample_atg_reading());

    let resp = s
        .client()
        .get(format!("{}/api/v1/tanks/01/level", s.base_url))
        .bearer_auth(&token)
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let body = resp.json::<serde_json::Value>().await.unwrap();
    assert_eq!(body["tank_id"], "01");
    assert_eq!(body["level_mm"], 5_000.0);
    assert_eq!(body["volume_litres"], 35_000.0);
    assert_eq!(body["ullage_litres"], 15_000.0);
    assert_eq!(body["temperature_c"], 15.0);
    assert!(body["timestamp"].is_string(), "timestamp must be present");
}

#[tokio::test]
async fn tank_level_unknown_tank_returns_404() {
    let s = spawn_server().await;
    let token = s.admin_login().await;
    // A reading exists, but only tank "01" is addressable (single-tank asset).
    *s.state.last_atg.write().await = Some(sample_atg_reading());

    let resp = s
        .client()
        .get(format!("{}/api/v1/tanks/02/level", s.base_url))
        .bearer_auth(&token)
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::NOT_FOUND);
}

#[tokio::test]
async fn tank_level_returns_503_before_first_reading() {
    let s = spawn_server().await;
    let token = s.admin_login().await;
    // No ATG poll has completed yet: last_atg is empty.
    let resp = s
        .client()
        .get(format!("{}/api/v1/tanks/01/level", s.base_url))
        .bearer_auth(&token)
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::SERVICE_UNAVAILABLE);
    let body = resp.json::<serde_json::Value>().await.unwrap();
    assert_eq!(body["code"], "no_atg_reading");
}

// ===========================================================================
// Cavitation checks on fresh readings (fms.md §5.1)
//
// Both dispense checks take a fresh on-demand reading and never the cached one.
// Creation needs the current volume strictly above the threshold; the start
// needs the current volume and the projected closing volume, the current
// volume less the preset, strictly above it. With the 50 000 L tank of these
// tests the threshold is 5 000 L.
// ===========================================================================

/// A dispense job whose vessel card is validated, ready to start: the operator's
/// token and the job id.
async fn startable_dispense(s: &TestServer) -> (String, String) {
    let admin = s.admin_login().await;
    s.enrol_card(&admin, 67826).await;
    let token = s.inject_operator_session().await;
    let job_id = s.create_job(&token, "dispense").await;
    let resp = s
        .client()
        .post(format!("{}/api/v1/jobs/{job_id}/rfid", s.base_url))
        .bearer_auth(&token)
        .json(&serde_json::json!({"card_number": 67826}))
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    (token, job_id)
}

/// `POST /api/v1/jobs/{job_id}/start` with `preset_volume`.
async fn start_dispense(
    s: &TestServer,
    token: &str,
    job_id: &str,
    preset_volume: f64,
) -> reqwest::Response {
    s.client()
        .post(format!("{}/api/v1/jobs/{job_id}/start", s.base_url))
        .bearer_auth(token)
        .json(&serde_json::json!({ "preset_volume": preset_volume }))
        .send()
        .await
        .unwrap()
}

/// The code of a refusal's body (fms.md §7).
async fn refusal_code(resp: reqwest::Response) -> serde_json::Value {
    resp.json::<serde_json::Value>().await.unwrap()["code"].clone()
}

#[tokio::test]
async fn dispense_create_without_a_fresh_reading_is_refused_with_503() {
    let s = spawn_server().await;
    let token = s.inject_operator_session().await;
    // A healthy cached reading does not stand in for the fresh one.
    *s.state.last_atg.write().await = Some(sample_atg_reading());
    s.atg.set_down(true);

    let resp = s
        .client()
        .post(format!("{}/api/v1/jobs", s.base_url))
        .bearer_auth(&token)
        .json(&serde_json::json!({
            "type": "dispense",
            "counterparty": TEST_VESSEL,
            "grade": "MGO",
        }))
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::SERVICE_UNAVAILABLE);
    assert_eq!(refusal_code(resp).await, "atg_read_failed");

    // No job was created.
    let current = s
        .client()
        .get(format!("{}/api/v1/jobs/current", s.base_url))
        .bearer_auth(&token)
        .send()
        .await
        .unwrap();
    assert_eq!(current.status(), StatusCode::NOT_FOUND);
    assert!(s.events_of(EventKind::JobOpened).is_empty());
}

/// Fills are exempt from the cavitation check, so a fill is created without a
/// reading (fms.md §5.1).
#[tokio::test]
async fn fill_create_without_a_reading_is_created() {
    let s = spawn_server().await;
    let token = s.inject_operator_session().await;
    s.atg.set_down(true);
    s.create_job(&token, "fill").await;
}

#[tokio::test]
async fn dispense_start_refused_when_tank_at_or_below_threshold() {
    let s = spawn_server().await;
    let (token, job_id) = startable_dispense(&s).await;
    s.atg.set_volume(5_000.0);

    let resp = start_dispense(&s, &token, &job_id, 100.0).await;
    assert_eq!(resp.status(), StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(refusal_code(resp).await, "cavitation_threshold");
}

/// The projected closing volume must be strictly above the threshold: 5 400 L
/// less a 400 L preset is refused, and less a 399 L preset is not. The refusal
/// leaves the job active, so the same job starts afterwards.
#[tokio::test]
async fn dispense_start_refused_when_projected_closing_volume_at_or_below_threshold() {
    let s = spawn_server().await;
    let (token, job_id) = startable_dispense(&s).await;
    s.atg.set_volume(5_400.0);

    let resp = start_dispense(&s, &token, &job_id, 400.0).await;
    assert_eq!(resp.status(), StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(refusal_code(resp).await, "cavitation_threshold");
    assert_eq!(s.current_job(&token).await["status"], "active");
    assert!(
        s.events_of(EventKind::TransactionOpen).is_empty(),
        "a refused start emits no transaction_open"
    );

    let resp = start_dispense(&s, &token, &job_id, 399.0).await;
    assert_eq!(resp.status(), StatusCode::OK);
    assert_eq!(s.current_job(&token).await["status"], "running");
}

#[tokio::test]
async fn dispense_start_without_a_fresh_reading_is_refused_with_503() {
    let s = spawn_server().await;
    let (token, job_id) = startable_dispense(&s).await;
    *s.state.last_atg.write().await = Some(sample_atg_reading());
    s.atg.set_down(true);

    let resp = start_dispense(&s, &token, &job_id, 500.0).await;
    assert_eq!(resp.status(), StatusCode::SERVICE_UNAVAILABLE);
    assert_eq!(refusal_code(resp).await, "atg_read_failed");
    assert_eq!(s.current_job(&token).await["status"], "active");
    assert!(s.events_of(EventKind::TransactionOpen).is_empty());
}

/// The preset is recorded as the register holds it, rounded to 0.1 L, and
/// `transaction_open` carries the same value, with the fresh reading as the
/// volume at the transfer's start (fms.md §7.4).
#[tokio::test]
async fn dispense_start_records_the_register_preset_and_the_fresh_reading() {
    let s = spawn_server().await;
    let (token, job_id) = startable_dispense(&s).await;

    let resp = start_dispense(&s, &token, &job_id, 1234.56).await;
    assert_eq!(resp.status(), StatusCode::OK);

    let events = s.events_of(EventKind::TransactionOpen);
    assert_eq!(events.len(), 1);
    assert_eq!(
        events[0].fields["preset_volume_l"],
        serde_json::json!(1234.6)
    );
    assert_eq!(
        events[0].fields["atg_volume_at_open_l"],
        serde_json::json!(35_000.0)
    );
    let guard = s.state.current_job.read().await;
    let job = guard.as_ref().expect("the dispense is the live job");
    assert_eq!(job.preset_volume_l(), Some(1234.6));
}

/// A preset that rounds to zero at the register's 0.1 L resolution cannot be
/// written, so the start is refused as invalid input, with no code.
#[tokio::test]
async fn dispense_start_with_a_preset_that_rounds_to_zero_returns_422() {
    let s = spawn_server().await;
    let (token, job_id) = startable_dispense(&s).await;

    let resp = start_dispense(&s, &token, &job_id, 0.04).await;
    assert_eq!(resp.status(), StatusCode::UNPROCESSABLE_ENTITY);
    assert!(refusal_code(resp).await.is_null());
    assert_eq!(s.current_job(&token).await["status"], "active");
}

/// The creation check needs the current volume strictly above the threshold:
/// a reading of exactly 5 000 L is refused, and 5 000.5 L is not.
#[tokio::test]
async fn dispense_create_at_the_threshold_is_refused_and_just_above_is_created() {
    let s = spawn_server().await;
    let token = s.inject_operator_session().await;
    let dispense = serde_json::json!({
        "type": "dispense",
        "counterparty": TEST_VESSEL,
        "grade": "MGO",
    });

    s.atg.set_volume(5_000.0);
    let resp = s
        .client()
        .post(format!("{}/api/v1/jobs", s.base_url))
        .bearer_auth(&token)
        .json(&dispense)
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(refusal_code(resp).await, "cavitation_threshold");

    s.atg.set_volume(5_000.5);
    let resp = s
        .client()
        .post(format!("{}/api/v1/jobs", s.base_url))
        .bearer_auth(&token)
        .json(&dispense)
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::CREATED);
}

/// The projection uses the preset the register holds. With a 5 100.08 L reading,
/// a 100.06 L request rounds to 100.1 L and projects 4 999.98 L, so the start is
/// refused, although the raw request would project 5 000.02 L. A 100.04 L request
/// rounds to 100.0 L and projects 5 000.08 L, so the same job then starts.
#[tokio::test]
async fn dispense_start_projects_the_rounded_preset() {
    let s = spawn_server().await;
    let (token, job_id) = startable_dispense(&s).await;
    s.atg.set_volume(5_100.08);

    let resp = start_dispense(&s, &token, &job_id, 100.06).await;
    assert_eq!(resp.status(), StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(refusal_code(resp).await, "cavitation_threshold");

    let resp = start_dispense(&s, &token, &job_id, 100.04).await;
    assert_eq!(resp.status(), StatusCode::OK);
}

/// A fill records null for its opening and closing volumes when the on-demand
/// read fails, even with a fresh cached reading at hand (fms.md §5.1).
#[tokio::test]
async fn fill_open_and_close_record_null_without_a_fresh_reading() {
    let s = spawn_server().await;
    let token = s.inject_operator_session().await;
    *s.state.last_atg.write().await = Some(sample_atg_reading());
    s.atg.set_down(true);

    let job_id = s.create_job(&token, "fill").await;
    let opened = s.events_of(EventKind::JobOpened);
    assert_eq!(opened.len(), 1);
    assert!(opened[0].fields["atg_volume_at_open_l"].is_null());

    let resp = s
        .client()
        .post(format!("{}/api/v1/jobs/{job_id}/close", s.base_url))
        .bearer_auth(&token)
        .json(&fill_close_body())
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::CREATED);
    let closed = s.events_of(EventKind::TransactionClosed);
    assert_eq!(closed.len(), 1);
    assert!(closed[0].fields["atg_volume_at_open_l"].is_null());
    assert!(closed[0].fields["atg_volume_at_close_l"].is_null());
}

/// A job in progress answers 409 before the gauge is read, so an unreachable
/// console does not turn it into a 503 (digital.md §4.1, step 2).
#[tokio::test]
async fn create_with_a_job_in_progress_answers_409_before_reading_the_gauge() {
    let s = spawn_server().await;
    let token = s.inject_operator_session().await;
    s.create_job(&token, "fill").await;
    s.atg.set_down(true);

    let resp = s
        .client()
        .post(format!("{}/api/v1/jobs", s.base_url))
        .bearer_auth(&token)
        .json(&serde_json::json!({
            "type": "dispense",
            "counterparty": TEST_VESSEL,
            "grade": "MGO",
        }))
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::CONFLICT);
}

/// A dispense's preset at creation must fit the register: one that rounds to
/// zero is refused, with no code, and no job is created (fms.md §7.4).
#[tokio::test]
async fn dispense_create_with_a_preset_that_rounds_to_zero_returns_422() {
    let s = spawn_server().await;
    let token = s.inject_operator_session().await;

    let resp = s
        .client()
        .post(format!("{}/api/v1/jobs", s.base_url))
        .bearer_auth(&token)
        .json(&serde_json::json!({
            "type": "dispense",
            "counterparty": TEST_VESSEL,
            "grade": "MGO",
            "preset_volume": 0.04,
        }))
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::UNPROCESSABLE_ENTITY);
    assert!(refusal_code(resp).await.is_null());
    assert!(s.events_of(EventKind::JobOpened).is_empty());
}

/// An invalid dispense create answers 422 with the validation message and no
/// code, and the gauge is not read for it, whatever the gauge would report: a
/// reading of 35 000 L, an unreachable console, or a reading of 4 000 L, below
/// the threshold. One request has a preset above the register's 6 553.5 L
/// ceiling, and one an empty counterparty (fms.md §7.4). A valid request then
/// reads the gauge once and receives the answer that reading gives, so the
/// count of reads is shown to count.
#[tokio::test]
async fn an_invalid_dispense_create_is_refused_before_the_gauge_is_read() {
    let invalid = [
        (
            serde_json::json!({
                "type": "dispense",
                "counterparty": TEST_VESSEL,
                "grade": "MGO",
                "preset_volume": 7000.0,
            }),
            JobValidationError::PresetExceedsRegisterLimit(7000.0).to_string(),
        ),
        (
            serde_json::json!({
                "type": "dispense",
                "counterparty": "",
                "grade": "MGO",
            }),
            JobValidationError::EmptyField("counterparty").to_string(),
        ),
    ];
    let valid = serde_json::json!({
        "type": "dispense",
        "counterparty": TEST_VESSEL,
        "grade": "MGO",
    });
    // The gauge's state, as a volume or an unreachable console, then the
    // status and code a valid request receives from it.
    for (gauge, volume, status, code) in [
        ("reads 35 000 L", Some(35_000.0), StatusCode::CREATED, None),
        (
            "is unreachable",
            None,
            StatusCode::SERVICE_UNAVAILABLE,
            Some("atg_read_failed"),
        ),
        (
            "reads 4 000 L",
            Some(4_000.0),
            StatusCode::UNPROCESSABLE_ENTITY,
            Some("cavitation_threshold"),
        ),
    ] {
        let s = spawn_server().await;
        let token = s.inject_operator_session().await;
        match volume {
            Some(v) => s.atg.set_volume(v),
            None => s.atg.set_down(true),
        }

        for (request, message) in &invalid {
            let resp = s
                .client()
                .post(format!("{}/api/v1/jobs", s.base_url))
                .bearer_auth(&token)
                .json(request)
                .send()
                .await
                .unwrap();
            assert_eq!(
                resp.status(),
                StatusCode::UNPROCESSABLE_ENTITY,
                "the gauge {gauge}: {request}"
            );
            assert_eq!(
                resp.json::<serde_json::Value>().await.unwrap(),
                serde_json::json!({ "error": message }),
                "the gauge {gauge}: {request}"
            );
        }
        assert_eq!(
            s.atg.connections(),
            0,
            "the gauge was read for an invalid request (the gauge {gauge})"
        );
        assert!(s.events_of(EventKind::JobOpened).is_empty());

        let resp = s
            .client()
            .post(format!("{}/api/v1/jobs", s.base_url))
            .bearer_auth(&token)
            .json(&valid)
            .send()
            .await
            .unwrap();
        assert_eq!(resp.status(), status, "the gauge {gauge}");
        assert_eq!(
            refusal_code(resp).await,
            code.map_or(serde_json::Value::Null, serde_json::Value::from),
            "the gauge {gauge}"
        );
        assert_eq!(s.atg.connections(), 1, "the gauge {gauge}");
    }
}

/// Request validation comes before the check for a job in progress: an invalid
/// create during another job answers 422 without a code. A valid one answers
/// 409 (`create_with_a_job_in_progress_answers_409_before_reading_the_gauge`).
#[tokio::test]
async fn an_invalid_create_during_another_job_answers_422() {
    let s = spawn_server().await;
    let token = s.inject_operator_session().await;
    s.create_job(&token, "fill").await;

    let resp = s
        .client()
        .post(format!("{}/api/v1/jobs", s.base_url))
        .bearer_auth(&token)
        .json(&serde_json::json!({
            "type": "dispense",
            "counterparty": "",
            "grade": "MGO",
        }))
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::UNPROCESSABLE_ENTITY);
    assert!(refusal_code(resp).await.is_null());
}

// ===========================================================================
// Cancellation (fms.md §7.4)
//
// Only an active job can be cancelled. A dispense cannot be cancelled once it
// is running, so a running dispense, one awaiting its BDN and a closed job
// answer 409 and keep their status. The dock operator cancels a job whose
// start the FMS refused, which is still active.
// ===========================================================================

/// `DELETE /api/v1/jobs/{job_id}`.
async fn delete_job(s: &TestServer, token: &str, job_id: &str) -> reqwest::Response {
    s.client()
        .delete(format!("{}/api/v1/jobs/{job_id}", s.base_url))
        .bearer_auth(token)
        .send()
        .await
        .unwrap()
}

/// The number of rows in `transactions`.
async fn transaction_count(s: &TestServer) -> i64 {
    sqlx::query_scalar::<_, i64>("SELECT COUNT(*) FROM transactions")
        .fetch_one(&s.state.db)
        .await
        .unwrap()
}

/// The cancel of a running dispense answers 409 without a code. The job stays
/// running with its transaction open: one `transaction_open` event, no
/// `transaction_closed` event and no transaction row. A second dispense create
/// is refused with 409 while it runs.
#[tokio::test]
async fn cancel_of_a_running_dispense_answers_409() {
    let s = spawn_server().await;
    let (token, job_id) = startable_dispense(&s).await;
    let resp = start_dispense(&s, &token, &job_id, 500.0).await;
    assert_eq!(resp.status(), StatusCode::OK);

    let resp = delete_job(&s, &token, &job_id).await;
    assert_eq!(resp.status(), StatusCode::CONFLICT);
    assert!(refusal_code(resp).await.is_null());

    let current = s.current_job(&token).await;
    assert_eq!(current["job_id"], job_id);
    assert_eq!(current["status"], "running");
    let events = s.log_queues.events.drain();
    let count = |kind: EventKind| events.iter().filter(|e| e.kind == kind).count();
    assert_eq!(count(EventKind::TransactionOpen), 1);
    assert_eq!(count(EventKind::TransactionClosed), 0);
    assert_eq!(transaction_count(&s).await, 0);

    let resp = s
        .client()
        .post(format!("{}/api/v1/jobs", s.base_url))
        .bearer_auth(&token)
        .json(&serde_json::json!({
            "type": "dispense",
            "counterparty": TEST_VESSEL,
            "grade": "MGO",
        }))
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::CONFLICT);
    let current = s.current_job(&token).await;
    assert_eq!(current["job_id"], job_id);
    assert_eq!(current["status"], "running");
}

/// Once metering is complete, the cancel answers 409: for the dispense awaiting
/// its BDN, and again once the BDN has closed it. The job keeps its status each
/// time.
#[tokio::test]
async fn cancel_of_a_dispense_after_metering_answers_409() {
    let s = spawn_server().await;
    let (token, job_id) = startable_dispense(&s).await;
    let resp = start_dispense(&s, &token, &job_id, 500.0).await;
    assert_eq!(resp.status(), StatusCode::OK);
    // The auto-stop, as the Modbus task records it: the metering transaction
    // row, then the job awaiting its BDN.
    s.seed_dispense_transaction(Uuid::parse_str(&job_id).unwrap())
        .await;
    s.state
        .current_job
        .write()
        .await
        .as_mut()
        .unwrap()
        .set_status(JobStatus::AwaitingBdn);

    let resp = delete_job(&s, &token, &job_id).await;
    assert_eq!(resp.status(), StatusCode::CONFLICT);
    assert_eq!(s.current_job(&token).await["status"], "awaiting_bdn");

    let resp = s
        .client()
        .post(format!("{}/api/v1/jobs/{job_id}/bdn", s.base_url))
        .bearer_auth(&token)
        .json(&serde_json::json!({ "bdn_number": "BDN-0001" }))
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::CREATED);
    assert_eq!(s.current_job(&token).await["status"], "closed");

    let resp = delete_job(&s, &token, &job_id).await;
    assert_eq!(resp.status(), StatusCode::CONFLICT);
    assert_eq!(s.current_job(&token).await["status"], "closed");
}

/// The cancel of a fill closed by its delivery ticket answers 409, and the fill
/// stays closed.
#[tokio::test]
async fn cancel_of_a_closed_fill_answers_409() {
    let s = spawn_server().await;
    let token = s.inject_operator_session().await;
    let job_id = s.create_job(&token, "fill").await;
    let resp = s
        .client()
        .post(format!("{}/api/v1/jobs/{job_id}/close", s.base_url))
        .bearer_auth(&token)
        .json(&fill_close_body())
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::CREATED);

    let resp = delete_job(&s, &token, &job_id).await;
    assert_eq!(resp.status(), StatusCode::CONFLICT);
    assert_eq!(s.current_job(&token).await["status"], "closed");
}

/// The dock operator's cancel after a refused start: the job is still active,
/// so the cancel answers 204, the job is cancelled, and a new job can be
/// created. A second cancel, as when the first answer was lost, answers 204
/// and the job stays cancelled.
#[tokio::test]
async fn cancel_of_an_active_job_after_a_refused_start_answers_204() {
    let s = spawn_server().await;
    let (token, job_id) = startable_dispense(&s).await;
    s.atg.set_volume(5_000.0);
    let resp = start_dispense(&s, &token, &job_id, 100.0).await;
    assert_eq!(resp.status(), StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(refusal_code(resp).await, "cavitation_threshold");
    assert_eq!(s.current_job(&token).await["status"], "active");

    let resp = delete_job(&s, &token, &job_id).await;
    assert_eq!(resp.status(), StatusCode::NO_CONTENT);
    assert_eq!(s.current_job(&token).await["status"], "cancelled");

    let resp = delete_job(&s, &token, &job_id).await;
    assert_eq!(resp.status(), StatusCode::NO_CONTENT);
    assert_eq!(s.current_job(&token).await["status"], "cancelled");

    s.atg.set_volume(35_000.0);
    s.create_job(&token, "dispense").await;
}
