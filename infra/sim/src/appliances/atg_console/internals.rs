//! Private implementation of the ATG console. Not visible to other modules.
//!
//! Wire format per tls-protocol.md: computer-format responses
//! `SOH <echo> <YYMMDDHHmm> <body> && <checksum> ETX`, checksum = two's
//! complement of the 8-bit sum of all preceding bytes (including SOH), and
//! big-endian IEEE-754 floats as 8 upper-case hex characters.

use std::net::SocketAddr;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;

use time::PrimitiveDateTime;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::{mpsc, oneshot, watch};
use tokio::task::JoinSet;

use super::AtgConsoleState;
use crate::clock::SimClock;
use crate::physical::atg_probe::ProbeReading;
use crate::physical::bund_leak_sensor::BundReading;
use crate::supervision::propagate_connection_panic;

const SOH: u8 = 0x01;
const ETX: u8 = 0x03;
/// Maximum number of concurrent console sessions (sim-impl.md, "Bounded session
/// capacity"). A real tank-gauge console serves a small, finite number of
/// simultaneous connections; further connections wait in the listener backlog
/// until a session frees. Modelling this bound makes session exhaustion a
/// meaningful availability fault: holding all slots denies the fuel-management
/// system its poll.
const MAX_ATG_SESSIONS: usize = 10;
/// Product code character for the tank (Marine Gas Oil / distillate).
const PRODUCT_CODE: char = 'D';
/// The single tank's number, two ASCII decimal digits.
const TANK_NUMBER: &str = "01";

/// Immutable console configuration: the tank geometry needed to derive the
/// i201 inventory fields.
pub(super) struct Config {
    pub k_l_per_mm: f64,
    pub tank_capacity_l: f64,
}

#[allow(clippy::too_many_arguments)]
pub(super) fn spawn(
    clock: SimClock,
    config: Config,
    start: AtgConsoleState,
    mut state_rx: mpsc::Receiver<oneshot::Sender<AtgConsoleState>>,
    probe_request_tx: mpsc::Sender<oneshot::Sender<ProbeReading>>,
    mut bund_rx: watch::Receiver<BundReading>,
    mut alarm_clear_rx: mpsc::Receiver<()>,
    listen_addr: SocketAddr,
) -> tokio::task::JoinHandle<()> {
    // Bind synchronously so callers can connect as soon as `spawn` returns.
    let std_listener =
        std::net::TcpListener::bind(listen_addr).expect("atg_console: bind TCP listener");
    std_listener
        .set_nonblocking(true)
        .expect("atg_console: set_nonblocking");
    let listener =
        TcpListener::from_std(std_listener).expect("atg_console: adopt std listener into Tokio");

    let config = Arc::new(config);
    let latched = Arc::new(AtomicBool::new(start.bund_alarm_latched));

    tokio::spawn(async move {
        // Per-connection handlers are supervised: a panic in one propagates and
        // brings the sim down (a broken client connection is handled inside the
        // handler, not by panicking).
        let mut connections: JoinSet<()> = JoinSet::new();

        loop {
            tokio::select! {
                // Only accept while below the session cap. At capacity this arm
                // is disabled, so further connections wait in the listener
                // backlog until a session frees (`join_next` below) — the bounded
                // capacity a real console presents.
                accept = listener.accept(), if connections.len() < MAX_ATG_SESSIONS => {
                    if let Ok((stream, _addr)) = accept {
                        connections.spawn(handle_connection(
                            stream,
                            clock.clone(),
                            Arc::clone(&config),
                            probe_request_tx.clone(),
                            Arc::clone(&latched),
                        ));
                    }
                    // A transient accept error is ignored — a real appliance
                    // keeps serving.
                }
                changed = bund_rx.changed() => {
                    changed.expect("atg_console: bund leak sensor channel closed");
                    if bund_rx.borrow().wet {
                        latched.store(true, Ordering::SeqCst);
                    }
                    // A dry reading does not clear the latch; only an explicit
                    // alarm-clear does.
                }
                request = state_rx.recv() => {
                    request
                        .expect("atg_console: orchestrator state channel closed")
                        .send(AtgConsoleState {
                            bund_alarm_latched: latched.load(Ordering::SeqCst),
                        })
                        .expect("atg_console: orchestrator dropped the state reply");
                }
                cleared = alarm_clear_rx.recv() => {
                    cleared.expect("atg_console: alarm-clear channel closed");
                    latched.store(false, Ordering::SeqCst);
                }
                Some(joined) = connections.join_next(), if !connections.is_empty() => {
                    propagate_connection_panic(joined);
                }
            }
        }
    })
}

