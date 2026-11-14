//! Private implementation of the fuel tank. Not visible to other modules.

use std::time::Duration;

use tokio::sync::{mpsc, oneshot, watch};

use crate::appliances::dispenser::DispenserControlState;
use crate::clock::{SimClock, SimInstant};

use super::{
    FuelTankReport, FuelTankRows, FuelTankState, InterlockCircuit, InterlockPlug, OutflowRate,
    TankSounding,
};

/// Litre tolerance for boundary comparisons. Absorbs floating-point error when
/// a scheduled wake lands the volume on a physical boundary.
const TOL_L: f64 = 1e-6;

/// Immutable physical configuration supplied at construction.
pub(super) struct Config {
    pub tank_capacity_l: f64,
    pub overfill_interlock_pct: f64,
    pub k_l_per_mm: f64,
    pub inflow_rate_l_per_min: f64,
    pub outflow_rate_l_per_min: f64,
}

/// Input state read from the inbound watch channels at each wake.
#[derive(Clone, Copy)]
struct Inputs {
    dispenser: DispenserControlState,
    esd: bool,
    tanker_pump: bool,
}

/// Why the tank woke.
enum Wake {
    /// An inbound watch channel changed; no payload.
    Input,
    /// A sounding request, carrying the reply channel.
    Sounding(oneshot::Sender<TankSounding>),
    /// A state request from the orchestrator, carrying the reply channel.
    State(oneshot::Sender<FuelTankReport>),
    /// The road tanker driver plugged in, carrying the lease request.
    Plug(InterlockPlug),
    /// A self-scheduled physical boundary crossing.
    Boundary,
    /// The driver dropped its interlock receiver (unplugged).
    Unplugged,
}

struct Tank {
    clock: SimClock,
    cfg: Config,
    volume_l: f64,
    temperature_c: f64,
    last_update: SimInstant,
    /// Flags in effect during the *current* interval; recomputed at the end of
    /// each `step` for the next interval.
    inflow_active: bool,
    outflow_active: bool,
    /// Secondary-containment bund wet state. Ground-truth physical state:
    /// starts dry and latches wet the first time fuel escapes (the volume
    /// reaches capacity). It is never cleared here — escaped fuel physically
    /// remains in the bund until it is drained, and no drying mechanism exists
    /// yet. When one is introduced it will clear this flag; until then nothing
    /// writes the bund dry.
    bund_wet: bool,
}

impl Tank {
    fn overfill_threshold_l(&self) -> f64 {
        self.cfg.overfill_interlock_pct / 100.0 * self.cfg.tank_capacity_l
    }

    /// Net rate in litres per minute using the flags in effect during the
    /// current interval. Positive fills, negative drains.
    fn net_rate_l_per_min(&self) -> f64 {
        let mut rate = 0.0;
        if self.inflow_active {
            rate += self.cfg.inflow_rate_l_per_min;
        }
        if self.outflow_active {
            rate -= self.cfg.outflow_rate_l_per_min;
        }
        rate
    }

    /// Integrate volume over the interval since the last update using the flow
    /// flags in effect during it, then recompute the derived flags from the
    /// current inputs for the next interval. Panics on cavitation.
    fn step(&mut self, inputs: Inputs) {
        let now = self.clock.now();
        let elapsed_min = now.duration_since(self.last_update).as_secs_f64() / 60.0;
        let net_l = self.net_rate_l_per_min() * elapsed_min;
        self.volume_l = (self.volume_l + net_l).clamp(0.0, self.cfg.tank_capacity_l);
        self.last_update = now;

        // Latch the bund wet once fuel escapes. The tolerance absorbs the
        // floating-point error when a scheduled wake lands the volume on the
        // capacity boundary (consistent with the empty and interlock-level
        // comparisons). Once set, it is never cleared: the escaped fuel remains
        // in the bund until drained, and no drying mechanism exists yet.
        if self.volume_l >= self.cfg.tank_capacity_l - TOL_L {
            self.bund_wet = true;
        }

        // Flags in effect for the next interval. Inflow is the tanker pump
        // running; the overfill interlock, ESD, and cable gate that pump in the
        // driver's pump controller, so they do not appear here.
        self.inflow_active = inputs.tanker_pump;
        self.outflow_active =
            inputs.dispenser.solenoid_open && inputs.dispenser.pump_energised && !inputs.esd;

        // The tank cannot sustain outflow while empty: the pump would draw air
        // and cavitate. The ~10% cavitation guard is FMS software only, so a
        // defeated guard reaches here. Fatal (sim-impl.md error taxonomy).
        if self.outflow_active && self.volume_l <= TOL_L {
            panic!(
                "fuel_tank: cavitation — pump drawing from an empty tank \
                 (volume {:.6} L at {} L/min outflow); the FMS cavitation guard \
                 has been defeated",
                self.volume_l, self.cfg.outflow_rate_l_per_min
            );
        }
    }

