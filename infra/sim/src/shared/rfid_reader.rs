//! Common RFID reader task shared by the vessel and operator RFID reader
//! appliances.
//!
//! The two readers are physically distinct sealed devices on different hosts,
//! but their behaviour is identical: on each card presentation they emit the
//! card number on a serial line. Both appliance modules re-export this `spawn`;
//! their own module docs give the device-specific context.

use tokio::sync::mpsc;

use super::RfidCardNumber;
use crate::serial::SerialConnection;

/// Spawn an RFID reader task.
///
/// Waits for card presentation events on `card_rx`. On each event it writes the
/// card number to `serial` as a decimal integer followed by a newline — e.g.
/// `RfidCardNumber(12345)` becomes `"12345\n"` — matching a standard
/// Wiegand-to-serial decoder, which emits the full 26-bit value as a single
/// unsigned decimal token with no facility-code separation.
///
/// Purely event-driven. A closed card channel (the presenting actor has gone)
/// or a serial write failure (the boundary transport is broken) is fatal — the
/// card read must not silently vanish.
pub fn spawn(
    mut card_rx: mpsc::Receiver<RfidCardNumber>,
    mut serial: SerialConnection,
) -> tokio::task::JoinHandle<()> {
    tokio::spawn(async move {
        loop {
            let card = card_rx
                .recv()
                .await
                .expect("rfid_reader: card presentation channel closed");
            let line = format!("{}\n", card.0);
            // A real Wiegand decoder drives the wire regardless of whether the
            // host is reading; a write error therefore means the transport
            // itself has failed.
            serial
                .write_all(line.as_bytes())
                .await
                .expect("rfid_reader: serial write failed");
        }
    })
}
