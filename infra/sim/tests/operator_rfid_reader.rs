//! Integration test for `sim::appliances::operator_rfid_reader`.
//!
//! The reader behaviour is covered by the shared suite (`tests/rfid_reader.rs`)
//! against `sim::shared::rfid_reader`. This module re-exports that `spawn`, so a
//! single smoke test confirms the operator-context entry point is wired.

use tokio::sync::mpsc;

use sim::appliances::operator_rfid_reader;
use sim::serial::SerialConnection;
use sim::shared::RfidCardNumber;

#[tokio::test]
async fn spawn_delivers_card_to_serial() {
    let (card_tx, card_rx) = mpsc::channel(16);
    let (serial, handle) = SerialConnection::loopback();
    operator_rfid_reader::spawn(card_rx, serial);

    card_tx.send(RfidCardNumber(7)).await.unwrap();
    tokio::task::yield_now().await;

    assert_eq!(String::from_utf8(handle.bytes()).unwrap(), "7\n");
}
