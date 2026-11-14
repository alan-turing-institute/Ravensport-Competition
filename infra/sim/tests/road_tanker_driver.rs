//! Integration tests for `sim::actors::road_tanker_driver`.
//!
//! Exercises the actor through its public `spawn()` interface only, holding the
//! channel ends neighbouring entities own (see sim-tests.md). The test plays the
//! fuel tank: it services each `InterlockPlug` and holds the circuit sender so
//! it can drive the interlock `Dead` mid-fill. The driver runs the pump on a
//! `clock.sleep`, so the tests run under
//! `SimClock::isolated(TimeModel::Synthetic, …)` + `block_on`; the delivery
//! duration is reached by awaiting the next pump/ticket event, which parks the
//! runtime and lets Synthetic time auto-advance.

use std::time::Duration;

use time::macros::datetime;
use time::PrimitiveDateTime;
use tokio::sync::oneshot::error::TryRecvError;
use tokio::sync::{mpsc, oneshot, watch};

use sim::actors::dock_operator::DockJob;
use sim::actors::fuel_supplier::FillJob;
use sim::actors::road_tanker_driver::{self, DeliveryTicket, RoadTankerDriverState};
use sim::clock::{SimClock, TimeModel};
use sim::physical::fuel_tank::{InterlockCircuit, InterlockPlug};
use sim::stop::{stop_channel, StopSender};

const SIM_START: PrimitiveDateTime = datetime!(2025-01-01 0:00);

// Construction parameters (sim-tests.md).
const PUMP_RATE_L_PER_MIN: f64 = 600.0;
const DRIVER_ID: &str = "driver-01";
const TANKER_REGISTRATION: &str = "AB12 CDE";

// 3 000 L at 600 L/min is exactly 5 minutes of simulated time.
const TEST_VOLUME_L: f64 = 3000.0;

/// A test fill job and the completion receiver the supplier would hold. The
/// caller keeps the receiver alive so the driver's completion signal succeeds.
fn test_fill_job() -> (FillJob, oneshot::Receiver<()>) {
    let (completed, completed_rx) = oneshot::channel();
    (
        FillJob {
            delivery_volume_l: TEST_VOLUME_L,
            completed,
        },
        completed_rx,
    )
}

/// Counterpart channel ends held by the test.
struct Harness {
    arrival_tx: mpsc::Sender<FillJob>,
    dock_job_rx: mpsc::Receiver<DockJob>,
    tanker_pump_rx: watch::Receiver<bool>,
    interlock_plug_rx: mpsc::Receiver<InterlockPlug>,
    ticket_rx: mpsc::Receiver<DeliveryTicket>,
    stop: StopSender,
    report_rx: oneshot::Receiver<RoadTankerDriverState>,
}

fn spawn_driver(clock: SimClock) -> Harness {
    spawn_driver_from(clock, RoadTankerDriverState::default())
}

fn spawn_driver_from(clock: SimClock, start: RoadTankerDriverState) -> Harness {
    spawn_driver_full(clock, start, 16).0
}

/// Spawn the driver with a ticket channel of `capacity` slots. Also returns a
/// sender on that channel, so a test can take a slot before the driver sends.
fn spawn_driver_with_ticket_capacity(
    clock: SimClock,
    capacity: usize,
) -> (Harness, mpsc::Sender<DeliveryTicket>) {
    spawn_driver_full(clock, RoadTankerDriverState::default(), capacity)
}

/// Spawn the driver from `start` with a ticket channel of `capacity` slots.
fn spawn_driver_full(
    clock: SimClock,
    start: RoadTankerDriverState,
    capacity: usize,
) -> (Harness, mpsc::Sender<DeliveryTicket>) {
    let (arrival_tx, arrival_rx) = mpsc::channel(16);
    let (dock_job_tx, dock_job_rx) = mpsc::channel(16);
    let (tanker_pump_tx, tanker_pump_rx) = watch::channel(false);
    let (interlock_plug_tx, interlock_plug_rx) = mpsc::channel(16);
    let (ticket_tx, ticket_rx) = mpsc::channel(capacity);
    let (stop, stop_rx) = stop_channel();
    let (report_tx, report_rx) = oneshot::channel();

    road_tanker_driver::spawn(
        clock,
        start,
        stop_rx,
        report_tx,
        PUMP_RATE_L_PER_MIN,
        DRIVER_ID.to_string(),
        TANKER_REGISTRATION.to_string(),
        arrival_rx,
        dock_job_tx,
        tanker_pump_tx,
        interlock_plug_tx,
        ticket_tx.clone(),
    );

    (
        Harness {
            arrival_tx,
            dock_job_rx,
            tanker_pump_rx,
            interlock_plug_rx,
            ticket_rx,
            stop,
            report_rx,
        },
        ticket_tx,
    )
}

