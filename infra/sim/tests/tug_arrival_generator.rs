//! Integration tests for `sim::physical::tug_arrival_generator`.
//!
//! Exercises the entity through its public `spawn()` interface only, holding
//! the channel ends that neighbouring entities would own (see sim-tests.md).
//! The tests run under `SimClock::isolated`, `TimeModel::Synthetic` unless a
//! test says otherwise.
//!
//! The test body plays the tug engineer: unless a test says otherwise, it fires
//! each arrival's `departed` as soon as it receives the arrival, so the tug
//! leaves at once and the interval between its consecutive arrivals is its time
//! away. Each `recv().await` lets the generator's sleeps drive simulated time
//! forward, so reading `clock.now()` after a receive yields that arrival's
//! simulated instant. Statistical tests use a fixed seed, so the sample
//! statistics are deterministic.

use std::collections::HashMap;
use std::panic::{self, AssertUnwindSafe};
use std::time::Duration;

use rand::distr::Uniform;
use rand::SeedableRng;
use rand_chacha::ChaCha8Rng;
use rand_distr::{Distribution, Exp1};
use tokio::sync::{mpsc, oneshot};
use tokio::task::JoinHandle;

use sim::actors::tug_engineer::VesselId;
use sim::clock::{SimClock, SimInstant, TimeModel};
use sim::physical::tug_arrival_generator::{
    self, DispenseJob, TugArrival, TugArrivalState, TugPhase, TugState, Vessel,
};
use sim::shared::RfidCardNumber;
use sim::stop::{stop_channel, StopSender};
use time::macros::datetime;
use time::PrimitiveDateTime;

const SIM_START: PrimitiveDateTime = datetime!(2025-01-01 0:00);

// Construction parameters (sim-tests.md).
const VESSEL_A: Vessel = Vessel {
    vessel_id: VesselId(1999997),
    name: "Tug A",
    rfid_card: RfidCardNumber(99999),
    mean_time_away: Duration::from_secs(15 * 60),
};
const VESSEL_B: Vessel = Vessel {
    vessel_id: VesselId(1999985),
    name: "Tug B",
    rfid_card: RfidCardNumber(99998),
    mean_time_away: Duration::from_secs(60 * 60),
};
const VESSEL_C: Vessel = Vessel {
    vessel_id: VesselId(1999973),
    name: "Tug C",
    rfid_card: RfidCardNumber(99997),
    mean_time_away: Duration::from_secs(30 * 60),
};
static ROSTER_A: [Vessel; 1] = [VESSEL_A];
static ROSTER_AB: [Vessel; 2] = [VESSEL_A, VESSEL_B];
static ROSTER_ABC: [Vessel; 3] = [VESSEL_A, VESSEL_B, VESSEL_C];

// A refuel range other than the default 500–3 000 L, so the tests show the
// bounds are read rather than fixed.
const REFUEL_VOLUME_MIN_L: f64 = 200.0;
const REFUEL_VOLUME_MAX_L: f64 = 800.0;
const SEED: u64 = 42;

/// Spawn the generator for `vessels` with the test's refuel range and seed,
/// on a channel with one slot per roster vessel.
fn spawn_generator(
    clock: SimClock,
    vessels: &'static [Vessel],
) -> (mpsc::Receiver<TugArrival>, Vec<JoinHandle<()>>) {
    let (rx, tasks, controls) =
        spawn_generator_from(clock, vessels, TugArrivalState::new(SEED, vessels));
    // Never stopped: the controls are kept for the life of the test process, so
    // that neither the stop request nor the tug engineer's report resolves.
    std::mem::forget(controls);
    (rx, tasks)
}

/// The test's ends of the generator's stop request, of the tug engineer's
/// report signal, and of the generator's report.
struct Controls {
    stop: StopSender,
    engineer_at_rest: oneshot::Sender<()>,
    report: oneshot::Receiver<TugArrivalState>,
}

