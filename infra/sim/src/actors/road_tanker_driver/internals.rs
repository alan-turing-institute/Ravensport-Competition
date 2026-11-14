//! Private implementation of the road tanker driver actor. Not visible to
//! other modules.
//!
//! A single `async fn` whose sequential body encodes the driver's causal
//! behaviour: forward each delivery to the dock operator, wait for the
//! go-ahead, then act as the tanker's pump controller — plug in to establish
//! the overfill interlock circuit, run the pump only while that circuit is
//! live, sign and hand over the delivery ticket, then signal the fuel supplier
//! that the delivery has ended. The loop processes one delivery to completion
//! before the next. A closed inter-actor channel (or a dropped completion
//! receiver) is fatal (sim-impl.md error handling).

use std::time::Duration;

use tokio::sync::{mpsc, oneshot, watch};

use super::{DeliveryTicket, RoadTankerDriverState};
use crate::actors::dock_operator::DockJob;
use crate::actors::fuel_supplier::FillJob;
use crate::clock::SimClock;
use crate::physical::fuel_tank::{InterlockCircuit, InterlockPlug};
use crate::stop::{rest, StopRequest};

const ENTITY: &str = "road_tanker_driver";

/// Fuel grade delivered, from world.yaml's `process.grade` (narrative §1).
const FUEL_GRADE: &str = crate::constants::FUEL_GRADE;

/// Reference density of the grade at 15 °C, in kg/L (sim.md §1.1
/// `rho_ref_kg_per_L`), from world.yaml's `process.density_kg_per_l` — the
/// value the FMS also receives as `FMS_DENSITY_KG_PER_L`. The tanker delivers
/// at the reference temperature, so the ticket's temperature-corrected volume
/// equals its gross volume.
const DENSITY_KG_PER_L: f64 = crate::constants::DENSITY_KG_PER_L;

/// The temperature of the product the tanker arrives with, in °C — the driver's
/// own constant, measured by the tanker's sealed meter (sim.md §8.4.1). Never
/// taken from the shore tank: the independence of the two measurements is what
/// gives the ±1% check force on a fill. Equal to the 15 °C reference, so the
/// correction is identity (sim.md §1.3).
const TANKER_PRODUCT_TEMPERATURE_C: f64 = 15.0;

