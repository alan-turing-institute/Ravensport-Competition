//! Logical clock — the authoritative simulated time source for the sim.
//!
//! The public interface is defined directly in this file per the module
//! convention in sim-impl.md; the private implementation lives in
//! `internals` and is not visible to other modules.
//!
//! Owns: `SimClock`, `TimeModel`, `SimInstant`.

mod internals;

use std::sync::Arc;
use std::time::Duration;

use time::PrimitiveDateTime;

/// Specifies how simulated time advances in isolated mode.
#[derive(Debug, Clone, Copy)]
pub enum TimeModel {
    /// Simulated time advances at a fixed multiple of wall-clock time.
    /// `clock.sleep(d)` delegates to `tokio::time::sleep(d / multiplier)`.
    /// A multiplier of 1.0 matches wall-clock pace. Must be finite and
    /// positive (> 0.0); [`SimClock::isolated`] panics otherwise.
    WallTimeMultiple(f64),

    /// Simulated time advances only when all tasks are `Poll::Pending`.
    /// `clock.sleep(d)` parks on a notifier; the runtime's `on_thread_park`
    /// hook advances the clock to the earliest pending deadline and wakes the
    /// corresponding tasks when the scheduler reaches quiescence.
    Synthetic,
}

/// A point in simulated time.
///
/// Represented internally as the [`Duration`] elapsed since sim start, and
/// obtained from [`SimClock::now`]. Used by paced entities to compute the
/// time elapsed between state transitions.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub struct SimInstant {
    since_start: Duration,
}

impl SimInstant {
    /// Construct from a duration elapsed since sim start. Private to the
    /// `clock` module; only `internals` builds `SimInstant` values.
    fn at(since_start: Duration) -> Self {
        Self { since_start }
    }

    /// Returns the duration elapsed since `earlier`. Saturates at
    /// [`Duration::ZERO`] if `earlier` is later than `self`.
    pub fn duration_since(&self, earlier: SimInstant) -> Duration {
        self.since_start.saturating_sub(earlier.since_start)
    }
}

/// Logical clock handle shared across sim entities.
///
/// Cheap to clone: all clones share the same underlying state via an `Arc`,
/// so a sleep on one clone advances the simulated time observable through any
/// other clone.
#[derive(Clone)]
pub struct SimClock {
    inner: Arc<internals::Inner>,
}

/// Marks a boundary I/O round-trip as in flight for as long as it is held.
///
/// In [`TimeModel::Synthetic`], simulated time will not advance while any guard
/// is alive, so a real round-trip (HTTP/Modbus/TLS over loopback, completing in
/// real microseconds) is drained before the clock moves — making boundary I/O
/// effectively instantaneous in simulated time without being skipped over.
/// Obtained from [`SimClock::io_guard`] and dropped when the exchange completes.
///
/// Only sim entities may hold a guard. A mocked digital component must never
/// construct one: when it calls into the sim, the sim *server* it reaches
/// guards the exchange; when a sim entity calls out, that *client* guards it.
#[must_use = "the guard must be held for the duration of the I/O round-trip"]
pub struct IoGuard {
    inner: Arc<internals::Inner>,
}

impl Drop for IoGuard {
    fn drop(&mut self) {
        internals::release_io(&self.inner);
    }
}

impl SimClock {
    /// Private constructor used by `internals::isolated` (and so by `coupled`).
    fn from_inner(inner: Arc<internals::Inner>) -> Self {
        Self { inner }
    }

    /// Construct a `SimClock` for coupled mode — running against the digital
    /// world (sim.md §2.1): 1:1 wall-clock pace, with the calendar starting at
    /// the current UTC time. Sim time and digital time are then one time base,
    /// so a date the sim writes on paper is the date the digital world records.
    ///
    /// The system clock is read once, here, and never again: `now_datetime()`
    /// is that start time plus the monotonic elapsed duration. Re-reading it
    /// would let anything able to set the shared host clock move the sim's
    /// time; a step in the host clock after construction is therefore not
    /// followed, and the sim must be restarted after one.
    pub fn coupled() -> (Self, tokio::runtime::Runtime) {
        internals::coupled()
    }

    /// Construct a `SimClock` for isolated mode with the given `TimeModel` and
    /// sim-start epoch (the calendar date-time that simulated t=0 represents).
    /// Not for a run against the digital world: a fixed epoch puts every sim
    /// date-time out of step with the digital hosts' clocks.
    ///
    /// Returns the clock and a `current_thread` Tokio runtime configured for
    /// the chosen mode. For `Synthetic`, the `on_thread_park` hook is
    /// installed on the runtime internally; the caller never handles it.
    ///
    /// Panics if `model` is `WallTimeMultiple` with a multiplier that is not
    /// finite and positive.
    ///
    /// ```no_run
    /// use sim::clock::{SimClock, TimeModel};
    /// use time::macros::datetime;
    /// let (clock, runtime) =
    ///     SimClock::isolated(TimeModel::Synthetic, datetime!(2025-01-01 0:00));
    /// runtime.block_on(async {
    ///     let _ = clock.now_datetime();
    /// });
    /// ```
    pub fn isolated(model: TimeModel, start: PrimitiveDateTime) -> (Self, tokio::runtime::Runtime) {
        internals::isolated(model, start)
    }

    /// Returns the current simulated time as a [`SimInstant`].
    pub fn now(&self) -> SimInstant {
        self.inner.now()
    }

    /// Returns the current simulated wall-clock date and time — the sim-start
    /// epoch plus the elapsed simulated duration.
    pub fn now_datetime(&self) -> PrimitiveDateTime {
        self.inner.now_datetime()
    }

    /// Suspend the caller for `duration` of simulated time.
    pub async fn sleep(&self, duration: Duration) {
        internals::sleep(Arc::clone(&self.inner), duration).await;
    }

    /// Acquire an [`IoGuard`] for a boundary I/O round-trip. Hold it across the
    /// whole exchange (request and response); see [`IoGuard`] for the rules on
    /// who may call this.
    pub fn io_guard(&self) -> IoGuard {
        internals::acquire_io(&self.inner);
        IoGuard {
            inner: Arc::clone(&self.inner),
        }
    }
}