/// Spawn the generator for `vessels` from `start`, with the test's refuel
/// range, on a channel with one slot per roster vessel.
fn spawn_generator_from(
    clock: SimClock,
    vessels: &'static [Vessel],
    start: TugArrivalState,
) -> (mpsc::Receiver<TugArrival>, Vec<JoinHandle<()>>, Controls) {
    let (tx, rx) = mpsc::channel(vessels.len().max(1));
    let (stop, stop_rx) = stop_channel();
    let (engineer_at_rest, engineer_at_rest_rx) = oneshot::channel();
    let (report_tx, report) = oneshot::channel();
    let tasks = tug_arrival_generator::spawn(
        clock,
        vessels,
        REFUEL_VOLUME_MIN_L,
        REFUEL_VOLUME_MAX_L,
        start,
        stop_rx,
        engineer_at_rest_rx,
        report_tx,
        tx,
    );
    let controls = Controls {
        stop,
        engineer_at_rest,
        report,
    };
    (rx, tasks, controls)
}

/// One arrival as the test observed it: the tug, the simulated instant, and the
/// requested volume.
struct Observed {
    vessel_id: VesselId,
    vessel_name: &'static str,
    rfid_card: RfidCardNumber,
    at: SimInstant,
    volume_l: f64,
}

/// Receive the next arrival and fire its `departed` at once, so the tug leaves
/// the dock the instant it arrives.
async fn arrive_and_depart(clock: &SimClock, rx: &mut mpsc::Receiver<TugArrival>) -> Observed {
    let arrival = rx.recv().await.expect("the generator closed its channel");
    let observed = Observed {
        vessel_id: arrival.job.vessel_id,
        vessel_name: arrival.job.vessel_name,
        rfid_card: arrival.job.rfid_card,
        at: clock.now(),
        volume_l: arrival.job.volume_l,
    };
    arrival
        .departed
        .send(())
        .expect("the generator should be awaiting the tug's departure");
    observed
}

/// The first `count` times away and requested volumes of `vessel`, with every
/// tug leaving the dock the instant it arrives.
async fn sequence_of(
    clock: &SimClock,
    rx: &mut mpsc::Receiver<TugArrival>,
    vessel: VesselId,
    count: usize,
) -> Vec<(Duration, f64)> {
    let mut last_departure = clock.now();
    let mut sequence = Vec::with_capacity(count);
    while sequence.len() < count {
        let observed = arrive_and_depart(clock, rx).await;
        if observed.vessel_id == vessel {
            sequence.push((
                observed.at.duration_since(last_departure),
                observed.volume_l,
            ));
            last_departure = observed.at;
        }
    }
    sequence
}

/// The message of a panic payload, or the empty string.
fn panic_message(payload: &(dyn std::any::Any + Send)) -> &str {
    payload
        .downcast_ref::<String>()
        .map(String::as_str)
        .or_else(|| payload.downcast_ref::<&str>().copied())
        .unwrap_or("")
}

#[test]
fn arrival_sent_after_time_away() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let (mut rx, _tasks) = spawn_generator(clock.clone(), &ROSTER_AB);

        // 10 × vessel A's mean time away.
        clock.sleep(10 * VESSEL_A.mean_time_away).await;

        let mut count = 0;
        while let Ok(arrival) = rx.try_recv() {
            arrival.departed.send(()).unwrap();
            count += 1;
        }
        assert!(count >= 1, "expected at least one arrival, got {count}");
    });
}

/// Each tug's intervals between consecutive arrivals follow an exponential
/// distribution with that tug's own mean, and each arrival's job carries that
/// tug's name and card.
#[test]
fn each_tug_keeps_its_own_mean_time_away() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let (mut rx, _tasks) = spawn_generator(clock.clone(), &ROSTER_AB);

        let mut arrivals: HashMap<VesselId, Vec<SimInstant>> = HashMap::new();
        while ROSTER_AB
            .iter()
            .any(|v| arrivals.get(&v.vessel_id).map_or(0, Vec::len) < 500)
        {
            let observed = arrive_and_depart(&clock, &mut rx).await;
            let vessel = ROSTER_AB
                .iter()
                .find(|v| v.vessel_id == observed.vessel_id)
                .expect("an arrival of a tug not on the roster");
            assert_eq!(observed.vessel_name, vessel.name);
            assert_eq!(observed.rfid_card, vessel.rfid_card);
            arrivals
                .entry(observed.vessel_id)
                .or_default()
                .push(observed.at);
        }

        for vessel in &ROSTER_AB {
            let instants = &arrivals[&vessel.vessel_id][..500];
            let intervals: Vec<f64> = instants
                .windows(2)
                .map(|w| w[1].duration_since(w[0]).as_secs_f64())
                .collect();
            let n = intervals.len() as f64;
            let mean = intervals.iter().sum::<f64>() / n;
            let variance = intervals.iter().map(|x| (x - mean).powi(2)).sum::<f64>() / (n - 1.0);
            let coefficient_of_variation = variance.sqrt() / mean;
            let configured = vessel.mean_time_away.as_secs_f64();

            assert!(
                (mean - configured).abs() < 0.1 * configured,
                "{}: mean time away {mean}s not within 10% of {configured}s",
                vessel.name
            );
            assert!(
                (coefficient_of_variation - 1.0).abs() < 0.15,
                "{}: coefficient of variation {coefficient_of_variation} not within 0.15 of 1.0",
                vessel.name
            );
        }
    });
}

