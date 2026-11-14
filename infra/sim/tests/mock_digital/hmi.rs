//! Mock HMI (`hmi-01`) for the integration test.
//!
//! A thin HTTP presentation proxy (digital.md §2.3: the HMI carries no business
//! logic). It serves the HMI API over real HTTP to the dock operator,
//! translating each request into an [`FmsCommand`] to the FMS and the reply
//! back into an HTTP response. During login it reads the operator RFID card off
//! its serial port. Every request is recorded for the integration test.

use std::net::SocketAddr;
use std::sync::{Arc, Mutex};

use serde_json::{json, Value};
use tokio::io::{AsyncBufReadExt, AsyncReadExt, AsyncWriteExt, BufReader};
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::{mpsc, oneshot};

use sim::serial::LoopbackHandle;

use super::FmsCommand;

/// One captured HTTP request/response (the integration test's observation
/// surface). `response` is the JSON body the HMI returned — e.g. the
/// ATG-reported `volume_litres` for a `GET .../level`.
#[derive(Clone)]
pub struct Recorded {
    pub method: String,
    pub path: String,
    pub body: String,
    pub response: String,
}

pub struct MockHmi {
    requests: Arc<Mutex<Vec<Recorded>>>,
    pub addr: SocketAddr,
}

impl MockHmi {
    /// Bind synchronously (so the dock operator can connect immediately) and
    /// spawn the server. The HMI reads the operator RFID card from
    /// `operator_rfid` and forwards everything to the FMS via `fms_tx`.
    pub fn start(operator_rfid: LoopbackHandle, fms_tx: mpsc::Sender<FmsCommand>) -> MockHmi {
        let std_listener = std::net::TcpListener::bind("127.0.0.1:0").expect("mock HMI bind");
        std_listener
            .set_nonblocking(true)
            .expect("mock HMI set_nonblocking");
        let addr = std_listener.local_addr().unwrap();
        let listener = TcpListener::from_std(std_listener).expect("mock HMI adopt listener");

        let requests = Arc::new(Mutex::new(Vec::new()));
        let server_requests = Arc::clone(&requests);
        let operator_rfid = Arc::new(operator_rfid);
        tokio::spawn(async move {
            while let Ok((stream, _)) = listener.accept().await {
                tokio::spawn(handle(
                    stream,
                    Arc::clone(&server_requests),
                    fms_tx.clone(),
                    Arc::clone(&operator_rfid),
                ));
            }
        });
        MockHmi { requests, addr }
    }

    pub fn requests(&self) -> Vec<Recorded> {
        self.requests.lock().unwrap().clone()
    }
}

/// Send one command to the FMS and await its reply.
async fn fms_reply<T>(
    fms_tx: &mpsc::Sender<FmsCommand>,
    command: impl FnOnce(oneshot::Sender<T>) -> FmsCommand,
) -> T {
    let (reply, rx) = oneshot::channel();
    fms_tx
        .send(command(reply))
        .await
        .expect("mock HMI: FMS channel closed");
    rx.await.expect("mock HMI: FMS dropped the reply")
}

async fn handle(
    stream: TcpStream,
    requests: Arc<Mutex<Vec<Recorded>>>,
    fms_tx: mpsc::Sender<FmsCommand>,
    operator_rfid: Arc<LoopbackHandle>,
) {
    let (read_half, mut write_half) = stream.into_split();
    let mut reader = BufReader::new(read_half);
    loop {
        let mut request_line = String::new();
        match reader.read_line(&mut request_line).await {
            Ok(0) | Err(_) => return,
            Ok(_) => {}
        }
        if request_line.trim().is_empty() {
            return;
        }
        let mut parts = request_line.split_whitespace();
        let method = parts.next().unwrap_or("").to_string();
        let path = parts.next().unwrap_or("").to_string();

        let mut content_length = 0usize;
        loop {
            let mut header = String::new();
            match reader.read_line(&mut header).await {
                Ok(0) | Err(_) => return,
                Ok(_) => {}
            }
            let header = header.trim_end();
            if header.is_empty() {
                break;
            }
            if let Some((name, value)) = header.split_once(':') {
                if name.trim().eq_ignore_ascii_case("content-length") {
                    content_length = value.trim().parse().unwrap_or(0);
                }
            }
        }
        let mut body_buf = vec![0u8; content_length];
        if content_length > 0 && reader.read_exact(&mut body_buf).await.is_err() {
            return;
        }
        let body = String::from_utf8_lossy(&body_buf).to_string();

        let (status, payload) = route(&method, &path, &body, &fms_tx, &operator_rfid).await;
        let payload = payload.to_string();

        // Record the request together with the response body, so the test can
        // read the ATG-reported level the HMI returned.
        requests.lock().unwrap().push(Recorded {
            method,
            path,
            body,
            response: payload.clone(),
        });

        let response = format!(
            "HTTP/1.1 {} {}\r\ncontent-type: application/json\r\ncontent-length: {}\r\n\r\n{}",
            status,
            reason(status),
            payload.len(),
            payload
        );
        if write_half.write_all(response.as_bytes()).await.is_err() {
            return;
        }
    }
}

