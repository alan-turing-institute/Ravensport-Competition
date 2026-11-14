#[path = "harness/mod.rs"]
mod harness;

use std::sync::{Arc, Mutex};

use axum::http::StatusCode;
use axum::routing::{delete, get, post};
use axum::{Json, Router};
use serde_json::{json, Value};

use harness::{spawn_hmi, MockFms, MockRfid};

// ---------------------------------------------------------------------------
// Mock FMS router helpers
// ---------------------------------------------------------------------------

const MOCK_JOB_UUID: &str = "00000000-0000-0000-0000-000000000001";
const MOCK_TX_UUID: &str = "00000000-0000-0000-0000-000000000002";

/// FMS routes shared by all scenarios (everything except the live-job read).
fn base_fms_routes() -> Router {
    Router::new()
        .route("/api/v1/auth/login", post(mock_fms_login))
        .route("/api/v1/auth/logout", delete(mock_fms_logout))
        .route("/api/v1/jobs", post(mock_fms_create_job))
        .route("/api/v1/jobs/{job_id}/start", post(mock_fms_start_dispense))
        .route("/api/v1/jobs/{job_id}/close", post(mock_fms_close_fill))
        .route("/api/v1/jobs/{job_id}/bdn", post(mock_fms_link_bdn))
        .route(
            "/api/v1/jobs/current/totaliser",
            get(mock_fms_get_totaliser),
        )
        .route(
            "/api/v1/tanks/{tank_id}/level",
            get(mock_fms_get_tank_level),
        )
}

/// Mock whose `GET /jobs/current` returns the supplied job document.
fn mock_fms_with_current(current: Value) -> Router {
    base_fms_routes().route(
        "/api/v1/jobs/current",
        get(move || {
            let c = current.clone();
            async move { Json(c) }
        }),
    )
}

/// Default mock: a live dispense job, active and not yet RFID-validated.
fn mock_fms_router() -> Router {
    mock_fms_with_current(json!({
        "job_id": MOCK_JOB_UUID,
        "type": "dispense",
        "status": "active",
        "rfid_validated": false,
    }))
}

async fn mock_fms_login(Json(body): Json<Value>) -> (StatusCode, Json<Value>) {
    if body["username"] == "dock-operator" && body["rfid_card_number"] == 1_000_002_u32 {
        (
            StatusCode::OK,
            Json(json!({ "session_token": "token-test", "role": "operator" })),
        )
    } else {
        (
            StatusCode::UNAUTHORIZED,
            Json(json!({ "error": "unauthorised" })),
        )
    }
}

async fn mock_fms_logout() -> StatusCode {
    StatusCode::NO_CONTENT
}

async fn mock_fms_create_job(Json(body): Json<Value>) -> (StatusCode, Json<Value>) {
    let job_type = body["type"].as_str().unwrap_or("dispense");
    (
        StatusCode::CREATED,
        Json(json!({ "job_id": MOCK_JOB_UUID, "type": job_type, "status": "active" })),
    )
}

async fn mock_fms_start_dispense(
    axum::extract::Path(_job_id): axum::extract::Path<String>,
) -> StatusCode {
    StatusCode::OK
}

async fn mock_fms_close_fill(
    axum::extract::Path(_job_id): axum::extract::Path<String>,
) -> (StatusCode, Json<Value>) {
    (
        StatusCode::CREATED,
        Json(json!({ "transaction_id": MOCK_TX_UUID })),
    )
}

async fn mock_fms_link_bdn(
    axum::extract::Path(_job_id): axum::extract::Path<String>,
) -> (StatusCode, Json<Value>) {
    (
        StatusCode::CREATED,
        Json(json!({ "transaction_id": MOCK_TX_UUID })),
    )
}

async fn mock_fms_get_tank_level(
    axum::extract::Path(tank_id): axum::extract::Path<String>,
) -> Json<Value> {
    Json(json!({
        "tank_id": tank_id,
        "level_mm": 3571.0,
        "volume_litres": 25000.0,
        "ullage_litres": 25000.0,
        "temperature_c": 15.0,
        "timestamp": "2026-06-13T12:00:00Z",
    }))
}

