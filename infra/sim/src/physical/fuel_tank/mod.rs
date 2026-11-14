//! Fuel tank physical model.
//!
//! The public interface is defined directly in this file per the module
//! convention in sim-impl.md; the private implementation lives in
//! `internals` and is not visible to other modules.
//!
//! Owns: `TankSounding`, `OutflowRate`, `InterlockCircuit`, `InterlockPlug`,
//! `FuelTankState`, `FuelTankRows`, `FuelTankReport`.

mod internals;

use serde::{Deserialize, Serialize};
use tokio::sync::{mpsc, oneshot, watch};

use crate::appliances::dispenser::DispenserControlState;
use crate::clock::SimClock;

/// Outflow rate from the fuel tank, in litres per minute. `None` means no
/// outflow is occurring; `Some(rate_l_per_min)` carries the active rate.
/// Published by the fuel tank and consumed by the dispenser PD flow meter.
pub type OutflowRate = Option<f64>;

/// Level and temperature signal returned to the ATG probe in response to an
/// on-demand sounding request. Computed lazily at the moment of request.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct TankSounding {
    /// Fuel level in millimetres. Derived: `volume_l / k_l_per_mm`.
    pub level_mm: f64,
    /// Fuel temperature in degrees Celsius.
    pub temperature_c: f64,
}

/// State of the overfill interlock circuit at the fill cabinet socket, as
/// sensed by a plugged-in tanker's pump controller. The circuit latches Dead:
/// once broken for a given lease it is never driven back to Live. The fill
/// terminates and any subsequent fill re-plugs for a fresh lease, seeded from
/// the state at that later plug-in.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum InterlockCircuit {
    /// The circuit is healthy: the tank is below the overfill threshold and the
    /// ESD is not pressed. The pump controller may run the tanker pump.
    Live,
    /// The circuit is broken: the tank is at or above
    /// `overfill_interlock_pct / 100 × tank_capacity_l`, or the ESD is pressed. The pump controller cuts the tanker pump. Once a
    /// lease reaches this state it remains Dead until the lease ends.
    Dead,
}

/// A request to establish the overfill interlock circuit, sent by the road
/// tanker driver when it plugs the overfill cable into the fill cabinet socket.
/// The tank replies on `reply` with a receiver carrying the live
/// `InterlockCircuit` state, seeded with the value at the instant of plugging
/// in. The circuit exists only while the driver holds the receiver; dropping it
/// models unplugging and ends the lease.
#[derive(Debug)]
pub struct InterlockPlug {
    /// Reply channel for the leased interlock-state receiver.
    pub reply: oneshot::Sender<watch::Receiver<InterlockCircuit>>,
}

/// The physical model's own state variables (sim.md §1.2), saved at a stop.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct FuelTankState {
    pub volume_l: f64,
    pub temperature_c: f64,
    pub bund_wet: bool,
}

/// The actor-owned and appliance-owned rows of sim.md §1.2, as the tank last
/// read them. The sim is at rest when every one is false, and so no flow is
/// active.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct FuelTankRows {
    pub esd_active: bool,
    pub tanker_pump_running: bool,
    pub interlock_plug_connected: bool,
    pub dispenser_solenoid_open: bool,
    pub dispenser_pump_energised: bool,
}

impl FuelTankRows {
    pub fn at_rest(&self) -> bool {
        *self == FuelTankRows::default()
    }
}

/// The tank's reply to a state request: its state brought up to the instant
/// of the request, and the rows it read then.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct FuelTankReport {
    pub state: FuelTankState,
    pub rows: FuelTankRows,
}

/// Spawn the fuel tank physical model task.
///
/// Waits concurrently on any inbound watch-channel change, sounding requests,
/// interlock plug-in requests, and a self-scheduled wake at the next physical
/// boundary crossing (the overfill threshold, the overflow point, or empty).
/// `inflow_active` is `tanker_pump_running`; the overfill interlock is
/// published as an `InterlockCircuit` lease to the road tanker driver. Reaching
/// the overflow point latches `bund_wet` (it starts dry and is never written
/// back to dry — the escaped fuel remains in the bund until drained, and no
/// drying mechanism exists yet); reaching empty while outflow is active panics
/// (cavitation). See sim-impl.md.
///
/// The tank starts from `start`: the configured initial values on a cold
/// start, the checkpoint's on a warm one. It runs for the life of the sim,
/// through a stop, and answers each request on `state_rx` with a
/// `FuelTankReport`.
#[allow(clippy::too_many_arguments)]
pub fn spawn(
    clock: SimClock,
    tank_capacity_l: f64,
    // The overfill interlock level as a percentage of `tank_capacity_l`
    // (`process.overfill_interlock_pct`, sim.md §1.3).
    overfill_interlock_pct: f64,
    k_l_per_mm: f64,
    inflow_rate_l_per_min: f64,
    outflow_rate_l_per_min: f64,
    start: FuelTankState,
    state_rx: mpsc::Receiver<oneshot::Sender<FuelTankReport>>,
    sounding_rx: mpsc::Receiver<oneshot::Sender<TankSounding>>,
    bund_wet_tx: watch::Sender<bool>,
    interlock_plug_rx: mpsc::Receiver<InterlockPlug>,
    outflow_rate_tx: watch::Sender<OutflowRate>,
    dispenser_ctrl_rx: watch::Receiver<DispenserControlState>,
    esd_rx: watch::Receiver<bool>,
    tanker_pump_rx: watch::Receiver<bool>,
) -> tokio::task::JoinHandle<()> {
    internals::spawn(
        clock,
        internals::Config {
            tank_capacity_l,
            overfill_interlock_pct,
            k_l_per_mm,
            inflow_rate_l_per_min,
            outflow_rate_l_per_min,
        },
        start,
        state_rx,
        sounding_rx,
        bund_wet_tx,
        interlock_plug_rx,
        outflow_rate_tx,
        dispenser_ctrl_rx,
        esd_rx,
        tanker_pump_rx,
    )
}