async fn route(
    method: &str,
    path: &str,
    body: &str,
    fms_tx: &mpsc::Sender<FmsCommand>,
    operator_rfid: &LoopbackHandle,
) -> (u16, Value) {
    let body_json: Value = serde_json::from_str(body).unwrap_or(Value::Null);
    let segments: Vec<&str> = path.trim_matches('/').split('/').collect();

    match (method, segments.as_slice()) {
        ("POST", ["api", "auth", "login"]) => {
            let username = body_json["username"]
                .as_str()
                .unwrap_or_default()
                .to_string();
            let password = body_json["password"]
                .as_str()
                .unwrap_or_default()
                .to_string();
            let challenge_id = fms_reply(fms_tx, |reply| FmsCommand::Login {
                username,
                password,
                reply,
            })
            .await;
            (202, json!({ "challenge_id": challenge_id }))
        }
        ("GET", ["api", "auth", "challenge", _id]) => {
            let card = read_operator_card(operator_rfid).await;
            let token = fms_reply(fms_tx, |reply| FmsCommand::ValidateCard { card, reply }).await;
            (
                200,
                json!({ "token": token, "expires_at": "2025-01-01T00:30:00Z" }),
            )
        }
        ("GET", ["api", "tanks", _tank, "level"]) => {
            let level = fms_reply(fms_tx, |reply| FmsCommand::GetTankLevel { reply }).await;
            (
                200,
                json!({
                    "tank_id": "01",
                    "level_mm": 0.0,
                    "volume_litres": level,
                    "ullage_litres": (50_000.0 - level).max(0.0),
                    "temperature_c": 15.0,
                    "timestamp": "2025-01-01T00:00:00Z"
                }),
            )
        }
        ("POST", ["api", "jobs", "refuel"]) => {
            let vessel_id = body_json["vessel_id"]
                .as_str()
                .unwrap_or_default()
                .to_string();
            let volume = body_json["requested_volume_litres"].as_f64().unwrap_or(0.0);
            let job_id = fms_reply(fms_tx, |reply| FmsCommand::CreateRefuelJob {
                vessel_id,
                requested_volume_litres: volume,
                reply,
            })
            .await;
            (
                201,
                json!({ "job_id": job_id, "status": "pending_vessel_rfid" }),
            )
        }
        ("POST", ["api", "jobs", "tank-fill"]) => {
            let registration = body_json["tanker_registration"]
                .as_str()
                .unwrap_or_default()
                .to_string();
            let volume = body_json["delivery_volume_litres"].as_f64().unwrap_or(0.0);
            let job_id = fms_reply(fms_tx, |reply| FmsCommand::CreateFillJob {
                tanker_registration: registration,
                delivery_volume_litres: volume,
                reply,
            })
            .await;
            (201, json!({ "job_id": job_id, "status": "open" }))
        }
        ("POST", ["api", "jobs", id, "dispenser", "start"]) => {
            let job_id = (*id).to_string();
            let preset = body_json["preset_volume_litres"].as_f64().unwrap_or(0.0);
            fms_reply(fms_tx, |reply| FmsCommand::StartDispense {
                job_id: job_id.clone(),
                preset_volume_litres: preset,
                reply,
            })
            .await;
            (200, json!({ "job_id": job_id, "status": "dispensing" }))
        }
        ("GET", ["api", "jobs", "current", "dispenser", "totaliser"]) => {
            match fms_reply(fms_tx, |reply| FmsCommand::GetTotaliser { reply }).await {
                Some(totaliser) => (
                    200,
                    json!({
                        "job_id": totaliser.job_id,
                        "dispensed_litres": totaliser.dispensed_litres,
                        "preset_litres": 0.0,
                        "status": totaliser.status
                    }),
                ),
                None => (404, json!({ "error": "no_current_job" })),
            }
        }
        ("GET", ["api", "jobs", "current"]) => {
            match fms_reply(fms_tx, |reply| FmsCommand::GetJobStatus { reply }).await {
                Some(job) => (
                    200,
                    json!({
                        "job_id": job.job_id,
                        "type": "refuel",
                        "status": job.status,
                        "opened_at": job.opened_at,
                    }),
                ),
                None => (404, json!({ "error": "no_current_job" })),
            }
        }
        ("PATCH", ["api", "jobs", id]) => {
            let job_id = (*id).to_string();
            let bdn_number = body_json["bdn_number"]
                .as_str()
                .unwrap_or_default()
                .to_string();
            fms_reply(fms_tx, |reply| FmsCommand::EnterBdn {
                job_id: job_id.clone(),
                bdn_number: bdn_number.clone(),
                reply,
            })
            .await;
            (200, json!({ "job_id": job_id, "bdn_number": bdn_number }))
        }
        ("POST", ["api", "delivery-tickets"]) => {
            let job_id = body_json["job_id"].as_str().unwrap_or_default().to_string();
            let ticket_number = body_json["ticket_number"]
                .as_str()
                .unwrap_or_default()
                .to_string();
            fms_reply(fms_tx, |reply| FmsCommand::TranscribeTicket {
                job_id: job_id.clone(),
                ticket_number,
                reply,
            })
            .await;
            (
                201,
                json!({ "ticket_id": "ticket-1", "job_id": job_id, "recorded_at": "2025-01-01T00:00:00Z" }),
            )
        }
        _ => (404, json!({ "error": "not_found" })),
    }
}

/// Wait until the operator RFID card (a decimal number + newline) is present on
/// the serial line, then return it. The reader is event-driven, so yielding
/// lets it run.
async fn read_operator_card(handle: &LoopbackHandle) -> u32 {
    for _ in 0..100_000 {
        let bytes = handle.bytes();
        if let Some(newline) = bytes.iter().position(|&b| b == b'\n') {
            let text =
                std::str::from_utf8(&bytes[..newline]).expect("mock HMI: operator card not UTF-8");
            return text
                .trim()
                .parse()
                .expect("mock HMI: operator card not a number");
        }
        tokio::task::yield_now().await;
    }
    panic!("mock HMI: operator card was never presented");
}

fn reason(status: u16) -> &'static str {
    match status {
        201 => "Created",
        202 => "Accepted",
        404 => "Not Found",
        _ => "OK",
    }
}