async fn mock_fms_get_totaliser() -> Json<Value> {
    Json(json!({
        "job_id": MOCK_JOB_UUID,
        "dispensed_litres": 250.0,
        "preset_litres": 500.0,
        "status": "dispensing",
    }))
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[tokio::test]
async fn health_returns_ok() {
    let fms = MockFms::start(mock_fms_router()).await;
    let rfid = MockRfid::start(1_000_002).await;
    let (hmi_url, client) = spawn_hmi(&fms.base_url(), rfid.addr).await;

    let resp = client
        .get(format!("{hmi_url}/health"))
        .send()
        .await
        .unwrap();

    assert_eq!(resp.status(), 200);
    let body: Value = resp.json().await.unwrap();
    assert_eq!(body["status"], "ok");
}

#[tokio::test]
async fn login_returns_challenge_id() {
    let fms = MockFms::start(mock_fms_router()).await;
    let rfid = MockRfid::start(1_000_002).await;
    let (hmi_url, client) = spawn_hmi(&fms.base_url(), rfid.addr).await;

    let resp = client
        .post(format!("{hmi_url}/api/auth/login"))
        .json(&json!({ "username": "dock-operator", "password": "operator-password" }))
        .send()
        .await
        .unwrap();

    assert_eq!(resp.status(), 202);
    let body: Value = resp.json().await.unwrap();
    assert!(
        body["challenge_id"].as_str().is_some(),
        "challenge_id missing"
    );
}

#[tokio::test]
async fn challenge_reads_rfid_and_returns_token() {
    let fms = MockFms::start(mock_fms_router()).await;
    let rfid = MockRfid::start(1_000_002).await;
    let (hmi_url, client) = spawn_hmi(&fms.base_url(), rfid.addr).await;

    // First, get a challenge id.
    let login_resp: Value = client
        .post(format!("{hmi_url}/api/auth/login"))
        .json(&json!({ "username": "dock-operator", "password": "operator-password" }))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    let challenge_id = login_resp["challenge_id"].as_str().unwrap();

    // Resolve the challenge — RFID card is sent by mock reader.
    let resp = client
        .get(format!("{hmi_url}/api/auth/challenge/{challenge_id}"))
        .send()
        .await
        .unwrap();

    assert_eq!(resp.status(), 200);
    let body: Value = resp.json().await.unwrap();
    assert_eq!(body["token"], "token-test");
    assert!(body["expires_at"].as_str().is_some());
}

#[tokio::test]
async fn challenge_expired_returns_408() {
    let fms = MockFms::start(mock_fms_router()).await;
    let rfid = MockRfid::start(1_000_002).await;
    let (hmi_url, client) = spawn_hmi(&fms.base_url(), rfid.addr).await;

    let resp = client
        .get(format!("{hmi_url}/api/auth/challenge/nonexistent-id"))
        .send()
        .await
        .unwrap();

    assert_eq!(resp.status(), 408);
    let body: Value = resp.json().await.unwrap();
    assert_eq!(body["error"], "challenge_expired");
}

#[tokio::test]
async fn logout_returns_204() {
    let fms = MockFms::start(mock_fms_router()).await;
    let rfid = MockRfid::start(1_000_002).await;
    let (hmi_url, client) = spawn_hmi(&fms.base_url(), rfid.addr).await;

    let resp = client
        .delete(format!("{hmi_url}/api/auth/logout"))
        .bearer_auth("token-test")
        .send()
        .await
        .unwrap();

    assert_eq!(resp.status(), 204);
}

#[tokio::test]
async fn create_refuel_returns_job_id_with_pending_status() {
    let fms = MockFms::start(mock_fms_router()).await;
    let rfid = MockRfid::start(1_000_002).await;
    let (hmi_url, client) = spawn_hmi(&fms.base_url(), rfid.addr).await;

    let resp = client
        .post(format!("{hmi_url}/api/jobs/refuel"))
        .bearer_auth("token-test")
        .json(&json!({
            "vessel_id": "1999997",
            "product": "MGO",
            "requested_volume_litres": 500.0
        }))
        .send()
        .await
        .unwrap();

    assert_eq!(resp.status(), 201);
    let body: Value = resp.json().await.unwrap();
    assert!(body["job_id"].as_str().is_some());
    assert_eq!(body["status"], "pending_vessel_rfid");
}

#[tokio::test]
async fn create_refuel_forwards_fms_422_verbatim() {
    // The FMS rejects the dispense with 422, as its cavitation guard does when
    // the tank is at or below the threshold. The HMI must forward that status
    // and reason unchanged, not collapse it into a generic 500 (which the sim
    // would treat as a retryable server fault and panic on).
    async fn reject() -> (StatusCode, Json<Value>) {
        (
            StatusCode::UNPROCESSABLE_ENTITY,
            Json(json!({
                "error": "tank level 3000 L is at or below the cavitation threshold (5000 L)",
                "code": "cavitation_threshold"
            })),
        )
    }
    let fms = MockFms::start(Router::new().route("/api/v1/jobs", post(reject))).await;
    let rfid = MockRfid::start(1_000_002).await;
    let (hmi_url, client) = spawn_hmi(&fms.base_url(), rfid.addr).await;

    let resp = client
        .post(format!("{hmi_url}/api/jobs/refuel"))
        .bearer_auth("token-test")
        .json(&json!({
            "vessel_id": "1999997",
            "product": "MGO",
            "requested_volume_litres": 500.0
        }))
        .send()
        .await
        .unwrap();

    assert_eq!(resp.status(), 422);
    let body: Value = resp.json().await.unwrap();
    assert!(
        body["error"]
            .as_str()
            .unwrap_or_default()
            .contains("cavitation threshold"),
        "forwarded body should carry the FMS reason, got {body:?}"
    );
    // The code reaches the caller unchanged: the dock operator decides on it
    // (hmi.md §7).
    assert_eq!(body["code"], "cavitation_threshold");
}

/// A start refused because the FMS could not evaluate its check reaches the
/// caller with its status and code unchanged (hmi.md §7.3).
#[tokio::test]
async fn dispenser_start_forwards_fms_503_with_its_code() {
    async fn unavailable() -> (StatusCode, Json<Value>) {
        (
            StatusCode::SERVICE_UNAVAILABLE,
            Json(json!({
                "error": "the on-demand ATG read failed",
                "code": "atg_read_failed"
            })),
        )
    }
    let fms =
        MockFms::start(Router::new().route("/api/v1/jobs/{job_id}/start", post(unavailable))).await;
    let rfid = MockRfid::start(1_000_002).await;
    let (hmi_url, client) = spawn_hmi(&fms.base_url(), rfid.addr).await;

    let resp = client
        .post(format!(
            "{hmi_url}/api/jobs/{MOCK_JOB_UUID}/dispenser/start"
        ))
        .bearer_auth("token-test")
        .json(&json!({ "preset_volume_litres": 500.0 }))
        .send()
        .await
        .unwrap();

    assert_eq!(resp.status(), 503);
    let body: Value = resp.json().await.unwrap();
    assert_eq!(
        body,
        json!({ "error": "the on-demand ATG read failed", "code": "atg_read_failed" })
    );
}

/// A mock FMS answering only the job cancel, with 204, recording the job id
/// and the authorisation header of each cancel it receives.
fn capturing_cancel_fms(seen: Arc<Mutex<Vec<(String, String)>>>) -> Router {
    Router::new().route(
        "/api/v1/jobs/{job_id}",
        delete(
            move |axum::extract::Path(job_id): axum::extract::Path<String>,
                  headers: axum::http::HeaderMap| {
                let seen = seen.clone();
                async move {
                    let authorisation = headers
                        .get(axum::http::header::AUTHORIZATION)
                        .and_then(|v| v.to_str().ok())
                        .unwrap_or_default()
                        .to_owned();
                    seen.lock().unwrap().push((job_id, authorisation));
                    StatusCode::NO_CONTENT
                }
            },
        ),
    )
}

/// The cancel reaches the FMS for the same job with the operator's token, and
/// the FMS's 204 is the HMI's answer (hmi.md §7.3).
#[tokio::test]
async fn cancel_job_is_forwarded_to_the_fms() {
    let seen = Arc::new(Mutex::new(Vec::new()));
    let fms = MockFms::start(capturing_cancel_fms(seen.clone())).await;
    let rfid = MockRfid::start(1_000_002).await;
    let (hmi_url, client) = spawn_hmi(&fms.base_url(), rfid.addr).await;

    let resp = client
        .delete(format!("{hmi_url}/api/jobs/{MOCK_JOB_UUID}"))
        .bearer_auth("token-test")
        .send()
        .await
        .unwrap();

    assert_eq!(resp.status(), 204);
    assert_eq!(
        *seen.lock().unwrap(),
        vec![(MOCK_JOB_UUID.to_owned(), "Bearer token-test".to_owned())]
    );
}

/// A job that is no longer the current one answers 404 at the FMS, and the
/// HMI forwards it unchanged (hmi.md §7.3).
#[tokio::test]
async fn cancel_job_forwards_fms_404_verbatim() {
    async fn not_current() -> (StatusCode, Json<Value>) {
        (StatusCode::NOT_FOUND, Json(json!({ "error": "not found" })))
    }
    let fms =
        MockFms::start(Router::new().route("/api/v1/jobs/{job_id}", delete(not_current))).await;
    let rfid = MockRfid::start(1_000_002).await;
    let (hmi_url, client) = spawn_hmi(&fms.base_url(), rfid.addr).await;

    let resp = client
        .delete(format!("{hmi_url}/api/jobs/{MOCK_JOB_UUID}"))
        .bearer_auth("token-test")
        .send()
        .await
        .unwrap();

    assert_eq!(resp.status(), 404);
    let body: Value = resp.json().await.unwrap();
    assert_eq!(body, json!({ "error": "not found" }));
}

/// The FMS refuses the cancel of a job that is no longer active, once
/// dispensing has started or metering is complete, with 409, and the HMI
/// forwards it unchanged (hmi.md §7.3, fms.md §7.4).
#[tokio::test]
async fn cancel_job_forwards_fms_409_verbatim() {
    async fn not_active() -> (StatusCode, Json<Value>) {
        (
            StatusCode::CONFLICT,
            Json(json!({ "error": "job is not in active state" })),
        )
    }
    let fms =
        MockFms::start(Router::new().route("/api/v1/jobs/{job_id}", delete(not_active))).await;
    let rfid = MockRfid::start(1_000_002).await;
    let (hmi_url, client) = spawn_hmi(&fms.base_url(), rfid.addr).await;

    let resp = client
        .delete(format!("{hmi_url}/api/jobs/{MOCK_JOB_UUID}"))
        .bearer_auth("token-test")
        .send()
        .await
        .unwrap();

    assert_eq!(resp.status(), 409);
    let body: Value = resp.json().await.unwrap();
    assert_eq!(body, json!({ "error": "job is not in active state" }));
}

/// A mock FMS that records the method and target of every request it receives,
/// on any path, and answers each with 204.
fn recording_fms(seen: Arc<Mutex<Vec<String>>>) -> Router {
    Router::new().fallback(move |method: axum::http::Method, uri: axum::http::Uri| {
        let seen = seen.clone();
        async move {
            seen.lock().unwrap().push(format!("{method} {uri}"));
            StatusCode::NO_CONTENT
        }
    })
}

/// A job identifier that is not a UUID is refused with 400 and the HMI's own
/// error token, and no request reaches the FMS (hmi.md §7). Decoded into the
/// FMS path, the first two would reach the FMS's cards and logout routes with
/// the caller's token, and the third, which begins with a valid UUID, would
/// leave the jobs path after it. The same mock then receives a cancel for a
/// valid UUID written in upper case, as the UUID's canonical form: the
/// forwarded path is built from the parsed UUID.
#[tokio::test]
async fn cancel_refuses_a_job_id_that_is_not_a_uuid_before_reaching_the_fms() {
    let seen = Arc::new(Mutex::new(Vec::new()));
    let fms = MockFms::start(recording_fms(seen.clone())).await;
    let rfid = MockRfid::start(1_000_002).await;
    let (hmi_url, client) = spawn_hmi(&fms.base_url(), rfid.addr).await;

    for job_id in [
        "..%2Fcards%2F00000000-0000-0000-0000-0000000000aa",
        "..%2Fauth%2Flogout",
        "00000000-0000-0000-0000-000000000001%2F..%2F..%2Fcards",
        "job-test-1",
    ] {
        let resp = client
            .delete(format!("{hmi_url}/api/jobs/{job_id}"))
            .bearer_auth("token-test")
            .send()
            .await
            .unwrap();
        assert_eq!(resp.status(), 400, "{job_id}");
        assert_eq!(
            resp.headers()[reqwest::header::CONTENT_TYPE],
            "application/json",
            "{job_id}"
        );
        let body: Value = resp.json().await.unwrap();
        assert_eq!(body, json!({ "error": "invalid_job_id" }), "{job_id}");
    }
    let forwarded = seen.lock().unwrap().clone();
    assert!(
        forwarded.is_empty(),
        "requests reached the FMS: {forwarded:?}"
    );

    let resp = client
        .delete(format!(
            "{hmi_url}/api/jobs/00000000-0000-0000-0000-0000000000AB"
        ))
        .bearer_auth("token-test")
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), 204);
    assert_eq!(
        *seen.lock().unwrap(),
        vec!["DELETE /api/v1/jobs/00000000-0000-0000-0000-0000000000ab".to_owned()]
    );
}

