//! Integration tests for `sim::actors::tug_engineer`.
//!
//! Exercises the actor through its public `spawn()` interface only, holding the
//! channel ends neighbouring entities own (see sim-tests.md). The tug engineer
//! is purely event-driven, so no clock is required — plain `#[tokio::test]`s.
//!
//! The test holds: `job_tx` (tug arrival generator role — sends `TugArrival`,
//! keeping each arrival's `departed` receiver), `dock_job_rx` (dock operator
//! role — receives `DockJob::Dispense` and fires its proceed oneshot), `card_rx`
//! (vessel RFID reader role), `bdn_to_engineer_tx` (operator sending the
//! operator-signed BDN in), and `bdn_from_engineer_rx` (operator receiving the
//! co-signed BDN back).

use std::time::Duration;

use time::macros::datetime;
use tokio::sync::oneshot::error::TryRecvError;
use tokio::sync::{mpsc, oneshot};
use tokio::task::JoinHandle;

use sim::actors::dock_operator::{DispenseHandoff, DockJob, JobAdmission, BDN};
use sim::actors::tug_engineer::{self, VesselId};
use sim::physical::tug_arrival_generator::{DispenseJob, TugArrival};
use sim::serial::ReaderReadiness;
use sim::shared::RfidCardNumber;
use sim::stop::{stop_channel, StopSender};

const TEST_CARD: RfidCardNumber = RfidCardNumber(99999);

fn test_job() -> DispenseJob {
    DispenseJob {
        vessel_id: VesselId(1999997),
        arrived_at: datetime!(2025-01-01 0:00),
        vessel_name: "Tug Alpha",
        volume_l: 500.0,
        rfid_card: TEST_CARD,
    }
}

/// The second vessel, where a test needs one (sim-tests.md).
fn second_job() -> DispenseJob {
    DispenseJob {
        vessel_id: VesselId(1999985),
        arrived_at: datetime!(2025-01-01 0:05),
        vessel_name: "Tug Bravo",
        volume_l: 500.0,
        rfid_card: RfidCardNumber(99998),
    }
}

/// `job` as the tug arrival generator sends it, and the `departed` receiver the
/// generator keeps.
fn arrival(job: DispenseJob) -> (TugArrival, oneshot::Receiver<()>) {
    let (departed, departed_rx) = oneshot::channel();
    (TugArrival { job, departed }, departed_rx)
}

/// A signed BDN as the dock operator would hand it to the engineer for
/// co-signing.
fn operator_signed_bdn() -> BDN {
    BDN {
        bdn_number: "BDN-0001".to_string(),
        delivery_commenced_at: datetime!(2025-01-01 0:00),
        vessel_id: VesselId(1999997),
        vessel_name: "Tug Alpha",
        port: "Ravensport",
        supplier_name: "Ravensport Harbour Authority",
        supplier_address: "Fuel Dock, East Quay, Ravensport RV1 4QD",
        supplier_telephone: "01632 960482",
        grade: "MGO".to_string(),
        volume_l: 500.0,
        temperature_c: 15.0,
        temp_corrected_volume_l: 500.0,
        quantity_t: 0.42,
        density_kg_per_m3: 840.0,
        sulphur_pct_mm: 0.10,
        flashpoint_c: 66.0,
        sulphur_limit_declared: true,
        start_totaliser_l: 1000.0,
        end_totaliser_l: 1500.0,
        operator_id: "jrenard".to_string(),
        engineer_id: "engineer-1".to_string(),
        signed_by_operator: true,
        signed_by_engineer: false,
    }
}

/// Counterpart channel ends held by the test, and the engineer's task.
struct Harness {
    job_tx: mpsc::Sender<TugArrival>,
    dock_job_rx: mpsc::Receiver<DockJob>,
    card_rx: mpsc::Receiver<RfidCardNumber>,
    bdn_to_engineer_tx: mpsc::Sender<DispenseHandoff>,
    bdn_from_engineer_rx: mpsc::Receiver<BDN>,
    stop: StopSender,
    report: oneshot::Receiver<()>,
    engineer: JoinHandle<()>,
}

