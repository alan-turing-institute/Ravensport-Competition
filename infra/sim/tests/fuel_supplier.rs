//! Integration tests for `sim::actors::fuel_supplier`.
//!
//! Exercises the actor through its public `spawn()` interface only (see
//! sim-tests.md). A mock ATG console — a minimal Veeder-Root TLS server that
//! answers `i201` with a watch-controlled volume and `i101` with no alarm —
//! stands in for `atg-relay-01`, and records the simulated instant of each
//! `i201` it answers, so the tests can observe the supplier's polls. The test
//! body plays the road tanker driver: it holds each `FillJob`'s `completed`
//! sender and fires it only where a test says the delivery has ended.
//!
//! The supplier polls on a `clock.sleep` cadence, so the tests run under
//! `SimClock::isolated(TimeModel::Synthetic, …)` + `block_on`. Positive tests
//! await the dispatched `FillJob` (auto-advancing the clock); negative tests
//! advance a bounded amount with a cloned clock handle and then assert nothing
//! was dispatched.

use std::net::SocketAddr;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use time::macros::datetime;
use time::PrimitiveDateTime;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::{mpsc, oneshot, watch};
use tokio::task::JoinHandle;

use sim::actors::fuel_supplier::{self, FillJob, FuelSupplierState, SupplierOrder, SupplierStart};
use sim::clock::{SimClock, SimInstant, TimeModel};
use sim::stop::{stop_channel, StopSender};

const SIM_START: PrimitiveDateTime = datetime!(2025-01-01 0:00);

// Construction parameters (sim-tests.md).
const POLL_INTERVAL: Duration = Duration::from_secs(60); // 1 min
const REORDER_THRESHOLD_L: f64 = 10_000.0;
const DELIVERY_VOLUME_L: f64 = 15_000.0;
const LEAD_TIME: Duration = Duration::from_secs(1800); // 30 min

/// The longest from a moment at which the supplier may order, with the level
/// below threshold, to the `FillJob`: at most one interval to the next poll,
/// which places the order, then the lead time. The extra half interval keeps
/// the bound clear of the poll schedule.
const FILL_JOB_BOUND: Duration = Duration::from_secs(
    POLL_INTERVAL.as_secs() + LEAD_TIME.as_secs() + POLL_INTERVAL.as_secs() / 2,
);

const SOH: u8 = 0x01;
const ETX: u8 = 0x03;

// ---------------------------------------------------------------------------
// Mock ATG console (Veeder-Root TLS server)
// ---------------------------------------------------------------------------

/// The simulated instants at which the mock ATG answered an `i201` query.
type PollLog = Arc<Mutex<Vec<SimInstant>>>;

/// Bind a mock ATG synchronously and spawn its accept loop. Returns its
/// address, a `watch` sender for the reported tank volume, and the log of the
/// `i201` queries it answers.
fn spawn_mock_atg(clock: SimClock) -> (SocketAddr, watch::Sender<f64>, PollLog) {
    let (volume_tx, volume_rx) = watch::channel(0.0_f64);
    let polls = PollLog::default();
    let std_listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    std_listener.set_nonblocking(true).unwrap();
    let addr = std_listener.local_addr().unwrap();
    let listener = TcpListener::from_std(std_listener).unwrap();

    let log = Arc::clone(&polls);
    tokio::spawn(async move {
        while let Ok((stream, _)) = listener.accept().await {
            tokio::spawn(serve_atg(
                stream,
                volume_rx.clone(),
                clock.clone(),
                Arc::clone(&log),
            ));
        }
    });
    (addr, volume_tx, polls)
}

