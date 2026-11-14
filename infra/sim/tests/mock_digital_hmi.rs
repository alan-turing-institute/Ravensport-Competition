//! Tests for the mock HMI (`tests/mock_digital/hmi.rs`). The test drives the
//! HMI over real HTTP (`reqwest`, as the dock operator does) and plays the FMS
//! (holding the command receiver), asserting that each endpoint translates to
//! the right `FmsCommand` and the right HTTP response.

mod mock_digital;

use serde_json::{json, Value};
use tokio::sync::mpsc;

use sim::serial::SerialConnection;

use mock_digital::hmi::MockHmi;
use mock_digital::{FmsCommand, JobStatus, Totaliser};

struct Harness {
    hmi: MockHmi,
    fms_rx: mpsc::Receiver<FmsCommand>,
    operator_serial: SerialConnection,
    client: reqwest::Client,
}

fn spawn_hmi() -> Harness {
    let (operator_serial, operator_handle) = SerialConnection::loopback();
    let (fms_tx, fms_rx) = mpsc::channel(16);
    let hmi = MockHmi::start(operator_handle, fms_tx);
    Harness {
        hmi,
        fms_rx,
        operator_serial,
        client: reqwest::Client::new(),
    }
}

fn url(harness: &Harness, path: &str) -> String {
    format!("http://{}{}", harness.hmi.addr, path)
}

/// Issue `request` while concurrently servicing the one `FmsCommand` the HMI
/// forwards (via `handle`, which asserts and replies). Returns the response.
async fn exchange<F>(
    fms_rx: &mut mpsc::Receiver<FmsCommand>,
    request: reqwest::RequestBuilder,
    handle: F,
) -> reqwest::Response
where
    F: FnOnce(FmsCommand),
{
    let pending = tokio::spawn(request.send());
    let command = fms_rx.recv().await.expect("HMI forwarded no command");
    handle(command);
    pending
        .await
        .expect("request task panicked")
        .expect("request failed")
}

async fn json_body(response: reqwest::Response) -> Value {
    response.json().await.expect("response body is JSON")
}

#[tokio::test]
async fn login_forwards_credentials_and_returns_challenge() {
    let mut h = spawn_hmi();
    let request = h
        .client
        .post(url(&h, "/api/auth/login"))
        .json(&json!({ "username": "dock-operator", "password": "operator-password" }));

    let response = exchange(&mut h.fms_rx, request, |command| match command {
        FmsCommand::Login {
            username,
            password,
            reply,
        } => {
            assert_eq!(username, "dock-operator");
            assert_eq!(password, "operator-password");
            reply.send("chal-9".to_string()).unwrap();
        }
        _ => panic!("expected Login"),
    })
    .await;

    assert_eq!(response.status(), 202);
    assert_eq!(json_body(response).await["challenge_id"], "chal-9");
}

#[tokio::test]
async fn challenge_reads_operator_card_and_returns_token() {
    let mut h = spawn_hmi();
    // The operator presents their card before the challenge resolves.
    h.operator_serial.write_all(b"1000002\n").await.unwrap();

    let request = h.client.get(url(&h, "/api/auth/challenge/chal-9"));
    let response = exchange(&mut h.fms_rx, request, |command| match command {
        FmsCommand::ValidateCard { card, reply } => {
            assert_eq!(card, 1_000_002);
            reply.send("token-9".to_string()).unwrap();
        }
        _ => panic!("expected ValidateCard"),
    })
    .await;

    assert_eq!(response.status(), 200);
    assert_eq!(json_body(response).await["token"], "token-9");
}

#[tokio::test]
async fn refuel_forwards_job_fields_and_returns_job_id() {
    let mut h = spawn_hmi();
    let request = h.client.post(url(&h, "/api/jobs/refuel")).json(&json!({
        "vessel_id": "1999997", "product": "MGO", "requested_volume_litres": 500.0
    }));

    let response = exchange(&mut h.fms_rx, request, |command| match command {
        FmsCommand::CreateRefuelJob {
            vessel_id,
            requested_volume_litres,
            reply,
        } => {
            assert_eq!(vessel_id, "1999997");
            assert_eq!(requested_volume_litres, 500.0);
            reply.send("job-7".to_string()).unwrap();
        }
        _ => panic!("expected CreateRefuelJob"),
    })
    .await;

    assert_eq!(response.status(), 201);
    let body = json_body(response).await;
    assert_eq!(body["job_id"], "job-7");
    assert_eq!(body["status"], "pending_vessel_rfid");
}

#[tokio::test]
async fn job_status_resolves_current_job() {
    let mut h = spawn_hmi();
    let request = h.client.get(url(&h, "/api/jobs/current"));

    let response = exchange(&mut h.fms_rx, request, |command| match command {
        FmsCommand::GetJobStatus { reply } => {
            reply
                .send(Some(JobStatus {
                    job_id: "job-7".to_string(),
                    status: "rfid_validated".to_string(),
                    opened_at: None,
                }))
                .unwrap();
        }
        _ => panic!("expected GetJobStatus"),
    })
    .await;

    let body = json_body(response).await;
    assert_eq!(body["job_id"], "job-7");
    assert_eq!(body["status"], "rfid_validated");
}

