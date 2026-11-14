//! Vessel RFID reader appliance.
//!
//! Mounted at the dispenser. Reads the tug's RFID tag when the tug engineer
//! presents it at the reader, and delivers the card number to the FMS host
//! over a serial connection (mirroring a Wiegand-to-serial decoder). The FMS
//! validates the card number against the vessel ID on the pending
//! authorisation.
//!
//! The reader behaviour is the common [`crate::shared::rfid_reader`]; this
//! module is the vessel-context entry point and re-exports its `spawn`.
//!
//! Owns: `spawn()` (re-exported).

pub use crate::shared::rfid_reader::spawn;
