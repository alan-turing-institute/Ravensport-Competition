//! Tests for the mock ATG relay (`tests/mock_digital/relay.rs`): it forwards
//! bytes transparently in both directions, including the real Veeder-Root TLS
//! exchange to a real ATG console.

mod mock_digital;

use std::net::SocketAddr;

use time::macros::datetime;
use time::PrimitiveDateTime;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::{mpsc, watch};

use sim::appliances::atg_console;
use sim::clock::{SimClock, TimeModel};
use sim::physical::atg_probe::ProbeReading;
use sim::physical::bund_leak_sensor::BundReading;

use mock_digital::relay::MockRelay;

const SIM_START: PrimitiveDateTime = datetime!(2025-01-01 0:00);
const ETX: u8 = 0x03;

fn free_addr() -> SocketAddr {
    std::net::TcpListener::bind("127.0.0.1:0")
        .unwrap()
        .local_addr()
        .unwrap()
}

/// A backend that echoes the first four bytes it receives.
async fn spawn_echo_backend() -> SocketAddr {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    tokio::spawn(async move {
        if let Ok((mut stream, _)) = listener.accept().await {
            let mut buf = [0u8; 4];
            if stream.read_exact(&mut buf).await.is_ok() {
                let _ = stream.write_all(&buf).await;
            }
        }
    });
    addr
}

#[tokio::test]
async fn relay_forwards_bytes_in_both_directions() {
    let backend_addr = spawn_echo_backend().await;
    let relay = MockRelay::start(backend_addr);

    let mut client = TcpStream::connect(relay.addr).await.unwrap();
    client.write_all(b"ping").await.unwrap();

    // Receiving the echo proves the request reached the backend and the reply
    // reached the client — both directions through the relay.
    let mut buf = [0u8; 4];
    client.read_exact(&mut buf).await.unwrap();
    assert_eq!(&buf, b"ping");
}

#[test]
fn relay_proxies_tls_to_a_real_atg_console() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let atg_addr = free_addr();
        let (probe_tx, mut probe_rx) = mpsc::channel(16);
        let (_bund_tx, bund_rx) = watch::channel(BundReading::default());
        let (_clear_tx, clear_rx) = mpsc::channel(16);
        let (_state_tx, state_rx) = mpsc::channel(1);
        atg_console::spawn(
            clock.clone(),
            7.0,
            50_000.0,
            Default::default(),
            state_rx,
            probe_tx,
            bund_rx,
            clear_rx,
            atg_addr,
        );

        let relay = MockRelay::start(atg_addr);

        // Connect as the fuel supplier would — through the relay — and issue an
        // i201 inventory request.
        let mut client = TcpStream::connect(relay.addr).await.unwrap();
        client.write_all(b"\x01i20100").await.unwrap();

        // Service the probe request the console makes to satisfy the i201.
        let probe_reply = probe_rx.recv().await.unwrap();
        probe_reply
            .send(ProbeReading {
                level_mm: 5000.0,
                temperature_c: 15.0,
            })
            .unwrap();

        // Read the response (forwarded back through the relay) up to ETX.
        let mut response = Vec::new();
        loop {
            let mut byte = [0u8; 1];
            client.read_exact(&mut byte).await.unwrap();
            response.push(byte[0]);
            if byte[0] == ETX {
                break;
            }
        }

        assert_eq!(response[0], 0x01, "response starts with SOH");
        assert_eq!(&response[1..7], b"i20100", "echoed i201 function code");
    });
}