/// A ticket the driver did not write, used to take a slot of the ticket channel.
fn placeholder_ticket() -> DeliveryTicket {
    DeliveryTicket {
        ticket_number: "PLACEHOLDER".to_string(),
        delivery_commenced_at: SIM_START,
        grade: String::new(),
        gross_volume_l: 0.0,
        temperature_c: 0.0,
        temp_corrected_volume_l: 0.0,
        density_kg_per_l: 0.0,
        start_totaliser_l: 0.0,
        end_totaliser_l: 0.0,
        tanker_registration: String::new(),
        driver_id: String::new(),
        signed_by_driver: false,
        signed_by_operator: false,
    }
}

/// Reach `FillJobForwarded`: the test job has been forwarded as `DockJob::Fill`;
/// the actor is blocking on the proceed oneshot. Returns the proceed sender and
/// the completion receiver (held so the driver's completion signal succeeds).
async fn forward_fill(harness: &mut Harness) -> (oneshot::Sender<()>, oneshot::Receiver<()>) {
    let (job, completed_rx) = test_fill_job();
    harness.arrival_tx.send(job).await.unwrap();
    let DockJob::Fill(_, _, proceed_tx) = harness.dock_job_rx.recv().await.unwrap() else {
        panic!("expected DockJob::Fill");
    };
    (proceed_tx, completed_rx)
}

/// Service the driver's plug-in: receive the `InterlockPlug`, reply with a
/// receiver seeded `initial`, and return the sender so the test can drive the
/// circuit `Dead` and observe when the driver unplugs.
async fn service_plug(
    harness: &mut Harness,
    initial: InterlockCircuit,
) -> watch::Sender<InterlockCircuit> {
    let plug = harness
        .interlock_plug_rx
        .recv()
        .await
        .expect("driver should plug in");
    let (circuit_tx, circuit_rx) = watch::channel(initial);
    plug.reply.send(circuit_rx).unwrap();
    circuit_tx
}

/// Reach `FillInProgress`: proceed fired, plugged in against a `Live` circuit,
/// and the pump running. Returns the completion receiver and the circuit sender
/// (held so the driver's interlock receiver stays open during the fill).
async fn reach_fill_in_progress(
    harness: &mut Harness,
) -> (oneshot::Receiver<()>, watch::Sender<InterlockCircuit>) {
    let (proceed_tx, completed_rx) = forward_fill(harness).await;
    proceed_tx.send(()).unwrap();
    let circuit_tx = service_plug(harness, InterlockCircuit::Live).await;
    harness.tanker_pump_rx.changed().await.unwrap();
    assert!(*harness.tanker_pump_rx.borrow());
    (completed_rx, circuit_tx)
}

#[test]
fn fill_job_forwarded_to_dock_operator_on_arrival() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let mut harness = spawn_driver(clock);
        let (job, _completed_rx) = test_fill_job();
        harness.arrival_tx.send(job).await.unwrap();

        let DockJob::Fill(volume, registration, _proceed) =
            harness.dock_job_rx.recv().await.unwrap()
        else {
            panic!("expected DockJob::Fill");
        };
        assert_eq!(volume, TEST_VOLUME_L);
        // The registration the driver prints on its ticket, so the job the dock
        // operator creates names the tanker delivering.
        assert_eq!(registration, TANKER_REGISTRATION);
    });
}

#[test]
fn interlock_plugged_in_after_proceed() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let mut harness = spawn_driver(clock);
        let (proceed_tx, _completed_rx) = forward_fill(&mut harness).await;

        proceed_tx.send(()).unwrap();
        // Reply to keep the driver from blocking; the point is that it plugged in.
        let _circuit_tx = service_plug(&mut harness, InterlockCircuit::Live).await;
    });
}

#[test]
fn tanker_pump_starts_after_proceed() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let mut harness = spawn_driver(clock);
        let (proceed_tx, _completed_rx) = forward_fill(&mut harness).await;

        proceed_tx.send(()).unwrap();
        let _circuit_tx = service_plug(&mut harness, InterlockCircuit::Live).await;
        harness.tanker_pump_rx.changed().await.unwrap();
        assert!(*harness.tanker_pump_rx.borrow());
    });
}

