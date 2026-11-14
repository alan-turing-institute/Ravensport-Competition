//! Bund leak sensor instrument.
//!
//! The public interface is defined directly in this file per the module
//! convention in sim-impl.md; the private implementation lives in
//! `internals` and is not visible to other modules.
//!
//! Owns: `BundReading`.

mod internals;

use tokio::sync::watch;

/// The reading produced by the bund leak sensor on each sampling.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct BundReading {
    /// True when fuel is detected in the secondary containment bund. In the
    /// real installation this is a continuous resistance signal; in the sim it
    /// is updated on each change to `bund_wet` in the physical model.
    pub wet: bool,
}

/// Spawn the bund leak sensor task.
///
/// Waits for changes on `bund_wet_rx`. On each change it publishes a fresh
/// `BundReading` via `bund_tx`. The sensor does not latch — it republishes the
/// transition to dry as faithfully as the transition to wet; alarm latching is
/// the ATG console's responsibility.
pub fn spawn(
    bund_wet_rx: watch::Receiver<bool>,
    bund_tx: watch::Sender<BundReading>,
) -> tokio::task::JoinHandle<()> {
    internals::spawn(bund_wet_rx, bund_tx)
}