/// Like [`spawn_mock_atg`], but the **first** connection is accepted and then
/// dropped immediately (a transient failure the supplier must retry). Returns
/// the connection counter so a test can confirm the retry happened.
fn spawn_mock_atg_dropping_first(
    clock: SimClock,
) -> (SocketAddr, watch::Sender<f64>, Arc<AtomicUsize>) {
    let (volume_tx, volume_rx) = watch::channel(0.0_f64);
    let connections = Arc::new(AtomicUsize::new(0));
    let counter = Arc::clone(&connections);
    let polls = PollLog::default();
    let std_listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    std_listener.set_nonblocking(true).unwrap();
    let addr = std_listener.local_addr().unwrap();
    let listener = TcpListener::from_std(std_listener).unwrap();

    tokio::spawn(async move {
        while let Ok((stream, _)) = listener.accept().await {
            if counter.fetch_add(1, Ordering::SeqCst) == 0 {
                drop(stream); // first connection fails before any response
                continue;
            }
            tokio::spawn(serve_atg(
                stream,
                volume_rx.clone(),
                clock.clone(),
                Arc::clone(&polls),
            ));
        }
    });
    (addr, volume_tx, connections)
}

/// Serve TLS requests on one connection: `i201` → inventory with the current
/// volume, recording the instant in `polls`; `i101` → no-alarm status.
async fn serve_atg(
    mut stream: TcpStream,
    volume_rx: watch::Receiver<f64>,
    clock: SimClock,
    polls: PollLog,
) {
    loop {
        let mut soh = [0u8; 1];
        if stream.read_exact(&mut soh).await.is_err() {
            return;
        }
        let mut code = [0u8; 6];
        if stream.read_exact(&mut code).await.is_err() {
            return;
        }
        let body = match &code[1..4] {
            b"201" => {
                polls.lock().unwrap().push(clock.now());
                let volume = *volume_rx.borrow();
                let mut body = String::from("01D000007"); // TT=01, p=D, ssss=0000, NN=07
                body.push_str(&float_field(volume as f32));
                for _ in 0..6 {
                    body.push_str(&float_field(0.0));
                }
                body
            }
            b"101" => "000000".to_string(),
            _ => continue,
        };
        let response = frame(&code, &body);
        if stream.write_all(&response).await.is_err() {
            return;
        }
    }
}

fn float_field(value: f32) -> String {
    value
        .to_be_bytes()
        .iter()
        .map(|byte| format!("{byte:02X}"))
        .collect()
}

/// Frame an echoed code and body as `SOH echo timestamp body && checksum ETX`.
fn frame(echo: &[u8], body: &str) -> Vec<u8> {
    let mut message = vec![SOH];
    message.extend_from_slice(echo);
    message.extend_from_slice(b"0000000000"); // timestamp
    message.extend_from_slice(body.as_bytes());
    message.extend_from_slice(b"&&");
    let sum = message
        .iter()
        .fold(0u16, |acc, &b| acc.wrapping_add(u16::from(b)));
    let checksum = 0u16.wrapping_sub(sum);
    message.extend_from_slice(format!("{checksum:04X}").as_bytes());
    message.push(ETX);
    message
}

// ---------------------------------------------------------------------------
// Harness
// ---------------------------------------------------------------------------

struct Harness {
    volume_tx: watch::Sender<f64>,
    arrival_rx: mpsc::Receiver<FillJob>,
    polls: PollLog,
    supplier: JoinHandle<()>,
    start: SimInstant,
    stop: StopSender,
    driver_at_rest: Option<oneshot::Sender<()>>,
    report_rx: oneshot::Receiver<FuelSupplierState>,
}

impl Harness {
    /// The simulated time of each poll the mock ATG answered, measured from
    /// the supplier's spawn.
    fn poll_times(&self) -> Vec<Duration> {
        self.polls
            .lock()
            .unwrap()
            .iter()
            .map(|instant| instant.duration_since(self.start))
            .collect()
    }
}

/// Spawn the mock ATG and the supplier; return a clock clone for advancing time
/// alongside the harness.
fn spawn_supplier(clock: SimClock) -> (SimClock, Harness) {
    let start = FuelSupplierState::new(clock.now_datetime(), POLL_INTERVAL);
    spawn_supplier_from(clock, start)
}