    /// Delay from now until the next boundary the tank must act on, given the
    /// net flow in effect. `None` when no flow is active, or when the flow is
    /// heading away from every remaining boundary.
    fn next_boundary_delay(&self, lease_active: bool) -> Option<Duration> {
        let net = self.net_rate_l_per_min();
        let threshold = self.overfill_threshold_l();
        let capacity = self.cfg.tank_capacity_l;
        let v = self.volume_l;

        if net > 0.0 {
            // Rising: the nearest boundary strictly above the current volume.
            // The interlock threshold matters only while a tanker is plugged in.
            let mut next = capacity;
            if lease_active && threshold > v + TOL_L {
                next = next.min(threshold);
            }
            if next > v + TOL_L {
                Some(Duration::from_secs_f64((next - v) / net * 60.0))
            } else {
                None
            }
        } else if net < 0.0 {
            // Falling: the nearest boundary strictly below the current volume.
            // Empty is always relevant (cavitation); the threshold only while
            // plugged in (the circuit going live again below it).
            let mut next = 0.0_f64;
            if lease_active && threshold < v - TOL_L {
                next = next.max(threshold);
            }
            if next < v - TOL_L {
                Some(Duration::from_secs_f64((v - next) / -net * 60.0))
            } else {
                None
            }
        } else {
            None
        }
    }

    fn interlock(&self, esd: bool) -> InterlockCircuit {
        if esd || self.volume_l >= self.overfill_threshold_l() - TOL_L {
            InterlockCircuit::Dead
        } else {
            InterlockCircuit::Live
        }
    }

    fn outflow_rate(&self) -> OutflowRate {
        if self.outflow_active {
            Some(self.cfg.outflow_rate_l_per_min)
        } else {
            None
        }
    }

    fn sounding(&self) -> TankSounding {
        TankSounding {
            level_mm: self.volume_l / self.cfg.k_l_per_mm,
            temperature_c: self.temperature_c,
        }
    }
}

