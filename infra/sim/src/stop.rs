//! The stop request and the coming to rest that follows it (sim.md §9.1,
//! sim-impl.md "Stopping and warm start").
//!
//! The orchestrator holds the [`StopSender`]; every entity that begins work of
//! its own holds a clone of the [`StopRequest`]. An entity checks the request
//! where it would begin work, never inside work in progress. Once stopped and
//! idle it reports its state with [`rest`], which never returns, so the entity
//! keeps every channel end it owns until the process exits.

use std::future::Future;

use time::PrimitiveDateTime;
use tokio::sync::{oneshot, watch};

use crate::clock::SimClock;

/// The orchestrator's end of the stop request.
pub struct StopSender(watch::Sender<bool>);

impl StopSender {
    /// Ask every entity holding a [`StopRequest`] to stop.
    pub fn stop(&self) {
        self.0.send_replace(true);
    }
}

/// Resolves once the sim has been asked to stop. Cloneable; every entity that
/// begins work of its own holds one.
#[derive(Clone)]
pub struct StopRequest(watch::Receiver<bool>);

impl StopRequest {
    /// Resolves once a stop has been requested; at once if it already has. A
    /// request whose sender is gone never resolves, so an entity spawned
    /// without an orchestrator (a test) runs until the process ends.
    pub async fn requested(&mut self) {
        if self.0.wait_for(|stop| *stop).await.is_err() {
            std::future::pending::<()>().await;
        }
    }

    pub fn is_requested(&self) -> bool {
        *self.0.borrow()
    }
}

/// A stop request and its sender.
pub fn stop_channel() -> (StopSender, StopRequest) {
    let (tx, rx) = watch::channel(false);
    (StopSender(tx), StopRequest(rx))
}

/// Report `state` on `report` and then wait for the process to exit. The task
/// never finishes, so the supervisor's rule that a finished task is a failure
/// holds while the sim comes to rest.
pub async fn rest<S>(entity: &str, report: oneshot::Sender<S>, state: S) -> ! {
    if report.send(state).is_err() {
        panic!("{entity}: the orchestrator dropped the report receiver");
    }
    std::future::pending().await
}

/// Await a peer's report, routed by the orchestrator (sim-impl.md, "Coming to
/// rest"). A dropped sender means the orchestrator has gone, which is fatal.
pub async fn after_peer(
    entity: &str,
    peer: &str,
    at_rest: impl Future<Output = Result<(), oneshot::error::RecvError>>,
) {
    at_rest
        .await
        .unwrap_or_else(|_| panic!("{entity}: the orchestrator dropped the {peer} report signal"));
}

/// Sleep until the due time `at` on the sim clock; a due time already passed
/// is a sleep of zero (sim-impl.md, "Warm start").
pub async fn sleep_until(clock: &SimClock, at: PrimitiveDateTime) {
    let now = clock.now_datetime();
    if at > now {
        clock.sleep((at - now).unsigned_abs()).await;
    }
}
