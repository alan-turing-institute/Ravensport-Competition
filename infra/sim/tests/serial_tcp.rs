//! Integration tests for the TCP `SerialConnection` backend (`sim::serial`).
//!
//! The TCP backend is the boundary transport for the vessel and operator RFID
//! readers (sim-network.md): the reader binds a loopback port and streams each
//! card-number frame to whichever digital client is connected, identical on the
//! wire to the in-process loopback backend. These tests exercise that transport
//! end to end over a real socket — a client connecting and reading a card;
//! framing parity with the loopback; sequential and concurrent clients; a
//! client that disconnects mid-stream; and the recovered (not fatal) handling
//! of a dropped or absent peer.
//!
//! Card presentation is server-push: a frame written before a client has
//! connected is lost on the wire, exactly as on a real serial line. The kernel
//! completes the TCP handshake before the reader's accept loop subscribes the
//! connection, so a card sent the instant `connect` returns can race ahead of
//! that subscription. The helpers therefore prove a client is subscribed by
//! driving one sentinel frame through it before asserting on the frames that
//! follow.

use std::net::SocketAddr;
use std::time::Duration;

use tokio::io::{AsyncBufReadExt, BufReader};
use tokio::net::TcpStream;
use tokio::sync::mpsc;
use tokio::time::timeout;

use sim::serial::SerialConnection;
use sim::shared::rfid_reader;
use sim::shared::RfidCardNumber;

/// A working frame must arrive promptly once a client is subscribed; this bound
/// is generous and is only ever reached when a frame was genuinely not sent.
const READ_TIMEOUT: Duration = Duration::from_secs(1);
/// A missed sentinel means the client was not yet subscribed; a short bound
/// keeps the subscription retry cheap (delivery is sub-millisecond when live).
const SYNC_TIMEOUT: Duration = Duration::from_millis(200);

/// A free loopback address: bind to port 0, read the assigned port, release it,
/// and hand the address to the backend to bind (the appliance-test pattern).
fn free_addr() -> SocketAddr {
    std::net::TcpListener::bind("127.0.0.1:0")
        .unwrap()
        .local_addr()
        .unwrap()
}

/// Read one newline-terminated frame from `client`, failing if none arrives
/// within [`READ_TIMEOUT`].
async fn read_frame(client: &mut BufReader<TcpStream>) -> String {
    let mut line = String::new();
    let n = timeout(READ_TIMEOUT, client.read_line(&mut line))
        .await
        .expect("timed out waiting for a card frame")
        .expect("read from client connection");
    assert!(n > 0, "client connection closed before a frame arrived");
    line
}

/// Connect `count` clients and return them once all are subscribed, proven by
/// driving one `sentinel` card that every client reads back. A single sentinel
/// round per success leaves each client's buffer clean — every delivered
/// sentinel is consumed — so callers can assert on the frames that follow
/// without draining. Closes the connect/subscribe race described in the module
/// docs.
async fn connect_subscribed(
    addr: SocketAddr,
    card_tx: &mpsc::Sender<RfidCardNumber>,
    count: usize,
    sentinel: RfidCardNumber,
) -> Vec<BufReader<TcpStream>> {
    let mut clients = Vec::with_capacity(count);
    for _ in 0..count {
        clients.push(BufReader::new(TcpStream::connect(addr).await.unwrap()));
    }

    let expected = format!("{}\n", sentinel.0);
    loop {
        card_tx.send(sentinel).await.unwrap();
        let mut all_subscribed = true;
        for client in clients.iter_mut() {
            let mut line = String::new();
            match timeout(SYNC_TIMEOUT, client.read_line(&mut line)).await {
                Ok(Ok(n)) if n > 0 => assert_eq!(line, expected, "sentinel frame garbled"),
                // This client missed the sentinel (not yet subscribed); retry.
                _ => all_subscribed = false,
            }
        }
        if all_subscribed {
            return clients;
        }
    }
}

#[tokio::test]
async fn client_reads_a_presented_card() {
    let addr = free_addr();
    let (card_tx, card_rx) = mpsc::channel(16);
    rfid_reader::spawn(card_rx, SerialConnection::tcp(addr));

    let mut client = connect_subscribed(addr, &card_tx, 1, RfidCardNumber(1))
        .await
        .pop()
        .unwrap();
    card_tx.send(RfidCardNumber(12345)).await.unwrap();
    assert_eq!(read_frame(&mut client).await, "12345\n");
}

#[tokio::test]
async fn framing_matches_loopback_exactly() {
    let cards = [
        RfidCardNumber(12345),
        RfidCardNumber(67890),
        RfidCardNumber(11),
    ];

    // Loopback reference: the reader writes the frames into a captured buffer.
    let (lb_tx, lb_rx) = mpsc::channel(16);
    let (serial, handle) = SerialConnection::loopback();
    rfid_reader::spawn(lb_rx, serial);
    for card in cards {
        lb_tx.send(card).await.unwrap();
        tokio::task::yield_now().await;
    }
    let loopback_bytes = handle.bytes();

    // TCP: the reader streams the same frames to a connected client.
    let addr = free_addr();
    let (tcp_tx, tcp_rx) = mpsc::channel(16);
    rfid_reader::spawn(tcp_rx, SerialConnection::tcp(addr));
    let mut client = connect_subscribed(addr, &tcp_tx, 1, RfidCardNumber(999))
        .await
        .pop()
        .unwrap();
    let mut tcp_bytes = Vec::new();
    for card in cards {
        tcp_tx.send(card).await.unwrap();
        tcp_bytes.extend_from_slice(read_frame(&mut client).await.as_bytes());
    }

    // A digital client must not be able to tell the two backends apart.
    assert_eq!(
        tcp_bytes, loopback_bytes,
        "TCP framing must be byte-identical to the loopback backend"
    );
}

