//! The fuel supplier's log (`sim::actors::fuel_supplier`): the poll that places
//! an order logs the order, with the volume it read and the tanker's scheduled
//! arrival, one lead time after the poll, from which the episode runner reads a
//! pending order at a fork.
//!
//! The test captures the supplier's events with a subscriber of its own. It
//! lives in a test binary of its own, because tracing caches each event's
//! interest across the threads of one process, and a test on another thread
//! that reaches the supplier first, with no subscriber, would hide the events
//! from this one. A minimal automatic tank gauge answers `i201` with a fixed
//! volume below the reorder threshold and `i101` with no alarm.

use std::net::SocketAddr;
use std::time::Duration;

use time::format_description::well_known::Rfc3339;
use time::macros::datetime;
use time::{OffsetDateTime, PrimitiveDateTime};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::{mpsc, oneshot};

use sim::actors::fuel_supplier::{self, FuelSupplierState, SupplierStart};
use sim::clock::{SimClock, TimeModel};
use sim::stop::stop_channel;

const SIM_START: PrimitiveDateTime = datetime!(2025-01-01 0:00);
const POLL_INTERVAL: Duration = Duration::from_secs(60);
const LEAD_TIME: Duration = Duration::from_secs(1800);
const REORDER_THRESHOLD_L: f64 = 10_000.0;
const LEVEL_L: f32 = 8_000.0;

/// A writer that keeps every line the test's subscriber formats.
#[derive(Clone, Default)]
struct Captured(std::sync::Arc<std::sync::Mutex<Vec<u8>>>);

impl std::io::Write for Captured {
    fn write(&mut self, buf: &[u8]) -> std::io::Result<usize> {
        self.0.lock().unwrap().extend_from_slice(buf);
        Ok(buf.len())
    }

    fn flush(&mut self) -> std::io::Result<()> {
        Ok(())
    }
}

impl<'a> tracing_subscriber::fmt::MakeWriter<'a> for Captured {
    type Writer = Captured;

    fn make_writer(&'a self) -> Self::Writer {
        self.clone()
    }
}

/// An automatic tank gauge that reports LEVEL_L and no alarm.
fn spawn_gauge() -> SocketAddr {
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    listener.set_nonblocking(true).unwrap();
    let addr = listener.local_addr().unwrap();
    let listener = TcpListener::from_std(listener).unwrap();
    tokio::spawn(async move {
        while let Ok((stream, _)) = listener.accept().await {
            tokio::spawn(answer(stream));
        }
    });
    addr
}

async fn answer(mut stream: TcpStream) {
    loop {
        let mut soh = [0u8; 1];
        let mut code = [0u8; 6];
        if stream.read_exact(&mut soh).await.is_err() || stream.read_exact(&mut code).await.is_err()
        {
            return;
        }
        let body = match &code[1..4] {
            b"201" => {
                let mut body = String::from("01D000007");
                body.push_str(&float_field(LEVEL_L));
                for _ in 0..6 {
                    body.push_str(&float_field(0.0));
                }
                body
            }
            b"101" => "000000".to_string(),
            _ => continue,
        };
        if stream.write_all(&frame(&code, &body)).await.is_err() {
            return;
        }
    }
}

fn float_field(value: f32) -> String {
    value
        .to_be_bytes()
        .iter()
        .map(|byte| format!("{byte:02X}"))
        .collect()
}

/// An echoed code and body as `SOH echo timestamp body && checksum ETX`
/// (tls-protocol.md §6).
fn frame(echo: &[u8], body: &str) -> Vec<u8> {
    let mut message = vec![0x01];
    message.extend_from_slice(echo);
    message.extend_from_slice(b"0000000000");
    message.extend_from_slice(body.as_bytes());
    message.extend_from_slice(b"&&");
    let sum = message
        .iter()
        .fold(0u16, |acc, &b| acc.wrapping_add(u16::from(b)));
    message.extend_from_slice(format!("{:04X}", 0u16.wrapping_sub(sum)).as_bytes());
    message.push(0x03);
    message
}

