//! Tug engineer actor.
//!
//! The public interface is defined directly in this file per the module
//! convention in sim-impl.md; the private implementation lives in
//! `internals` and is not visible to other modules.
//!
//! Owns: `VesselId`.

mod internals;

use serde::{Deserialize, Serialize};
use tokio::sync::{mpsc, oneshot};

use crate::actors::dock_operator::{DispenseHandoff, DockJob, BDN};
use crate::physical::tug_arrival_generator::TugArrival;
use crate::serial::ReaderReadiness;
use crate::shared::RfidCardNumber;
use crate::stop::StopRequest;

/// Identifies a vessel in the port roster by its seven-digit International
/// Maritime Organization (IMO) number, which is fixed to the hull for life and,
/// on the bunker delivery note, identifies the vessel alongside its name
/// (MARPOL Annex VI Appendix V).
///
/// Defined here ahead of the tug engineer's own implementation because it is
/// the tug-engineer-owned identifier the tug arrival generator and dock
/// operator embed in their records.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub struct VesselId(pub u32);

/// Spawn the tug engineer actor.
///
/// One actor acts for the engineer of whichever tug is being served, one tug at
/// a time. Tugs that arrive while another is served wait in the arrival
/// channel, which delivers them in the order they arrived (sim.md §6). On each
/// iteration it receives the next `TugArrival`, keeps its `departed` sender,
/// and forwards the job as `DockJob::Dispense(job, proceed_tx)` onto the dock
/// operator's job channel. It then awaits the proceed signal, which the dock
/// operator fires with the FMS's admission once the HMI has answered the
/// create. If the job is active, it waits for the FMS to be subscribed to the
/// vessel reader, presents the vessel RFID card to the dispenser reader via
/// `card_tx`, and blocks on `bdn_rx` for the outcome: an operator-signed `BDN`,
/// which it co-signs and returns on `bdn_tx`, or notice that the FMS refused
/// the start. A refusal at either point sends the tug away unserved, recorded
/// at INFO (sim-impl.md, "Refusals"). In every case the tug then leaves the
/// dock: the actor fires `departed` before taking the next arrival. A
/// `departed` receiver already dropped is fatal, since the tug arrival
/// generator has stopped.
///
/// On a stop it finishes serving the tug in progress, takes up no further
/// arrival, and reports on `report`. The tugs waiting in the arrival channel
/// are reported by the tug arrival generator as at the dock; the engineer holds
/// the channel, and with it their `departed` senders, until the process exits.
#[allow(clippy::too_many_arguments)]
pub fn spawn(
    stop: StopRequest,
    report: oneshot::Sender<()>,
    job_rx: mpsc::Receiver<TugArrival>,
    dock_job_tx: mpsc::Sender<DockJob>,
    card_tx: mpsc::Sender<RfidCardNumber>,
    vessel_rfid_ready: ReaderReadiness,
    bdn_rx: mpsc::Receiver<DispenseHandoff>,
    bdn_tx: mpsc::Sender<BDN>,
) -> tokio::task::JoinHandle<()> {
    internals::spawn(
        stop,
        report,
        job_rx,
        dock_job_tx,
        card_tx,
        vessel_rfid_ready,
        bdn_rx,
        bdn_tx,
    )
}