/// Like [`spawn_supplier`], with the cold start that a start state gives
/// (sim-impl.md, "Start state"), as the orchestrator makes it.
fn spawn_supplier_starting(clock: SimClock, start: SupplierStart) -> (SimClock, Harness) {
    let from = FuelSupplierState::cold(clock.now_datetime(), POLL_INTERVAL, start);
    spawn_supplier_from(clock, from)
}

/// Spawn the mock ATG and the supplier from `start`.
fn spawn_supplier_from(clock: SimClock, from: FuelSupplierState) -> (SimClock, Harness) {
    let test_clock = clock.clone();
    let started = clock.now();
    let (atg_addr, volume_tx, polls) = spawn_mock_atg(clock.clone());
    let local_addr: SocketAddr = ([127, 0, 0, 1], 0).into();
    let (arrival_tx, arrival_rx) = mpsc::channel(16);
    let (stop, stop_rx) = stop_channel();
    let (driver_at_rest, driver_at_rest_rx) = oneshot::channel();
    let (report_tx, report_rx) = oneshot::channel();

    let supplier = fuel_supplier::spawn(
        clock,
        from,
        stop_rx,
        driver_at_rest_rx,
        report_tx,
        local_addr,
        atg_addr,
        POLL_INTERVAL,
        REORDER_THRESHOLD_L,
        DELIVERY_VOLUME_L,
        LEAD_TIME,
        arrival_tx,
    );

    (
        test_clock,
        Harness {
            volume_tx,
            arrival_rx,
            polls,
            supplier,
            start: started,
            stop,
            driver_at_rest: Some(driver_at_rest),
            report_rx,
        },
    )
}

/// Await the next `FillJob`, failing the test if none arrives within `bound`
/// of simulated time.
async fn recv_within(
    clock: &SimClock,
    arrival_rx: &mut mpsc::Receiver<FillJob>,
    bound: Duration,
) -> FillJob {
    tokio::select! {
        job = arrival_rx.recv() => job.expect("the supplier closed its FillJob channel"),
        () = clock.sleep(bound) => panic!("no FillJob within {bound:?}"),
    }
}

#[test]
fn no_dispatch_when_level_above_threshold() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let (test_clock, mut harness) = spawn_supplier(clock);
        harness.volume_tx.send(20_000.0).unwrap();

        // Three poll cycles pass with the level above threshold.
        test_clock.sleep(3 * POLL_INTERVAL).await;
        assert!(
            harness.arrival_rx.try_recv().is_err(),
            "no delivery should be dispatched above the reorder threshold"
        );
    });
}

#[test]
fn dispatch_triggered_when_level_below_threshold() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let (_test_clock, mut harness) = spawn_supplier(clock);
        harness.volume_tx.send(8_000.0).unwrap();

        // Awaiting the dispatch auto-advances through the poll and lead time.
        let job = harness.arrival_rx.recv().await;
        assert!(
            job.is_some(),
            "a delivery should be dispatched below threshold"
        );
    });
}

#[test]
fn fill_job_volume_matches_configured_value() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let (_test_clock, mut harness) = spawn_supplier(clock);
        harness.volume_tx.send(8_000.0).unwrap();

        let job = harness.arrival_rx.recv().await.unwrap();
        assert_eq!(job.delivery_volume_l, DELIVERY_VOLUME_L);
    });
}

#[test]
fn no_dispatch_before_lead_time_expires() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let (test_clock, mut harness) = spawn_supplier(clock);
        harness.volume_tx.send(8_000.0).unwrap();

        // One poll has happened (decision made) but the lead time has not elapsed.
        test_clock.sleep(POLL_INTERVAL).await;
        assert!(
            harness.arrival_rx.try_recv().is_err(),
            "the FillJob must not be sent until the lead time has elapsed"
        );
    });
}

