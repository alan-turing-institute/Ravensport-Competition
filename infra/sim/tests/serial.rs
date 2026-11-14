//! Integration tests for `sim::serial::SerialConnection`.
//!
//! Cover the in-process loopback transport through the public API: written
//! bytes are captured by the `LoopbackHandle`, accumulate in order, and pass
//! through verbatim — the link is a byte-transparent transport, not
//! text-specific. `#[tokio::test]`s, since `write_all` is async.

use sim::serial::SerialConnection;

#[tokio::test]
async fn loopback_starts_empty() {
    let (_serial, handle) = SerialConnection::loopback();
    assert!(handle.bytes().is_empty());
}

#[tokio::test]
async fn write_all_is_captured_by_handle() {
    let (mut serial, handle) = SerialConnection::loopback();
    serial.write_all(b"hello").await.unwrap();
    assert_eq!(String::from_utf8(handle.bytes()).unwrap(), "hello");
}

#[tokio::test]
async fn successive_writes_accumulate_in_order() {
    let (mut serial, handle) = SerialConnection::loopback();
    serial.write_all(b"12345\n").await.unwrap();
    serial.write_all(b"67890\n").await.unwrap();
    assert_eq!(String::from_utf8(handle.bytes()).unwrap(), "12345\n67890\n");
}

#[tokio::test]
async fn empty_write_is_a_no_op() {
    let (mut serial, handle) = SerialConnection::loopback();
    serial.write_all(b"").await.unwrap();
    assert!(handle.bytes().is_empty());
}

#[tokio::test]
async fn transports_arbitrary_bytes() {
    // Non-text byte values must pass through verbatim.
    let (mut serial, handle) = SerialConnection::loopback();
    let raw = [0x00u8, 0xFF, 0x10, 0x0A, 0x7F];
    serial.write_all(&raw).await.unwrap();
    assert_eq!(handle.bytes(), Vec::from(raw));
}
