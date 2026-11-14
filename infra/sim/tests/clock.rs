//! Integration tests for `sim::clock`.
//!
//! Each test drives the clock through its public interface only
//! (`SimClock::isolated`, `SimClock::now`, `SimClock::now_datetime`,
//! `SimClock::sleep`). Synthetic-mode tests run their body under the runtime
//! returned by `isolated`; `WallTimeMultiple` tests additionally take control of
//! Tokio's timer via `tokio::time::pause`/`advance` (see sim-tests.md).

use std::time::Duration;

use sim::clock::{SimClock, TimeModel};
use time::macros::datetime;
use time::PrimitiveDateTime;

/// Sim-start epoch used across the tests (the calendar time of simulated t=0).
const SIM_START: PrimitiveDateTime = datetime!(2025-01-01 0:00);

// ---------------------------------------------------------------------------
// Synthetic mode
// ---------------------------------------------------------------------------

#[test]
fn now_is_stable_without_sleep() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let t0 = clock.now();
        let t1 = clock.now();
        assert_eq!(t1.duration_since(t0), Duration::ZERO);
    });
}

#[test]
fn sleep_advances_simulated_time() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let t0 = clock.now();
        clock.sleep(Duration::from_secs(60)).await;
        assert_eq!(clock.now().duration_since(t0), Duration::from_secs(60));
    });
}

#[test]
fn sequential_sleeps_accumulate() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let t0 = clock.now();
        clock.sleep(Duration::from_secs(30)).await;
        clock.sleep(Duration::from_secs(45)).await;
        assert_eq!(clock.now().duration_since(t0), Duration::from_secs(75));
    });
}

#[test]
fn sleep_does_not_consume_wall_clock_time() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let wall_start = std::time::Instant::now();
        clock.sleep(Duration::from_secs(3600)).await;
        assert!(
            wall_start.elapsed() < Duration::from_millis(100),
            "synthetic sleep consumed real time: {:?}",
            wall_start.elapsed()
        );
    });
}

#[test]
fn concurrent_sleeps_resolve_in_deadline_order() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let t0 = clock.now();
        let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel();

        // Register sleeps in the order 30 s, 10 s, 60 s — a non-sorted order,
        // so a correct implementation must resolve them by deadline, not by
        // registration order.
        let mut handles = Vec::new();
        for secs in [30u64, 10, 60] {
            let clock = clock.clone();
            let tx = tx.clone();
            handles.push(tokio::spawn(async move {
                clock.sleep(Duration::from_secs(secs)).await;
                tx.send(clock.now()).unwrap();
            }));
        }
        drop(tx);

        for handle in handles {
            handle.await.unwrap();
        }

        let mut elapsed = Vec::new();
        while let Ok(instant) = rx.try_recv() {
            elapsed.push(instant.duration_since(t0));
        }

        assert_eq!(elapsed.len(), 3);
        assert!(
            elapsed[0] < elapsed[1] && elapsed[1] < elapsed[2],
            "instants not strictly increasing: {elapsed:?}"
        );
        assert_eq!(elapsed[0], Duration::from_secs(10));
        assert_eq!(elapsed[1], Duration::from_secs(30));
        assert_eq!(elapsed[2], Duration::from_secs(60));
    });
}

#[test]
fn clone_shares_simulated_time() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let clock2 = clock.clone();
        let t0 = clock.now();
        let handle = tokio::spawn(async move {
            clock2.sleep(Duration::from_secs(30)).await;
        });
        handle.await.unwrap();
        assert_eq!(clock.now().duration_since(t0), Duration::from_secs(30));
    });
}

#[test]
fn cancelled_sleep_does_not_stall_later_sleeps() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let t0 = clock.now();

        // The 5 s sleep is cancelled when the 1 s sleep wins the race.
        tokio::select! {
            _ = clock.sleep(Duration::from_secs(1)) => {}
            _ = clock.sleep(Duration::from_secs(5)) => unreachable!("the 1 s sleep wins"),
        }
        assert_eq!(clock.now().duration_since(t0), Duration::from_secs(1));

        // The cancelled sleeper's deadline (5 s) must not advance time to a
        // notifier nobody awaits — which would park the runtime with nothing
        // left to wake it. A subsequent 10 s sleep must still complete at 11 s.
        clock.sleep(Duration::from_secs(10)).await;
        assert_eq!(clock.now().duration_since(t0), Duration::from_secs(11));
    });
}

// ---------------------------------------------------------------------------
// Wall-clock date-time
// ---------------------------------------------------------------------------

#[test]
fn now_datetime_is_epoch_plus_elapsed() {
    // An epoch close to midnight, so the elapsed sleep crosses a day boundary
    // and exercises the calendar arithmetic.
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, datetime!(2025-01-01 23:30));
    runtime.block_on(async {
        assert_eq!(clock.now_datetime(), datetime!(2025-01-01 23:30));
        clock.sleep(Duration::from_secs(3600)).await; // one hour, across midnight
        assert_eq!(clock.now_datetime(), datetime!(2025-01-02 0:30));
    });
}

// ---------------------------------------------------------------------------
// WallTimeMultiple mode
// ---------------------------------------------------------------------------

