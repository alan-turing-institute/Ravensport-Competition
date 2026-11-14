# Simulation Clock with Tokio

## The Problem

We want a simulation clock that advances automatically when all tasks are waiting —
when nothing can make forward progress without time moving. This enables deterministic,
fast simulation without wall-clock delays.

---

## The `current_thread` Runtime

Tokio has two runtime flavors:

- **`multi_thread`** (default): spawns N worker threads (one per CPU core). Tasks can
  migrate between threads between yield points. Used via `#[tokio::main]` or
  `Builder::new_multi_thread()`.

- **`current_thread`**: runs the entire async runtime on a **single OS thread**. All
  tasks are interleaved cooperatively on that one thread. Used via
  `Builder::new_current_thread()` or `#[tokio::main(flavor = "current_thread")]`.

For simulation, `current_thread` is the natural choice: it is deterministic, simpler to
reason about, and is exactly what Tokio's own `time::pause()` (used in tests) is built
on for the same reasons.

---

## How `on_thread_park` Works

`on_thread_park` is a callback registered on the runtime builder that fires on a worker
thread just before it parks (goes idle). A thread parks when its **run queue is empty**
— there are no runnable tasks. Tasks that are `Poll::Pending` (e.g. waiting on a channel
receive or a `simclock.sleep()`) are not in the run queue; they are parked on wakers.

On `current_thread`, there is exactly one thread. When it parks, **every task is
`Poll::Pending`** — nothing can make forward progress. This is the quiescence signal.

```rust
// Inside SimClock::isolated() — the hook is registered here and never
// exposed externally. `inner` is an Arc<Mutex<SimClockInner>> cloned
// from the clock before the runtime is built.
let inner_for_hook = Arc::clone(&inner);
let runtime = tokio::runtime::Builder::new_current_thread()
    .on_thread_park(move || {
        // All tasks are Poll::Pending. Safe to advance the clock.
        SimClock::advance_to_next_wakeup(&inner_for_hook);
    })
    .enable_all()
    .build()
    .unwrap();
```

No atomic counter is needed on `current_thread` — one thread parking is definitionally
all threads parking.

---

## Is Quiescence Detection Accurate? (Channel Safety)

A natural concern: could `on_thread_park` fire prematurely — e.g. when one task has just
sent a message on a channel and the receiver is about to wake up?

**No.** When a task sends on a Tokio channel, the receiver's waker is called
**synchronously** as part of the send operation, before the sending task yields or
completes. The receiving task is added to the run queue immediately. The thread will not
park while there are runnable tasks in the queue, so `on_thread_park` can only fire after
the receiver has run. There is no window where the clock advances with work still
outstanding in a pipe.

This is a general property of Tokio's scheduler: `waker.wake()` enqueues the task
synchronously within the current task's execution quantum. The thread only parks after
all resulting work has been processed.

---

## The SimClock Implementation

### Internal Structure

```rust
use std::collections::BTreeMap;
use std::sync::{Arc, Mutex};
use std::time::Duration;
use tokio::sync::Notify;

pub struct SimClock {
    inner: Arc<Mutex<SimClockInner>>,
}

struct SimClockInner {
    now: Duration,
    // In-flight sleepers keyed by deadline (ties broken by a monotonic id), so
    // the soonest is `first_entry`. An entry is removed when the hook fires it
    // or when its sleep future is dropped (see "Cancellation" below), so the
    // map never holds a deadline nobody is awaiting.
    sleepers: BTreeMap<SleeperKey, Arc<Notify>>,
    next_id: u64,
}

#[derive(Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
struct SleeperKey {
    wake_at: Duration,
    id: u64,
}
```

