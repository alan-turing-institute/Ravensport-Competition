//! Private implementation of the dispenser PD flow meter. Not visible to other
//! modules.

use std::time::Duration;

use tokio::sync::{mpsc, watch};

use super::Pulse;
use crate::clock::SimClock;
use crate::physical::fuel_tank::OutflowRate;

pub(super) fn spawn(
    clock: SimClock,
    k_factor: f64,
    mut outflow_rate_rx: watch::Receiver<OutflowRate>,
    pulse_tx: mpsc::Sender<Pulse>,
) -> tokio::task::JoinHandle<()> {
    tokio::spawn(async move {
        loop {
            let Some(rate_l_per_min) = (*outflow_rate_rx.borrow()).filter(|r| *r > 0.0) else {
                // No outflow: block until the rate changes.
                outflow_rate_rx
                    .changed()
                    .await
                    .expect("flow_meter: fuel tank outflow-rate channel closed");
                continue;
            };

            // Outflow has just turned on. Anchor the start instant and count
            // pulses against *elapsed flow time*, not against a per-pulse sleep:
            // a real timer always fires a little late, and counting per sleep
            // would let that lateness accumulate into unbounded drift. Instead,
            // each wake emits however many pulses the elapsed time now implies,
            // catching up after a late wake. The pulse total therefore tracks
            // the volume the tank has actually displaced, to within one pulse,
            // regardless of clock mode or multiplier.
            let start = clock.now();
            let interval = Duration::from_secs_f64(60.0 / (k_factor * rate_l_per_min));
            let mut emitted: u64 = 0;
            loop {
                let rate_changed = tokio::select! {
                    () = clock.sleep(interval) => false,
                    changed = outflow_rate_rx.changed() => {
                        changed.expect("flow_meter: fuel tank outflow-rate channel closed");
                        true
                    }
                };

                // Pulses the elapsed flow time implies: volume × k = (rate ×
                // elapsed) × k. Emit any not yet sent to catch up to it.
                let elapsed_min = clock.now().duration_since(start).as_secs_f64() / 60.0;
                let target = (rate_l_per_min * elapsed_min * k_factor).floor() as u64;
                while emitted < target {
                    pulse_tx
                        .send(Pulse { k_factor })
                        .await
                        .expect("flow_meter: dispenser pulse channel closed");
                    emitted += 1;
                }

                if rate_changed {
                    // The rate changed (possibly to zero): re-evaluate it in the
                    // outer loop, re-anchoring if outflow is still occurring.
                    break;
                }
            }
        }
    })
}