#[test]
fn pump_starts_only_after_interlock_established() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let mut harness = spawn_driver(clock);
        let (proceed_tx, _completed_rx) = forward_fill(&mut harness).await;

        proceed_tx.send(()).unwrap();
        // The plug-in arrives while the pump is still off.
        let plug = harness
            .interlock_plug_rx
            .recv()
            .await
            .expect("driver should plug in");
        assert!(
            !*harness.tanker_pump_rx.borrow(),
            "pump must not start before the interlock circuit is established"
        );
        let (circuit_tx, circuit_rx) = watch::channel(InterlockCircuit::Live);
        plug.reply.send(circuit_rx).unwrap();

        harness.tanker_pump_rx.changed().await.unwrap();
        assert!(*harness.tanker_pump_rx.borrow());
        let _hold = circuit_tx;
    });
}

#[test]
fn pump_never_starts_when_interlock_dead_at_plug_in() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let mut harness = spawn_driver(clock);
        let (proceed_tx, _completed_rx) = forward_fill(&mut harness).await;

        proceed_tx.send(()).unwrap();
        // Tank already at or above the overfill interlock level: the circuit
        // is dead at plug-in.
        let _circuit_tx = service_plug(&mut harness, InterlockCircuit::Dead).await;

        let ticket = harness.ticket_rx.recv().await.unwrap();
        assert_eq!(ticket.gross_volume_l, 0.0);
        assert!(
            !*harness.tanker_pump_rx.borrow(),
            "pump must never start into a full tank"
        );
    });
}

#[test]
fn tanker_pump_stops_after_delivery_volume_reached() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let mut harness = spawn_driver(clock);
        let (_completed_rx, _circuit_tx) = reach_fill_in_progress(&mut harness).await;

        // The 300 s pump run auto-advances while we await the next change.
        harness.tanker_pump_rx.changed().await.unwrap();
        assert!(!*harness.tanker_pump_rx.borrow());
    });
}

#[test]
fn interlock_released_after_pump_stops() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let mut harness = spawn_driver(clock);
        let (_completed_rx, circuit_tx) = reach_fill_in_progress(&mut harness).await;

        harness.tanker_pump_rx.changed().await.unwrap();
        assert!(!*harness.tanker_pump_rx.borrow(), "pump stopped");
        assert_eq!(
            circuit_tx.receiver_count(),
            0,
            "driver unplugs (drops its interlock receiver) only after the pump is off"
        );
    });
}

#[test]
fn delivery_ticket_sent_with_correct_fields() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let mut harness = spawn_driver(clock);
        let (completed_rx, _circuit_tx) = reach_fill_in_progress(&mut harness).await;

        let ticket = harness.ticket_rx.recv().await.unwrap();
        assert_eq!(ticket.gross_volume_l, TEST_VOLUME_L);
        assert_eq!(ticket.driver_id, "driver-01");
        assert_eq!(ticket.tanker_registration, "AB12 CDE");
        assert!(ticket.signed_by_driver);
        assert!(!ticket.signed_by_operator);
        // The tanker meter delta equals the delivered volume.
        assert_eq!(
            ticket.end_totaliser_l - ticket.start_totaliser_l,
            TEST_VOLUME_L
        );

        // Having handed over the ticket, the driver signals the supplier that
        // the delivery is complete.
        completed_rx
            .await
            .expect("driver should signal delivery completion");
    });
}

#[test]
fn the_first_ticket_and_the_meter_start_from_1_and_0_by_default() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let mut harness = spawn_driver(clock);
        let (_completed_rx, _circuit_tx) = reach_fill_in_progress(&mut harness).await;

        let ticket = harness.ticket_rx.recv().await.unwrap();
        assert_eq!(ticket.ticket_number, "TICKET-000001");
        assert_eq!(ticket.start_totaliser_l, 0.0);
    });
}

#[test]
fn ticket_number_and_meter_follow_the_start_state() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let mut harness = spawn_driver_from(
            clock,
            RoadTankerDriverState {
                meter_totaliser_l: 54_000.0,
                ticket_seq: 4,
            },
        );
        let (_completed_rx, _circuit_tx) = reach_fill_in_progress(&mut harness).await;

        let ticket = harness.ticket_rx.recv().await.unwrap();
        assert_eq!(ticket.ticket_number, "TICKET-000004");
        assert_eq!(ticket.start_totaliser_l, 54_000.0);
        assert_eq!(ticket.end_totaliser_l, 54_000.0 + TEST_VOLUME_L);
    });
}