fn spawn_engineer() -> Harness {
    let (job_tx, job_rx) = mpsc::channel(16);
    let (dock_job_tx, dock_job_rx) = mpsc::channel(16);
    let (card_tx, card_rx) = mpsc::channel(16);
    let (bdn_to_engineer_tx, bdn_rx) = mpsc::channel(16);
    let (bdn_tx, bdn_from_engineer_rx) = mpsc::channel(16);
    let (stop, stop_rx) = stop_channel();
    let (report_tx, report) = oneshot::channel();
    let engineer = tug_engineer::spawn(
        stop_rx,
        report_tx,
        job_rx,
        dock_job_tx,
        card_tx,
        ReaderReadiness::always_ready(),
        bdn_rx,
        bdn_tx,
    );
    Harness {
        job_tx,
        dock_job_rx,
        card_rx,
        bdn_to_engineer_tx,
        bdn_from_engineer_rx,
        stop,
        report,
        engineer,
    }
}

/// Reach `DispenseJobForwarded`: the test arrival has been forwarded as
/// `DockJob::Dispense`; the actor is blocking on the proceed oneshot. Returns
/// the retained proceed sender and the arrival's `departed` receiver.
async fn forward_job(
    harness: &mut Harness,
) -> (oneshot::Sender<JobAdmission>, oneshot::Receiver<()>) {
    let (tug, departed_rx) = arrival(test_job());
    harness.job_tx.send(tug).await.unwrap();
    let DockJob::Dispense(_, proceed_tx) = harness.dock_job_rx.recv().await.unwrap() else {
        panic!("expected DockJob::Dispense");
    };
    (proceed_tx, departed_rx)
}

/// Reach `ProceedReceived`: proceed has been fired and the vessel card drained.
/// Returns the arrival's `departed` receiver.
async fn reach_proceed_received(harness: &mut Harness) -> oneshot::Receiver<()> {
    let (proceed_tx, departed_rx) = forward_job(harness).await;
    proceed_tx.send(JobAdmission::Active).unwrap();
    harness.card_rx.recv().await.unwrap();
    departed_rx
}

#[tokio::test]
async fn dispense_job_forwarded_to_dock_operator_on_arrival() {
    let mut harness = spawn_engineer();
    let (tug, _departed_rx) = arrival(test_job());
    harness.job_tx.send(tug).await.unwrap();

    let DockJob::Dispense(job, _proceed_tx) = harness.dock_job_rx.recv().await.unwrap() else {
        panic!("expected DockJob::Dispense");
    };
    assert_eq!(job.vessel_id, VesselId(1999997));
    assert_eq!(job.volume_l, 500.0);
}

#[tokio::test]
async fn rfid_card_not_sent_before_proceed() {
    let mut harness = spawn_engineer();
    let (_proceed_tx, _departed_rx) = forward_job(&mut harness).await;

    // Give the actor a chance to run; it must be blocked on the proceed signal.
    tokio::task::yield_now().await;
    assert!(
        harness.card_rx.try_recv().is_err(),
        "card must not be presented before the operator confirms the job"
    );
}

#[tokio::test]
async fn rfid_card_sent_after_proceed() {
    let mut harness = spawn_engineer();
    let (proceed_tx, _departed_rx) = forward_job(&mut harness).await;

    proceed_tx.send(JobAdmission::Active).unwrap();
    let card = harness.card_rx.recv().await;
    assert!(card.is_some());
}

#[tokio::test]
async fn rfid_card_matches_vessel_on_job() {
    let mut harness = spawn_engineer();
    let (proceed_tx, _departed_rx) = forward_job(&mut harness).await;

    proceed_tx.send(JobAdmission::Active).unwrap();
    let card = harness.card_rx.recv().await.unwrap();
    assert_eq!(card, TEST_CARD);
}

#[tokio::test]
async fn bdn_cosigned_and_returned_after_receipt() {
    let mut harness = spawn_engineer();
    let _departed_rx = reach_proceed_received(&mut harness).await;

    harness
        .bdn_to_engineer_tx
        .send(DispenseHandoff::Sign(operator_signed_bdn()))
        .await
        .unwrap();
    let returned = harness.bdn_from_engineer_rx.recv().await.unwrap();
    assert!(returned.signed_by_engineer);
}

