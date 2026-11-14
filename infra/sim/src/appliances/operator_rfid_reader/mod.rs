//! Operator RFID reader appliance.
//!
//! Mounted at the HMI workstation. Reads the dock operator's personal RFID card
//! when the operator presents it during login, and delivers the card number to
//! the HMI host over a serial connection (the same mechanism as the vessel
//! reader). The HMI passes the card number to the FMS for validation against
//! its operator card table.
//!
//! The reader behaviour is the common [`crate::shared::rfid_reader`]; this
//! module is the operator-context entry point and re-exports its `spawn`.
//!
//! Owns: `spawn()` (re-exported).

pub use crate::shared::rfid_reader::spawn;
