//! Harness for the three-way integration test.
//!
//! Brings up the real digital world — a real FMS and a real HMI built from their
//! own crates' public interfaces — plus the small supporting services the FMS
//! reaches that are *not* the sim: the corp-IT LDAP directory, the log-server,
//! and PostgreSQL. The sim itself is constructed by the test (it owns the clock).
//!
//! Boundary transports are real: the HMI and FMS connect to the sim's RFID
//! readers, ATG console, and dispenser over TCP exactly as they would in
//! deployment. Nothing here mocks the FMS or HMI — only the appliances and
//! infrastructure a port's own IT/OT team would not write.
//!
//! What is faithful vs. simplified, relative to a real deployment:
//! - **Faithful:** every FMS/HMI HTTP, Modbus TCP, Veeder-Root TLS, and RFID
//!   serial-over-TCP exchange; the FMS state machine, DB schema (real Postgres,
//!   real append-only triggers), background pollers, and event push.
//! - **Simplified:** TLS termination and HAProxy source-IP binding are omitted
//!   (HMI<->FMS speak plain HTTP); corp-IT is a bind-only LDAP stub; the
//!   log-server is an HTTP sink. This test deliberately does not cover the
//!   container/network topology.

use std::collections::HashMap;
use std::future::Future;
use std::net::SocketAddr;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use serde_json::Value;
use tokio::net::TcpListener;
use tokio::task::{Id, JoinSet};

/// A free loopback address. Bound momentarily to learn an OS-assigned port, then
/// released; the caller binds it for real. Carries the usual small race, accepted
/// for a test (the same approach `full_sim.rs` uses).
pub fn free_addr() -> SocketAddr {
    std::net::TcpListener::bind("127.0.0.1:0")
        .unwrap()
        .local_addr()
        .unwrap()
}

// ===========================================================================
// Supervision of spawned tasks
// ===========================================================================

/// Named tasks that are each expected to run for the whole test. tokio catches
/// a spawned task's panic, so without supervision a dead task does not fail the
/// test: it continues with that part of the digital world missing.
#[derive(Default)]
pub struct BackgroundTasks {
    set: JoinSet<()>,
    names: HashMap<Id, &'static str>,
}

impl BackgroundTasks {
    pub fn spawn<F>(&mut self, name: &'static str, task: F)
    where
        F: Future<Output = ()> + Send + 'static,
    {
        let handle = self.set.spawn(task);
        self.names.insert(handle.id(), name);
    }

    /// Await the first task to stop. Every task runs for the whole test, so a
    /// return is as much a failure as a panic; returns a description naming
    /// the task. Pending while every task runs, mirroring
    /// `SimHandles::await_first_failure`.
    pub async fn await_first_failure(&mut self) -> String {
        match self.set.join_next_with_id().await {
            Some(Ok((id, ()))) => format!("{} exited", self.names[&id]),
            Some(Err(error)) => {
                let name = self.names[&error.id()];
                if error.is_panic() {
                    format!("{name} panicked: {}", panic_message(error.into_panic()))
                } else {
                    format!("{name} was cancelled")
                }
            }
            None => std::future::pending().await,
        }
    }
}

/// Best-effort extraction of a panic's message.
fn panic_message(payload: Box<dyn std::any::Any + Send>) -> String {
    if let Some(message) = payload.downcast_ref::<&str>() {
        (*message).to_string()
    } else if let Some(message) = payload.downcast_ref::<String>() {
        message.clone()
    } else {
        "non-string payload".to_string()
    }
}

// ===========================================================================
// PostgreSQL (real, embedded — no Docker)
// ===========================================================================

use postgresql_embedded::{PostgreSQL, Settings, VersionReq};

/// A running embedded PostgreSQL with the `fms` database created. Must be held
/// for the lifetime of the test: dropping it stops the server and removes the
/// temporary data directory.
pub struct Db {
    _pg: PostgreSQL,
    pub url: String,
}

/// Download (first run only), start an embedded PostgreSQL, and create the `fms`
/// database. Returns the connection URL and the live handle.
pub async fn start_postgres() -> Db {
    // The per-command timeout governs `initdb` and `pg_ctl start`. The crate's
    // 5-second default is too tight on a loaded developer machine (initdb does
    // real work and the test is compiling/running concurrently), surfacing as
    // `DatabaseInitializationError("deadline has elapsed")`. Give it ample room.
    let settings = Settings {
        timeout: Some(Duration::from_secs(60)),
        // PostgreSQL 16 is the major version the FMS host runs (Ubuntu 24.04's
        // `postgresql` package). The exact release is pinned so that the test
        // keeps the same server when theseus-rs/postgresql-binaries publishes
        // a new release. Release 18.7.0 stores time zone aliases as tar hard
        // links, which the postgresql_archive 0.20.4 extractor drops, so the
        // `UTC` zone can be missing and sqlx's `TimeZone=UTC` then fails every
        // connection. Release 16.15.0 reads time zones from the host's
        // `/usr/share/zoneinfo`, as Ubuntu's own package does.
        version: VersionReq::parse("=16.15.0").expect("pinned PostgreSQL version"),
        ..Settings::default()
    };
    let mut pg = PostgreSQL::new(settings);
    pg.setup().await.expect("embedded postgres setup");
    pg.start().await.expect("embedded postgres start");
    pg.create_database("fms")
        .await
        .expect("create fms database");
    let url = pg.settings().url("fms");
    Db { _pg: pg, url }
}

// ===========================================================================
// Log-server (vmagent/vlagent) sinks
// ===========================================================================

/// Records every domain event the FMS pushes to `POST /insert/jsonline`
/// (`fms.md` §5.4 path 2, `hist.md` §4.2's flat envelope — fields sit at the
/// top level, not nested under a `payload` key). ATG metrics pushed to
/// `/api/v1/import` (path 1) and the authentication/card-table syslog stream
/// (path 3) are accepted and discarded: nothing in this suite asserts on them.
pub struct LogSink {
    pub base_url: String,
    pub syslog_addr: SocketAddr,
    pub events: Arc<Mutex<Vec<Value>>>,
}

impl LogSink {
    /// All `event_type` values recorded so far, in arrival order.
    pub fn event_types(&self) -> Vec<String> {
        self.events
            .lock()
            .unwrap()
            .iter()
            .filter_map(|e| e.get("event_type")?.as_str().map(str::to_owned))
            .collect()
    }