#[tokio::test]
async fn create_tank_fill_returns_job_id_with_open_status() {
    let fms = MockFms::start(mock_fms_router()).await;
    let rfid = MockRfid::start(1_000_002).await;
    let (hmi_url, client) = spawn_hmi(&fms.base_url(), rfid.addr).await;

    let resp = client
        .post(format!("{hmi_url}/api/jobs/tank-fill"))
        .bearer_auth("token-test")
        .json(&json!({
            "tanker_registration": "RP-TANKER-01",
            "product": "MGO",
            "delivery_volume_litres": 15_000.0
        }))
        .send()
        .await
        .unwrap();

    assert_eq!(resp.status(), 201);
    let body: Value = resp.json().await.unwrap();
    assert!(body["job_id"].as_str().is_some());
    assert_eq!(body["status"], "open");
}

#[tokio::test]
async fn current_status_translates_active_unvalidated_to_pending_vessel_rfid() {
    let fms = MockFms::start(mock_fms_router()).await;
    let rfid = MockRfid::start(1_000_002).await;
    let (hmi_url, client) = spawn_hmi(&fms.base_url(), rfid.addr).await;

    let resp = client
        .get(format!("{hmi_url}/api/jobs/current"))
        .bearer_auth("token-test")
        .send()
        .await
        .unwrap();

    assert_eq!(resp.status(), 200);
    let body: Value = resp.json().await.unwrap();
    assert_eq!(body["status"], "pending_vessel_rfid");
    assert_eq!(body["type"], "refuel");
}