The structures above show the `Synthetic` mechanism only. The full `SimClock`
also carries a sim-start **epoch** (a `time::PrimitiveDateTime`) supplied to
`isolated(model, start)`, and exposes `now_datetime()` = epoch + elapsed for
calendar wall-clock timestamps (e.g. the ATG console's `YYMMDDHHmm`). In
coupled mode the epoch is the current UTC time at construction rather than a
supplied date, and the system clock is read only then — never by `now_datetime()`
— so a later step in the host clock is not followed (sim.md §2.1). A warm start
(sim.md §9.3) does not change the clock: the epoch is still the current UTC
time at construction, and the checkpoint's due times, which are absolute
date-times, are converted to sleeps of their distance from `now_datetime()`, or
zero for one already passed. The `WallTimeMultiple` mode and the complete
public interface are in sim-impl.md.

### Why `std::sync::Mutex`, Not `tokio::sync::Mutex`

`on_thread_park` is a **synchronous** callback — it runs as raw thread-level code, not
inside an async context. You cannot `.await` inside it. This rules out
`tokio::sync::Mutex`, whose `.lock()` is async and returns a future.

`std::sync::Mutex` is correct here because:

- The critical section (map insert/remove, updating `now`) is purely in-memory and completes
  in nanoseconds.
- There is no `.await` inside the lock.
- Briefly blocking a thread for nanoseconds has no meaningful impact.

### Using `std::sync::Mutex` Safely from Async Code

**Rule: never hold a `std::sync::MutexGuard` across an `.await` point.**

For `Send` futures (which Tokio tasks must be), the compiler enforces this:
`MutexGuard<T>` is `!Send`, so a future that holds a guard across `.await` will not
compile as `Send`.

The pattern is to scope the guard so it drops before any `.await`:

```rust
pub fn sleep(&self, duration: Duration) -> impl Future<Output = ()> {
    let notify = Arc::new(Notify::new());

    let key = {
        let mut inner = self.inner.lock().unwrap();
        let id = inner.next_id;
        inner.next_id += 1;
        let key = SleeperKey { wake_at: inner.now + duration, id };
        inner.sleepers.insert(key, Arc::clone(&notify));
        key
    }; // lock released here, before any .await

    // The guard removes this sleeper if the future is dropped before the
    // notifier fires (e.g. cancelled in a `select!`); see "Cancellation".
    let guard = SleeperGuard { inner: Arc::clone(&self.inner), key };
    async move {
        let _guard = guard;
        notify.notified().await;
    }
}
```

Why this matters on `current_thread`: if you hold `std::sync::Mutex` across `.await`,
you yield the task while holding the lock. Another task on the same thread tries to
acquire it and **blocks the thread**. On `current_thread` that is the only thread —
guaranteed deadlock.

### Advancing the Clock

`advance_to_next_wakeup` is a private function called exclusively from the
`on_thread_park` hook registered in `isolated()`. It is not part of the
public `SimClock` interface.

The key pattern: complete all state mutation and **release the lock** before
firing any notifications. Once `notify_one()` is called, the notified task
becomes runnable and a thread may unpark and start executing — potentially
calling `simclock.sleep()` again and trying to acquire the same lock. State
must be fully updated before that can happen.

```rust
// Private — called only from the on_thread_park hook in isolated().
fn advance_to_next_wakeup(inner: &Mutex<SimClockInner>) {
    let to_wake: Vec<Arc<Notify>> = {
        let mut inner = inner.lock().unwrap();

        let next = match inner.sleepers.keys().next() {
            Some(key) => key.wake_at,
            None => return, // nothing sleeping, nothing to do
        };
        inner.now = next;

        // Cancelled sleepers have already removed themselves (see
        // "Cancellation"), so the soonest entry is always live.
        let mut wakes = vec![];
        while let Some(entry) = inner.sleepers.first_entry() {
            if entry.key().wake_at <= next {
                wakes.push(entry.remove());
            } else {
                break;
            }
        }
        wakes
    }; // lock released here

    // Fire notifications only after state is fully updated and lock is released.
    for notify in to_wake {
        notify.notify_one();
    }
}
```

### Cancellation

A `sleep` future can be dropped before its deadline — most commonly when it
loses a `tokio::select!` race. The sleeper was registered in the queue at
`sleep` time, so a dropped future would otherwise leave a deadline nobody is
awaiting. The hook would then advance simulated time to that dead deadline and
`notify_one` a `Notify` with no waiter, waking no task; on `current_thread`
(no real timer) the runtime would then park with nothing to unpark it — a hang.

To prevent this, `sleep` attaches a `SleeperGuard` to the future. Its `Drop`
removes the sleeper from the queue by key:

```rust
struct SleeperGuard {
    inner: Arc<Mutex<SimClockInner>>,
    key: SleeperKey,
}

impl Drop for SleeperGuard {
    fn drop(&mut self) {
        if let Ok(mut inner) = self.inner.lock() {
            inner.sleepers.remove(&self.key); // no-op if the hook already fired it
        }
    }
}
```

Removal is idempotent: on normal completion the hook has already removed the
entry, so the guard's removal finds nothing. Keying the queue by `SleeperKey`
makes both the hook's pop and the guard's removal `O(log n)` with no scan, and
routing every mutation through the same `Mutex` keeps it correct under the
multi-thread extension below.

---

## Extending to Multi-Thread (`multi_thread` Runtime)

On a multi-thread runtime with N worker threads, `on_thread_park` fires per thread. The
approach is to count parked threads and advance the clock only when all N are parked.

```rust
use std::sync::atomic::{AtomicUsize, Ordering};

let num_workers = 4;
let parked = Arc::new(AtomicUsize::new(0));

let inner_for_park = Arc::clone(&inner);
let parked_for_park = Arc::clone(&parked);
let parked_for_unpark = Arc::clone(&parked);

tokio::runtime::Builder::new_multi_thread()
    .worker_threads(num_workers)
    .on_thread_park(move || {
        let prev = parked_for_park.fetch_add(1, Ordering::SeqCst);
        if prev + 1 == num_workers {
            // Last thread to park — all tasks are Poll::Pending.
            SimClock::advance_to_next_wakeup(&inner_for_park);
        }
    })
    .on_thread_unpark(move || {
        parked_for_unpark.fetch_sub(1, Ordering::SeqCst);
    })
    .build()
    .unwrap()
```

The lock-before-notify ordering is even more important here: `notify_one()` can
immediately unpark one of the N-1 already-sleeping threads. That thread decrements the
counter and starts executing the woken task while the "last to park" thread is still
inside `advance_to_next_wakeup`. The lock must be released and state fully updated before
the first `notify_one()`.

---

## Real Boundary I/O (the `io_guard`)

The sim meets the digital world over real sockets — HMI HTTP, dispenser Modbus,
ATG Veeder-Root TLS — which in `Synthetic` mode run over loopback. Real I/O
presents the same hazard as `spawn_blocking`: a task awaiting socket readiness
is `Poll::Pending` and parks on the reactor, so `on_thread_park` fires and would
advance the clock *past* the in-flight exchange. The bytes arrive microseconds
later in real time, but at a simulated instant the clock has already left behind.
With several entities `clock.sleep`-ing concurrently the hook finds a sleeper to
advance to on every park, so the runtime never blocks on the reactor long enough
for loopback readiness to be delivered, and the boundary stalls while simulated
time races ahead.

The fix is this project's analogue of Tokio's `auto_advance_inhibit_count`: a
`pending_io` counter on the shared clock state. `advance_to_next_wakeup` returns
without advancing — and without waking a sleeper — while `pending_io > 0`, so
the runtime parks on the reactor and resumes naturally when the I/O resolves;
the next park, with the count back at zero, advances time as usual. A boundary
round-trip is therefore instantaneous in simulated time — drained in real
microseconds before the clock moves — without being skipped over.

A round-trip is marked in flight by an `IoGuard` (`clock.io_guard()`): an RAII
handle that increments the counter on creation and decrements it on drop, held
across a whole exchange (request and response).

**Only sim entities may hold a guard.** Every boundary round-trip is bracketed
by the sim side:

- A sim **client** guards the round-trip it initiates — the dock operator across
  each HMI HTTP call, the fuel supplier across each ATG poll. The guard covers
  the whole nested chain the response depends on (HMI → FMS → its own
  Modbus/TLS), so the server needs no guard when the client is in-process.
- A sim **server** guards each exchange it serves, from request received to
  response written, in the ATG console and the dispenser. This covers a
  round-trip whose client is *not* an in-process sim entity (a mocked FMS polling
  on its own schedule), which the client rule cannot reach. A server's *idle*
  wait for the next request is left unguarded, so simulated time advances while
  an appliance sits idle between polls.

A mocked digital component must **never** construct a guard: when it calls into
the sim, the sim server it reaches guards the exchange. The mock is not part of
the simulated-physical world and does not participate in its clock.

This makes `Synthetic` time usable for the full self-running sim — every entity
plus real loopback transport — with the run taking only as long as the host
needs to move the bytes.

## `spawn_blocking` Is Not Supported

**Do not use `tokio::task::spawn_blocking` in this project.**

`spawn_blocking` runs a closure on a separate OS thread pool, outside the async worker
threads. These blocking threads are invisible to `on_thread_park` — they do not park via
the same mechanism. If a blocking task is running while all async worker threads happen
to be parked, the clock will advance even though real work is still in progress.

Tokio's own `time::pause()` handles this via an internal `auto_advance_inhibit_count`
that prevents clock advancement while blocking tasks are running. Implementing equivalent
support here would require tracking blocking task lifecycles across spawn and join.

Since this project does not need `spawn_blocking`, the simplest and correct approach is
to not use it at all. If it becomes necessary in the future, the clock advancement logic
must be updated before it can be safely used.