/// Every requested volume lies within the configured bounds, and the sample
/// mean is near the mean of Uniform(200, 800): the configured bounds, not
/// fixed ones, are respected, and the draw is approximately unbiased.
#[test]
fn requested_volumes_within_configured_bounds() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let (mut rx, _tasks) = spawn_generator(clock.clone(), &ROSTER_AB);

        let mut volumes = Vec::with_capacity(500);
        for _ in 0..500 {
            volumes.push(arrive_and_depart(&clock, &mut rx).await.volume_l);
        }

        for &v in &volumes {
            assert!(
                (REFUEL_VOLUME_MIN_L..=REFUEL_VOLUME_MAX_L).contains(&v),
                "volume {v} outside [{REFUEL_VOLUME_MIN_L}, {REFUEL_VOLUME_MAX_L}]"
            );
        }
        let mean = volumes.iter().sum::<f64>() / volumes.len() as f64;
        assert!(
            (mean - 500.0).abs() < 25.0,
            "sample mean {mean} not within 25 L of 500"
        );
    });
}

/// A tug that is waiting or refuelling does not arrive again (sim.md §6); once
/// it departs, it arrives again.
#[test]
fn tug_does_not_arrive_again_before_departing() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let (mut rx, _tasks) = spawn_generator(clock.clone(), &ROSTER_A);

        // The first arrival; its `departed` is held, so the tug is still at the
        // dock.
        let first = rx.recv().await.unwrap();
        clock.sleep(20 * VESSEL_A.mean_time_away).await;
        assert!(
            rx.try_recv().is_err(),
            "the tug arrived again before it had departed"
        );

        first.departed.send(()).unwrap();
        // The chance that an exponential time away exceeds 100 means is e^-100.
        let again = tokio::select! {
            arrival = rx.recv() => arrival,
            () = clock.sleep(100 * VESSEL_A.mean_time_away) => None,
        };
        let again = again.expect("the tug did not arrive again after departing");
        assert_eq!(again.job.vessel_id, VESSEL_A.vessel_id);
    });
}

/// Each tug's stream derives from the seed and its own IMO number, so adding a
/// tug to the roster leaves the others' times away and volumes unchanged.
#[test]
fn adding_a_tug_leaves_other_sequences_unchanged() {
    let alone = {
        let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
        runtime.block_on(async {
            let (mut rx, _tasks) = spawn_generator(clock.clone(), &ROSTER_A);
            sequence_of(&clock, &mut rx, VESSEL_A.vessel_id, 20).await
        })
    };
    let with_b = {
        let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
        runtime.block_on(async {
            let (mut rx, _tasks) = spawn_generator(clock.clone(), &ROSTER_AB);
            sequence_of(&clock, &mut rx, VESSEL_A.vessel_id, 20).await
        })
    };
    assert_eq!(alone, with_b, "adding vessel B changed vessel A's sequence");
}

/// The draws do not depend on the clock mode.
#[test]
fn sequence_independent_of_clock_mode() {
    let volumes_under = |model: TimeModel| -> Vec<f64> {
        let (clock, runtime) = SimClock::isolated(model, SIM_START);
        runtime.block_on(async {
            let (mut rx, _tasks) = spawn_generator(clock.clone(), &ROSTER_A);
            sequence_of(&clock, &mut rx, VESSEL_A.vessel_id, 10)
                .await
                .into_iter()
                .map(|(_, volume_l)| volume_l)
                .collect()
        })
    };
    assert_eq!(
        volumes_under(TimeModel::Synthetic),
        volumes_under(TimeModel::WallTimeMultiple(3600.0)),
        "vessel A's requested volumes differ between the clock modes"
    );
}

