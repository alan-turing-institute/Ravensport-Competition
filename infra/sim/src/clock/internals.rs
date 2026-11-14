//! Private implementation of the logical clock. Not visible to other modules.

use std::collections::BTreeMap;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use time::PrimitiveDateTime;
use tokio::runtime::{Builder, Runtime};
use tokio::sync::Notify;

use super::{SimClock, SimInstant, TimeModel};

/// Shared clock state behind the `SimClock` handle. Identical across clones.
pub(super) struct Inner {
    /// Calendar date-time the sim's t=0 corresponds to. Added to the elapsed
    /// simulated duration to produce `now_datetime`.
    epoch: PrimitiveDateTime,
    mode: Mode,
    /// Count of boundary I/O round-trips a sim entity currently has in flight
    /// (each `IoGuard` holds it up). In `Synthetic` mode the `on_thread_park`
    /// hook refuses to advance time while this is non-zero, so a pending
    /// round-trip — real work the host completes in real microseconds — is
    /// drained before simulated time moves. Only sim entities ever touch it;
    /// mocked digital components must not (the sim server they call guards the
    /// exchange instead). Inert in `WallTime` mode (the hook is not installed).
    pending_io: AtomicUsize,
}

enum Mode {
    /// Simulated time is tracked explicitly and advanced by the
    /// `on_thread_park` hook when the scheduler is quiescent.
    Synthetic(Mutex<SyntheticState>),

    /// Simulated time is derived from Tokio's clock: elapsed Tokio time since
    /// `start`, scaled by `multiplier`. `start` is captured lazily on first
    /// use inside the runtime (Tokio's clock is only readable there).
    WallTime {
        multiplier: f64,
        start: Mutex<Option<tokio::time::Instant>>,
    },
}

struct SyntheticState {
    now: Duration,
    /// In-flight sleepers keyed by wake deadline (ties broken by a monotonic
    /// id), so the soonest is `first_entry`. An entry is removed either when
    /// the hook fires it or when its `SleeperGuard` is dropped (a cancelled
    /// `sleep`), so the map never holds a deadline nobody is awaiting.
    sleepers: BTreeMap<SleeperKey, Arc<Notify>>,
    next_id: u64,
}

/// Orders the sleeper map by wake deadline, then by insertion id so that
/// distinct sleepers sharing a deadline remain distinct keys.
#[derive(Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
struct SleeperKey {
    wake_at: Duration,
    id: u64,
}

/// Removes a sleeper from the queue when its `sleep` future is dropped before
/// firing (e.g. cancelled inside a `select!`). On normal completion the hook
/// has already removed the entry, so the removal here is a no-op. This is what
/// guarantees the hook never advances time to a deadline nobody awaits — which
/// would park the runtime with nothing left to wake it.
struct SleeperGuard {
    inner: Arc<Inner>,
    key: SleeperKey,
}

impl Drop for SleeperGuard {
    fn drop(&mut self) {
        if let Mode::Synthetic(state) = &self.inner.mode {
            if let Ok(mut state) = state.lock() {
                state.sleepers.remove(&self.key);
            }
        }
    }
}

/// Record that a boundary I/O round-trip has begun (see [`Inner::pending_io`]).
pub(super) fn acquire_io(inner: &Inner) {
    inner.pending_io.fetch_add(1, Ordering::SeqCst);
}

/// Record that a boundary I/O round-trip has finished. Called only from
/// `IoGuard::drop`, so it cannot underflow the count.
pub(super) fn release_io(inner: &Inner) {
    inner.pending_io.fetch_sub(1, Ordering::SeqCst);
}

/// Coupled mode: the 1:1 wall-time model, with the epoch read from the system
/// clock as UTC — the only read of wall-clock time the sim makes (sim.md §2.1).
pub(super) fn coupled() -> (SimClock, Runtime) {
    let now = time::OffsetDateTime::now_utc();
    isolated(
        TimeModel::WallTimeMultiple(1.0),
        PrimitiveDateTime::new(now.date(), now.time()),
    )
}