#[allow(clippy::too_many_arguments)]
pub(super) fn spawn(
    clock: SimClock,
    start: RoadTankerDriverState,
    mut stop: StopRequest,
    report: oneshot::Sender<RoadTankerDriverState>,
    pump_rate_l_per_min: f64,
    driver_id: String,
    tanker_registration: String,
    mut arrival_rx: mpsc::Receiver<FillJob>,
    dock_job_tx: mpsc::Sender<DockJob>,
    tanker_pump_tx: watch::Sender<bool>,
    interlock_plug_tx: mpsc::Sender<InterlockPlug>,
    ticket_tx: mpsc::Sender<DeliveryTicket>,
) -> tokio::task::JoinHandle<()> {
    tokio::spawn(async move {
        // The tanker's own meter is monotonic across deliveries; the ticket
        // records absolute start/end readings.
        // The start state's meter reading and first ticket number on a cold
        // start, or the checkpoint's on a warm start (Start::cold, Start::warm).
        let RoadTankerDriverState {
            mut meter_totaliser_l,
            mut ticket_seq,
        } = start;

        loop {
            let FillJob {
                delivery_volume_l,
                completed,
            } = tokio::select! {
                biased;
                _ = stop.requested() => break,
                job = arrival_rx.recv() => {
                    job.expect("road_tanker_driver: fuel supplier arrival channel closed")
                }
            };

            // Forward the volume to the dock operator and wait for the operator
            // to confirm the job is active in the FMS. The supplier's completion
            // oneshot stays with the driver, to be fired once the delivery ends.
            let (proceed_tx, proceed_rx) = oneshot::channel();
            dock_job_tx
                .send(DockJob::Fill(
                    delivery_volume_l,
                    tanker_registration.clone(),
                    proceed_tx,
                ))
                .await
                .expect("road_tanker_driver: dock operator job channel closed");
            proceed_rx
                .await
                .expect("road_tanker_driver: dock operator dropped the proceed signal");

            // Plug the overfill cable into the fill cabinet socket, establishing
            // the interlock circuit. The tank replies with the live circuit
            // state; the driver holds the receiver for the fill.
            let (reply_tx, reply_rx) = oneshot::channel();
            interlock_plug_tx
                .send(InterlockPlug { reply: reply_tx })
                .await
                .expect("road_tanker_driver: interlock plug channel closed");
            let mut circuit = reply_rx
                .await
                .expect("road_tanker_driver: fuel tank dropped the interlock reply");

            // The start of the transfer, written onto the ticket at completion
            // rather than read again there (sim.md §8.3). Taken before the
            // circuit check, so a pump that never starts still records the
            // instant of the attempted start and the ticket falls in a window.
            let delivery_commenced_at = clock.now_datetime();

            // Pump controller: run the pump only while the circuit is live.
            let delivered_l = if *circuit.borrow() == InterlockCircuit::Dead {
                // Already tripped (tank at or above the overfill interlock
                // level, or ESD): the pump
                // controller never starts the pump.
                0.0
            } else {
                let pump_start = clock.now();
                tanker_pump_tx
                    .send(true)
                    .expect("road_tanker_driver: tanker pump channel closed");

                let full_duration =
                    Duration::from_secs_f64(delivery_volume_l / pump_rate_l_per_min * 60.0);
                let delivered_l = tokio::select! {
                    _ = clock.sleep(full_duration) => delivery_volume_l,
                    res = circuit.changed() => {
                        res.expect("road_tanker_driver: fuel tank interlock channel closed");
                        // The circuit went dead: the pump controller cuts the
                        // pump. Record the volume actually metered so far.
                        let elapsed_min =
                            clock.now().duration_since(pump_start).as_secs_f64() / 60.0;
                        (pump_rate_l_per_min * elapsed_min).min(delivery_volume_l)
                    }
                };

                tanker_pump_tx
                    .send(false)
                    .expect("road_tanker_driver: tanker pump channel closed");
                delivered_l
            };

            // Unplug: dropping the receiver ends the interlock lease.
            drop(circuit);

            // Generate and sign the delivery ticket for the volume actually
            // delivered; hand it to the operator.
            let start_totaliser_l = meter_totaliser_l;
            let end_totaliser_l = meter_totaliser_l + delivered_l;
            meter_totaliser_l = end_totaliser_l;
            let ticket = DeliveryTicket {
                ticket_number: format!("TICKET-{ticket_seq:06}"),
                delivery_commenced_at,
                grade: FUEL_GRADE.to_string(),
                gross_volume_l: delivered_l,
                temperature_c: TANKER_PRODUCT_TEMPERATURE_C,
                temp_corrected_volume_l: delivered_l,
                density_kg_per_l: DENSITY_KG_PER_L,
                start_totaliser_l,
                end_totaliser_l,
                tanker_registration: tanker_registration.clone(),
                driver_id: driver_id.clone(),
                signed_by_driver: true,
                signed_by_operator: false,
            };
            ticket_seq += 1;

            ticket_tx
                .send(ticket)
                .await
                .expect("road_tanker_driver: dock operator ticket channel closed");

            // The delivery has ended, whatever its outcome; the signal ends the
            // fuel supplier's pending order.
            completed
                .send(())
                .expect("road_tanker_driver: fuel supplier dropped the completion receiver");
        }

        // A tanker that has arrived and not been taken up is held, not served:
        // its completion sender stays open, and the fuel supplier reports it as
        // due (sim-impl.md, "Coming to rest").
        let _waiting = arrival_rx.try_recv().ok();
        rest(
            ENTITY,
            report,
            RoadTankerDriverState {
                meter_totaliser_l,
                ticket_seq,
            },
        )
        .await
    })
}
