//! Integration tests for `sim::appliances::atg_console`.
//!
//! Exercises the entity through its public `spawn()` interface only, holding
//! the channel ends that neighbouring entities would own (see sim-tests.md).
//! The console queries `SimClock::now_datetime()` for the response timestamp, so
//! the tests run under `SimClock::isolated(...)` + `block_on` (not
//! `#[tokio::test]`) while still doing real TCP on loopback. The wire format is
//! specified in tls-protocol.md.

use std::net::SocketAddr;

use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpStream;
use tokio::sync::{mpsc, oneshot, watch};

use sim::appliances::atg_console::{self, AtgConsoleState};
use sim::clock::{SimClock, TimeModel};
use sim::physical::atg_probe::ProbeReading;
use sim::physical::bund_leak_sensor::BundReading;
use time::macros::datetime;
use time::PrimitiveDateTime;

const K_L_PER_MM: f64 = 7.0;
const TANK_CAPACITY_L: f64 = 50_000.0;
const SIM_START: PrimitiveDateTime = datetime!(2025-01-01 0:00);

const SOH: u8 = 0x01;
const ETX: u8 = 0x03;

/// A fixed probe reading; with the construction parameters it yields
/// volume = 35 000 L, ullage = 15 000 L (see sim-tests.md).
const TEST_READING: ProbeReading = ProbeReading {
    level_mm: 5000.0,
    temperature_c: 15.0,
};

/// Counterpart channel ends held by the test.
struct Harness {
    probe_sounding_rx: mpsc::Receiver<oneshot::Sender<ProbeReading>>,
    bund_tx: watch::Sender<BundReading>,
    alarm_clear_tx: mpsc::Sender<()>,
    // Retained so the console's state request channel stays open.
    _state_tx: mpsc::Sender<oneshot::Sender<AtgConsoleState>>,
}

fn free_addr() -> SocketAddr {
    // Bind to an ephemeral port, read it back, and drop the listener so the
    // console can re-bind it.
    std::net::TcpListener::bind("127.0.0.1:0")
        .unwrap()
        .local_addr()
        .unwrap()
}

fn spawn_console(clock: SimClock, addr: SocketAddr) -> Harness {
    let (probe_tx, probe_sounding_rx) = mpsc::channel(16);
    let (bund_tx, bund_rx) = watch::channel(BundReading::default());
    let (alarm_clear_tx, alarm_clear_rx) = mpsc::channel(16);
    let (state_tx, state_rx) = mpsc::channel(1);
    atg_console::spawn(
        clock,
        K_L_PER_MM,
        TANK_CAPACITY_L,
        Default::default(),
        state_rx,
        probe_tx,
        bund_rx,
        alarm_clear_rx,
        addr,
    );
    Harness {
        probe_sounding_rx,
        bund_tx,
        alarm_clear_tx,
        _state_tx: state_tx,
    }
}

/// Read a full computer-format response (up to and including `ETX`).
async fn read_response(client: &mut TcpStream) -> Vec<u8> {
    let mut buf = Vec::new();
    loop {
        let mut byte = [0u8; 1];
        client.read_exact(&mut byte).await.unwrap();
        buf.push(byte[0]);
        if byte[0] == ETX {
            break;
        }
    }
    buf
}

/// The body between the `YYMMDDHHmm` timestamp and the `&&` checksum tag.
/// Layout: SOH(1) + echo(6) + timestamp(10) … body … `&&`(2) + checksum(4) + ETX(1).
fn response_body(response: &[u8]) -> &[u8] {
    &response[17..response.len() - 7]
}

fn decode_float(hex: &[u8]) -> f32 {
    let text = std::str::from_utf8(hex).unwrap();
    let mut bytes = [0u8; 4];
    for (i, byte) in bytes.iter_mut().enumerate() {
        *byte = u8::from_str_radix(&text[i * 2..i * 2 + 2], 16).unwrap();
    }
    f32::from_be_bytes(bytes)
}

/// Service one pending probe request with the fixed test reading.
async fn service_probe(harness: &mut Harness) {
    let reply = harness.probe_sounding_rx.recv().await.unwrap();
    reply.send(TEST_READING).unwrap();
}