pub(super) fn isolated(model: TimeModel, epoch: PrimitiveDateTime) -> (SimClock, Runtime) {
    match model {
        TimeModel::Synthetic => {
            let inner = Arc::new(Inner {
                epoch,
                mode: Mode::Synthetic(Mutex::new(SyntheticState {
                    now: Duration::ZERO,
                    sleepers: BTreeMap::new(),
                    next_id: 0,
                })),
                pending_io: AtomicUsize::new(0),
            });
            let inner_for_hook = Arc::clone(&inner);
            let runtime = Builder::new_current_thread()
                .enable_all()
                .on_thread_park(move || advance_to_next_wakeup(&inner_for_hook))
                .build()
                .expect("failed to build current_thread runtime");
            (SimClock::from_inner(inner), runtime)
        }
        TimeModel::WallTimeMultiple(multiplier) => {
            assert!(
                multiplier.is_finite() && multiplier > 0.0,
                "WallTimeMultiple multiplier must be finite and positive, got {multiplier}"
            );
            let inner = Arc::new(Inner {
                epoch,
                mode: Mode::WallTime {
                    multiplier,
                    start: Mutex::new(None),
                },
                pending_io: AtomicUsize::new(0),
            });
            let runtime = Builder::new_current_thread()
                .enable_all()
                .build()
                .expect("failed to build current_thread runtime");
            (SimClock::from_inner(inner), runtime)
        }
    }
}

impl Inner {
    /// Elapsed simulated time since sim start.
    fn elapsed(&self) -> Duration {
        match &self.mode {
            Mode::Synthetic(state) => {
                state
                    .lock()
                    .expect("sim clock: synthetic state mutex poisoned")
                    .now
            }
            Mode::WallTime { multiplier, start } => {
                let mut start = start
                    .lock()
                    .expect("sim clock: wall-time start mutex poisoned");
                let start_instant = *start.get_or_insert_with(tokio::time::Instant::now);
                let elapsed = tokio::time::Instant::now().duration_since(start_instant);
                elapsed.mul_f64(*multiplier)
            }
        }
    }

    pub(super) fn now(&self) -> SimInstant {
        SimInstant::at(self.elapsed())
    }

    pub(super) fn now_datetime(&self) -> PrimitiveDateTime {
        self.epoch + self.elapsed()
    }
}

pub(super) async fn sleep(inner: Arc<Inner>, duration: Duration) {
    match &inner.mode {
        Mode::WallTime { multiplier, .. } => {
            tokio::time::sleep(duration.div_f64(*multiplier)).await;
            return;
        }
        Mode::Synthetic(_) => {}
    }

    // Synthetic: register a sleeper, then park on its notifier. The guard
    // removes the entry if this future is dropped before the notifier fires.
    let notify = Arc::new(Notify::new());
    let key = register_sleeper(&inner, duration, &notify);
    let _guard = SleeperGuard {
        inner: Arc::clone(&inner),
        key,
    };
    notify.notified().await;
}

fn register_sleeper(inner: &Inner, duration: Duration, notify: &Arc<Notify>) -> SleeperKey {
    let Mode::Synthetic(state) = &inner.mode else {
        unreachable!("register_sleeper is only called in Synthetic mode");
    };
    let mut state = state
        .lock()
        .expect("sim clock: synthetic state mutex poisoned");
    let id = state.next_id;
    state.next_id += 1;
    let key = SleeperKey {
        wake_at: state.now + duration,
        id,
    };
    state.sleepers.insert(key, Arc::clone(notify));
    key
}