#[test]
fn interlock_trip_cuts_pump_mid_fill_with_actual_volume() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let mut harness = spawn_driver(clock.clone());
        let (_completed_rx, circuit_tx) = reach_fill_in_progress(&mut harness).await;

        // Halfway through the 300 s fill, the interlock trips.
        clock.sleep(std::time::Duration::from_secs(150)).await;
        circuit_tx.send(InterlockCircuit::Dead).unwrap();

        harness.tanker_pump_rx.changed().await.unwrap();
        assert!(!*harness.tanker_pump_rx.borrow(), "pump cut on the trip");

        let ticket = harness.ticket_rx.recv().await.unwrap();
        // 600 L/min × 2.5 min = 1 500 L actually metered, not the ordered 3 000 L.
        assert_eq!(ticket.gross_volume_l, 1500.0);
    });
}

/// How a delivery ends.
#[derive(Clone, Copy, Debug)]
enum DeliveryOutcome {
    FullVolumeDelivered,
    StoppedPartWay,
    PumpNeverStarted,
}

/// The driver signals the end of the delivery on every outcome, and only after
/// sending the ticket. The fuel supplier's pending order ends on this signal
/// (sim.md §5.3), so an outcome that did not fire it would leave the supplier
/// unable to order again.
///
/// The ticket channel has one slot, taken by a placeholder, so the driver's
/// ticket waits in its send until the test takes the placeholder. Once
/// simulated time moves on, the driver has done all it can while it waits; a
/// completion signal fired before the ticket was sent would be visible then.
#[test]
fn completion_signalled_on_every_outcome() {
    for outcome in [
        DeliveryOutcome::FullVolumeDelivered,
        DeliveryOutcome::StoppedPartWay,
        DeliveryOutcome::PumpNeverStarted,
    ] {
        let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
        runtime.block_on(async {
            let (mut harness, ticket_tx) = spawn_driver_with_ticket_capacity(clock.clone(), 1);
            ticket_tx.send(placeholder_ticket()).await.unwrap();

            let (mut completed_rx, _circuit_tx) = match outcome {
                DeliveryOutcome::FullVolumeDelivered => {
                    let held = reach_fill_in_progress(&mut harness).await;
                    // The 300 s pump run ends at the full volume.
                    harness.tanker_pump_rx.changed().await.unwrap();
                    held
                }
                DeliveryOutcome::StoppedPartWay => {
                    let held = reach_fill_in_progress(&mut harness).await;
                    clock.sleep(Duration::from_secs(150)).await;
                    held.1.send(InterlockCircuit::Dead).unwrap();
                    harness.tanker_pump_rx.changed().await.unwrap();
                    held
                }
                DeliveryOutcome::PumpNeverStarted => {
                    let (proceed_tx, completed_rx) = forward_fill(&mut harness).await;
                    proceed_tx.send(()).unwrap();
                    let circuit_tx = service_plug(&mut harness, InterlockCircuit::Dead).await;
                    (completed_rx, circuit_tx)
                }
            };

            clock.sleep(Duration::from_secs(1)).await;
            assert!(
                matches!(completed_rx.try_recv(), Err(TryRecvError::Empty)),
                "{outcome:?}: the delivery's end was signalled before its ticket was sent"
            );

            let placeholder = harness.ticket_rx.recv().await.unwrap();
            assert_eq!(placeholder.ticket_number, "PLACEHOLDER");
            let ticket = harness.ticket_rx.recv().await.unwrap();
            assert_eq!(
                ticket.driver_id, DRIVER_ID,
                "{outcome:?}: the driver's ticket"
            );
            completed_rx
                .await
                .unwrap_or_else(|_| panic!("{outcome:?}: the delivery's end was not signalled"));
        });
    }
}

#[test]
fn sequential_fill_jobs_processed_one_at_a_time() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let mut harness = spawn_driver(clock);

        // Two arrivals back to back (holding both completion receivers so the
        // driver's completion signals succeed).
        let (job1, _completed_rx1) = test_fill_job();
        let (job2, _completed_rx2) = test_fill_job();
        harness.arrival_tx.send(job1).await.unwrap();
        harness.arrival_tx.send(job2).await.unwrap();

        // Process the first delivery: proceed, then service its plug-in.
        let DockJob::Fill(_, _, proceed_tx) = harness.dock_job_rx.recv().await.unwrap() else {
            panic!("expected DockJob::Fill");
        };
        proceed_tx.send(()).unwrap();
        let _circuit_tx = service_plug(&mut harness, InterlockCircuit::Live).await;

        // The driver is now busy with the first fill; the second job must not be
        // forwarded yet.
        assert!(
            harness.dock_job_rx.try_recv().is_err(),
            "second job forwarded before the first delivery completed"
        );

        // First delivery completes (300 s auto-advance) and its ticket arrives.
        harness.ticket_rx.recv().await.unwrap();

        // Only now is the second job forwarded.
        let second = harness.dock_job_rx.recv().await.unwrap();
        assert!(matches!(second, DockJob::Fill(..)));
    });
}