/// The supplier polls once in every interval from its first poll onwards,
/// through the lead time and after the `FillJob`, while the order is pending:
/// it monitors the level continuously, and an order does not move its poll
/// schedule (sim.md §5.3).
#[test]
fn polls_continue_while_an_order_is_pending() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let (test_clock, mut harness) = spawn_supplier(clock);
        harness.volume_tx.send(8_000.0).unwrap();

        // The first poll places the order; the FillJob follows its lead time.
        // Its `completed` sender is held, not fired, so the order stays pending.
        let _pending = recv_within(&test_clock, &mut harness.arrival_rx, FILL_JOB_BOUND).await;

        // Ten further intervals. The extra half interval keeps the test's own
        // wake clear of the poll at the tenth.
        test_clock
            .sleep(10 * POLL_INTERVAL + POLL_INTERVAL / 2)
            .await;

        // The poll that placed the order, one in each of the 30 intervals of
        // the lead time, and one in each of the 10 after the FillJob.
        let expected: Vec<Duration> = (1..=41u32).map(|k| k * POLL_INTERVAL).collect();
        assert_eq!(
            harness.poll_times(),
            expected,
            "the supplier must poll at every interval while an order is pending"
        );
    });
}

#[test]
fn no_second_order_while_an_order_is_pending() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let (test_clock, mut harness) = spawn_supplier(clock);
        harness.volume_tx.send(8_000.0).unwrap();

        // First order; its `completed` sender is held, so the delivery has not
        // ended.
        let _pending = recv_within(&test_clock, &mut harness.arrival_rx, FILL_JOB_BOUND).await;
        let dispatched_at = test_clock.now().duration_since(harness.start);

        // The level stays below threshold. The advance exceeds the lead time, so
        // a second order placed by any below-threshold poll during it would have
        // produced a FillJob.
        test_clock.sleep(3 * (POLL_INTERVAL + LEAD_TIME)).await;
        assert!(
            harness.arrival_rx.try_recv().is_err(),
            "a second order must not be placed while the first is pending"
        );
        assert!(
            harness.poll_times().iter().any(|&t| t > dispatched_at),
            "no poll during the advance, so no below-threshold poll was refused an order"
        );
    });
}

#[test]
fn ordering_resumes_after_completion() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let (test_clock, mut harness) = spawn_supplier(clock);
        harness.volume_tx.send(8_000.0).unwrap();

        // First order; the delivery ends (the driver's signal).
        let first = recv_within(&test_clock, &mut harness.arrival_rx, FILL_JOB_BOUND).await;
        first
            .completed
            .send(())
            .expect("the supplier should still be awaiting the completion signal");

        // The level is still below threshold, so the next poll places an order,
        // whose FillJob follows its lead time.
        let second = recv_within(&test_clock, &mut harness.arrival_rx, FILL_JOB_BOUND).await;
        assert_eq!(second.delivery_volume_l, DELIVERY_VOLUME_L);
    });
}

#[test]
fn dropped_completion_signal_is_fatal() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let (test_clock, mut harness) = spawn_supplier(clock);
        harness.volume_tx.send(8_000.0).unwrap();

        // The FillJob is dropped, and its `completed` sender with it.
        let first = recv_within(&test_clock, &mut harness.arrival_rx, FILL_JOB_BOUND).await;
        drop(first);

        let outcome = tokio::select! {
            outcome = &mut harness.supplier => outcome,
            () = test_clock.sleep(POLL_INTERVAL) => {
                panic!("the supplier still runs after its completion signal was dropped")
            }
        };
        let error = outcome.expect_err("the supplier returned rather than panicked");
        assert!(error.is_panic(), "the supplier was cancelled, not panicked");
        let payload = error.into_panic();
        let message = payload
            .downcast_ref::<String>()
            .map(String::as_str)
            .or_else(|| payload.downcast_ref::<&str>().copied())
            .unwrap_or("");
        assert!(
            message.contains("completion"),
            "the supplier panicked, but not on the dropped completion signal: {message}"
        );
    });
}

