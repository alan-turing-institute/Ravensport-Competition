//! A minimal mock HMI HTTP/1.1 server for the dock operator integration tests.
//!
//! It records every inbound request and replies with either a per-route queued
//! response (scripted by the test) or a built-in default "minimum valid success"
//! response. A route may also be *gated* — held open until released — to model
//! the login long-poll. Connections are persistent (the client keeps them
//! alive), so each connection handler loops over successive requests.
//!
//! Lives under `tests/mock_hmi/` (a subdirectory module) so Cargo does not
//! compile it as its own test binary.

use std::collections::{HashMap, VecDeque};
use std::net::SocketAddr;
use std::sync::{Arc, Mutex};

use tokio::io::{AsyncBufReadExt, AsyncReadExt, AsyncWriteExt, BufReader};
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::Notify;

/// One captured request.
#[derive(Clone)]
pub struct Recorded {
    pub method: String,
    pub path: String,
    pub authorization: Option<String>,
    pub body: String,
}

struct Canned {
    status: u16,
    body: String,
}

#[derive(Default)]
struct State {
    queued: HashMap<String, VecDeque<Canned>>,
    gates: HashMap<String, Arc<Notify>>,
    requests: Vec<Recorded>,
}

pub struct MockHmi {
    state: Arc<Mutex<State>>,
    pub addr: SocketAddr,
}

impl MockHmi {
    /// Bind synchronously (so the address is ready before the operator connects)
    /// and spawn the accept loop.
    pub fn start() -> MockHmi {
        let std_listener = std::net::TcpListener::bind("127.0.0.1:0").expect("mock HMI bind");
        std_listener
            .set_nonblocking(true)
            .expect("mock HMI set_nonblocking");
        let addr = std_listener.local_addr().unwrap();
        let listener = TcpListener::from_std(std_listener).expect("mock HMI adopt listener");

        let state = Arc::new(Mutex::new(State::default()));
        let server_state = Arc::clone(&state);
        tokio::spawn(async move {
            while let Ok((stream, _)) = listener.accept().await {
                tokio::spawn(handle(stream, Arc::clone(&server_state)));
            }
        });
        MockHmi { state, addr }
    }

    /// Queue a scripted response for the next request to `method`+`path`.
    pub fn enqueue(&self, method: &str, path: &str, status: u16, body: &str) {
        self.state
            .lock()
            .unwrap()
            .queued
            .entry(key(method, path))
            .or_default()
            .push_back(Canned {
                status,
                body: body.to_string(),
            });
    }

    /// Hold the next response to `method`+`path` open until the returned
    /// `Notify` is fired (the test may simply drop it to hold forever).
    pub fn gate(&self, method: &str, path: &str) -> Arc<Notify> {
        let notify = Arc::new(Notify::new());
        self.state
            .lock()
            .unwrap()
            .gates
            .insert(key(method, path), Arc::clone(&notify));
        notify
    }

    /// Snapshot of the requests captured so far, in arrival order.
    pub fn requests(&self) -> Vec<Recorded> {
        self.state.lock().unwrap().requests.clone()
    }
}

fn key(method: &str, path: &str) -> String {
    format!("{method} {path}")
}

