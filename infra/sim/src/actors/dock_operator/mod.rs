//! Dock operator actor.
//!
//! The public interface is defined directly in this file per the module
//! convention in sim-impl.md; the private implementation lives in
//! `internals` and is not visible to other modules.
//!
//! Owns: `DockJob`, `JobAdmission`, `DispenseHandoff`, `BDN`, `DockOperatorState`.

mod internals;

use serde::{Deserialize, Serialize};
use time::PrimitiveDateTime;
use tokio::sync::oneshot;

use crate::actors::tug_engineer::VesselId;
use crate::physical::tug_arrival_generator::DispenseJob;

/// Inbound job for the dock operator.
///
/// Both variants carry a oneshot sender the dock operator fires once the FMS
/// has answered the create, releasing the counterpart actor to proceed with its
/// physical actions. A fill is always admitted, so its oneshot carries no
/// value; a dispense may be refused, so its oneshot carries the FMS's
/// admission.
///
/// Defined here ahead of the dock operator's own implementation because it is
/// the dock-operator-owned job type the tug engineer and road tanker driver
/// forward onto the operator's job channel.
#[derive(Debug)]
pub enum DockJob {
    /// A tug refuelling, with the dispense job to action.
    Dispense(DispenseJob, oneshot::Sender<JobAdmission>),
    /// A tanker delivery, with the delivery volume in litres and the arriving
    /// tanker's registration. The driver keeps the fuel supplier's completion
    /// oneshot, so only these are forwarded.
    Fill(f64, String, oneshot::Sender<()>),
}

/// Whether the FMS admitted a submitted dispense job. `Refused` means the tug
/// departs unserved: the engineer presents no vessel card and no BDN follows.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum JobAdmission {
    Active,
    Refused,
}

/// The outcome of a dispense, handed from the dock operator to the tug engineer:
/// a signed BDN to co-sign, or notice that the FMS refused the dispense after the
/// job was already active, in which case the tug departs unserved.
// One handoff passes per dispense, so the size of `Sign` costs nothing, and the
// variant carries the `BDN` itself, as sim-impl.md declares it.
#[allow(clippy::large_enum_variant)]
#[derive(Debug, Clone, PartialEq)]
pub enum DispenseHandoff {
    Sign(BDN),
    Refused,
}

/// Bunker Delivery Note (BDN), per narrative §2.2 and the ten items of MARPOL
/// Annex VI Appendix V (sim.md §8.2; item numbers in brackets below).
///
/// Created and signed by the dock operator on dispense completion, passed to
/// the tug engineer for co-signing, then transcribed into the FMS by its
/// number.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct BDN {
    pub bdn_number: String,
    /// Instant the dispense began, recorded at the pump start and carried
    /// forward to completion, when the note is written — not read at
    /// completion (sim.md §8.3). UTC (sim.md §2.1). [3]
    #[serde(with = "crate::checkpoint::utc")]
    pub delivery_commenced_at: PrimitiveDateTime,
    /// IMO number of the vessel, as validated by the FMS against the RFID card. [1]
    pub vessel_id: VesselId,
    /// Name of the vessel. Appendix V requires both the name and the IMO
    /// number of the receiving ship. [1]
    #[serde(deserialize_with = "crate::checkpoint::static_str::deserialize")]
    pub vessel_name: crate::checkpoint::StaticStr,
    /// Port of delivery. [2]
    #[serde(deserialize_with = "crate::checkpoint::static_str::deserialize")]
    pub port: crate::checkpoint::StaticStr,
    /// The supplier is the port: the Ravensport Harbour Authority, whose
    /// representative is the dock operator. Not the fuel supplier actor, which
    /// supplies the port (sim.md §8.2). [4]
    #[serde(deserialize_with = "crate::checkpoint::static_str::deserialize")]
    pub supplier_name: crate::checkpoint::StaticStr,
    #[serde(deserialize_with = "crate::checkpoint::static_str::deserialize")]
    pub supplier_address: crate::checkpoint::StaticStr,
    #[serde(deserialize_with = "crate::checkpoint::static_str::deserialize")]
    pub supplier_telephone: crate::checkpoint::StaticStr,
    /// Product name. [5]
    pub grade: String,
    pub volume_l: f64,
    /// Fuel temperature at the transfer, in °C, read from the HMI. Not an
    /// Appendix V item; carried so the corrected volume is checkable against
    /// its inputs (sim.md §8.4).
    pub temperature_c: f64,
    /// `volume_l` corrected to 15 °C (sim.md §8.4).
    pub temp_corrected_volume_l: f64,
    /// `temp_corrected_volume_l` × density at 15 °C, in tonnes: the mass in
    /// vacuum. [6]
    pub quantity_t: f64,
    /// Density at 15 °C, in kg/m³. A property of the grade, not a
    /// measurement. [7]
    pub density_kg_per_m3: f64,
    /// Sulphur content, % m/m. [8]
    pub sulphur_pct_mm: f64,
    /// Flashpoint in °C; a chosen value, not a measurement. [9]
    pub flashpoint_c: f64,
    /// The declaration that the fuel conforms with regulation 14.1, the global
    /// 0.50% m/m sulphur limit. [10]
    pub sulphur_limit_declared: bool,
    /// Dispenser meter totaliser reading at transfer start.
    pub start_totaliser_l: f64,
    /// Dispenser meter totaliser reading at transfer end.
    pub end_totaliser_l: f64,
    /// The dock operator's username, the `operator` on the job's log events.
    pub operator_id: String,
    pub engineer_id: String,
    /// The supplier's representative's signature on the declaration. [10]
    pub signed_by_operator: bool,
    pub signed_by_engineer: bool,
}

