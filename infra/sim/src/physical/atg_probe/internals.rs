//! Private implementation of the ATG probe. Not visible to other modules.

use rand::SeedableRng;
use rand_chacha::ChaCha8Rng;
use rand_distr::{Distribution, Normal};
use tokio::sync::{mpsc, oneshot};

use super::{AtgProbeState, ProbeReading};
use crate::physical::fuel_tank::TankSounding;

/// Standard deviation of the probe's level noise, in millimetres. Derived from
/// the Veeder-Root Mag-FLEX ±0.5 mm repeatability treated as a ±2σ bound.
const NOISE_STD_DEV_MM: f64 = 0.25;

pub(super) fn spawn(
    mut reading_rx: mpsc::Receiver<oneshot::Sender<ProbeReading>>,
    sounding_tx: mpsc::Sender<oneshot::Sender<TankSounding>>,
    noise_enabled: bool,
    start: AtgProbeState,
    mut state_rx: mpsc::Receiver<oneshot::Sender<AtgProbeState>>,
) -> tokio::task::JoinHandle<()> {
    // Validate the noise distribution on the setup path, before spawning.
    let normal = Normal::new(0.0, NOISE_STD_DEV_MM)
        .expect("atg_probe: σ is finite and non-negative, so Normal::new succeeds");

    tokio::spawn(async move {
        let mut rng = ChaCha8Rng::seed_from_u64(start.noise_seed);
        rng.set_word_pos(start.noise_word_pos);

        loop {
            let reply_to_console = tokio::select! {
                request = reading_rx.recv() => {
                    request.expect("atg_probe: console request channel closed")
                }
                request = state_rx.recv() => {
                    request
                        .expect("atg_probe: orchestrator state channel closed")
                        .send(AtgProbeState {
                            noise_seed: start.noise_seed,
                            noise_word_pos: rng.get_word_pos(),
                        })
                        .expect("atg_probe: orchestrator dropped the state reply");
                    continue;
                }
            };

            // Forward a fresh sounding request to the fuel tank and await it.
            let (sounding_reply_tx, sounding_reply_rx) = oneshot::channel();
            sounding_tx
                .send(sounding_reply_tx)
                .await
                .expect("atg_probe: fuel tank sounding channel closed");
            let sounding = sounding_reply_rx
                .await
                .expect("atg_probe: fuel tank dropped the sounding reply");

            let level_mm = if noise_enabled {
                sounding.level_mm + normal.sample(&mut rng)
            } else {
                sounding.level_mm
            };

            reply_to_console
                .send(ProbeReading {
                    level_mm,
                    temperature_c: sounding.temperature_c,
                })
                .expect("atg_probe: console dropped the reading reply");
        }
    })
}