#[tokio::test]
async fn start_dispense_returns_dispensing_status() {
    let fms = MockFms::start(mock_fms_router()).await;
    let rfid = MockRfid::start(1_000_002).await;
    let (hmi_url, client) = spawn_hmi(&fms.base_url(), rfid.addr).await;

    let resp = client
        .post(format!("{hmi_url}/api/jobs/job-test-1/dispenser/start"))
        .bearer_auth("token-test")
        .json(&json!({ "preset_volume_litres": 500.0 }))
        .send()
        .await
        .unwrap();

    assert_eq!(resp.status(), 200);
    let body: Value = resp.json().await.unwrap();
    assert_eq!(body["status"], "dispensing");
}

#[tokio::test]
async fn delivery_ticket_returns_201_with_ticket_id() {
    let fms = MockFms::start(mock_fms_router()).await;
    let rfid = MockRfid::start(1_000_002).await;
    let (hmi_url, client) = spawn_hmi(&fms.base_url(), rfid.addr).await;

    let resp = client
        .post(format!("{hmi_url}/api/delivery-tickets"))
        .bearer_auth("token-test")
        .json(&ticket_body())
        .send()
        .await
        .unwrap();

    assert_eq!(resp.status(), 201);
    let body: Value = resp.json().await.unwrap();
    assert!(body["ticket_id"].as_str().is_some());
    assert_eq!(body["job_id"], "job-test-1");
    assert!(body["recorded_at"].as_str().is_some());
}