/// Advance simulated time to the soonest pending deadline and wake every
/// sleeper due at or before it. Called only from the `on_thread_park` hook in
/// `isolated`, where all tasks are guaranteed `Poll::Pending`.
///
/// Cancelled sleepers have already removed themselves via `SleeperGuard`, so
/// the soonest entry is always live and at least one real task is woken.
///
/// All state mutation completes and the lock is released before any notifier
/// fires: once woken, a task may immediately call `sleep` again and contend
/// for the same lock, so state must be fully updated first.
fn advance_to_next_wakeup(inner: &Inner) {
    let Mode::Synthetic(state_mutex) = &inner.mode else {
        return;
    };

    // A boundary I/O round-trip is in flight: that is real work, not the mere
    // passage of time, so simulated time must not advance past it. Returning
    // without waking a sleeper lets the runtime park on the reactor and resume
    // naturally when the I/O resolves; the next park (with the guard dropped)
    // advances time as usual.
    if inner.pending_io.load(Ordering::SeqCst) > 0 {
        return;
    }

    let to_wake: Vec<Arc<Notify>> = {
        let mut state = state_mutex
            .lock()
            .expect("sim clock: synthetic state mutex poisoned");

        let next = match state.sleepers.keys().next() {
            Some(key) => key.wake_at,
            None => return, // nothing sleeping, nothing to do
        };
        state.now = next;

        let mut wakes = Vec::new();
        while let Some(entry) = state.sleepers.first_entry() {
            if entry.key().wake_at <= next {
                wakes.push(entry.remove());
            } else {
                break;
            }
        }
        wakes
    }; // lock released here

    for notify in to_wake {
        notify.notify_one();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use time::macros::datetime;

    /// A `Synthetic` clock state with no runtime, for exercising the advance
    /// logic directly.
    fn synthetic_inner() -> Arc<Inner> {
        Arc::new(Inner {
            epoch: datetime!(2025-01-01 0:00),
            mode: Mode::Synthetic(Mutex::new(SyntheticState {
                now: Duration::ZERO,
                sleepers: BTreeMap::new(),
                next_id: 0,
            })),
            pending_io: AtomicUsize::new(0),
        })
    }

    /// The quiescence hook must not advance simulated time while a boundary I/O
    /// round-trip is in flight (an `IoGuard` is held); once the guard is
    /// released it advances to the soonest sleeper as usual. This is the
    /// invariant that makes Synthetic time safe with real loopback I/O.
    #[test]
    fn pending_io_holds_time_until_released() {
        let inner = synthetic_inner();
        let notify = Arc::new(Notify::new());
        register_sleeper(&inner, Duration::from_secs(10), &notify);

        // A guard is held: a park must not advance the clock.
        acquire_io(&inner);
        advance_to_next_wakeup(&inner);
        assert_eq!(
            inner.now().duration_since(SimInstant::at(Duration::ZERO)),
            Duration::ZERO,
            "time advanced while I/O was in flight"
        );

        // Guard released: the next park advances to the pending sleeper.
        release_io(&inner);
        advance_to_next_wakeup(&inner);
        assert_eq!(
            inner.now().duration_since(SimInstant::at(Duration::ZERO)),
            Duration::from_secs(10),
            "time did not advance after I/O completed"
        );
    }

    /// Nested guards: time stays held until the *last* one is released.
    #[test]
    fn time_held_until_last_guard_released() {
        let inner = synthetic_inner();
        let notify = Arc::new(Notify::new());
        register_sleeper(&inner, Duration::from_secs(5), &notify);

        acquire_io(&inner);
        acquire_io(&inner);
        release_io(&inner);
        advance_to_next_wakeup(&inner);
        assert_eq!(
            inner.now().duration_since(SimInstant::at(Duration::ZERO)),
            Duration::ZERO,
            "time advanced with one guard still held"
        );

        release_io(&inner);
        advance_to_next_wakeup(&inner);
        assert_eq!(
            inner.now().duration_since(SimInstant::at(Duration::ZERO)),
            Duration::from_secs(5)
        );
    }
}