/// The dock operator's state at a stop (sim-impl.md, "State types").
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct DockOperatorState {
    /// The sequence number the next BDN takes.
    pub bdn_seq: u32,
}

impl Default for DockOperatorState {
    /// The cold-start state: the first BDN next.
    fn default() -> Self {
        Self { bdn_seq: 1 }
    }
}

use std::net::SocketAddr;

use tokio::sync::{mpsc, watch};

use crate::actors::reconciliation_user::ReconciliationRecord;
use crate::actors::road_tanker_driver::DeliveryTicket;
use crate::clock::SimClock;
use crate::serial::ReaderReadiness;
use crate::shared::RfidCardNumber;

/// Spawn the dock operator actor.
///
/// Establishes one authenticated HMI session at startup (two-phase login:
/// password then RFID card), then loops over `DockJob`s one at a time, branching
/// on job type. All digital interactions use the HMI HTTP API (digital.md §2.3),
/// source-bound to `local_addr`; `Authorization: Bearer <token>` is sent on
/// every request after login. The sim speaks plain HTTP to `hmi_addr`; TLS is
/// terminated at the HAProxy layer (sim-networking.md).
///
/// **Order of jobs.** Each job is completed before the next is taken
/// (narrative §2.2; `sim.md` §6). Before taking a job, the operator moves every
/// job already waiting on `job_rx` into two queues of its own, one for fills
/// and one for dispenses, then takes a waiting fill if there is one, otherwise
/// the oldest waiting dispense; with both queues empty it awaits `job_rx`. A
/// road tanker waiting to deliver is therefore handled before any waiting tug,
/// and tugs in the order in which they arrived.
///
/// **Dispense** (`DockJob::Dispense`): create the refuel job (`POST
/// /api/jobs/refuel`), answer the proceed oneshot with `JobAdmission::Active`
/// on the 201, poll `GET /api/jobs/current` until `rfid_validated`, set the
/// preset and start the pump (`POST …/dispenser/start`), recording that instant
/// as the BDN's commencement, poll `GET /api/jobs/current/dispenser/totaliser`
/// until `complete`, read the tank temperature (`GET /api/tanks/01/level`),
/// sign a `BDN` and send it as `DispenseHandoff::Sign` for the engineer to
/// co-sign, enter the BDN number (`PATCH /api/jobs/{id}`), and post the BDN to
/// the reconciliation user. The first BDN takes `start.bdn_seq`: on a cold
/// start the start state's number (sim-impl.md, "Start state"), on a warm start
/// the checkpoint's; each later BDN takes the next number. A create the FMS
/// refuses answers the oneshot with
/// `JobAdmission::Refused` and ends the job; a start it refuses is followed by
/// a cancel (`DELETE /api/jobs/{id}`) and `DispenseHandoff::Refused` in place
/// of the BDN (sim-impl.md, "Refusals").
///
/// **Fill** (`DockJob::Fill`): check ullage (`GET /api/tanks/01/level`), create
/// the fill job (`POST /api/jobs/tank-fill`) naming the arriving tanker, fire the proceed oneshot on the
/// 201, await the driver's `DeliveryTicket`, counter-sign it, transcribe it
/// (`POST /api/delivery-tickets`), and post the ticket to the reconciliation
/// user.
///
/// The dock operator begins no work of its own, so it takes no stop request:
/// it serves every `DockJob` it receives until the tug engineer and the road
/// tanker driver have both reported (`peers_at_rest`), then reports its BDN
/// sequence on `report` and holds its channel ends until the process exits.
#[allow(clippy::too_many_arguments)]
pub fn spawn(
    clock: SimClock,
    start: DockOperatorState,
    peers_at_rest: oneshot::Receiver<()>,
    report: oneshot::Sender<DockOperatorState>,
    job_rx: mpsc::Receiver<DockJob>,
    esd_tx: watch::Sender<bool>,
    operator_card_tx: mpsc::Sender<RfidCardNumber>,
    operator_rfid_ready: ReaderReadiness,
    ticket_rx: mpsc::Receiver<DeliveryTicket>,
    bdn_sign_tx: mpsc::Sender<DispenseHandoff>,
    bdn_signed_rx: mpsc::Receiver<BDN>,
    recon_tx: mpsc::UnboundedSender<ReconciliationRecord>,
    local_addr: SocketAddr,
    hmi_addr: SocketAddr,
) -> tokio::task::JoinHandle<()> {
    internals::spawn(
        clock,
        start,
        peers_at_rest,
        report,
        job_rx,
        esd_tx,
        operator_card_tx,
        operator_rfid_ready,
        ticket_rx,
        bdn_sign_tx,
        bdn_signed_rx,
        recon_tx,
        local_addr,
        hmi_addr,
    )
}