/// The road tanker's start time, as printed on its ticket: just before
/// midnight at a month end, the case the transcribed start exists for.
const TICKET_COMMENCED_AT: &str = "2026-01-31T23:58:30Z";

/// A complete delivery ticket as the operator transcribes it (hmi.md §7.4).
fn ticket_body() -> Value {
    json!({
        "job_id": "job-test-1",
        "ticket_number": "TICKET-001",
        "delivery_commenced_at": TICKET_COMMENCED_AT,
        "delivered_volume_litres": 15_000.0,
        "temperature_c": 12.0,
        "density_kg_per_l": 0.845,
        "temp_corrected_volume_litres": 15_045.0,
        "supplier": "Ravensport Fuels",
        "tanker_registration": "RP-TANKER-01"
    })
}

/// A mock FMS answering only the fill close, recording the body it receives so
/// a test can assert what the HMI forwarded.
fn capturing_close_fms(captured: Arc<Mutex<Option<Value>>>) -> Router {
    Router::new().route(
        "/api/v1/jobs/{job_id}/close",
        post(move |Json(body): Json<Value>| {
            let captured = captured.clone();
            async move {
                *captured.lock().unwrap() = Some(body);
                (
                    StatusCode::CREATED,
                    Json(json!({ "transaction_id": MOCK_TX_UUID })),
                )
            }
        }),
    )
}