async fn handle(stream: TcpStream, state: Arc<Mutex<State>>) {
    let (read_half, mut write_half) = stream.into_split();
    let mut reader = BufReader::new(read_half);
    loop {
        // Request line: METHOD PATH HTTP/1.1
        let mut request_line = String::new();
        match reader.read_line(&mut request_line).await {
            Ok(0) | Err(_) => return, // connection closed
            Ok(_) => {}
        }
        if request_line.trim().is_empty() {
            return;
        }
        let mut parts = request_line.split_whitespace();
        let method = parts.next().unwrap_or("").to_string();
        let path = parts.next().unwrap_or("").to_string();

        // Headers until a blank line.
        let mut content_length = 0usize;
        let mut authorization = None;
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
                match name.trim().to_ascii_lowercase().as_str() {
                    "content-length" => content_length = value.trim().parse().unwrap_or(0),
                    "authorization" => authorization = Some(value.trim().to_string()),
                    _ => {}
                }
            }
        }

        // Body, if any.
        let mut body_buf = vec![0u8; content_length];
        if content_length > 0 && reader.read_exact(&mut body_buf).await.is_err() {
            return;
        }
        let body = String::from_utf8_lossy(&body_buf).to_string();

        let route = key(&method, &path);
        state.lock().unwrap().requests.push(Recorded {
            method,
            path,
            authorization,
            body,
        });

        // Gate (long-poll): hold the response until released.
        let gate = state.lock().unwrap().gates.get(&route).cloned();
        if let Some(notify) = gate {
            notify.notified().await;
        }

        // Scripted response, else the route default.
        let canned = {
            let mut s = state.lock().unwrap();
            s.queued.get_mut(&route).and_then(VecDeque::pop_front)
        }
        .unwrap_or_else(|| default_response(&route));

        let response = format!(
            "HTTP/1.1 {} {}\r\ncontent-type: application/json\r\ncontent-length: {}\r\n\r\n{}",
            canned.status,
            reason(canned.status),
            canned.body.len(),
            canned.body
        );
        if write_half.write_all(response.as_bytes()).await.is_err() {
            return;
        }
    }
}

fn reason(status: u16) -> &'static str {
    match status {
        201 => "Created",
        204 => "No Content",
        202 => "Accepted",
        _ => "OK",
    }
}

/// The minimum valid success response for each known route. Fixed identifiers
/// (`chal-1`, `job-1`) keep the paths deterministic.
fn default_response(route: &str) -> Canned {
    let (status, body): (u16, &str) = match route {
        "POST /api/auth/login" => (202, r#"{"challenge_id":"chal-1"}"#),
        "GET /api/auth/challenge/chal-1" => (
            200,
            r#"{"token":"token-1","expires_at":"2025-01-01T00:00:00Z"}"#,
        ),
        // The FMS knows the one tank as `01` and answers 404 for any other id.
        "GET /api/tanks/01/level" => (
            200,
            r#"{"tank_id":"01","level_mm":5000.0,"volume_litres":35000.0,"ullage_litres":15000.0,"temperature_c":15.0,"timestamp":"2025-01-01T00:00:00Z"}"#,
        ),
        "POST /api/jobs/refuel" => (201, r#"{"job_id":"job-1","status":"pending_vessel_rfid"}"#),
        "POST /api/jobs/tank-fill" => (201, r#"{"job_id":"job-1","status":"open"}"#),
        // Serves both the vessel-tag gate poll and the operator's read of the
        // dispense start after completion, so it carries an `opened_at`; a test
        // asserting either state's `opened_at` scripts its own response.
        "GET /api/jobs/current" => (
            200,
            r#"{"job_id":"job-1","type":"refuel","status":"rfid_validated","opened_at":"2025-01-01T00:00:00.5Z"}"#,
        ),
        "POST /api/jobs/job-1/dispenser/start" => {
            (200, r#"{"job_id":"job-1","status":"dispensing"}"#)
        }
        "GET /api/jobs/current/dispenser/totaliser" => (
            200,
            r#"{"job_id":"job-1","dispensed_litres":500.0,"preset_litres":500.0,"status":"complete"}"#,
        ),
        "PATCH /api/jobs/job-1" => (200, r#"{"job_id":"job-1","bdn_number":"BDN-000001"}"#),
        // The cancel of a job whose start the FMS refused (hmi.md §7.3).
        "DELETE /api/jobs/job-1" => (204, ""),
        "POST /api/delivery-tickets" => (
            201,
            r#"{"ticket_id":"ticket-1","job_id":"job-1","recorded_at":"2025-01-01T00:00:00Z"}"#,
        ),
        _ => (200, "{}"),
    };
    Canned {
        status,
        body: body.to_string(),
    }
}
