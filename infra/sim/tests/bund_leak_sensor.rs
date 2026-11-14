//! Integration tests for `sim::physical::bund_leak_sensor`.
//!
//! Exercises the entity through its public `spawn()` interface only, holding
//! the channel ends that neighbouring entities would own (see sim-tests.md).
//! The sensor is purely event-driven, so these are plain `#[tokio::test]`s.

use tokio::sync::watch;

use sim::physical::bund_leak_sensor::{self, BundReading};

#[tokio::test]
async fn wet_reading_published_on_bund_wet_signal() {
    let (bund_wet_tx, bund_wet_rx) = watch::channel(false);
    let (bund_tx, mut bund_rx) = watch::channel(BundReading::default());
    bund_leak_sensor::spawn(bund_wet_rx, bund_tx);

    bund_wet_tx.send(true).unwrap();
    bund_rx.changed().await.unwrap();
    assert!(bund_rx.borrow().wet);
}

#[tokio::test]
async fn dry_reading_published_on_bund_dry_signal() {
    let (bund_wet_tx, bund_wet_rx) = watch::channel(false);
    let (bund_tx, mut bund_rx) = watch::channel(BundReading::default());
    bund_leak_sensor::spawn(bund_wet_rx, bund_tx);

    // Establish the wet state first.
    bund_wet_tx.send(true).unwrap();
    bund_rx.changed().await.unwrap();
    assert!(bund_rx.borrow().wet);

    // The transition back to dry must also be published — confirming the
    // sensor does not latch.
    bund_wet_tx.send(false).unwrap();
    bund_rx.changed().await.unwrap();
    assert!(!bund_rx.borrow().wet);
}