/// Serve one TCP connection. Reads fixed-length function codes and writes a
/// response per request until the connection closes. Connections are
/// persistent (a real FMS polls repeatedly).
async fn handle_connection(
    mut stream: TcpStream,
    clock: SimClock,
    config: Arc<Config>,
    probe_request_tx: mpsc::Sender<oneshot::Sender<ProbeReading>>,
    latched: Arc<AtomicBool>,
) {
    loop {
        // Idle between requests: skip until the next SOH. No I/O guard — while
        // the console is merely waiting for the next poll, simulated time should
        // advance freely.
        loop {
            let mut byte = [0u8; 1];
            if stream.read_exact(&mut byte).await.is_err() {
                return; // connection closed
            }
            if byte[0] == SOH {
                break;
            }
        }

        // A request has begun. Guard the exchange (read the rest, build and
        // write the response) so simulated time does not advance while the
        // client awaits our reply. The guard is the sim *server's* — the client
        // may be a mocked digital component, which is not permitted to guard.
        let _io = clock.io_guard();
        let mut code = [0u8; 6];
        if stream.read_exact(&mut code).await.is_err() {
            return;
        }

        let response = build_response(&code, &clock, &config, &probe_request_tx, &latched).await;
        if stream.write_all(&response).await.is_err() {
            return; // client disconnected — drop the connection
        }
    }
}

/// Build the full computer-format response for a request. The ATG probe is an
/// internal instrument, so losing its channel is fatal (not client I/O).
async fn build_response(
    code: &[u8; 6],
    clock: &SimClock,
    config: &Config,
    probe_request_tx: &mpsc::Sender<oneshot::Sender<ProbeReading>>,
    latched: &AtomicBool,
) -> Vec<u8> {
    // Function number is the three characters after the format letter.
    let body = match &code[1..4] {
        b"201" => {
            let (reply_tx, reply_rx) = oneshot::channel();
            probe_request_tx
                .send(reply_tx)
                .await
                .expect("atg_console: ATG probe request channel closed");
            let reading = reply_rx
                .await
                .expect("atg_console: ATG probe dropped the reading reply");
            inventory_body(reading, config)
        }
        b"205" => status_body(),
        b"101" => alarm_body(latched.load(Ordering::SeqCst)),
        // Unrecognised function code: the console's fixed error frame, exactly
        // as a real appliance answers a malformed request.
        _ => return b"\x019999FF1B\x03".to_vec(),
    };

    frame(code, clock.now_datetime(), &body)
}

/// i201 per-tank record: `TT p ssss NN` then the seven IEEE-754 float fields.
fn inventory_body(reading: ProbeReading, config: &Config) -> String {
    let volume = reading.level_mm * config.k_l_per_mm;
    let fields = [
        volume,                          // Volume
        volume,                          // TC Volume (identity at constant 15 °C)
        config.tank_capacity_l - volume, // Ullage
        reading.level_mm,                // Height
        0.0,                             // Water (height) — no water sensor
        reading.temperature_c,           // Temperature
        0.0,                             // Water Volume
    ];

    let mut body = format!("{TANK_NUMBER}{PRODUCT_CODE}000007"); // p, ssss=0000, NN=07
    for field in fields {
        body.push_str(&float_field(field));
    }
    body
}

/// i205 per-tank status record: tank number then the active-alarm count.
/// The console models no tank alarms, so the count is always zero.
fn status_body() -> String {
    format!("{TANK_NUMBER}00")
}

/// i101 body: the bund liquid-sensor fuel alarm when latched, otherwise the
/// all-normal entry.
fn alarm_body(latched: bool) -> String {
    if latched {
        "030301".to_string()
    } else {
        "000000".to_string()
    }
}

/// One IEEE-754 single-precision float as eight upper-case big-endian hex
/// characters.
fn float_field(value: f64) -> String {
    (value as f32)
        .to_be_bytes()
        .iter()
        .map(|byte| format!("{byte:02X}"))
        .collect()
}

/// `YYMMDDHHmm` from the clock's current simulated wall-clock time.
fn timestamp(datetime: PrimitiveDateTime) -> String {
    format!(
        "{:02}{:02}{:02}{:02}{:02}",
        datetime.year().rem_euclid(100),
        u8::from(datetime.month()),
        datetime.day(),
        datetime.hour(),
        datetime.minute(),
    )
}

/// Wrap an echoed function code, timestamp, and body into a complete
/// computer-format message with the two's-complement checksum and ETX.
fn frame(echo: &[u8], datetime: PrimitiveDateTime, body: &str) -> Vec<u8> {
    let mut message = Vec::new();
    message.push(SOH);
    message.extend_from_slice(echo);
    message.extend_from_slice(timestamp(datetime).as_bytes());
    message.extend_from_slice(body.as_bytes());
    message.extend_from_slice(b"&&");

    // Checksum covers every byte preceding it, including the SOH.
    let sum = message
        .iter()
        .fold(0u16, |acc, &byte| acc.wrapping_add(u16::from(byte)));
    let checksum = 0u16.wrapping_sub(sum);
    message.extend_from_slice(format!("{checksum:04X}").as_bytes());

    message.push(ETX);
    message
}