#[tokio::test]
async fn bdn_fields_preserved_on_cosigning() {
    let mut harness = spawn_engineer();
    let _departed_rx = reach_proceed_received(&mut harness).await;

    let sent = operator_signed_bdn();
    harness
        .bdn_to_engineer_tx
        .send(DispenseHandoff::Sign(sent.clone()))
        .await
        .unwrap();
    let returned = harness.bdn_from_engineer_rx.recv().await.unwrap();

    let mut expected = sent;
    expected.signed_by_engineer = true;
    assert_eq!(returned, expected);
}

/// The engineer serves one tug at a time, in the order they arrived, and
/// forwards the next tug's job only once the tug before it has left.
#[tokio::test]
async fn waiting_tugs_served_one_at_a_time_in_arrival_order() {
    let mut harness = spawn_engineer();

    // Two arrivals back to back: Tug Alpha, then Tug Bravo.
    let (alpha, mut alpha_departed_rx) = arrival(test_job());
    let (bravo, _bravo_departed_rx) = arrival(second_job());
    harness.job_tx.send(alpha).await.unwrap();
    harness.job_tx.send(bravo).await.unwrap();

    let DockJob::Dispense(first, proceed_tx) = harness.dock_job_rx.recv().await.unwrap() else {
        panic!("expected DockJob::Dispense");
    };
    assert_eq!(
        first.vessel_id,
        VesselId(1999997),
        "Tug Alpha arrived first"
    );
    proceed_tx.send(JobAdmission::Active).unwrap();
    harness.card_rx.recv().await.unwrap();

    // While Tug Alpha is served, Tug Bravo's job is not forwarded.
    tokio::task::yield_now().await;
    assert!(
        harness.dock_job_rx.try_recv().is_err(),
        "Tug Bravo's job was forwarded while Tug Alpha was being served"
    );

    harness
        .bdn_to_engineer_tx
        .send(DispenseHandoff::Sign(operator_signed_bdn()))
        .await
        .unwrap();
    harness.bdn_from_engineer_rx.recv().await.unwrap();

    let DockJob::Dispense(second, _proceed_tx) = harness.dock_job_rx.recv().await.unwrap() else {
        panic!("expected DockJob::Dispense");
    };
    assert_eq!(
        alpha_departed_rx.try_recv(),
        Ok(()),
        "Tug Bravo's job was forwarded before Tug Alpha had left"
    );
    assert_eq!(
        second.vessel_id,
        VesselId(1999985),
        "Tug Bravo arrived second"
    );
}

/// The tug leaves once both signatures are on the BDN (narrative §2.2), and
/// not before.
#[tokio::test]
async fn departure_signalled_after_cosigned_bdn_returned() {
    let mut harness = spawn_engineer();
    let mut departed_rx = reach_proceed_received(&mut harness).await;

    tokio::task::yield_now().await;
    assert_eq!(
        departed_rx.try_recv(),
        Err(TryRecvError::Empty),
        "the tug left before the BDN was co-signed"
    );

    harness
        .bdn_to_engineer_tx
        .send(DispenseHandoff::Sign(operator_signed_bdn()))
        .await
        .unwrap();
    harness.bdn_from_engineer_rx.recv().await.unwrap();
    departed_rx
        .await
        .expect("the tug did not leave after the co-signed BDN was returned");
}

/// The tug arrival generator that awaited the departure has stopped.
#[tokio::test]
async fn dropped_departure_receiver_is_fatal() {
    let mut harness = spawn_engineer();
    drop(reach_proceed_received(&mut harness).await);

    harness
        .bdn_to_engineer_tx
        .send(DispenseHandoff::Sign(operator_signed_bdn()))
        .await
        .unwrap();
    harness.bdn_from_engineer_rx.recv().await.unwrap();

    let outcome = tokio::time::timeout(Duration::from_secs(5), &mut harness.engineer)
        .await
        .expect("the engineer still runs after the departure receiver was dropped");
    let error = outcome.expect_err("the engineer returned rather than panicked");
    assert!(error.is_panic(), "the engineer was cancelled, not panicked");
    let payload = error.into_panic();
    let message = payload
        .downcast_ref::<String>()
        .map(String::as_str)
        .or_else(|| payload.downcast_ref::<&str>().copied())
        .unwrap_or("");
    assert!(
        message.contains("depart"),
        "the engineer panicked, but not on the dropped departure receiver: {message}"
    );
}