#[test]
fn transient_connection_failure_is_retried_then_recovers() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let (atg_addr, volume_tx, connections) = spawn_mock_atg_dropping_first(clock.clone());
        let local_addr: SocketAddr = ([127, 0, 0, 1], 0).into();
        let (arrival_tx, mut arrival_rx) = mpsc::channel(16);
        let (_stop, stop_rx) = stop_channel();
        let (_driver_at_rest, driver_at_rest_rx) = oneshot::channel();
        let (report_tx, _report_rx) = oneshot::channel();
        let start = FuelSupplierState::new(clock.now_datetime(), POLL_INTERVAL);
        fuel_supplier::spawn(
            clock,
            start,
            stop_rx,
            driver_at_rest_rx,
            report_tx,
            local_addr,
            atg_addr,
            POLL_INTERVAL,
            REORDER_THRESHOLD_L,
            DELIVERY_VOLUME_L,
            LEAD_TIME,
            arrival_tx,
        );
        volume_tx.send(8_000.0).unwrap();

        // The first poll's connection is dropped; the supplier retries and still
        // dispatches the delivery (the retry backoff sleeps on the sim clock).
        let job = arrival_rx.recv().await;
        assert!(
            job.is_some(),
            "supplier must recover from a dropped connection"
        );
        assert!(
            connections.load(Ordering::SeqCst) >= 2,
            "supplier must have retried the connection after the first was dropped"
        );
    });
}

// ---------------------------------------------------------------------------
// The start that a start state gives (sim-impl.md, "Start state")
// ---------------------------------------------------------------------------

#[test]
fn a_pending_order_is_delivered_once_the_remaining_lead_time_has_passed() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let remaining = Duration::from_secs(600);
        let (test_clock, mut harness) =
            spawn_supplier_starting(clock, SupplierStart::OrderPending(remaining));
        // Above the threshold, so no poll could dispatch on its own.
        harness.volume_tx.send(20_000.0).unwrap();

        test_clock.sleep(remaining - Duration::from_secs(1)).await;
        assert!(
            harness.arrival_rx.try_recv().is_err(),
            "the pending order's tanker arrives only after the remaining lead time"
        );
        let job = harness.arrival_rx.recv().await.unwrap();
        assert_eq!(test_clock.now_datetime(), SIM_START + remaining);
        assert_eq!(job.delivery_volume_l, DELIVERY_VOLUME_L);
    });
}

#[test]
fn with_an_order_pending_the_polls_keep_their_schedule_from_the_start() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let remaining = Duration::from_secs(600);
        let (test_clock, mut harness) =
            spawn_supplier_starting(clock, SupplierStart::OrderPending(remaining));
        harness.volume_tx.send(8_000.0).unwrap();

        let job = harness.arrival_rx.recv().await.unwrap();
        assert_eq!(test_clock.now_datetime(), SIM_START + remaining);
        // The first poll came one interval after the start, and the polls kept
        // their interval through the pending order without placing another.
        let polls = harness.poll_times();
        assert_eq!(polls.first(), Some(&POLL_INTERVAL), "polls: {polls:?}");
        assert!(
            polls
                .windows(2)
                .all(|pair| pair[1] - pair[0] == POLL_INTERVAL),
            "the polls left their schedule: {polls:?}"
        );

        // The delivery ends between two polls, and the next poll places the
        // next order.
        test_clock.sleep(POLL_INTERVAL / 2).await;
        job.completed.send(()).unwrap();
        let _second = harness.arrival_rx.recv().await.unwrap();
        assert_eq!(
            test_clock.now_datetime(),
            SIM_START + remaining + POLL_INTERVAL + LEAD_TIME
        );
    });
}

#[test]
fn with_no_order_placed_the_first_poll_comes_at_the_drawn_time() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let first_poll = Duration::from_secs(20);
        let (test_clock, mut harness) =
            spawn_supplier_starting(clock, SupplierStart::FirstPollAfter(first_poll));
        harness.volume_tx.send(8_000.0).unwrap();

        let _job = harness.arrival_rx.recv().await.unwrap();
        assert_eq!(
            test_clock.now_datetime(),
            SIM_START + first_poll + LEAD_TIME
        );
    });
}

// ── Stopping and warm start (sim-tests.md, "Stopping and warm start") ────────