static EMPTY_ROSTER: [Vessel; 0] = [];
static ZERO_MEAN_ROSTER: [Vessel; 1] = [Vessel {
    mean_time_away: Duration::ZERO,
    ..VESSEL_A
}];

/// A configuration the generator must refuse: its name, roster, refuel range,
/// and the word the refusal's message must contain.
type RefusedCase = (&'static str, &'static [Vessel], (f64, f64), &'static str);

/// Each case is called separately, inside the runtime, so that a case the
/// generator accepted would spawn rather than panic for want of a runtime. The
/// panic must name what was wrong, so that it is the generator's own check that
/// refuses the case.
#[test]
fn invalid_configuration_refused_at_spawn() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    let ok = (REFUEL_VOLUME_MIN_L, REFUEL_VOLUME_MAX_L);
    let cases: [RefusedCase; 10] = [
        ("empty roster", &EMPTY_ROSTER, ok, "roster"),
        (
            "mean time away of zero",
            &ZERO_MEAN_ROSTER,
            ok,
            "mean_time_away",
        ),
        (
            "minimum equal to the maximum",
            &ROSTER_A,
            (800.0, 800.0),
            "refuel_volume",
        ),
        (
            "minimum above the maximum",
            &ROSTER_A,
            (900.0, 800.0),
            "refuel_volume",
        ),
        ("zero minimum", &ROSTER_A, (0.0, 800.0), "refuel_volume"),
        (
            "negative minimum",
            &ROSTER_A,
            (-100.0, 800.0),
            "refuel_volume",
        ),
        ("NaN minimum", &ROSTER_A, (f64::NAN, 800.0), "refuel_volume"),
        ("NaN maximum", &ROSTER_A, (200.0, f64::NAN), "refuel_volume"),
        (
            "infinite minimum",
            &ROSTER_A,
            (f64::NEG_INFINITY, 800.0),
            "refuel_volume",
        ),
        (
            "infinite maximum",
            &ROSTER_A,
            (200.0, f64::INFINITY),
            "refuel_volume",
        ),
    ];
    runtime.block_on(async {
        for (name, vessels, (min, max), named) in cases {
            let (tx, _rx) = mpsc::channel(16);
            let (_stop, stop_rx) = stop_channel();
            let (_engineer_at_rest, engineer_at_rest_rx) = oneshot::channel();
            let (report_tx, _report) = oneshot::channel();
            let outcome = panic::catch_unwind(AssertUnwindSafe(|| {
                tug_arrival_generator::spawn(
                    clock.clone(),
                    vessels,
                    min,
                    max,
                    TugArrivalState::new(SEED, vessels),
                    stop_rx,
                    engineer_at_rest_rx,
                    report_tx,
                    tx,
                )
            }));
            let payload = match outcome {
                Ok(tasks) => {
                    tasks.iter().for_each(JoinHandle::abort);
                    panic!("{name} was accepted at spawn");
                }
                Err(payload) => payload,
            };
            let message = panic_message(payload.as_ref());
            assert!(
                message.contains(named),
                "{name} was refused, but not by the check naming {named}: {message}"
            );
        }
    });
}

/// The task can no longer tell when its tug left.
#[test]
fn dropped_departure_signal_is_fatal() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let (mut rx, mut tasks) = spawn_generator(clock.clone(), &ROSTER_A);

        // The arrival is dropped, and its `departed` sender with it.
        drop(rx.recv().await.unwrap());

        // The first task is the tug's; the last collects the tugs' reports.
        let task = tasks.remove(0);
        let outcome = tokio::select! {
            outcome = task => outcome,
            () = clock.sleep(Duration::from_secs(60)) => {
                panic!("the generator still runs after the departure signal was dropped")
            }
        };
        let error = outcome.expect_err("the generator returned rather than panicked");
        assert!(
            error.is_panic(),
            "the generator was cancelled, not panicked"
        );
        let payload = error.into_panic();
        let message = panic_message(payload.as_ref());
        assert!(
            message.contains("depart"),
            "the generator panicked, but not on the dropped departure signal: {message}"
        );
    });
}

