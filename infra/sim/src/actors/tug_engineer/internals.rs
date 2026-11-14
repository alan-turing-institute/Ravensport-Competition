//! Private implementation of the tug engineer actor. Not visible to other
//! modules.
//!
//! A single `async fn` whose sequential body encodes the engineer's causal
//! behaviour: forward each arriving tug's job to the dock operator, wait for the
//! operator's go-ahead, present the vessel RFID card, co-sign the BDN, then
//! signal that the tug has left. If the FMS refuses the dispense, before the job
//! is active or at the start, the tug departs unserved and its departure is
//! signalled in the same way (sim-impl.md, "Refusals"). The loop serves one tug
//! to its departure before taking the next, matching the real-world
//! single-counterparty interaction.

use tokio::sync::{mpsc, oneshot};

use crate::actors::dock_operator::{DispenseHandoff, DockJob, JobAdmission, BDN};
use crate::physical::tug_arrival_generator::TugArrival;
use crate::serial::ReaderReadiness;
use crate::shared::RfidCardNumber;
use crate::stop::{rest, StopRequest};

const ENTITY: &str = "tug_engineer";

#[allow(clippy::too_many_arguments)]
pub(super) fn spawn(
    mut stop: StopRequest,
    report: oneshot::Sender<()>,
    mut job_rx: mpsc::Receiver<TugArrival>,
    dock_job_tx: mpsc::Sender<DockJob>,
    card_tx: mpsc::Sender<RfidCardNumber>,
    vessel_rfid_ready: ReaderReadiness,
    mut bdn_rx: mpsc::Receiver<DispenseHandoff>,
    bdn_tx: mpsc::Sender<BDN>,
) -> tokio::task::JoinHandle<()> {
    tokio::spawn(async move {
        loop {
            // No tug is taken up after a stop. The tugs left in the channel
            // are reported at the dock by the generator, and the channel is
            // held, with their departure signals, by `rest`'s wait.
            if stop.is_requested() {
                break;
            }
            // The departure signal stays with the engineer until the tug leaves.
            let TugArrival { job, departed } = tokio::select! {
                biased;
                _ = stop.requested() => break,
                arrival = job_rx.recv() => arrival.expect("tug_engineer: arrival job channel closed"),
            };

            // The card is carried on the job so the engineer presents the
            // correct vessel tag without a roster lookup.
            let card: RfidCardNumber = job.rfid_card;
            let vessel_id = job.vessel_id.0;
            let vessel_name = job.vessel_name;

            // Forward to the dock operator with a fresh proceed channel.
            let (proceed_tx, proceed_rx) = oneshot::channel();
            dock_job_tx
                .send(DockJob::Dispense(job, proceed_tx))
                .await
                .expect("tug_engineer: dock operator job channel closed");

            // Wait for the operator to report the FMS's answer to the create.
            let admission = proceed_rx
                .await
                .expect("tug_engineer: dock operator dropped the proceed signal");
            match admission {
                JobAdmission::Refused => {
                    tracing::info!(
                        vessel_id,
                        vessel_name,
                        "the FMS refused the dispense; the tug departs unserved"
                    );
                }
                JobAdmission::Active => {
                    // Present the vessel RFID card at the dispenser reader, but
                    // only once the FMS is subscribed to the reader — otherwise
                    // the one-shot card is broadcast to an unattached reader and
                    // lost on the wire.
                    vessel_rfid_ready.wait_ready().await;
                    card_tx
                        .send(card)
                        .await
                        .expect("tug_engineer: vessel RFID reader channel closed");

                    // Block for the outcome: the operator-signed BDN to co-sign
                    // and return, or notice that the FMS refused the start.
                    let handoff = bdn_rx
                        .recv()
                        .await
                        .expect("tug_engineer: dock operator BDN channel closed");
                    match handoff {
                        DispenseHandoff::Sign(mut bdn) => {
                            bdn.signed_by_engineer = true;
                            bdn_tx
                                .send(bdn)
                                .await
                                .expect("tug_engineer: dock operator co-signed-BDN channel closed");
                        }
                        DispenseHandoff::Refused => {
                            tracing::info!(
                                vessel_id,
                                vessel_name,
                                "the FMS refused the dispense start; the tug departs unserved"
                            );
                        }
                    }
                }
            }

            // The tug leaves the dock: with both signatures on the BDN
            // (narrative §2.2), or unserved where the FMS refused the job. Its
            // next time away starts on this signal.
            departed.send(()).unwrap_or_else(|()| {
                panic!(
                    "tug_engineer: the tug arrival generator dropped the departure \
                     receiver of vessel {vessel_id}"
                )
            });
        }

        // The arrival channel is moved into the wait, so that the tugs left in
        // it keep their departure signals open.
        let _held = job_rx;
        rest(ENTITY, report, ()).await
    })
}
