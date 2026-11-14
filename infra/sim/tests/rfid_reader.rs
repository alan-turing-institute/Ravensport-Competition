//! Integration tests for the shared RFID reader (`sim::shared::rfid_reader`).
//!
//! The vessel and operator RFID reader appliances both re-export this `spawn`,
//! so its behaviour is tested once here. The reader is purely event-driven, so
//! these are plain `#[tokio::test]`s.

use tokio::sync::mpsc;

use sim::serial::SerialConnection;
use sim::shared::rfid_reader;
use sim::shared::RfidCardNumber;

#[tokio::test]
async fn card_number_written_to_serial_on_presentation() {
    let (card_tx, card_rx) = mpsc::channel(16);
    let (serial, handle) = SerialConnection::loopback();
    rfid_reader::spawn(card_rx, serial);

    card_tx.send(RfidCardNumber(12345)).await.unwrap();
    tokio::task::yield_now().await;

    assert_eq!(String::from_utf8(handle.bytes()).unwrap(), "12345\n");
}

#[tokio::test]
async fn each_card_presentation_produces_one_serial_write() {
    let (card_tx, card_rx) = mpsc::channel(16);
    let (serial, handle) = SerialConnection::loopback();
    rfid_reader::spawn(card_rx, serial);

    for number in [1u32, 2, 3] {
        card_tx.send(RfidCardNumber(number)).await.unwrap();
        tokio::task::yield_now().await;
    }

    assert_eq!(String::from_utf8(handle.bytes()).unwrap(), "1\n2\n3\n");
}

#[tokio::test]
async fn distinct_card_numbers_produce_distinct_serial_output() {
    let (card_tx, card_rx) = mpsc::channel(16);
    let (serial, handle) = SerialConnection::loopback();
    rfid_reader::spawn(card_rx, serial);

    card_tx.send(RfidCardNumber(11111)).await.unwrap();
    tokio::task::yield_now().await;
    card_tx.send(RfidCardNumber(22222)).await.unwrap();
    tokio::task::yield_now().await;

    let contents = String::from_utf8(handle.bytes()).unwrap();
    let tokens: Vec<&str> = contents.trim_end_matches('\n').split('\n').collect();
    assert_eq!(tokens.len(), 2);
    assert_ne!(tokens[0], tokens[1]);
}