    /// The first recorded event of `event_type`, if any.
    pub fn first_payload(&self, event_type: &str) -> Option<Value> {
        self.events
            .lock()
            .unwrap()
            .iter()
            .find(|e| e.get("event_type").and_then(Value::as_str) == Some(event_type))
            .cloned()
    }

    /// The `atg_volume_at_open_l` of the first *fill* `transaction_closed`.
    ///
    /// Dispenses also emit `transaction_closed` (and close first), so a plain
    /// `first_payload("transaction_closed")` would return the dispense's open
    /// level — near the full tank — not the fill's. A fill's event is the one
    /// carrying the delivery-ticket field `delivered_volume_l`.
    pub fn first_fill_open_level(&self) -> Option<f64> {
        self.events
            .lock()
            .unwrap()
            .iter()
            .filter(|e| e.get("event_type").and_then(Value::as_str) == Some("transaction_closed"))
            .find(|e| e.get("delivered_volume_l").is_some())
            .and_then(|e| e.get("atg_volume_at_open_l")?.as_f64())
    }

    /// The dispense's `transaction_closed` event (as opposed to the fill's,
    /// which shares the event type — see `first_fill_open_level`). Identified
    /// by `volume_dispensed_l`, a field only the dispense variant carries.
    pub fn first_dispense_close_payload(&self) -> Option<Value> {
        self.events
            .lock()
            .unwrap()
            .iter()
            .filter(|e| e.get("event_type").and_then(Value::as_str) == Some("transaction_closed"))
            .find(|e| e.get("volume_dispensed_l").is_some())
            .cloned()
    }
}

/// Start the log-server sinks on ephemeral ports: an HTTP listener presenting
/// vmagent's and vlagent's ingest paths (`fms.md` §5.4), and a TCP listener
/// standing in for vlagent's syslog listener. Domain events posted to
/// `/insert/jsonline` are parsed and recorded (one JSON object per line, as
/// vlagent accepts); `/api/v1/import` and the syslog connection are accepted
/// and their bodies discarded.
pub async fn start_log_sink() -> LogSink {
    let events: Arc<Mutex<Vec<Value>>> = Arc::new(Mutex::new(Vec::new()));
    let recorder = events.clone();

    let app = axum::Router::new()
        .route(
            "/insert/jsonline",
            axum::routing::post(move |body: String| {
                let recorder = recorder.clone();
                async move {
                    for line in body.lines().filter(|l| !l.trim().is_empty()) {
                        if let Ok(event) = serde_json::from_str::<Value>(line) {
                            recorder.lock().unwrap().push(event);
                        }
                    }
                    axum::http::StatusCode::OK
                }
            }),
        )
        .route(
            "/api/v1/import",
            axum::routing::post(|_body: String| async { axum::http::StatusCode::OK }),
        );

    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    tokio::spawn(async move {
        axum::serve(listener, app).await.expect("log sink serve");
    });

    let syslog_listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let syslog_addr = syslog_listener.local_addr().unwrap();
    tokio::spawn(async move {
        while let Ok((mut stream, _)) = syslog_listener.accept().await {
            tokio::spawn(async move {
                use tokio::io::AsyncReadExt;
                let mut buf = [0u8; 1024];
                while matches!(stream.read(&mut buf).await, Ok(n) if n > 0) {}
            });
        }
    });

    LogSink {
        base_url: format!("http://{addr}"),
        syslog_addr,
        events,
    }
}

// ===========================================================================
// Corp-IT LDAP directory (bind-only stub)
// ===========================================================================

use futures::{SinkExt, StreamExt};
use ldap3_proto::proto::{LdapBindCred, LdapBindResponse, LdapMsg, LdapOp};
use ldap3_proto::LdapCodec;
use tokio_util::codec::Framed;

/// Start a minimal LDAP server on an ephemeral port. A simple-bind for
/// `expect_dn` with `expect_pw` succeeds; every other bind returns invalid
/// credentials. Returns the listen port.
///
/// This is the corp-IT directory the FMS binds against for operator login
/// (digital.md §5.2). Only the bind operation is implemented — that is all the
/// FMS uses.
pub async fn start_ldap_stub(expect_dn: String, expect_pw: String) -> u16 {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();

    tokio::spawn(async move {
        loop {
            let Ok((stream, _)) = listener.accept().await else {
                break;
            };
            let dn = expect_dn.clone();
            let pw = expect_pw.clone();
            tokio::spawn(async move {
                let mut framed = Framed::new(stream, LdapCodec::new(None, None));
                while let Some(Ok(msg)) = framed.next().await {
                    let LdapMsg { msgid, op, .. } = msg;
                    let resp = match op {
                        LdapOp::BindRequest(br) => {
                            let ok = br.dn == dn
                                && matches!(&br.cred, LdapBindCred::Simple(p) if *p == pw);
                            if ok {
                                LdapOp::BindResponse(LdapBindResponse::new_success(""))
                            } else {
                                LdapOp::BindResponse(LdapBindResponse::new_invalidcredentials(
                                    &br.dn,
                                    "invalid credentials",
                                ))
                            }
                        }
                        LdapOp::UnbindRequest => return,
                        _ => continue,
                    };
                    if framed.send(LdapMsg::new(msgid, resp)).await.is_err() {
                        break;
                    }
                }
            });
        }
    });

    port
}

// ===========================================================================
// FMS (real)
// ===========================================================================

/// The sim<->digital boundary endpoints and infrastructure URLs the FMS needs.
pub struct FmsParams {
    pub db_url: String,
    pub ldap_uri: String,
    pub ldap_base_dn: String,
    pub atg_addr: SocketAddr,
    pub dispenser_addr: SocketAddr,
    pub vessel_rfid_addr: SocketAddr,
    /// vmagent's import endpoint, for ATG metrics (`fms.md` §5.4 path 1).
    pub vmagent_url: String,
    /// vlagent's JSON-line endpoint, for domain events (`fms.md` §5.4 path 2).
    pub vlagent_jsonline_url: String,
    /// vlagent's syslog listener, for authentication and card-table events
    /// (`fms.md` §5.4 path 3).
    pub vlagent_syslog_host: String,
    pub vlagent_syslog_port: u16,
    pub k_factor: f64,
    pub tank_capacity_l: f64,
}

/// The running FMS: its base URL, and its background tasks and HTTP server
/// under supervision.
pub struct Fms {
    pub base_url: String,
    pub tasks: BackgroundTasks,
}

/// Build and serve the real FMS bound to `addr`, as `fms::run` does but over
/// plain HTTP (TLS is HAProxy's job in deployment) and with a test config.
/// Without the TLS listener there is no need for `fms::run`'s process-default
/// crypto provider, so none is installed. Enrols the operator and vessel cards
/// the sim actors present, then spawns the real ATG, Modbus, vessel-RFID and
/// log-pusher background tasks.
pub async fn start_fms(addr: SocketAddr, p: FmsParams) -> Fms {
    let config = fms::config::Config {
        database_url: p.db_url.clone(),
        listen_port: addr.port(),
        tls_cert_path: String::new(),
        tls_key_path: String::new(),
        admin_username: "fms-admin".to_owned(),
        admin_password_hash: String::new(),
        ldap_uri: p.ldap_uri,
        ldap_base_dn: p.ldap_base_dn,
        // The harness enrols the cards it needs directly (below), so no
        // startup seeding.
        seed_cards: Vec::new(),
        atg_host: p.atg_addr.ip().to_string(),
        atg_port: p.atg_addr.port(),
        atg_poll_interval: Duration::from_millis(100),
        dispenser_host: p.dispenser_addr.ip().to_string(),
        dispenser_port: p.dispenser_addr.port(),
        modbus_poll_interval: Duration::from_millis(50),
        vessel_rfid_host: p.vessel_rfid_addr.ip().to_string(),
        vessel_rfid_tcp_port: p.vessel_rfid_addr.port(),
        vmagent_url: p.vmagent_url,
        vlagent_jsonline_url: p.vlagent_jsonline_url,
        vlagent_syslog_host: p.vlagent_syslog_host,
        vlagent_syslog_port: p.vlagent_syslog_port,
        session_timeout: Duration::from_secs(3600),
        tank_capacity_l: p.tank_capacity_l,
        cavitation_threshold_pct: 10.0,
        k_factor: p.k_factor,
        // The values the sim writes onto the paper artefacts; FMS and sim must
        // agree on them (deployment-config.md §1).
        grade: sim::constants::FUEL_GRADE.to_owned(),
        density_kg_per_l: sim::constants::DENSITY_KG_PER_L,
    };

    let pool = fms::db::connect(&p.db_url).await.expect("fms db connect");
    fms::db::run_migrations(&pool)
        .await
        .expect("fms migrations");

    // Enrol the cards the sim presents: each roster tug's tag and the operator
    // badge (1_000_002, dock operator). Each is labelled with its holder, as the
    // deployment seeds it (fms.md §8.2): the vessel RFID gate requires the tag
    // to be enrolled to the vessel the job names, and login requires the badge
    // to be enrolled to the operator's username (fms.md §4).
    for vessel in sim::constants::VESSELS {
        enrol_card(
            &pool,
            i32::try_from(vessel.rfid_card.0)
                .expect("a vessel card number beyond the card table's range"),
            &vessel.vessel_id.0.to_string(),
        )
        .await;
    }
    enrol_card(&pool, 1_000_002, sim::constants::OPERATOR_USERNAME).await;

    let queues = fms::log_queue::LogQueues::new(fms::log_queue::QUEUE_DEPTH);
    let state = fms::state::AppState::new(pool, Arc::new(config.clone()), queues.clone());

    let mut tasks = BackgroundTasks::default();
    tasks.spawn("fms atg poller", fms::background::atg::run(state.clone()));
    tasks.spawn(
        "fms modbus poller",
        fms::background::modbus::run(state.clone()),
    );
    tasks.spawn(
        "fms vessel rfid reader",
        fms::background::rfid::run(state.clone()),
    );
    tasks.spawn(
        "fms metrics pusher",
        fms::background::log_pusher::run(Arc::clone(&queues.metrics), config.vmagent_url.clone()),
    );
    tasks.spawn(
        "fms events pusher",
        fms::background::log_pusher::run(
            Arc::clone(&queues.events),
            config.vlagent_jsonline_url.clone(),
        ),
    );
    tasks.spawn(
        "fms syslog pusher",
        fms::background::syslog::run(
            Arc::clone(&queues.syslog),
            config.vlagent_syslog_host.clone(),
            config.vlagent_syslog_port,
            fms::background::syslog::local_hostname(),
        ),
    );

    let router = fms::api::router(state);
    let listener = TcpListener::bind(addr).await.expect("fms bind");
    let base_url = format!("http://{}", listener.local_addr().unwrap());
    tasks.spawn("fms http server", async move {
        axum::serve(
            listener,
            router.into_make_service_with_connect_info::<SocketAddr>(),
        )
        .await
        .expect("fms serve");
    });
    Fms { base_url, tasks }
}

async fn enrol_card(pool: &sqlx::PgPool, card_number: i32, label: &str) {
    sqlx::query(
        "INSERT INTO card_table (card_id, card_number, label, active, action)
         VALUES (gen_random_uuid(), $1, $2, true, 'enrol')",
    )
    .bind(card_number)
    .bind(label)
    .execute(pool)
    .await
    .expect("enrol card");
}

// ===========================================================================
// HMI (real)
// ===========================================================================

/// Build and serve the real HMI bound to `addr`, exactly as the HMI binary does
/// but over plain HTTP. It connects out to the FMS at `fms_url` and reads the
/// operator RFID reader at `operator_rfid_addr`. Mirrors the binary's startup:
/// the reader connection is maintained by a background task, so the HTTP
/// listener binds without waiting for the reader.
pub async fn start_hmi(addr: SocketAddr, fms_url: String, operator_rfid_addr: SocketAddr) {
    // Plain HTTP in this harness (see doc comment above), so no CA is needed.
    let fms_client = hmi::fms::FmsClient::new(fms_url, None).expect("hmi fms client");
    let rfid = hmi::rfid::RfidReader::new(operator_rfid_addr);
    rfid.start();

    let state = hmi::state::AppState {
        fms: Arc::new(fms_client),
        challenges: hmi::challenge::ChallengeStore::new(30),
        rfid,
    };

    let router = hmi::routes::router(state);
    let listener = TcpListener::bind(addr).await.expect("hmi bind");
    tokio::spawn(async move {
        axum::serve(listener, router).await.expect("hmi serve");
    });
}
