//! Integration tests for `sim::physical::atg_probe`.
//!
//! Exercises the entity through its public `spawn()` interface only, holding
//! the channel ends that neighbouring entities would own (see sim-tests.md).
//! The probe is purely event-driven, so these are plain `#[tokio::test]`s.
//!
//! Each reading round-trip interleaves three steps: the test (ATG console
//! role) sends a reading request; the probe forwards a sounding request, which
//! the test (fuel tank role) services with a fixed `TankSounding`; the probe
//! applies the noise model and replies.

use tokio::sync::{mpsc, oneshot};

use sim::physical::atg_probe::{self, AtgProbeState, ProbeReading};
use sim::physical::fuel_tank::TankSounding;

const SEED: u64 = 42;

/// Drive one reading request through the probe, servicing its sounding request
/// with `reply`, and return the resulting `ProbeReading`.
async fn read_once(
    reading_tx: &mpsc::Sender<oneshot::Sender<ProbeReading>>,
    sounding_rx: &mut mpsc::Receiver<oneshot::Sender<TankSounding>>,
    reply: TankSounding,
) -> ProbeReading {
    let (probe_reply_tx, probe_reply_rx) = oneshot::channel();
    reading_tx.send(probe_reply_tx).await.unwrap();
    let sounding_reply_tx = sounding_rx
        .recv()
        .await
        .expect("probe forwards a sounding request");
    sounding_reply_tx.send(reply).unwrap();
    probe_reply_rx.await.unwrap()
}

#[tokio::test]
async fn reading_request_forwarded_to_tank() {
    let (reading_tx, reading_rx) = mpsc::channel(16);
    let (sounding_tx, mut sounding_rx) = mpsc::channel(16);
    let (_state_tx, state_rx) = mpsc::channel(1);
    atg_probe::spawn(
        reading_rx,
        sounding_tx,
        false,
        AtgProbeState::new(SEED),
        state_rx,
    );

    let (probe_reply_tx, _probe_reply_rx) = oneshot::channel();
    reading_tx.send(probe_reply_tx).await.unwrap();

    // The probe must forward the request to the tank rather than caching or
    // short-circuiting it.
    let sounding_reply_tx = sounding_rx
        .recv()
        .await
        .expect("probe should forward a sounding request");
    // Reply to unblock the probe.
    sounding_reply_tx
        .send(TankSounding {
            level_mm: 5000.0,
            temperature_c: 15.0,
        })
        .unwrap();
}

#[tokio::test]
async fn probe_reading_reflects_tank_sounding_with_noise_disabled() {
    let (reading_tx, reading_rx) = mpsc::channel(16);
    let (sounding_tx, mut sounding_rx) = mpsc::channel(16);
    let (_state_tx, state_rx) = mpsc::channel(1);
    atg_probe::spawn(
        reading_rx,
        sounding_tx,
        false,
        AtgProbeState::new(SEED),
        state_rx,
    );

    let sounding = TankSounding {
        level_mm: 1234.5,
        temperature_c: 15.0,
    };
    let reading = read_once(&reading_tx, &mut sounding_rx, sounding).await;
    assert_eq!(reading.level_mm, sounding.level_mm);
    assert_eq!(reading.temperature_c, sounding.temperature_c);
}

#[tokio::test]
async fn noise_disabled_gives_deterministic_readings() {
    let (reading_tx, reading_rx) = mpsc::channel(16);
    let (sounding_tx, mut sounding_rx) = mpsc::channel(16);
    let (_state_tx, state_rx) = mpsc::channel(1);
    atg_probe::spawn(
        reading_rx,
        sounding_tx,
        false,
        AtgProbeState::new(SEED),
        state_rx,
    );

    let sounding = TankSounding {
        level_mm: 1234.5,
        temperature_c: 15.0,
    };
    let first = read_once(&reading_tx, &mut sounding_rx, sounding).await;
    let second = read_once(&reading_tx, &mut sounding_rx, sounding).await;
    assert_eq!(first.level_mm, second.level_mm);
}

#[tokio::test]
async fn noise_enabled_approximates_correct_distribution() {
    const N: usize = 1000;
    const L: f64 = 5000.0;
    const SIGMA: f64 = 0.25;

    let (reading_tx, reading_rx) = mpsc::channel(16);
    let (sounding_tx, mut sounding_rx) = mpsc::channel(16);
    let (_state_tx, state_rx) = mpsc::channel(1);
    atg_probe::spawn(
        reading_rx,
        sounding_tx,
        true,
        AtgProbeState::new(SEED),
        state_rx,
    );

    let sounding = TankSounding {
        level_mm: L,
        temperature_c: 15.0,
    };
    let mut samples = Vec::with_capacity(N);
    for _ in 0..N {
        samples.push(
            read_once(&reading_tx, &mut sounding_rx, sounding)
                .await
                .level_mm,
        );
    }

    let mean = samples.iter().sum::<f64>() / N as f64;
    let variance = samples.iter().map(|&x| (x - mean).powi(2)).sum::<f64>() / (N as f64 - 1.0);
    let std_dev = variance.sqrt();

    assert!(
        (mean - L).abs() < 0.05,
        "sample mean {mean} not within 0.05 mm of {L}"
    );
    assert!(
        (std_dev - SIGMA).abs() < 0.1 * SIGMA,
        "sample std dev {std_dev} not within 10% of {SIGMA}"
    );
}