#[tokio::test]
async fn sequential_clients_each_read_their_own_cards() {
    let addr = free_addr();
    let (card_tx, card_rx) = mpsc::channel(16);
    rfid_reader::spawn(card_rx, SerialConnection::tcp(addr));

    {
        let mut first = connect_subscribed(addr, &card_tx, 1, RfidCardNumber(1))
            .await
            .pop()
            .unwrap();
        card_tx.send(RfidCardNumber(100)).await.unwrap();
        assert_eq!(read_frame(&mut first).await, "100\n");
    } // first client drops

    let mut second = connect_subscribed(addr, &card_tx, 1, RfidCardNumber(2))
        .await
        .pop()
        .unwrap();
    card_tx.send(RfidCardNumber(200)).await.unwrap();
    assert_eq!(read_frame(&mut second).await, "200\n");
}

#[tokio::test]
async fn concurrent_clients_each_receive_the_card() {
    let addr = free_addr();
    let (card_tx, card_rx) = mpsc::channel(16);
    rfid_reader::spawn(card_rx, SerialConnection::tcp(addr));

    let mut clients = connect_subscribed(addr, &card_tx, 2, RfidCardNumber(1)).await;
    card_tx.send(RfidCardNumber(42)).await.unwrap();
    for client in clients.iter_mut() {
        assert_eq!(read_frame(client).await, "42\n");
    }
}

#[tokio::test]
async fn client_disconnecting_mid_stream_does_not_break_the_reader() {
    let addr = free_addr();
    let (card_tx, card_rx) = mpsc::channel(16);
    rfid_reader::spawn(card_rx, SerialConnection::tcp(addr));

    // A client connects, reads one card, then drops with a further card unread.
    {
        let mut first = connect_subscribed(addr, &card_tx, 1, RfidCardNumber(1))
            .await
            .pop()
            .unwrap();
        card_tx.send(RfidCardNumber(100)).await.unwrap();
        assert_eq!(read_frame(&mut first).await, "100\n");
        card_tx.send(RfidCardNumber(101)).await.unwrap();
    } // first drops mid-stream, leaving card 101 unread on its connection

    // The reader is unaffected: a fresh client reads subsequent cards.
    let mut second = connect_subscribed(addr, &card_tx, 1, RfidCardNumber(2))
        .await
        .pop()
        .unwrap();
    card_tx.send(RfidCardNumber(200)).await.unwrap();
    assert_eq!(read_frame(&mut second).await, "200\n");
}

#[tokio::test]
async fn readiness_resolves_once_a_client_is_subscribed() {
    // The readiness handle backs the actors' card-presentation gate: it must not
    // resolve while no digital client is connected, and must resolve once one is.
    let addr = free_addr();
    let serial = SerialConnection::tcp(addr);
    let readiness = serial.readiness();

    // No client yet — readiness must not resolve.
    assert!(
        timeout(SYNC_TIMEOUT, readiness.wait_ready()).await.is_err(),
        "readiness resolved before any client connected"
    );

    // A client connects — readiness resolves once the accept loop subscribes it.
    let _client = TcpStream::connect(addr).await.unwrap();
    timeout(READ_TIMEOUT, readiness.wait_ready())
        .await
        .expect("readiness did not resolve after a client connected");
}

#[tokio::test]
async fn loopback_readiness_is_always_ready() {
    // The loopback backend captures every write, so there is no subscription to
    // wait for; its readiness resolves immediately (used by in-process wiring).
    let (serial, _handle) = SerialConnection::loopback();
    timeout(SYNC_TIMEOUT, serial.readiness().wait_ready())
        .await
        .expect("loopback readiness must resolve immediately");
}

#[tokio::test]
async fn write_with_no_client_is_lost_not_an_error() {
    // The decoder drives the wire whether or not a host is listening; with no
    // client connected the frame is lost on the wire, but writing succeeds.
    let mut serial = SerialConnection::tcp(free_addr());
    serial
        .write_all(b"12345\n")
        .await
        .expect("write with no listener must succeed");
}

#[tokio::test]
async fn write_after_peer_drop_is_recovered_not_fatal() {
    let addr = free_addr();
    let mut serial = SerialConnection::tcp(addr);

    // Subscribe a client (directly, via the transport), then drop it.
    let mut client = BufReader::new(TcpStream::connect(addr).await.unwrap());
    loop {
        serial.write_all(b"0\n").await.unwrap();
        let mut line = String::new();
        if let Ok(Ok(n)) = timeout(SYNC_TIMEOUT, client.read_line(&mut line)).await {
            if n > 0 {
                break;
            }
        }
    }
    drop(client);

    // Writing with the peer gone must not error — were it to, the RFID reader's
    // write would panic and bring the sim down on a routine host disconnect.
    for _ in 0..10 {
        serial
            .write_all(b"42\n")
            .await
            .expect("write must not fail after a peer drops");
    }

    // And the transport recovers: a fresh client connects and reads again.
    let mut reconnected = BufReader::new(TcpStream::connect(addr).await.unwrap());
    loop {
        serial.write_all(b"99999\n").await.unwrap();
        let mut line = String::new();
        if let Ok(Ok(n)) = timeout(SYNC_TIMEOUT, reconnected.read_line(&mut line)).await {
            if n > 0 {
                assert_eq!(line, "99999\n");
                break;
            }
        }
    }
}
