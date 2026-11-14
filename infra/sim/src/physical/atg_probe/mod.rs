//! ATG probe instrument.
//!
//! The public interface is defined directly in this file per the module
//! convention in sim-impl.md; the private implementation lives in
//! `internals` and is not visible to other modules.
//!
//! Owns: `ProbeReading`, `AtgProbeState`.

mod internals;

use serde::{Deserialize, Serialize};
use tokio::sync::{mpsc, oneshot};

use crate::physical::fuel_tank::TankSounding;

/// The reading returned by the ATG probe in response to an on-demand request.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct ProbeReading {
    /// Fuel level in millimetres. When the noise model is enabled, independent
    /// Gaussian noise with σ = 0.25 mm is added to each reading (Veeder-Root
    /// Mag-FLEX ±0.5 mm repeatability treated as a ±2σ bound).
    pub level_mm: f64,
    /// Fuel temperature in degrees Celsius.
    pub temperature_c: f64,
}

/// The probe's noise generator at a stop (sim-impl.md, "State types"): its
/// seed and its position in that seed's sequence.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct AtgProbeState {
    pub noise_seed: u64,
    #[serde(with = "crate::checkpoint::decimal")]
    pub noise_word_pos: u128,
}

impl AtgProbeState {
    /// The cold-start state: the sequence of `noise_seed` from its beginning.
    pub fn new(noise_seed: u64) -> Self {
        Self {
            noise_seed,
            noise_word_pos: 0,
        }
    }
}

/// Spawn the ATG probe task.
///
/// Waits for reading requests on `reading_rx`. On each request it sends a
/// fresh oneshot sender to the fuel tank via `sounding_tx`, awaits the
/// lazily-computed `TankSounding`, applies the noise model, and responds to
/// the ATG console via the oneshot carried in the original request.
///
/// The noise sequence continues from `start`. The probe runs for the life of
/// the sim, through a stop, and answers each request on `state_rx` with its
/// noise generator's state.
pub fn spawn(
    reading_rx: mpsc::Receiver<oneshot::Sender<ProbeReading>>,
    sounding_tx: mpsc::Sender<oneshot::Sender<TankSounding>>,
    noise_enabled: bool,
    start: AtgProbeState,
    state_rx: mpsc::Receiver<oneshot::Sender<AtgProbeState>>,
) -> tokio::task::JoinHandle<()> {
    internals::spawn(reading_rx, sounding_tx, noise_enabled, start, state_rx)
}