/// A tug the FMS refuses at the create departs unserved: no vessel card is
/// presented, and the tug leaves before the next arrival is served.
#[tokio::test]
async fn refused_job_departs_without_presenting_card() {
    let mut harness = spawn_engineer();
    // Two arrivals: Tug Alpha is refused before activation.
    let (alpha, mut alpha_departed_rx) = arrival(test_job());
    let (bravo, _bravo_departed_rx) = arrival(second_job());
    harness.job_tx.send(alpha).await.unwrap();
    harness.job_tx.send(bravo).await.unwrap();
    let DockJob::Dispense(_, proceed_tx) = harness.dock_job_rx.recv().await.unwrap() else {
        panic!("expected DockJob::Dispense");
    };
    proceed_tx.send(JobAdmission::Refused).unwrap();

    // The engineer advances to the next arrival without presenting a card.
    let DockJob::Dispense(second, _proceed_tx) = harness.dock_job_rx.recv().await.unwrap() else {
        panic!("expected DockJob::Dispense");
    };
    assert_eq!(
        second.vessel_id,
        VesselId(1999985),
        "Tug Bravo is served next"
    );
    assert!(
        harness.card_rx.try_recv().is_err(),
        "no vessel card is presented for a refused job"
    );
    assert_eq!(
        alpha_departed_rx.try_recv(),
        Ok(()),
        "the refused tug had not left when the next arrival was served"
    );
}

/// A tug whose dispense the FMS refuses at the start departs unserved: no
/// co-signed BDN is returned, and the tug leaves before the next arrival is
/// served.
#[tokio::test]
async fn refusal_after_activation_departs_without_cosigning() {
    let mut harness = spawn_engineer();
    let mut alpha_departed_rx = reach_proceed_received(&mut harness).await;
    // A second arrival waits behind the first.
    let (bravo, _bravo_departed_rx) = arrival(second_job());
    harness.job_tx.send(bravo).await.unwrap();

    harness
        .bdn_to_engineer_tx
        .send(DispenseHandoff::Refused)
        .await
        .unwrap();

    // The engineer advances to the next arrival and returns no co-signed BDN.
    let DockJob::Dispense(second, _proceed_tx) = harness.dock_job_rx.recv().await.unwrap() else {
        panic!("expected DockJob::Dispense");
    };
    assert_eq!(
        second.vessel_id,
        VesselId(1999985),
        "Tug Bravo is served next"
    );
    assert!(
        harness.bdn_from_engineer_rx.try_recv().is_err(),
        "no co-signed BDN is returned for a refused start"
    );
    assert_eq!(
        alpha_departed_rx.try_recv(),
        Ok(()),
        "the tug refused at its start had not left when the next arrival was served"
    );
}

// ── Stopping (sim-tests.md, "Stopping and warm start") ───────────────────────

#[tokio::test]
async fn engineer_takes_up_no_arrival_after_stop_and_keeps_its_departure_open() {
    let mut harness = spawn_engineer();
    harness.stop.stop();
    let (tug, mut departed_rx) = arrival(test_job());
    harness.job_tx.send(tug).await.unwrap();

    (&mut harness.report).await.expect("the engineer reported");
    assert!(
        harness.dock_job_rx.try_recv().is_err(),
        "a tug was taken up after the stop"
    );
    assert!(harness.card_rx.try_recv().is_err(), "a card was presented");
    // The generator reports the tug at the dock; its departure stays open.
    assert_eq!(departed_rx.try_recv(), Err(TryRecvError::Empty));
    assert!(
        !harness.engineer.is_finished(),
        "the engineer's task finished"
    );
}

#[tokio::test]
async fn engineer_finishes_the_tug_in_progress_before_report() {
    let mut harness = spawn_engineer();
    let mut departed_rx = reach_proceed_received(&mut harness).await;
    harness.stop.stop();
    tokio::task::yield_now().await;
    assert_eq!(
        harness.report.try_recv(),
        Err(TryRecvError::Empty),
        "the engineer reported with a refuel in progress"
    );

    harness
        .bdn_to_engineer_tx
        .send(DispenseHandoff::Sign(operator_signed_bdn()))
        .await
        .unwrap();
    let bdn = harness.bdn_from_engineer_rx.recv().await.unwrap();
    assert!(bdn.signed_by_engineer);
    (&mut departed_rx).await.expect("the tug left");
    (&mut harness.report).await.expect("the engineer reported");
}