/// The HMI forwards every transcribed field to the FMS fill close (fms.md §7),
/// renaming the two volumes to the FMS's `_l` form — exactly that set, so a
/// dropped field and an unspecified one both fail.
#[tokio::test]
async fn delivery_ticket_forwards_the_tanker_figures_and_start_time_to_the_fms() {
    let captured = Arc::new(Mutex::new(None));
    let fms = MockFms::start(capturing_close_fms(captured.clone())).await;
    let rfid = MockRfid::start(1_000_002).await;
    let (hmi_url, client) = spawn_hmi(&fms.base_url(), rfid.addr).await;

    let resp = client
        .post(format!("{hmi_url}/api/delivery-tickets"))
        .bearer_auth("token-test")
        .json(&ticket_body())
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), 201);

    let sent = captured
        .lock()
        .unwrap()
        .clone()
        .expect("the FMS received a close");
    let mut keys: Vec<&str> = sent
        .as_object()
        .unwrap()
        .keys()
        .map(String::as_str)
        .collect();
    keys.sort_unstable();
    let mut expected = vec![
        "ticket_number",
        "delivery_commenced_at",
        "delivered_volume_l",
        "temperature_c",
        "density_kg_per_l",
        "temp_corrected_volume_l",
        "supplier",
        "tanker_registration",
    ];
    expected.sort_unstable();
    assert_eq!(keys, expected);

    assert_eq!(sent["delivered_volume_l"], json!(15_000.0));
    assert_eq!(sent["temperature_c"], json!(12.0));
    assert_eq!(sent["density_kg_per_l"], json!(0.845));
    assert_eq!(sent["temp_corrected_volume_l"], json!(15_045.0));
    // Compared as instants: the HMI may re-serialise the timestamp (`Z` or
    // `+00:00`), and either is the same start time.
    let forwarded = chrono::DateTime::parse_from_rfc3339(
        sent["delivery_commenced_at"]
            .as_str()
            .expect("a timestamp string"),
    )
    .unwrap();
    let printed = chrono::DateTime::parse_from_rfc3339(TICKET_COMMENCED_AT).unwrap();
    assert_eq!(forwarded, printed);
}

/// Without the start time the FMS cannot assign the transfer to an audit
/// window, so the HMI refuses the ticket and forwards nothing.
#[tokio::test]
async fn delivery_ticket_without_a_start_time_is_refused_before_forwarding() {
    let captured = Arc::new(Mutex::new(None));
    let fms = MockFms::start(capturing_close_fms(captured.clone())).await;
    let rfid = MockRfid::start(1_000_002).await;
    let (hmi_url, client) = spawn_hmi(&fms.base_url(), rfid.addr).await;

    let mut body = ticket_body();
    body.as_object_mut()
        .unwrap()
        .remove("delivery_commenced_at");
    let resp = client
        .post(format!("{hmi_url}/api/delivery-tickets"))
        .bearer_auth("token-test")
        .json(&body)
        .send()
        .await
        .unwrap();

    assert_eq!(resp.status(), 422);
    assert!(
        captured.lock().unwrap().is_none(),
        "nothing may be forwarded to the FMS"
    );
}