#[tokio::test]
async fn dispenser_start_forwards_preset() {
    let mut h = spawn_hmi();
    let request = h
        .client
        .post(url(&h, "/api/jobs/job-7/dispenser/start"))
        .json(&json!({ "preset_volume_litres": 500.0 }));

    let response = exchange(&mut h.fms_rx, request, |command| match command {
        FmsCommand::StartDispense {
            job_id,
            preset_volume_litres,
            reply,
        } => {
            assert_eq!(job_id, "job-7");
            assert_eq!(preset_volume_litres, 500.0);
            reply.send(()).unwrap();
        }
        _ => panic!("expected StartDispense"),
    })
    .await;

    assert_eq!(json_body(response).await["status"], "dispensing");
}

#[tokio::test]
async fn totaliser_returns_dispensed_volume_and_status() {
    let mut h = spawn_hmi();
    let request = h
        .client
        .get(url(&h, "/api/jobs/current/dispenser/totaliser"));

    let response = exchange(&mut h.fms_rx, request, |command| match command {
        FmsCommand::GetTotaliser { reply } => {
            reply
                .send(Some(Totaliser {
                    job_id: "job-7".to_string(),
                    dispensed_litres: 500.0,
                    status: "complete".to_string(),
                }))
                .unwrap();
        }
        _ => panic!("expected GetTotaliser"),
    })
    .await;

    let body = json_body(response).await;
    assert_eq!(body["job_id"], "job-7");
    assert_eq!(body["dispensed_litres"], 500.0);
    assert_eq!(body["status"], "complete");
}

#[tokio::test]
async fn bdn_patch_forwards_number() {
    let mut h = spawn_hmi();
    let request = h
        .client
        .patch(url(&h, "/api/jobs/job-7"))
        .json(&json!({ "bdn_number": "BDN-000001" }));

    let response = exchange(&mut h.fms_rx, request, |command| match command {
        FmsCommand::EnterBdn {
            job_id,
            bdn_number,
            reply,
        } => {
            assert_eq!(job_id, "job-7");
            assert_eq!(bdn_number, "BDN-000001");
            reply.send(()).unwrap();
        }
        _ => panic!("expected EnterBdn"),
    })
    .await;

    assert_eq!(json_body(response).await["bdn_number"], "BDN-000001");
}

#[tokio::test]
async fn tank_level_returns_volume() {
    let mut h = spawn_hmi();
    let request = h.client.get(url(&h, "/api/tanks/1/level"));

    let response = exchange(&mut h.fms_rx, request, |command| match command {
        FmsCommand::GetTankLevel { reply } => {
            reply.send(35_000.0).unwrap();
        }
        _ => panic!("expected GetTankLevel"),
    })
    .await;

    assert_eq!(json_body(response).await["volume_litres"], 35_000.0);
}

#[tokio::test]
async fn tank_fill_forwards_delivery_fields() {
    let mut h = spawn_hmi();
    let request = h.client.post(url(&h, "/api/jobs/tank-fill")).json(&json!({
        "tanker_registration": "AB12 CDE", "product": "MGO", "delivery_volume_litres": 15_000.0
    }));

    let response = exchange(&mut h.fms_rx, request, |command| match command {
        FmsCommand::CreateFillJob {
            tanker_registration,
            delivery_volume_litres,
            reply,
        } => {
            assert_eq!(tanker_registration, "AB12 CDE");
            assert_eq!(delivery_volume_litres, 15_000.0);
            reply.send("job-8".to_string()).unwrap();
        }
        _ => panic!("expected CreateFillJob"),
    })
    .await;

    assert_eq!(response.status(), 201);
    assert_eq!(json_body(response).await["status"], "open");
}

#[tokio::test]
async fn delivery_ticket_forwards_ticket() {
    let mut h = spawn_hmi();
    let request = h
        .client
        .post(url(&h, "/api/delivery-tickets"))
        .json(&json!({
            "job_id": "job-8", "ticket_number": "TICKET-77",
            "delivered_volume_litres": 15_000.0, "supplier": "Ravensport Fuels",
            "tanker_registration": "AB12 CDE"
        }));

    let response = exchange(&mut h.fms_rx, request, |command| match command {
        FmsCommand::TranscribeTicket {
            job_id,
            ticket_number,
            reply,
        } => {
            assert_eq!(job_id, "job-8");
            assert_eq!(ticket_number, "TICKET-77");
            reply.send(()).unwrap();
        }
        _ => panic!("expected TranscribeTicket"),
    })
    .await;

    assert_eq!(response.status(), 201);
}