// ---------------------------------------------------------------------------
// Inventory and status
// ---------------------------------------------------------------------------

#[test]
fn i201_returns_correct_inventory_data() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let addr = free_addr();
        let mut harness = spawn_console(clock.clone(), addr);
        let mut client = TcpStream::connect(addr).await.unwrap();

        client.write_all(b"\x01i20100").await.unwrap();
        service_probe(&mut harness).await;
        let response = read_response(&mut client).await;

        assert_eq!(response[0], SOH);
        assert_eq!(&response[1..7], b"i20100"); // echoed function code
        let body = response_body(&response);
        assert_eq!(&body[0..2], b"01"); // tank number
        assert_eq!(&body[2..3], b"D"); // product code
        assert_eq!(&body[3..7], b"0000"); // status bits
        assert_eq!(&body[7..9], b"07"); // field count

        let fields: Vec<f32> = (0..7)
            .map(|i| decode_float(&body[9 + i * 8..9 + i * 8 + 8]))
            .collect();
        let expected = [35000.0, 35000.0, 15000.0, 5000.0, 0.0, 15.0, 0.0];
        for (got, want) in fields.iter().zip(expected) {
            assert!(
                (f64::from(*got) - want).abs() < 1.0,
                "field {got} not within tolerance of {want}"
            );
        }
    });
}

#[test]
fn i205_returns_no_active_tank_alarms() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let addr = free_addr();
        let _harness = spawn_console(clock.clone(), addr);
        let mut client = TcpStream::connect(addr).await.unwrap();

        client.write_all(b"\x01i20500").await.unwrap();
        let response = read_response(&mut client).await;

        assert_eq!(&response[1..7], b"i20500");
        assert_eq!(response_body(&response), b"0100"); // tank 01, zero alarms
    });
}

// ---------------------------------------------------------------------------
// System alarm report (i101) and the bund-alarm latch
// ---------------------------------------------------------------------------

#[test]
fn i101_returns_no_alarms_in_clean_state() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let addr = free_addr();
        let _harness = spawn_console(clock.clone(), addr);
        let mut client = TcpStream::connect(addr).await.unwrap();

        client.write_all(b"\x01i10100").await.unwrap();
        let response = read_response(&mut client).await;

        assert_eq!(response_body(&response), b"000000");
    });
}

#[test]
fn i101_contains_bund_alarm_when_latched() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let addr = free_addr();
        let harness = spawn_console(clock.clone(), addr);

        harness.bund_tx.send(BundReading { wet: true }).unwrap();
        tokio::task::yield_now().await; // let the console latch the alarm

        let mut client = TcpStream::connect(addr).await.unwrap();
        client.write_all(b"\x01i10100").await.unwrap();
        let response = read_response(&mut client).await;

        assert_eq!(response_body(&response), b"030301");
    });
}

#[test]
fn bund_alarm_latches_after_wet_signal_clears() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let addr = free_addr();
        let harness = spawn_console(clock.clone(), addr);

        harness.bund_tx.send(BundReading { wet: true }).unwrap();
        tokio::task::yield_now().await;
        harness.bund_tx.send(BundReading { wet: false }).unwrap();
        tokio::task::yield_now().await;

        let mut client = TcpStream::connect(addr).await.unwrap();
        client.write_all(b"\x01i10100").await.unwrap();
        let response = read_response(&mut client).await;

        // The latch holds even though the physical signal went dry.
        assert_eq!(response_body(&response), b"030301");
    });
}

#[test]
fn bund_alarm_clears_after_latch_clear_signal() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let addr = free_addr();
        let harness = spawn_console(clock.clone(), addr);

        harness.bund_tx.send(BundReading { wet: true }).unwrap();
        tokio::task::yield_now().await;
        harness.bund_tx.send(BundReading { wet: false }).unwrap();
        tokio::task::yield_now().await;
        harness.alarm_clear_tx.send(()).await.unwrap();
        tokio::task::yield_now().await;

        let mut client = TcpStream::connect(addr).await.unwrap();
        client.write_all(b"\x01i10100").await.unwrap();
        let response = read_response(&mut client).await;

        assert_eq!(response_body(&response), b"000000");
    });
}

