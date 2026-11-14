//! Types shared across more than one sim entity.
//!
//! Owns: `RfidCardNumber`. Also hosts [`rfid_reader`], the common RFID reader
//! task that the vessel and operator RFID reader appliances re-export.

pub mod rfid_reader;

/// A 26-bit Wiegand card number read from a vessel or operator RFID reader.
/// The FMS maps this to a vessel identity or operator identity via its internal
/// card table.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, serde::Serialize, serde::Deserialize)]
pub struct RfidCardNumber(pub u32);
