//! Dispenser positive-displacement flow meter.
//!
//! The public interface is defined directly in this file per the module
//! convention in sim-impl.md; the private implementation lives in
//! `internals` and is not visible to other modules.
//!
//! Owns: `Pulse`.

mod internals;

use tokio::sync::{mpsc, watch};

use crate::clock::SimClock;
use crate::physical::fuel_tank::OutflowRate;

/// A single pulse emitted by the flow meter.
///
/// Carrying `k_factor` on every pulse guarantees the dispenser uses exactly
/// the K-factor the meter is operating with, eliminating any construction-site
/// mismatch between the two entities.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Pulse {
    /// K-factor of this meter, in pulses per litre.
    pub k_factor: f64,
}

/// Spawn the dispenser PD flow meter task.
///
/// Waits for a non-zero `OutflowRate` on `outflow_rate_rx`. When the outflow
/// turns on it anchors a start instant and thereafter counts pulses against the
/// *elapsed flow time*: it wakes roughly once per `60 / (k_factor ×
/// rate_l_per_min)` seconds and emits however many pulses the time elapsed since
/// the anchor implies (`rate × elapsed × k_factor`, floored), catching up if a
/// wake was late. Counting against absolute elapsed time rather than per-sleep
/// keeps the pulse total faithful to the volume the tank has actually displaced
/// — to within one pulse — under any clock mode or multiplier, instead of
/// accumulating each timer's lateness into drift. A rate change re-anchors; when
/// the rate is `None` it blocks until the next change. Each send models one
/// physical pulse on the meter's open-collector output wire.
pub fn spawn(
    clock: SimClock,
    k_factor: f64,
    outflow_rate_rx: watch::Receiver<OutflowRate>,
    pulse_tx: mpsc::Sender<Pulse>,
) -> tokio::task::JoinHandle<()> {
    internals::spawn(clock, k_factor, outflow_rate_rx, pulse_tx)
}