// ── Stopping and warm start (sim-tests.md, "Stopping and warm start") ────────

/// Yield until the generator's tasks have run as far as they can without the
/// clock advancing.
async fn settle() {
    for _ in 0..16 {
        tokio::task::yield_now().await;
    }
}

/// The report of a stopped generator, sent once the tug engineer's report is
/// signalled.
async fn report_after_engineer(controls: Controls) -> TugArrivalState {
    let Controls {
        stop,
        engineer_at_rest,
        report,
    } = controls;
    stop.stop();
    engineer_at_rest.send(()).unwrap();
    let state = report.await.expect("the generator dropped its report");
    // Kept so that the stop request stays live while the test ends.
    drop(stop);
    state
}

/// A tug's first scheduled arrival from a cold start at `SIM_START`, and its
/// stream's position between the draw of that time away and the draw of its
/// volume, computed apart from the generator: the seed, the stream set to the
/// tug's IMO number, and one unit exponential.
fn first_time_away(vessel: &Vessel) -> (PrimitiveDateTime, u128) {
    let mut rng = ChaCha8Rng::seed_from_u64(SEED);
    rng.set_stream(u64::from(vessel.vessel_id.0));
    let unit: f64 = Exp1.sample(&mut rng);
    (
        SIM_START + vessel.mean_time_away.mul_f64(unit),
        rng.get_word_pos(),
    )
}

#[test]
fn generator_reports_next_due_time_and_sends_nothing_after_stop() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let (mut rx, _tasks, controls) = spawn_generator_from(
            clock.clone(),
            &ROSTER_ABC,
            TugArrivalState::new(SEED, &ROSTER_ABC),
        );
        settle().await;
        let state = report_after_engineer(controls).await;

        assert_eq!(state.seed, SEED);
        assert_eq!(state.tugs.len(), ROSTER_ABC.len());
        let mut last_due = SIM_START;
        for (tug, vessel) in state.tugs.iter().zip(&ROSTER_ABC) {
            let (due, between_draws) = first_time_away(vessel);
            assert_eq!(tug.vessel_id, vessel.vessel_id);
            assert_eq!(
                tug.phase,
                TugPhase::Away {
                    next_arrival_at: Some(due)
                },
                "tug {} reports its first arrival's due time",
                vessel.name
            );
            assert_eq!(
                tug.word_pos, between_draws,
                "tug {} reports the position between the draws of its first time away and its \
                 volume",
                vessel.name
            );
            last_due = last_due.max(due);
        }

        clock
            .sleep((last_due - SIM_START).unsigned_abs() + Duration::from_secs(60))
            .await;
        assert!(rx.try_recv().is_err(), "an arrival was sent after the stop");
    });
}

#[test]
fn generator_reports_a_tug_at_the_dock() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let (mut rx, _tasks, controls) = spawn_generator_from(
            clock.clone(),
            &ROSTER_ABC,
            TugArrivalState::new(SEED, &ROSTER_ABC),
        );
        // One tug arrives, and the tug engineer does not take it up.
        let arrival = rx.recv().await.unwrap();
        let state = report_after_engineer(controls).await;

        for (tug, vessel) in state.tugs.iter().zip(&ROSTER_ABC) {
            let (due, between_draws) = first_time_away(vessel);
            let phase = if vessel.vessel_id == arrival.job.vessel_id {
                TugPhase::AtDock { job: arrival.job }
            } else {
                TugPhase::Away {
                    next_arrival_at: Some(due),
                }
            };
            assert_eq!(tug.phase, phase, "tug {}", vessel.name);
            assert_eq!(
                tug.word_pos, between_draws,
                "tug {} reports the position between the draws of its time away and its volume",
                vessel.name
            );
        }
        drop(arrival);
    });
}