// ---------------------------------------------------------------------------
// The ticket's start time and measurements (sim.md §8.1, §8.3, §8.4.1)
// ---------------------------------------------------------------------------

mod ticket_fields {
    use super::*;

    /// A fill whose pump starts at 23:58 on the last day of January and runs for
    /// 5 minutes (3 000 L at 600 L/min), so it completes in February.
    const BEFORE_MIDNIGHT: PrimitiveDateTime = datetime!(2025-01-31 23:58);

    /// The ticket is created at completion, so a driver reading the clock there
    /// would write the completion time into a field named for commencement — a
    /// substitution nothing downstream can detect, and one that moves a
    /// month-end transfer into the following month (sim.md §8.3).
    #[test]
    fn the_ticket_records_commencement_not_creation() {
        let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, BEFORE_MIDNIGHT);
        runtime.block_on(async {
            let mut harness = spawn_driver(clock.clone());
            let (_completed_rx, _circuit_tx) = reach_fill_in_progress(&mut harness).await;
            let started = clock.now_datetime();

            let ticket = harness.ticket_rx.recv().await.unwrap();
            let completed = clock.now_datetime();

            assert_ne!(
                started.date(),
                completed.date(),
                "the transfer must span midnight for this test to discriminate"
            );
            assert_eq!(ticket.delivery_commenced_at, started);
        });
    }

    /// The ticket's figures are the road tanker's own (sim.md §8.4.1): its
    /// product temperature is the driver's constant, 15 °C, never the shore
    /// tank's; density is the world constant the FMS also holds; and the
    /// correction to 15 °C is identity (sim.md §1.3).
    #[test]
    fn the_ticket_carries_the_tankers_own_measurements() {
        let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
        runtime.block_on(async {
            let mut harness = spawn_driver(clock);
            let (_completed_rx, _circuit_tx) = reach_fill_in_progress(&mut harness).await;
            let ticket = harness.ticket_rx.recv().await.unwrap();

            assert_eq!(ticket.temperature_c, 15.0);
            assert_eq!(ticket.density_kg_per_l, sim::constants::DENSITY_KG_PER_L);
            assert_eq!(ticket.temp_corrected_volume_l, ticket.gross_volume_l);
        });
    }
}

// ---------------------------------------------------------------------------
// Stopping and warm start (sim-tests.md, "Stopping and warm start")
// ---------------------------------------------------------------------------

#[test]
fn fill_in_progress_completes_before_report() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let mut harness = spawn_driver_from(
            clock,
            RoadTankerDriverState {
                meter_totaliser_l: 1_000.0,
                ticket_seq: 7,
            },
        );
        let (mut completed_rx, _circuit_tx) = reach_fill_in_progress(&mut harness).await;
        harness.stop.stop();

        harness.tanker_pump_rx.changed().await.unwrap();
        assert!(!*harness.tanker_pump_rx.borrow(), "the pump stops");
        let ticket = harness.ticket_rx.recv().await.unwrap();
        assert_eq!(
            ticket.gross_volume_l, TEST_VOLUME_L,
            "at the delivery volume"
        );
        assert_eq!(ticket.ticket_number, "TICKET-000007");
        assert_eq!(ticket.start_totaliser_l, 1_000.0);
        (&mut completed_rx).await.unwrap();

        let report = (&mut harness.report_rx).await.unwrap();
        assert_eq!(
            report,
            RoadTankerDriverState {
                meter_totaliser_l: 1_000.0 + TEST_VOLUME_L,
                ticket_seq: 8,
            }
        );
    });
}

#[test]
fn driver_holds_a_fill_job_it_did_not_take_up() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let mut harness = spawn_driver(clock);
        // The job and the stop reach the idle driver together.
        let (job, mut completed_rx) = test_fill_job();
        harness.arrival_tx.send(job).await.unwrap();
        harness.stop.stop();

        let report = (&mut harness.report_rx).await.unwrap();
        assert_eq!(report, RoadTankerDriverState::default());
        assert!(
            harness.dock_job_rx.try_recv().is_err(),
            "no DockJob is sent"
        );
        assert_eq!(
            completed_rx.try_recv(),
            Err(oneshot::error::TryRecvError::Empty),
            "the completion sender is held, neither fired nor dropped"
        );
    });
}