#[tokio::test]
async fn patch_bdn_links_bdn_number() {
    let fms = MockFms::start(mock_fms_router()).await;
    let rfid = MockRfid::start(1_000_002).await;
    let (hmi_url, client) = spawn_hmi(&fms.base_url(), rfid.addr).await;

    let resp = client
        .patch(format!("{hmi_url}/api/jobs/job-test-1"))
        .bearer_auth("token-test")
        .json(&json!({ "bdn_number": "BDN-001" }))
        .send()
        .await
        .unwrap();

    assert_eq!(resp.status(), 200);
    let body: Value = resp.json().await.unwrap();
    assert_eq!(body["job_id"], "job-test-1");
    assert_eq!(body["bdn_number"], "BDN-001");
}

#[tokio::test]
async fn current_status_translates_running_to_dispensing() {
    let fms = MockFms::start(mock_fms_with_current(json!({
        "job_id": MOCK_JOB_UUID, "type": "dispense", "status": "running", "rfid_validated": true,
    })))
    .await;
    let rfid = MockRfid::start(1_000_002).await;
    let (hmi_url, client) = spawn_hmi(&fms.base_url(), rfid.addr).await;

    let resp = client
        .get(format!("{hmi_url}/api/jobs/current"))
        .bearer_auth("token-test")
        .send()
        .await
        .unwrap();

    assert_eq!(resp.status(), 200);
    let body: Value = resp.json().await.unwrap();
    assert_eq!(body["status"], "dispensing");
}

#[tokio::test]
async fn current_status_translates_active_validated_to_rfid_validated() {
    let fms = MockFms::start(mock_fms_with_current(json!({
        "job_id": MOCK_JOB_UUID, "type": "dispense", "status": "active", "rfid_validated": true,
    })))
    .await;
    let rfid = MockRfid::start(1_000_002).await;
    let (hmi_url, client) = spawn_hmi(&fms.base_url(), rfid.addr).await;

    let resp = client
        .get(format!("{hmi_url}/api/jobs/current"))
        .bearer_auth("token-test")
        .send()
        .await
        .unwrap();

    assert_eq!(resp.status(), 200);
    let body: Value = resp.json().await.unwrap();
    assert_eq!(body["status"], "rfid_validated");
}

#[tokio::test]
async fn current_status_translates_closed_to_complete() {
    let fms = MockFms::start(mock_fms_with_current(json!({
        "job_id": MOCK_JOB_UUID, "type": "dispense", "status": "closed", "rfid_validated": true,
    })))
    .await;
    let rfid = MockRfid::start(1_000_002).await;
    let (hmi_url, client) = spawn_hmi(&fms.base_url(), rfid.addr).await;

    let resp = client
        .get(format!("{hmi_url}/api/jobs/current"))
        .bearer_auth("token-test")
        .send()
        .await
        .unwrap();

    assert_eq!(resp.status(), 200);
    let body: Value = resp.json().await.unwrap();
    assert_eq!(body["status"], "complete");
}

#[tokio::test]
async fn current_status_translates_awaiting_bdn() {
    let fms = MockFms::start(mock_fms_with_current(json!({
        "job_id": MOCK_JOB_UUID, "type": "dispense", "status": "awaiting_bdn", "rfid_validated": true,
    })))
    .await;
    let rfid = MockRfid::start(1_000_002).await;
    let (hmi_url, client) = spawn_hmi(&fms.base_url(), rfid.addr).await;

    let resp = client
        .get(format!("{hmi_url}/api/jobs/current"))
        .bearer_auth("token-test")
        .send()
        .await
        .unwrap();

    assert_eq!(resp.status(), 200);
    let body: Value = resp.json().await.unwrap();
    assert_eq!(body["status"], "awaiting_bdn");
}