#[test]
fn tug_leaving_after_stop_reports_its_next_due_time() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let (mut rx, _tasks, controls) = spawn_generator_from(
            clock.clone(),
            &ROSTER_A,
            TugArrivalState::new(SEED, &ROSTER_A),
        );
        let arrival = rx.recv().await.unwrap();
        let left_at = clock.now_datetime();
        // The refuel in progress completes after the stop, and the tug leaves
        // before the tug engineer reports.
        controls.stop.stop();
        arrival.departed.send(()).unwrap();
        let Controls {
            stop,
            engineer_at_rest,
            report,
        } = controls;
        let state = report.await.unwrap();
        let TugPhase::Away {
            next_arrival_at: Some(due),
        } = state.tugs[0].phase
        else {
            panic!("expected the tug away with a due time: {state:?}");
        };

        // As it left, it drew its next time away and requested volume, as at
        // any departure (sim.md §9.1), and it reports the position between the
        // two draws.
        let mut rng = ChaCha8Rng::seed_from_u64(SEED);
        rng.set_stream(u64::from(VESSEL_A.vessel_id.0));
        let _first_time_away: f64 = Exp1.sample(&mut rng);
        let volumes = Uniform::new(REFUEL_VOLUME_MIN_L, REFUEL_VOLUME_MAX_L).unwrap();
        assert_eq!(volumes.sample(&mut rng), arrival.job.volume_l);
        let unit: f64 = Exp1.sample(&mut rng);
        assert_eq!(due, left_at + VESSEL_A.mean_time_away.mul_f64(unit));
        assert_eq!(state.tugs[0].word_pos, rng.get_word_pos());
        drop((stop, engineer_at_rest));
    });
}

/// A run of the three tugs from `start`, in a runtime of its own whose clock
/// starts at `at`, every tug leaving the dock the instant it arrives, until
/// each tug has left the dock as many times as `departures` gives for it.
/// Returns each tug's jobs in the order drawn, the instant of the last
/// arrival, and the generator's report after a stop at that instant.
fn departures_then_report(
    at: PrimitiveDateTime,
    start: TugArrivalState,
    departures: &HashMap<u32, usize>,
) -> (
    HashMap<u32, Vec<DispenseJob>>,
    PrimitiveDateTime,
    TugArrivalState,
) {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, at);
    runtime.block_on(async {
        let (mut rx, _tasks, controls) = spawn_generator_from(clock.clone(), &ROSTER_ABC, start);
        let mut jobs: HashMap<u32, Vec<DispenseJob>> = HashMap::new();
        let left =
            |jobs: &HashMap<u32, Vec<DispenseJob>>, tug: u32| jobs.get(&tug).map_or(0, Vec::len);
        while departures.iter().any(|(tug, n)| left(&jobs, *tug) < *n) {
            let arrival = rx.recv().await.unwrap();
            jobs.entry(arrival.job.vessel_id.0)
                .or_default()
                .push(arrival.job);
            arrival.departed.send(()).unwrap();
        }
        let stopped_at = clock.now_datetime();
        (jobs, stopped_at, report_after_engineer(controls).await)
    })
}

/// A tug's times away and requested volumes, from its jobs in the order drawn,
/// the tug having left the dock the instant it arrived.
fn draws_of(jobs: &[DispenseJob]) -> Vec<(time::Duration, u64)> {
    let mut left_at = SIM_START;
    jobs.iter()
        .map(|job| {
            let time_away = job.arrived_at - left_at;
            left_at = job.arrived_at;
            (time_away, job.volume_l.to_bits())
        })
        .collect()
}

#[test]
fn each_tugs_draws_repeat_across_a_stop() {
    let tugs = || ROSTER_ABC.iter().map(|vessel| vessel.vessel_id.0);
    let all = |n: usize| tugs().map(|tug| (tug, n)).collect::<HashMap<_, _>>();
    let (uninterrupted, _, _) =
        departures_then_report(SIM_START, TugArrivalState::new(SEED, &ROSTER_ABC), &all(20));

    let (mut restored, stopped_at, state) =
        departures_then_report(SIM_START, TugArrivalState::new(SEED, &ROSTER_ABC), &all(10));
    let remaining = tugs()
        .map(|tug| (tug, 20usize.saturating_sub(restored[&tug].len())))
        .collect();
    let (after, _, _) = departures_then_report(stopped_at, state, &remaining);
    for (tug, jobs) in after {
        restored.entry(tug).or_default().extend(jobs);
    }

    for tug in tugs() {
        assert_eq!(
            draws_of(&restored[&tug])[..20],
            draws_of(&uninterrupted[&tug])[..20],
            "tug {tug}'s times away and volumes across the stop differ from an uninterrupted \
             run's"
        );
    }
}