#[allow(clippy::too_many_arguments)]
pub(super) fn spawn(
    clock: SimClock,
    cfg: Config,
    start: FuelTankState,
    mut state_rx: mpsc::Receiver<oneshot::Sender<FuelTankReport>>,
    mut sounding_rx: mpsc::Receiver<oneshot::Sender<TankSounding>>,
    bund_wet_tx: watch::Sender<bool>,
    mut interlock_plug_rx: mpsc::Receiver<InterlockPlug>,
    outflow_rate_tx: watch::Sender<OutflowRate>,
    mut dispenser_ctrl_rx: watch::Receiver<DispenserControlState>,
    mut esd_rx: watch::Receiver<bool>,
    mut tanker_pump_rx: watch::Receiver<bool>,
) -> tokio::task::JoinHandle<()> {
    let mut tank = Tank {
        last_update: clock.now(),
        volume_l: start.volume_l,
        temperature_c: start.temperature_c,
        inflow_active: false,
        outflow_active: false,
        bund_wet: start.bund_wet,
        clock,
        cfg,
    };

    // The overfill interlock circuit for the currently plugged-in tanker, if
    // any. `Some` only while the driver holds the matching receiver.
    let mut lease: Option<watch::Sender<InterlockCircuit>> = None;

    tokio::spawn(async move {
        loop {
            let sched_clock = tank.clock.clone();
            let boundary_delay = tank.next_boundary_delay(lease.is_some());

            // A closed input channel means an actor or instrument has died —
            // fatal, per the error-handling policy.
            let wake: Wake = tokio::select! {
                r = dispenser_ctrl_rx.changed() => {
                    r.expect("fuel_tank: dispenser control channel closed");
                    Wake::Input
                }
                r = esd_rx.changed() => {
                    r.expect("fuel_tank: ESD channel closed");
                    Wake::Input
                }
                r = tanker_pump_rx.changed() => {
                    r.expect("fuel_tank: tanker pump channel closed");
                    Wake::Input
                }
                maybe = sounding_rx.recv() => {
                    Wake::Sounding(maybe.expect("fuel_tank: ATG probe sounding channel closed"))
                }
                maybe = state_rx.recv() => {
                    Wake::State(maybe.expect("fuel_tank: orchestrator state channel closed"))
                }
                maybe = interlock_plug_rx.recv() => {
                    Wake::Plug(maybe.expect("fuel_tank: interlock plug channel closed"))
                }
                _ = async {
                    match boundary_delay {
                        Some(d) => sched_clock.sleep(d).await,
                        None => std::future::pending::<()>().await,
                    }
                } => Wake::Boundary,
                _ = async {
                    match &lease {
                        Some(tx) => tx.closed().await,
                        None => std::future::pending::<()>().await,
                    }
                } => Wake::Unplugged,
            };

            let inputs = Inputs {
                dispenser: *dispenser_ctrl_rx.borrow(),
                esd: *esd_rx.borrow(),
                tanker_pump: *tanker_pump_rx.borrow(),
            };
            tank.step(inputs);

            // Publish the bund state on change.
            bund_wet_tx.send_if_modified(|current| {
                let changed = *current != tank.bund_wet;
                *current = tank.bund_wet;
                changed
            });

            let new_outflow = tank.outflow_rate();
            outflow_rate_tx.send_if_modified(|current| {
                let changed = *current != new_outflow;
                *current = new_outflow;
                changed
            });

            match wake {
                Wake::Sounding(reply_tx) => {
                    reply_tx
                        .send(tank.sounding())
                        .expect("fuel_tank: ATG probe dropped the sounding reply");
                }
                Wake::Plug(plug) => {
                    // Establish the circuit, seeded with the state at plug-in.
                    // If the driver has already dropped the reply, no lease is
                    // held (it would immediately read as unplugged anyway).
                    let (tx, rx) = watch::channel(tank.interlock(inputs.esd));
                    if plug.reply.send(rx).is_ok() {
                        lease = Some(tx);
                    }
                }
                Wake::State(reply_tx) => {
                    let report = FuelTankReport {
                        state: FuelTankState {
                            volume_l: tank.volume_l,
                            temperature_c: tank.temperature_c,
                            bund_wet: tank.bund_wet,
                        },
                        rows: FuelTankRows {
                            esd_active: inputs.esd,
                            tanker_pump_running: inputs.tanker_pump,
                            // A driver that has unplugged but whose unplugging
                            // the tank has not yet woken for is not connected.
                            interlock_plug_connected: lease
                                .as_ref()
                                .is_some_and(|tx| !tx.is_closed()),
                            dispenser_solenoid_open: inputs.dispenser.solenoid_open,
                            dispenser_pump_energised: inputs.dispenser.pump_energised,
                        },
                    };
                    reply_tx
                        .send(report)
                        .expect("fuel_tank: orchestrator dropped the state reply");
                }
                Wake::Unplugged => lease = None,
                Wake::Input | Wake::Boundary => {}
            }

            // Keep the live lease (if any) current. The interlock latches Dead:
            // once the circuit breaks (overfill threshold reached, or ESD) it
            // stays broken for the life of the lease. The fill terminates and
            // any subsequent fill re-plugs for a fresh lease, so drive the lease
            // only towards Dead, never back to Live.
            if let Some(tx) = &lease {
                if tank.interlock(inputs.esd) == InterlockCircuit::Dead {
                    tx.send_if_modified(|current| {
                        let changed = *current != InterlockCircuit::Dead;
                        *current = InterlockCircuit::Dead;
                        changed
                    });
                }
            }
        }
    })
}