/// The FMS's dispense start reaches the dock operator unchanged, at its full
/// precision: the BDN carries it as the time of commencement (hmi.md,
/// narrative.md §2.2). Reformatting it would move the BDN off the record.
#[tokio::test]
async fn current_status_passes_the_dispense_start_through_unchanged() {
    let opened_at = "2026-10-16T09:27:13.631777577Z";
    let fms = MockFms::start(mock_fms_with_current(json!({
        "job_id": MOCK_JOB_UUID, "type": "dispense", "status": "awaiting_bdn",
        "rfid_validated": true, "opened_at": opened_at,
    })))
    .await;
    let rfid = MockRfid::start(1_000_002).await;
    let (hmi_url, client) = spawn_hmi(&fms.base_url(), rfid.addr).await;

    let resp = client
        .get(format!("{hmi_url}/api/jobs/current"))
        .bearer_auth("token-test")
        .send()
        .await
        .unwrap();

    assert_eq!(resp.status(), 200);
    let body: Value = resp.json().await.unwrap();
    assert_eq!(body["opened_at"], opened_at);
}

/// Before the dispense starts the FMS reports `opened_at` as `null`, and the
/// HMI reports the same rather than omitting the field.
#[tokio::test]
async fn current_status_reports_a_null_dispense_start_before_one() {
    let fms = MockFms::start(mock_fms_with_current(json!({
        "job_id": MOCK_JOB_UUID, "type": "dispense", "status": "active",
        "rfid_validated": true, "opened_at": null,
    })))
    .await;
    let rfid = MockRfid::start(1_000_002).await;
    let (hmi_url, client) = spawn_hmi(&fms.base_url(), rfid.addr).await;

    let body: Value = client
        .get(format!("{hmi_url}/api/jobs/current"))
        .bearer_auth("token-test")
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(body.get("opened_at"), Some(&Value::Null), "{body}");
}

#[tokio::test]
async fn current_status_translates_fill_type_to_tank_fill() {
    let fms = MockFms::start(mock_fms_with_current(json!({
        "job_id": MOCK_JOB_UUID, "type": "fill", "status": "active", "rfid_validated": false,
    })))
    .await;
    let rfid = MockRfid::start(1_000_002).await;
    let (hmi_url, client) = spawn_hmi(&fms.base_url(), rfid.addr).await;

    let resp = client
        .get(format!("{hmi_url}/api/jobs/current"))
        .bearer_auth("token-test")
        .send()
        .await
        .unwrap();

    assert_eq!(resp.status(), 200);
    let body: Value = resp.json().await.unwrap();
    assert_eq!(body["type"], "tank-fill");
}

#[tokio::test]
async fn get_tank_level_returns_level_fields() {
    let fms = MockFms::start(mock_fms_router()).await;
    let rfid = MockRfid::start(1_000_002).await;
    let (hmi_url, client) = spawn_hmi(&fms.base_url(), rfid.addr).await;

    let resp = client
        .get(format!("{hmi_url}/api/tanks/01/level"))
        .bearer_auth("token-test")
        .send()
        .await
        .unwrap();

    assert_eq!(resp.status(), 200);
    let body: Value = resp.json().await.unwrap();
    assert_eq!(body["tank_id"], "01");
    assert!(body["level_mm"].as_f64().is_some());
    assert!(body["volume_litres"].as_f64().is_some());
    assert!(body["ullage_litres"].as_f64().is_some());
    assert!(body["temperature_c"].as_f64().is_some());
    assert!(body["timestamp"].as_str().is_some());
}

#[tokio::test]
async fn get_totaliser_returns_dispensed_volume_and_status() {
    let fms = MockFms::start(mock_fms_router()).await;
    let rfid = MockRfid::start(1_000_002).await;
    let (hmi_url, client) = spawn_hmi(&fms.base_url(), rfid.addr).await;

    let resp = client
        .get(format!("{hmi_url}/api/jobs/current/dispenser/totaliser"))
        .bearer_auth("token-test")
        .send()
        .await
        .unwrap();

    assert_eq!(resp.status(), 200);
    let body: Value = resp.json().await.unwrap();
    assert!(body["dispensed_litres"].as_f64().is_some());
    assert!(body["preset_litres"].as_f64().is_some());
    assert_eq!(body["status"], "dispensing");
}
