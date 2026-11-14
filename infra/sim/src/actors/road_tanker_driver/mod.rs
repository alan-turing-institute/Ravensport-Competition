//! Road tanker driver actor.
//!
//! The public interface is defined directly in this file per the module
//! convention in sim-impl.md; the private implementation lives in
//! `internals` and is not visible to other modules.
//!
//! Owns: `DeliveryTicket`, `RoadTankerDriverState`.

mod internals;

use serde::{Deserialize, Serialize};
use time::PrimitiveDateTime;
use tokio::sync::{mpsc, oneshot, watch};

use crate::actors::dock_operator::DockJob;
use crate::actors::fuel_supplier::FillJob;
use crate::clock::SimClock;
use crate::physical::fuel_tank::InterlockPlug;
use crate::stop::StopRequest;

/// Paper delivery ticket created by the road tanker driver on fill completion,
/// per narrative §2.1. The driver signs it and hands it to the dock operator,
/// who counter-signs and transcribes the fields into the FMS via the HMI.
///
/// Defined here ahead of the driver's own implementation because it is the
/// driver-owned paper artefact the dock operator and reconciliation user consume.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct DeliveryTicket {
    pub ticket_number: String,
    /// The instant the fill began — the tanker pump's start, not the ticket's
    /// creation, which is completion (sim.md §8.3). UTC (sim.md §2.1).
    #[serde(with = "crate::checkpoint::utc")]
    pub delivery_commenced_at: PrimitiveDateTime,
    pub grade: String,
    pub gross_volume_l: f64,
    /// The tanker's own product temperature at the transfer, in °C.
    pub temperature_c: f64,
    pub temp_corrected_volume_l: f64,
    pub density_kg_per_l: f64,
    /// Tanker meter totaliser reading at transfer start.
    pub start_totaliser_l: f64,
    /// Tanker meter totaliser reading at transfer end.
    pub end_totaliser_l: f64,
    pub tanker_registration: String,
    pub driver_id: String,
    pub signed_by_driver: bool,
    pub signed_by_operator: bool,
}

/// The road tanker driver's state at a stop (sim-impl.md, "State types").
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct RoadTankerDriverState {
    /// The tanker meter's totaliser, monotonic across deliveries.
    pub meter_totaliser_l: f64,
    /// The sequence number the next delivery ticket takes.
    pub ticket_seq: u32,
}

impl Default for RoadTankerDriverState {
    /// The cold-start state: a zero meter, and the first ticket next.
    fn default() -> Self {
        Self {
            meter_totaliser_l: 0.0,
            ticket_seq: 1,
        }
    }
}

/// Spawn the road tanker driver actor, which also models the tanker's pump
/// controller.
///
/// Runs until the sim is stopped, processing one delivery at a time. On each
/// `FillJob` it forwards a `DockJob::Fill(volume, tanker_registration,
/// proceed_tx)` to the dock operator
/// and awaits the proceed signal (fired once the HMI confirms the job is active
/// in the FMS). It then plugs in — sending an `InterlockPlug` and holding the
/// returned `InterlockCircuit` receiver. The pump runs only while that circuit
/// is `Live`: if `Live`, the pump starts and runs until either the delivery
/// volume is reached or the circuit goes `Dead` (an overfill trip at the
/// overfill interlock level, or the ESD). On a `Dead` cut the ticket records the actual metered volume
/// (`pump_rate_l_per_min × elapsed`); if the circuit is already `Dead` at
/// plug-in, the pump never starts and nothing is delivered. The driver then
/// stops the pump, unplugs (dropping the receiver), and generates and signs a
/// `DeliveryTicket` which it hands to the dock operator. Then, on every outcome,
/// it fires the `FillJob`'s `completed` oneshot, ending the fuel supplier's
/// pending order.
///
/// `pump_rate_l_per_min` is the sim's configured inflow rate (the tanker pump
/// is the tank's only inflow source), sourced from the single
/// `Config::inflow_rate_l_per_min` field so the driver's elapsed-time
/// calculation and the physical model agree by construction.
///
/// The tanker's meter and the next ticket's number start from `start`: on a
/// cold start the start state's meter reading and first ticket number
/// (sim-impl.md, "Start state"), on a warm start the checkpoint's. Each later
/// ticket takes the next number.
///
/// On a stop it finishes the delivery in progress and takes up no further
/// job. A `FillJob` waiting in the arrival channel is drained and held, not
/// taken up, so its completion sender stays open; the driver reports its meter
/// and ticket sequence on `report` and holds its channel ends until the
/// process exits.
#[allow(clippy::too_many_arguments)]
pub fn spawn(
    clock: SimClock,
    start: RoadTankerDriverState,
    stop: StopRequest,
    report: oneshot::Sender<RoadTankerDriverState>,
    pump_rate_l_per_min: f64,
    driver_id: String,
    tanker_registration: String,
    arrival_rx: mpsc::Receiver<FillJob>,
    dock_job_tx: mpsc::Sender<DockJob>,
    tanker_pump_tx: watch::Sender<bool>,
    interlock_plug_tx: mpsc::Sender<InterlockPlug>,
    ticket_tx: mpsc::Sender<DeliveryTicket>,
) -> tokio::task::JoinHandle<()> {
    internals::spawn(
        clock,
        start,
        stop,
        report,
        pump_rate_l_per_min,
        driver_id,
        tanker_registration,
        arrival_rx,
        dock_job_tx,
        tanker_pump_tx,
        interlock_plug_tx,
        ticket_tx,
    )
}