/// The instant of the first poll of a supplier spawned at `SIM_START`.
const FIRST_POLL: PrimitiveDateTime = SIM_START.saturating_add(time::Duration::seconds(60));

#[test]
fn supplier_reports_tanker_due_during_lead_time() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let (test_clock, mut harness) = spawn_supplier(clock);
        harness.volume_tx.send(8_000.0).unwrap();

        // Stopped between two polls, half-way through the lead time.
        let stopped_after = POLL_INTERVAL + LEAD_TIME / 2 - POLL_INTERVAL / 2;
        test_clock.sleep(stopped_after).await;
        let next_poll_at = SIM_START + stopped_after + POLL_INTERVAL / 2;
        harness.stop.stop();

        let report = (&mut harness.report_rx).await.unwrap();
        assert_eq!(
            report.order,
            SupplierOrder::TankerDueAt(FIRST_POLL + LEAD_TIME)
        );
        assert_eq!(
            report.next_poll_at, next_poll_at,
            "the poll schedule is kept"
        );
        test_clock.sleep(LEAD_TIME).await;
        assert!(harness.arrival_rx.try_recv().is_err(), "no FillJob is sent");
    });
}

#[test]
fn supplier_reports_tanker_arrived_when_driver_did_not_take_it_up() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let (test_clock, mut harness) = spawn_supplier(clock);
        harness.volume_tx.send(8_000.0).unwrap();

        // The FillJob is sent at the end of the lead time and not received.
        test_clock
            .sleep(POLL_INTERVAL + LEAD_TIME + POLL_INTERVAL / 2)
            .await;
        harness.stop.stop();
        tokio::task::yield_now().await;
        assert!(
            harness.report_rx.try_recv().is_err(),
            "the supplier awaits the driver's report"
        );

        harness.driver_at_rest.take().unwrap().send(()).unwrap();
        let report = (&mut harness.report_rx).await.unwrap();
        assert_eq!(
            report.order,
            SupplierOrder::TankerDueAt(FIRST_POLL + LEAD_TIME)
        );
    });
}

#[test]
fn supplier_reports_no_order_after_delivery_ends() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let (test_clock, mut harness) = spawn_supplier(clock);
        harness.volume_tx.send(8_000.0).unwrap();

        // The driver takes the job up; the stop arrives during the fill.
        let job = harness.arrival_rx.recv().await.unwrap();
        harness.stop.stop();
        let stopped_at = test_clock.now().duration_since(harness.start);
        test_clock.sleep(Duration::from_secs(30 * 60)).await;
        job.completed.send(()).unwrap();

        let report = (&mut harness.report_rx).await.unwrap();
        assert_eq!(report.order, SupplierOrder::None);
        assert!(
            harness.poll_times().iter().all(|at| *at <= stopped_at),
            "a poll was made after the stop: {:?}",
            harness.poll_times()
        );
    });
}

#[test]
fn passed_due_time_is_acted_on_at_once() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        // The checkpoint was written two minutes before the start, with the
        // next poll due one minute before it.
        let start = FuelSupplierState {
            next_poll_at: SIM_START - Duration::from_secs(60),
            order: SupplierOrder::None,
        };
        let (test_clock, mut harness) = spawn_supplier_from(clock, start);
        harness.volume_tx.send(8_000.0).unwrap();

        // The dispatch follows the lead time from a poll made at the start.
        harness.arrival_rx.recv().await.unwrap();
        assert_eq!(test_clock.now_datetime(), SIM_START + LEAD_TIME);
    });
}

#[test]
fn saved_tanker_is_dispatched_at_once() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let start = FuelSupplierState {
            next_poll_at: SIM_START + POLL_INTERVAL,
            order: SupplierOrder::TankerDueAt(SIM_START - Duration::from_secs(30)),
        };
        let (test_clock, mut harness) = spawn_supplier_from(clock, start);
        harness.arrival_rx.recv().await.unwrap();
        assert_eq!(test_clock.now_datetime(), SIM_START);
    });
}