#[test]
fn sleep_completes_when_tokio_time_advances_by_scaled_duration() {
    let (clock, runtime) = SimClock::isolated(TimeModel::WallTimeMultiple(3.0), SIM_START);
    runtime.block_on(async {
        tokio::time::pause();
        let sleeper = tokio::spawn({
            let clock = clock.clone();
            async move {
                clock.sleep(Duration::from_secs(3)).await;
            }
        });
        // Let the spawned task arm its underlying Tokio timer before advancing.
        tokio::task::yield_now().await;
        // sleep(3 s) delegates to tokio::time::sleep(3 / 3 = 1 s).
        tokio::time::advance(Duration::from_secs(1)).await;
        sleeper.await.unwrap();
    });
}

#[test]
fn simulated_now_advances_by_full_sleep_duration() {
    let (clock, runtime) = SimClock::isolated(TimeModel::WallTimeMultiple(3.0), SIM_START);
    runtime.block_on(async {
        tokio::time::pause();
        let t0 = clock.now();
        let sleeper = tokio::spawn({
            let clock = clock.clone();
            async move {
                clock.sleep(Duration::from_secs(3)).await;
            }
        });
        tokio::task::yield_now().await;
        tokio::time::advance(Duration::from_secs(1)).await;
        sleeper.await.unwrap();
        // now() tracks the full simulated duration (~3 s), not the scaled
        // Tokio advance (1 s). The upper bound absorbs Tokio's 1 ms timer
        // granularity (the 1 s sleep fires at 1.001 s, so now() reads
        // 3.003 s); the point is that it is ~3 s, not 1 s.
        let elapsed = clock.now().duration_since(t0);
        assert!(
            elapsed >= Duration::from_secs(3)
                && elapsed < Duration::from_secs(3) + Duration::from_millis(50),
            "now() should report ~3 s of simulated time, got {elapsed:?}"
        );
    });
}

#[test]
fn multiplier_scales_tokio_sleep_proportionally() {
    let (clock, runtime) = SimClock::isolated(TimeModel::WallTimeMultiple(6.0), SIM_START);
    runtime.block_on(async {
        tokio::time::pause();
        let sleeper = tokio::spawn({
            let clock = clock.clone();
            async move {
                clock.sleep(Duration::from_secs(6)).await;
            }
        });
        tokio::task::yield_now().await;
        // sleep(6 s) delegates to tokio::time::sleep(6 / 6 = 1 s).
        tokio::time::advance(Duration::from_secs(1)).await;
        sleeper.await.unwrap();
    });
}

// ---------------------------------------------------------------------------
// Constructor validation
// ---------------------------------------------------------------------------

#[test]
#[should_panic]
fn zero_multiplier_panics() {
    let _ = SimClock::isolated(TimeModel::WallTimeMultiple(0.0), SIM_START);
}

#[test]
#[should_panic]
fn negative_multiplier_panics() {
    let _ = SimClock::isolated(TimeModel::WallTimeMultiple(-1.0), SIM_START);
}

#[test]
#[should_panic]
fn infinite_multiplier_panics() {
    let _ = SimClock::isolated(TimeModel::WallTimeMultiple(f64::INFINITY), SIM_START);
}

#[test]
#[should_panic]
fn nan_multiplier_panics() {
    let _ = SimClock::isolated(TimeModel::WallTimeMultiple(f64::NAN), SIM_START);
}

// ---------------------------------------------------------------------------
// Coupled mode (sim.md §2.1)
// ---------------------------------------------------------------------------
//
// Against the digital world the clock runs at 1:1 and starts at the current UTC
// time, so a date the sim writes on paper is the date the digital world records
// (sim.md §8.3). Narrative §5 holds the digital world's clocks to within one
// second, and the sim — standing in for the real world those clocks measure — is
// held to the same bar. These run against the real system clock; this is the
// only place the sim's *own* time can be checked to that resolution.

mod coupled {
    use super::*;

    /// The current UTC time in the sim's timezone-less representation.
    fn utc_now() -> PrimitiveDateTime {
        let now = time::OffsetDateTime::now_utc();
        PrimitiveDateTime::new(now.date(), now.time())
    }

    fn within_one_second(a: PrimitiveDateTime, b: PrimitiveDateTime) -> bool {
        (a - b).abs() <= time::Duration::SECOND
    }

    #[test]
    fn coupled_clock_starts_at_the_current_utc_time() {
        let (clock, runtime) = SimClock::coupled();
        runtime.block_on(async {
            let sim = clock.now_datetime();
            let utc = utc_now();
            assert!(
                within_one_second(sim, utc),
                "sim starts at {sim}, UTC is {utc}: a fixed or local start date puts \
                 every paper artefact out of step with the digital world"
            );
        });
    }

    /// 1:1 pace: a sleep takes its own length of wall time, simulated time
    /// advances by the wall time that passed, and the calendar is still with
    /// UTC afterwards.
    #[test]
    fn coupled_clock_runs_at_wall_clock_pace_and_stays_with_utc() {
        let (clock, runtime) = SimClock::coupled();
        runtime.block_on(async {
            let wall = std::time::Instant::now();
            let t0 = clock.now();
            clock.sleep(Duration::from_millis(300)).await;
            let wall_elapsed = wall.elapsed();
            let sim_elapsed = clock.now().duration_since(t0);

            assert!(
                wall_elapsed >= Duration::from_millis(300),
                "slept too short"
            );
            assert!(
                (sim_elapsed.as_secs_f64() - wall_elapsed.as_secs_f64()).abs() < 0.1,
                "simulated {sim_elapsed:?} against wall {wall_elapsed:?}: not 1:1"
            );
            let sim = clock.now_datetime();
            let utc = utc_now();
            assert!(
                within_one_second(sim, utc),
                "sim {sim} drifted from UTC {utc}"
            );
        });
    }
}