/// The poll that reads the level below the threshold logs the order, with the
/// volume it read and the tanker's scheduled arrival one lead time on, and
/// the tanker arrives then; the polls before and after it log no order.
#[test]
fn the_ordering_poll_logs_the_tankers_scheduled_arrival() {
    let captured = Captured::default();
    let subscriber = tracing_subscriber::fmt()
        .with_ansi(false)
        .with_writer(captured.clone())
        .with_env_filter(tracing_subscriber::EnvFilter::new(format!(
            "{}=info",
            fuel_supplier::LOG_TARGET
        )))
        .finish();
    let arrived = tracing::subscriber::with_default(subscriber, || {
        let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
        runtime.block_on(async {
            let gauge = spawn_gauge();
            let (arrival_tx, mut arrival_rx) = mpsc::channel(4);
            // Never stopped: the stop request and the driver's report are kept
            // for the life of the test process.
            let (stop, stop_rx) = stop_channel();
            let (driver_at_rest, driver_at_rest_rx) = oneshot::channel();
            let (report_tx, report_rx) = oneshot::channel();
            std::mem::forget((stop, driver_at_rest, report_rx));
            // The cold start the start state gives, as the orchestrator makes it.
            let start = FuelSupplierState::cold(
                clock.now_datetime(),
                POLL_INTERVAL,
                SupplierStart::FirstPollAfter(POLL_INTERVAL),
            );
            let _supplier = fuel_supplier::spawn(
                clock.clone(),
                start,
                stop_rx,
                driver_at_rest_rx,
                report_tx,
                ([127, 0, 0, 1], 0).into(),
                gauge,
                POLL_INTERVAL,
                REORDER_THRESHOLD_L,
                15_000.0,
                LEAD_TIME,
                arrival_tx,
            );
            let job = arrival_rx
                .recv()
                .await
                .expect("the supplier sent no tanker");
            let arrived = clock.now_datetime();
            drop(job);
            arrived
        })
    });

    let text = String::from_utf8(captured.0.lock().unwrap().clone()).unwrap();
    let orders: Vec<&str> = text
        .lines()
        .filter(|line| line.contains(" order placed "))
        .collect();
    assert_eq!(orders.len(), 1, "one order is logged:\n{text}");
    let order = orders[0];
    assert!(
        order.contains(&format!(" volume_l={LEVEL_L}")),
        "the order gives the volume the poll read: {order}"
    );
    let due = order
        .split_whitespace()
        .find_map(|word| word.strip_prefix("tanker_arrival="))
        .unwrap_or_else(|| panic!("the order gives no tanker arrival: {order}"));
    let due = OffsetDateTime::parse(due, &Rfc3339).expect("an RFC 3339 instant");
    // The first poll, one interval after the start, places the order.
    assert_eq!(due, (SIM_START + POLL_INTERVAL + LEAD_TIME).assume_utc());
    assert_eq!(
        arrived.assume_utc(),
        due,
        "the tanker arrives when the order said"
    );
    let polls = text.lines().filter(|l| l.contains(" poll ")).count();
    let placing = text
        .lines()
        .filter(|l| l.contains("order_placed=true"))
        .count();
    assert!(
        polls > 1,
        "the supplier kept polling through its order:\n{text}"
    );
    assert_eq!(
        placing, 1,
        "a poll while an order is pending places no order"
    );
}

/// An order pending at the start is logged as an ordering poll's is, with its
/// tanker's scheduled arrival, so that a fork reads it from the log.
#[test]
fn an_order_pending_at_the_start_logs_its_tankers_arrival() {
    const REMAINING: Duration = Duration::from_secs(600);
    let captured = Captured::default();
    let subscriber = tracing_subscriber::fmt()
        .with_ansi(false)
        .with_writer(captured.clone())
        .with_env_filter(tracing_subscriber::EnvFilter::new(format!(
            "{}=info",
            fuel_supplier::LOG_TARGET
        )))
        .finish();
    let arrived = tracing::subscriber::with_default(subscriber, || {
        let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
        runtime.block_on(async {
            let gauge = spawn_gauge();
            let (arrival_tx, mut arrival_rx) = mpsc::channel(4);
            // Never stopped: the stop request and the driver's report are kept
            // for the life of the test process.
            let (stop, stop_rx) = stop_channel();
            let (driver_at_rest, driver_at_rest_rx) = oneshot::channel();
            let (report_tx, report_rx) = oneshot::channel();
            std::mem::forget((stop, driver_at_rest, report_rx));
            // The cold start the start state gives, as the orchestrator makes it.
            let start = FuelSupplierState::cold(
                clock.now_datetime(),
                POLL_INTERVAL,
                SupplierStart::OrderPending(REMAINING),
            );
            let _supplier = fuel_supplier::spawn(
                clock.clone(),
                start,
                stop_rx,
                driver_at_rest_rx,
                report_tx,
                ([127, 0, 0, 1], 0).into(),
                gauge,
                POLL_INTERVAL,
                REORDER_THRESHOLD_L,
                15_000.0,
                LEAD_TIME,
                arrival_tx,
            );
            let job = arrival_rx
                .recv()
                .await
                .expect("the supplier sent no tanker");
            let arrived = clock.now_datetime();
            drop(job);
            arrived
        })
    });

    let text = String::from_utf8(captured.0.lock().unwrap().clone()).unwrap();
    let order = text
        .lines()
        .find(|line| line.contains(" order placed "))
        .unwrap_or_else(|| panic!("no order is logged:\n{text}"));
    let due = order
        .split_whitespace()
        .find_map(|word| word.strip_prefix("tanker_arrival="))
        .unwrap_or_else(|| panic!("the order gives no tanker arrival: {order}"));
    let due = OffsetDateTime::parse(due, &Rfc3339).expect("an RFC 3339 instant");
    assert_eq!(due, (SIM_START + REMAINING).assume_utc());
    assert_eq!(
        arrived.assume_utc(),
        due,
        "the tanker arrives when the order said"
    );
}