// ---------------------------------------------------------------------------
// Framing
// ---------------------------------------------------------------------------

#[test]
fn response_checksum_is_valid() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let addr = free_addr();
        let mut harness = spawn_console(clock.clone(), addr);
        let mut client = TcpStream::connect(addr).await.unwrap();

        client.write_all(b"\x01i20100").await.unwrap();
        service_probe(&mut harness).await;
        let response = read_response(&mut client).await;

        // Bytes preceding the 4-char checksum field: SOH … `&&`.
        let checksum_start = response.len() - 5; // 4 checksum chars + ETX
        let data = &response[..checksum_start];
        let sum = data
            .iter()
            .fold(0u16, |acc, &byte| acc.wrapping_add(u16::from(byte)));
        let checksum_text =
            std::str::from_utf8(&response[checksum_start..response.len() - 1]).unwrap();
        let checksum = u16::from_str_radix(checksum_text, 16).unwrap();

        assert_eq!(sum.wrapping_add(checksum), 0);
    });
}

#[test]
fn concurrent_clients_both_receive_valid_responses() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let addr = free_addr();
        let mut harness = spawn_console(clock.clone(), addr);

        let mut first = TcpStream::connect(addr).await.unwrap();
        let mut second = TcpStream::connect(addr).await.unwrap();
        first.write_all(b"\x01i20100").await.unwrap();
        second.write_all(b"\x01i20100").await.unwrap();

        // Both connections produce a probe request; service them as they arrive.
        service_probe(&mut harness).await;
        service_probe(&mut harness).await;

        let first_response = read_response(&mut first).await;
        let second_response = read_response(&mut second).await;

        for response in [&first_response, &second_response] {
            assert_eq!(response[0], SOH);
            assert_eq!(*response.last().unwrap(), ETX);
            assert_eq!(&response[1..7], b"i20100");
            assert_eq!(&response_body(response)[0..2], b"01");
        }
    });
}

// ---------------------------------------------------------------------------
// Bounded session capacity (sim-impl.md, "Bounded session capacity")
// ---------------------------------------------------------------------------

/// Mirrors `internals::MAX_ATG_SESSIONS`. The console serves a finite number of
/// concurrent sessions; holding them all denies a further poll until one frees.
const SESSION_CAP: usize = 10;

#[test]
fn session_capacity_is_bounded_and_frees_on_disconnect() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let addr = free_addr();
        let mut harness = spawn_console(clock.clone(), addr);

        // Fill every session slot: each connection is polled, serviced, and held
        // open so it continues to occupy its slot.
        let mut held = Vec::new();
        for _ in 0..SESSION_CAP {
            let mut c = TcpStream::connect(addr).await.unwrap();
            c.write_all(b"\x01i20100").await.unwrap();
            service_probe(&mut harness).await;
            let _ = read_response(&mut c).await;
            held.push(c);
        }

        // A further connection reaches ESTABLISHED — the kernel completes the
        // handshake into the listener backlog — but the console, at capacity,
        // never accepts it, so its poll is not serviced. No probe request even
        // reaches the harness, and the read times out.
        let mut extra = TcpStream::connect(addr).await.unwrap();
        extra.write_all(b"\x01i20100").await.unwrap();
        let starved = tokio::time::timeout(
            std::time::Duration::from_millis(300),
            read_response(&mut extra),
        )
        .await;
        assert!(
            starved.is_err(),
            "a poll was served while all {SESSION_CAP} session slots were held"
        );

        // Free one slot; the queued connection is now accepted and its already-sent
        // poll serviced — the denial is relieved when the held sessions release.
        drop(held.pop().unwrap());
        service_probe(&mut harness).await;
        let response =
            tokio::time::timeout(std::time::Duration::from_secs(2), read_response(&mut extra))
                .await
                .expect("the queued poll was not served after a session slot freed");
        assert_eq!(&response[1..7], b"i20100");
    });
}