/// A tug saved at the dock, not taken up, draws its volume again at the warm
/// start and leaves its stream where the uninterrupted run left it: its later
/// arrivals and volumes follow the uninterrupted run's.
#[test]
fn a_tug_at_the_dock_repeats_its_draws_across_a_stop() {
    let uninterrupted = {
        let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
        runtime.block_on(async {
            let (mut rx, _tasks) = spawn_generator(clock.clone(), &ROSTER_A);
            let mut jobs = Vec::new();
            for _ in 0..3 {
                let arrival = rx.recv().await.unwrap();
                jobs.push(arrival.job);
                arrival.departed.send(()).unwrap();
            }
            jobs
        })
    };

    // The first arrival is not taken up before the stop.
    let (first, state) = {
        let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
        runtime.block_on(async {
            let (mut rx, _tasks, controls) = spawn_generator_from(
                clock.clone(),
                &ROSTER_A,
                TugArrivalState::new(SEED, &ROSTER_A),
            );
            let arrival = rx.recv().await.unwrap();
            let state = report_after_engineer(controls).await;
            (arrival.job, state)
        })
    };
    assert_eq!(state.tugs[0].phase, TugPhase::AtDock { job: first });

    // The warm start sends the saved arrival again; the tug leaves at once.
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, first.arrived_at);
    let restored = runtime.block_on(async {
        let (mut rx, _tasks, controls) = spawn_generator_from(clock.clone(), &ROSTER_A, state);
        let mut jobs = Vec::new();
        for _ in 0..3 {
            let arrival = rx.recv().await.unwrap();
            jobs.push(arrival.job);
            arrival.departed.send(()).unwrap();
        }
        // Never stopped: the controls are kept for the life of the test process.
        std::mem::forget(controls);
        jobs
    });

    let key = |job: &DispenseJob| (job.arrived_at, job.volume_l.to_bits());
    assert_eq!(
        restored.iter().map(key).collect::<Vec<_>>(),
        uninterrupted.iter().map(key).collect::<Vec<_>>(),
        "the arrivals of a tug restored at the dock differ from an uninterrupted run's"
    );
}

#[test]
fn saved_tugs_at_the_dock_arrive_first_in_order_of_arrival() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let job = |vessel: &Vessel, minutes_before: i64| DispenseJob {
            arrived_at: SIM_START - time::Duration::minutes(minutes_before),
            vessel_id: vessel.vessel_id,
            vessel_name: vessel.name,
            volume_l: 1000.0,
            rfid_card: vessel.rfid_card,
        };
        let (job_a, job_b) = (job(&VESSEL_A, 2), job(&VESSEL_B, 5));
        let start = TugArrivalState {
            seed: SEED,
            tugs: vec![
                TugState {
                    vessel_id: VESSEL_A.vessel_id,
                    word_pos: 0,
                    phase: TugPhase::AtDock { job: job_a },
                },
                TugState {
                    vessel_id: VESSEL_B.vessel_id,
                    word_pos: 0,
                    phase: TugPhase::AtDock { job: job_b },
                },
            ],
        };
        let (mut rx, _tasks, controls) = spawn_generator_from(clock.clone(), &ROSTER_AB, start);

        let first = rx.try_recv().expect("a saved tug arrives at spawn");
        let second = rx.try_recv().expect("both saved tugs arrive at spawn");
        assert_eq!(first.job, job_b, "tug B arrived first");
        assert_eq!(second.job, job_a);
        assert_eq!(
            clock.now_datetime(),
            SIM_START,
            "they arrived without the clock advancing"
        );
        std::mem::forget((first, second, controls));
    });
}

#[test]
fn starting_state_not_matching_the_roster_is_refused() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let outcome = panic::catch_unwind(AssertUnwindSafe(|| {
            spawn_generator_from(
                clock.clone(),
                &ROSTER_AB,
                TugArrivalState::new(SEED, &ROSTER_A),
            )
        }));
        let payload = outcome
            .err()
            .expect("a state for another roster was accepted");
        assert!(panic_message(payload.as_ref()).contains("roster"));
    });
}
