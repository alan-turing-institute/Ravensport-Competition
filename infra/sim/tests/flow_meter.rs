//! Integration tests for `sim::physical::flow_meter`.
//!
//! Exercises the entity through its public `spawn()` interface only, holding
//! the channel ends that neighbouring entities would own (see sim-tests.md).
//! All tests run under `SimClock::isolated(TimeModel::Synthetic, SIM_START)`.

use std::time::Duration;

use tokio::sync::{mpsc, watch};

use sim::clock::{SimClock, TimeModel};
use sim::physical::flow_meter::{self, Pulse};
use sim::physical::fuel_tank::OutflowRate;
use time::macros::datetime;
use time::PrimitiveDateTime;

const SIM_START: PrimitiveDateTime = datetime!(2025-01-01 0:00);
const K_FACTOR: f64 = 10.0;
const ACTIVE_RATE_L_PER_MIN: f64 = 60.0;
// Pulse interval = 60 / (k_factor × rate) = 60 / (10 × 60) = 0.1 s.

fn secs(value: f64) -> Duration {
    Duration::from_secs_f64(value)
}

fn drain(pulse_rx: &mut mpsc::Receiver<Pulse>) -> Vec<Pulse> {
    let mut pulses = Vec::new();
    while let Ok(pulse) = pulse_rx.try_recv() {
        pulses.push(pulse);
    }
    pulses
}

#[test]
fn no_pulses_when_outflow_inactive() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let (_outflow_rate_tx, outflow_rate_rx) = watch::channel::<OutflowRate>(None);
        let (pulse_tx, mut pulse_rx) = mpsc::channel(1024);
        flow_meter::spawn(clock.clone(), K_FACTOR, outflow_rate_rx, pulse_tx);

        clock.sleep(secs(10.0)).await;
        assert!(pulse_rx.try_recv().is_err());
    });
}

#[test]
fn pulses_match_outflow_rate_and_k_factor() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let (outflow_rate_tx, outflow_rate_rx) = watch::channel::<OutflowRate>(None);
        let (pulse_tx, mut pulse_rx) = mpsc::channel(1024);
        flow_meter::spawn(clock.clone(), K_FACTOR, outflow_rate_rx, pulse_tx);

        outflow_rate_tx.send(Some(ACTIVE_RATE_L_PER_MIN)).unwrap();
        tokio::task::yield_now().await; // let the meter arm its first interval

        // 1.0 s = 10 complete 0.1 s intervals.
        clock.sleep(secs(1.0)).await;
        tokio::task::yield_now().await; // let the meter emit the boundary pulse

        let pulses = drain(&mut pulse_rx);
        assert_eq!(pulses.len(), 10);
        for pulse in &pulses {
            assert_eq!(pulse.k_factor, K_FACTOR);
        }
    });
}

#[test]
fn no_partial_pulse_on_mid_interval_deactivation() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let (outflow_rate_tx, outflow_rate_rx) = watch::channel::<OutflowRate>(None);
        let (pulse_tx, mut pulse_rx) = mpsc::channel(1024);
        flow_meter::spawn(clock.clone(), K_FACTOR, outflow_rate_rx, pulse_tx);

        outflow_rate_tx.send(Some(ACTIVE_RATE_L_PER_MIN)).unwrap();
        tokio::task::yield_now().await;

        // Half of one interval, then deactivate before it completes.
        clock.sleep(secs(0.05)).await;
        outflow_rate_tx.send(None).unwrap();
        tokio::task::yield_now().await;

        assert!(pulse_rx.try_recv().is_err());
    });
}

#[test]
fn pulses_stop_after_deactivation() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let (outflow_rate_tx, outflow_rate_rx) = watch::channel::<OutflowRate>(None);
        let (pulse_tx, mut pulse_rx) = mpsc::channel(1024);
        flow_meter::spawn(clock.clone(), K_FACTOR, outflow_rate_rx, pulse_tx);

        outflow_rate_tx.send(Some(ACTIVE_RATE_L_PER_MIN)).unwrap();
        tokio::task::yield_now().await;

        // 0.3 s = 3 complete intervals.
        clock.sleep(secs(0.3)).await;
        tokio::task::yield_now().await;
        assert_eq!(drain(&mut pulse_rx).len(), 3);

        // Deactivate, then advance well past several intervals: no more pulses.
        outflow_rate_tx.send(None).unwrap();
        tokio::task::yield_now().await;
        clock.sleep(secs(1.0)).await;
        tokio::task::yield_now().await;
        assert!(pulse_rx.try_recv().is_err());
    });
}

#[test]
fn pulses_resume_after_reactivation() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let (outflow_rate_tx, outflow_rate_rx) = watch::channel::<OutflowRate>(None);
        let (pulse_tx, mut pulse_rx) = mpsc::channel(1024);
        flow_meter::spawn(clock.clone(), K_FACTOR, outflow_rate_rx, pulse_tx);

        // Run for 3 intervals, then deactivate (as in pulses_stop_after_deactivation).
        outflow_rate_tx.send(Some(ACTIVE_RATE_L_PER_MIN)).unwrap();
        tokio::task::yield_now().await;
        clock.sleep(secs(0.3)).await;
        tokio::task::yield_now().await;
        assert_eq!(drain(&mut pulse_rx).len(), 3);

        outflow_rate_tx.send(None).unwrap();
        tokio::task::yield_now().await;
        clock.sleep(secs(1.0)).await;
        tokio::task::yield_now().await;
        assert!(pulse_rx.try_recv().is_err());

        // Reactivate: a fresh pulse train resumes.
        outflow_rate_tx.send(Some(ACTIVE_RATE_L_PER_MIN)).unwrap();
        tokio::task::yield_now().await;
        // 0.2 s = 2 complete intervals.
        clock.sleep(secs(0.2)).await;
        tokio::task::yield_now().await;
        assert_eq!(drain(&mut pulse_rx).len(), 2);
    });
}
