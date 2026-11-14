# Sim Implementation Plan

One section per simulated entity. Each section lists the entity's interactions
(from sim.md) and a scratchpad of design notes and open questions.

**Settled — module convention (all entities):** `mod.rs` defines the entity's
public interface directly — the `spawn()` signature and any public message or
return types. `internals.rs` holds the private implementation (the async task,
state structs, and helpers) and is not visible to other modules. No internal
struct is ever part of a public interface.

**Settled — execution model:** concurrent entities are Tokio tasks whose
public interface is a `spawn()` function. All channels are pre-created before
any entity is spawned and both ends are passed in as arguments. No `spawn()`
returns a channel. This breaks the cyclic channel dependency that would
otherwise arise between the fuel tank, flow meter, and dispenser, and makes
the complete communication topology visible at the construction site. Each
`spawn()` returns its task's `JoinHandle` so the binary's orchestrator can
supervise it (see *Error handling and task supervision*).

**Shared types:**

```rust
/// Identifies a vessel in the port roster by its seven-digit International
/// Maritime Organization (IMO) number (`roster.vessels[].imo_number` in
/// world.yaml, compiled in as `constants::VESSEL_IMO_NUMBER`). Used in job and
/// BDN records; the FMS matches it against the label of the vessel's RFID card.
/// The vessel's name (`constants::VESSEL_NAME`) is carried separately, for the BDN.
pub struct VesselId(pub u32);

/// A 26-bit Wiegand card number read from a vessel or operator RFID reader.
/// The FMS maps this to a vessel identity or operator identity via its
/// internal card table.
pub struct RfidCardNumber(pub u32);

/// Abstraction over the serial transport connecting an RFID reader to its
/// host (FMS for the vessel reader; HMI for the operator reader). An
/// abstraction is necessary because the transport depends on the deployment
/// substrate and must not be assumed by the reader. The reader writes
/// Wiegand-decoded card number strings to `SerialConnection` without
/// knowledge of the underlying transport, via the async
/// `write_all(&mut self, bytes: &[u8]) -> std::io::Result<()>`.
///
/// Backends: an in-process loopback —
/// `SerialConnection::loopback() -> (SerialConnection, LoopbackHandle)` — which
/// captures written bytes (`LoopbackHandle::bytes()`) for in-process wiring and
/// tests; and a TCP backend — `SerialConnection::tcp(addr)` — the boundary
/// transport that streams each written frame to every connected client over a
/// broadcast channel. The type is opaque so backends are non-breaking.
///
/// `readiness(&self) -> ReaderReadiness` reports whether a digital consumer is
/// subscribed to the reader: the TCP backend tracks the count of connected
/// clients (incremented as the accept loop subscribes a connection, decremented
/// when that connection's writer task ends); the loopback backend is always
/// ready. `ReaderReadiness::wait_ready()` resolves once a consumer is subscribed.
/// The dock operator and tug engineer await this before presenting a card, so a
/// one-shot card is never broadcast to an unattached reader (sim.md §4.3–4.4).
/// The handle is taken in the orchestrator before the connection is moved into
/// the reader task, and routed to the presenting actor.
pub struct SerialConnection { /* opaque; private backend enum */ }

/// Outflow rate from the fuel tank, in litres per minute. `None` means no
/// outflow is occurring; `Some(rate_L_per_min)` carries the active rate.
/// Published by the fuel tank on `outflow_rate_tx` and consumed by the
/// dispenser PD flow meter to derive its pulse interval.
pub type OutflowRate = Option<f64>;
```

---

## Error handling and task supervision

**Settled.** The sim fails fast and loudly. Every error is either (a) recovered
by a defined, logged process, or (b) fatal — taking the whole runtime down with
one diagnostic line. Nothing is silently dropped, and no failure may leave a
peer blocked.

### Why unhandled task panics are dangerous

A panic inside a `tokio::spawn`ed task is caught at the task boundary: the worker
thread survives, the panic is stored in the task's `JoinHandle` as a
`JoinError`, and if the handle is unobserved the payload is dropped and every
other task keeps running. A panicking entity therefore vanishes silently while
its peers block forever on channels that will never deliver — a hang whose
symptom is far from its cause. Supervision exists to convert any fatal error
into immediate, attributable, total shutdown.

### Task supervision (`JoinSet`)

Every entity `spawn()` returns its `JoinHandle<()>`. The binary's orchestrator
enrols all entity tasks in a top-level `tokio::task::JoinSet` and awaits
`join_next()`. Because every entity runs for the whole sim, the first task to
finish — for **any** reason — is an error:

- `Err(JoinError)` with `is_panic()` → a fatal panic; the payload is recovered
  via `into_panic()`.
- `Ok(())` or any other return → an entity exited when it must not have.

On either, the orchestrator logs one ERROR — the entity name (via an
`Id → name` map keyed on the `AbortHandle::id()` recorded at spawn) and the
payload — then calls `JoinSet::abort_all()`, flushes the subscriber, and exits
non-zero. `JoinSet` is chosen over `select_all`/`FuturesUnordered` so no
`futures` dependency is needed, and because it natively supports the dynamic
per-connection sets below.

### Nested (per-connection) tasks

The ATG console and dispenser accept loops spawn a task per client connection.
These are supervised by a **child `JoinSet` owned by the accept loop**, which
`select!`s between `listener.accept()` (spawning each new handler into the set)
and `child.join_next()`. A handler that finishes with a panic is propagated by
`std::panic::resume_unwind(err.into_panic())`, re-panicking the accept-loop task
with the original payload; that task is in the top-level set, so the panic
reaches the orchestrator and brings the sim down. No global panic hook or
`panic = "abort"` is used.

**Bounded session capacity (ATG console).** The ATG console accepts at most **10
concurrent client sessions**, modelling the finite session capacity of a real
sealed tank-gauge console. The accept arm is gated on the child `JoinSet`'s length
(`accept = listener.accept(), if connections.len() < MAX_ATG_SESSIONS => …`, with
`MAX_ATG_SESSIONS = 10`); while the limit is reached the console stops accepting,
so further connections wait in the kernel listen backlog until a session closes
and `join_next` frees a slot. Ten comfortably exceeds normal concurrent demand
(the fleet-management system's poll and the fuel supplier's poll), so ordinary
operation never blocks, while a client that opens and holds ten sessions denies
the feed to legitimate pollers — the behaviour a real console exhibits under
session exhaustion.

Within a per-connection handler the appliance behaves like real hardware: a
malformed or hostile request gets the protocol's defined error response, and a
broken client connection (socket error or EOF) is dropped — neither panics. A
panic therefore signals only **our own** bug (an internal invariant violation),
which `resume_unwind` propagates to the parent. Failure of an *internal* peer
channel (e.g. the ATG probe task has gone) is fatal per the taxonomy — that is
not client I/O.

### Error taxonomy

| Class | Example | Action |
|---|---|---|
| Startup / configuration | TCP bind fails, HTTP client build fails | **Fatal** on the synchronous setup path (before the task is spawned). |
| Invariant violation (our own bug) | impossible internal state, an internally-produced value that breaks an invariant | **Fatal** — panic with context. |
| Malformed / hostile client input (server appliances) | a bad Modbus or TLS frame from the FMS or supplier — which may be a compromised client | **Behave as the real appliance would** — return the protocol's defined error response, or drop the broken connection; **never** panic. A digital-world client must never be able to crash the sim. |
| Inter-actor channel closed mid-run | `recv()` returns `None`, `send` to a dropped peer, a missing proceed signal | **Fatal** — a peer has died. Propagate the error (panic message naming the channel/peer); the supervisor tags it with the entity name. A stop does not close channels: an entity at rest holds its channel ends until the process exits (see *Stopping and warm start*), so the rule holds during a stop too. |
| Transient digital-world I/O | connection refused/reset, timeout, HTTP 5xx | **Recover** — bounded retry with backoff (see below); on exhaustion, fatal, except a `503` that carries the FMS's code `atg_read_failed` on a request with a cavitation check, which is a refusal (see below). |
| Semantic HTTP response | 4xx (`409`, `401`, `408`, `422`) | **Per-endpoint** — expected condition (handle + log) or contract breach (fatal). Default fatal until a recovery is defined. Job-creation and job-state-transition `409` have defined recoveries (see reconciliation below), as does a `422` carrying the cavitation refusal code. |
| Deserialization | HMI body does not match the expected shape | **Fatal** — a contract/version breach. |

**Cavitation.** Running the pump with no fuel in the tank causes permanent
damage and is a total failure of the operational criteria. Reaching 0 L while
`outflow_active` therefore panics, terminating the RavensPort simulation.

### Retry policy (transient digital-world I/O)

Transient errors are retried up to **3 times** with backoff **1 s, 2 s, 4 s** slept on the `SimClock`. A request that recovers after one or more retries emits a single WARN (entity, request, attempt count). Exhausted retries are fatal, with one exception on a request carrying a cavitation check. There, a `503` that carries the code `atg_read_failed` (`fms.md` §7, FMS HTTP API) and persists through the whole schedule is an FMS refusal, handled as described below. The response that ends the schedule decides, by its code. A `503` without that code is a fault like any other 5xx. HAProxy answers `503` with its own page when it cannot reach its backend, on the sim host (`sim-network.md` §6, HAProxy configuration) and on `hmi-01` (`hmi.md` §9.3, HAProxy).

The ullage read before a fill (`GET /api/tanks/{id}/level`) carries no check. There, a `503` that carries the code `no_atg_reading` reports only that no ATG reading is available (`fms.md` §7.3, Tank level). An unavailable reading must not block a delivery (`digital.md` §4.3, Fill transaction lifecycle), so the ullage check is skipped with a WARN and the fill proceeds. A `503` without that code is a fault. HTTP 4xx are not retried. Non-idempotent job creation is handled separately (see below).

### Non-idempotent job creation (reconciliation)

`POST /api/jobs/refuel` and `POST /api/jobs/tank-fill` are not idempotent, and
the FMS admits only one live job at a time. A create whose response is lost in
transit cannot be retried blindly: the job may already have committed, so a
second POST collides with the occupied slot and returns `409 "a job is already
active"`. The generic retry above is therefore bypassed for creates.

Any ambiguous create outcome — a transient transport error, an HTTP 5xx, or a
`409` — triggers a reconciliation against `GET /api/jobs/current`:

- a live job distinct from the operator's previous one is the operator's own
  committed create (the operator is the sole client and works sequentially): its
  id is adopted and the create is not repeated;
- an empty slot, or the previous job still in it, means the create did not
  commit: it is re-POSTed under the same backoff schedule.

A `503` that carries `atg_read_failed` is excluded from this reconciliation. The FMS guarantees that a creation check it could not evaluate created no job (`fms.md` §7.4, Jobs), so the outcome is not ambiguous. The create is re-POSTed under the backoff schedule without a reconciliation poll. A `503` without the code is reconciled like any other 5xx.

A create rejected on its merits remains fatal, its body preserved verbatim — a 4xx
other than `401`, `409` and the refusals below, each of which has a defined recovery.

**Refusals.** A *dispense* create may be refused for a reason that is a legitimate operational outcome rather than a contract breach. Two are defined. A `422` is a refusal when it carries the code `cavitation_threshold` in its body (`fms.md §7`); the code, not the wording, decides, so the FMS may reword the message freely. A `503` is a refusal when it carries the code `atg_read_failed` and persists through the whole retry schedule. Either means no job exists, so the operator abandons it and the tug departs unserved.

A refused *fill* create is a contract breach and stays fatal: fills are exempt from
the cavitation check and tolerate an unavailable reading (`fms.md §5.1`), so the FMS
has no defined reason to refuse one.

### Non-idempotent job-state transitions (reconciliation)

Three further operator writes are one-way job-state transitions, not idempotent.
A request whose response is lost in transit cannot be retried blindly: the
transition may already have committed, so the retry meets the transitioned job
and is rejected with a `409`. The generic retry above is therefore bypassed for
each.

| Transition | Request | Transition | `409` on retry |
|---|---|---|---|
| Dispense start | `POST /api/jobs/{job_id}/dispenser/start` | `Active → Running` | `job is not in active state` |
| BDN link | `PATCH /api/jobs/{job_id}` | `→ Closed` | `a BDN is already linked` |
| Fill close | `POST /api/delivery-tickets` (FMS `…/close`) | `Active → Closed` | `fill job is not active` |

A `409` triggers a reconciliation against `GET /api/jobs/current`: if `job_id`
is still the live job and reports a status reachable **only after** the
transition committed, the request took effect and the operator proceeds without
repeating it. The committed statuses are `dispensing` (or `awaiting_bdn`, where
the preset volume was reached within the lost-response window) for the start, and
`complete` for the BDN link and the fill close. Any other outcome — a non-`409`
rejection, a different or absent live job, or a status showing the transition did
not take effect — remains fatal, its body preserved verbatim.

The dispense start carries the same two refusals as job creation, identified the same way: a `422` carrying `cavitation_threshold`, or a `503` carrying `atg_read_failed` that persists through the retry schedule. A refused start leaves the job untouched and `active` (`fms.md` §5.1, ATG poller). Since the job exists and, while active, blocks every later job — including the fill that restocks the tank — the operator cancels it (`DELETE /api/jobs/{job_id}`) before the tug departs unserved. The cancel is retried on the standard schedule. A `204` means the job is cancelled, and a `404` that the job is no longer the current one, which is the state the cancel seeks. Any other outcome is fatal, its body preserved verbatim: an unexpected 4xx, and any `5xx` that persists through the schedule, a `503` included. A cancel that fails leaves the refused job `active` in the single job slot, so the operator's next create would meet it, find it to be its own previous job, and fail at the end of its own schedule under a message that does not name the cause.

The BDN link and the fill close apply no cavitation check, so neither has a defined
refusal and one on either is a contract breach that stays fatal. The fill close does
take an on-demand reading, but an unavailable one records a null closing volume
rather than refusing the request (`fms.md §5.1`).

Job creation is handled by the separate reconciliation above rather than this
one because an uncommitted create must be re-POSTed, whereas an uncommitted
transition simply surfaces as a fatal, unexpected state.

Cancellation (`DELETE /api/jobs/{job_id}`) is a fourth operator write and is
deliberately outside this scheme. It needs no reconciliation, because the FMS answers
a cancel of a job it has already cancelled with `204` (`fms.md §7.4`): a cancel whose
response was lost is simply sent again. A `404` means the job is no longer the
current one, which is the state the call was trying to reach. Any other outcome is
fatal, as above.

### Logging

`tracing` + `tracing-subscriber`: a minimal `fmt` subscriber with an
`EnvFilter` defaulting to `warn`, so a healthy run is effectively silent. The
only events emitted:

- **WARN** — a recoverable digital-world condition: a request that failed
  transiently then recovered (once, with the attempt count), an expired HMI
  session being re-authenticated, a lost create or job-transition response
  reconciled to its already-committed job, an FMS refusal of a dispense create or
  start and the cancellation that follows it, an ullage check skipped because no
  ATG reading was available,
  and an audit finding — paper artefacts the log-server records do not
  account for (see the reconciliation user below).
  Individual pre-recovery attempts are not logged.
- **ERROR** — the single fatal event, emitted by the orchestrator as a task
  dies (entity + payload), immediately before teardown.
- **INFO** — suppressed by the default `warn` level:
  - sim start/stop;
  - the tug engineer recording that a refused dispense sent its tug away
    unserved;
  - the tug arrival generator's scheduled arrival and requested volume at
    each draw, again at a warm start for each tug that is away, and each tug's
    arrival;
  - the fuel supplier's poll, with the volume read, the alarm state and
    whether the poll placed an order;
  - the fuel supplier's order, with the poll's time, the reported volume and
    the road tanker's scheduled arrival, and again at any start with the order
    pending;
  - the road tanker's arrival, and the end of each delivery;
  - a clean audit with its per-check detail.

Apart from the fuel supplier's poll, there is no per-request or first-try-success logging. A warm start logs again each scheduled arrival of a tug that is away, and the fuel supplier's pending order. The log of `sim-host` lies on a tmpfs that a stop loses (`sim-network.md` §7, image, build, and supervision).

**Where the audit's output sits, and why.** The audit is the only
entity whose ordinary output is a judgement about the environment rather than a
record of its own progress, so the split between its two kinds of output matters.

A **finding** is WARN and is therefore seen in a normal run. It is a recoverable
digital-world condition in exactly the sense the list above uses: a record is
absent from the log server and the audit continues rather than terminating. One
event per audit carries the count and the shape, not one per artefact — a long
outage can leave hundreds unmatched, and the convention already prefers a summary
to a per-event trace.

A **clean audit** is INFO and is therefore suppressed by default. Its per-check
detail, including the actual margin on each numeric check, exists so a near miss
is visible before it becomes a fatal; that is forensic material for a test or an
investigation, which raises the filter, rather than something a healthy run
should print.

**ERROR is not used for a finding.** It is the single fatal event before
teardown, and an ERROR that did not end the run would remove the property that
makes the fatal legible.

`tracing` is the only channel: the actor writes nothing to disk (`sim.md` §5.4),
so the report is a tracing event and its visibility is entirely a question of
level. It reaches whoever is running the environment, not the digital world — the
audit is the environment's backstop and is not something the blue agent observes.

### Tests

Supervision is a binary concern. Integration tests spawn entities individually
and drop the returned `JoinHandle`, keeping normal unwinding (no orchestrator,
no process abort), so `#[should_panic]` and runtime-drop continue to work.

---

## Orchestrator (the sim binary)

The orchestrator is the single construction site that assembles and runs the
whole simulated-physical world. It is the counterpart, on the sim side, to the
digital world's deployment. It is split between the binary's thin entry point,
`src/main.rs`, and the `orchestrator` module, `src/orchestrator.rs`, which the
integration tests also use.

### Why it matters

- **It makes fatal errors actually fatal.** A panic in a `tokio::spawn`ed task
  is caught by the runtime and, if its `JoinHandle` is unobserved, silently
  dropped while the other tasks run on (see *Error handling and task
  supervision*). Unless something **observes** the entity handles, the
  per-entity panics throughout the sim do not bring the process down — they
  are swallowed, and peers hang. The orchestrator is what observes them and
  converts the first failure into immediate, attributed, total shutdown.
- **It is the one place the full topology is visible.** Per the execution-model
  convention, every channel is pre-created and both ends passed in at
  construction. The orchestrator holds that wiring, so the complete
  communication graph between physical model, instruments, appliances, and
  actors is auditable in a single file.
- **It owns lifecycle and configuration.** Deterministic startup of the whole
  topology, the clock mode, the network bindings at the sim<->digital boundary,
  and clean teardown all live here.

### What it does

1. **Logging.** `main` installs the `tracing` subscriber once — a `fmt`
   subscriber with an `EnvFilter` defaulting to `warn` unless `RUST_LOG`
   overrides it, so a healthy run is effectively silent (only recovered-retry
   WARNs and the single fatal ERROR appear).
2. **Clock + runtime.** `main` builds the clock in coupled mode,
   `SimClock::coupled()`, which returns the clock and the runtime: 1:1 pace,
   calendar starting at the current UTC time (sim.md §2.1). Integration tests
   build an isolated clock, `SimClock::isolated(model, start)`, instead. A
   compressed scenario runs in coupled mode too (sim.md §2.2, compressed
   scenarios).
3. **Configuration.** `main` constructs a `Config`: the physical-model
   parameters of sim.md §1.1, the tug roster and refuel volume range, the fuel
   supplier's ordering values, seeds, the start state, the sim<->digital
   boundary endpoints (the loopback ports HAProxy fronts, sim-network.md
   §3–§5), the RFID serial transports, and the actors' own configuration, such
   as the reconciliation user's. Values projected from `world.yaml` are read
   from the compiled `sim::constants` (below); the rest are literals in
   `main.rs` and `coupled.rs`. Each entity's `spawn` refuses an invalid value
   as a start-up fault. `main` then reads the checkpoint, if one is present,
   and validates it against the parameters. Each entity's starting state comes
   from the checkpoint, or from the configuration on a cold start (*Stopping and
   warm start*). A refused checkpoint is a start-up fault.
4. **Wiring and spawn.** `orchestrator::build(clock, config, checkpoint)` pre-creates every
   channel, spawns each entity, and enrols each returned `JoinHandle` in a
   `tokio::task::JoinSet`, returned as `SimHandles`. `overfill_interlock_pct`
   is passed to the fuel tank with `tank_capacity_l`. The tug arrival channel's
   capacity is the roster size, so a tug's arrival never waits on the channel
   (Tug arrival generator); the tug arrival generator contributes one task per
   tug, and one that collects their states at a stop. Integration tests call
   `build` directly.
5. **Supervise.** `orchestrator::run` builds, then awaits
   `SimHandles::await_first_failure`, which resolves when the first entity
   stops. Every entity is meant to run for the whole sim, so any stop is an
   error. A panic's message is recovered from its payload; by convention each
   entity's panic messages begin with its name, so the message names the
   failing entity. An unexpected return or a cancellation is reported as such.
   `run` logs **one** ERROR carrying the cause and exits non-zero; the process
   exit ends the remaining tasks.
6. **Stop.** On `SIGTERM`, `run` brings the entities to rest, writes the
   checkpoint and exits with status 0 (*Stopping and warm start*). Supervision
   continues as the entities come to rest: a task that finishes in that time
   is fatal, as in step 5, and no checkpoint is written.

### Start state

The start state (sim.md §2.3, start state) includes two `spawn` parameters, which `main` sets in `Config` at step 3 (configuration) and passes to their entities at step 4 (wiring and spawn). By default each takes the value below, which `Start::defaults` in `coupled.rs` sets.

| Parameter | Entity | Default |
|---|---|---|
| `initial_volume_l` | Fuel tank | 35,000 litres |
| `first_audit_after` | Reconciliation user | `None`, since `world.yaml` gives a calendar audit period (`deployment-config.md` §5, render-time validation) |

The rest of the start state is fixed in the entities. Every tug starts away from the dock, its first time away drawn when its task starts (*Tug arrival generator*). The fuel supplier starts with no order placed and polls first one `poll_interval` after the start (*Fuel supplier*). The systems administrator's first session comes one `check_interval` after the start (*Systems administrator*). The readings of the dispenser's cumulative totaliser and of the road tanker's meter start at zero, and the first BDN and the first delivery ticket are numbered 1. The reconciliation user holds no paper artefact from before its spawn (*Reconciliation user*, "Cadence and window"). A warm start takes each entity's state from the checkpoint in place of this start state (*Stopping and warm start*).

### Compressed scenarios

In a compressed scenario (sim.md §2.2, compressed scenarios), `render.py` projects each scaled value from `world.yaml` already multiplied or divided by the compression factor (`deployment-config.md` §4, projection targets). The scaled values are the flow rates, each tug's `mean_time_away`, the fuel supplier's `poll_interval` and `lead_time`, and the systems administrator's `check_interval`. The scaled values also include the dock operator's job-status poll, the audit period in hours, the settling delay, the boundary margin and the FMS session lifetime. The sim uses each scaled value as it arrives, and holds no compression factor of its own.

The reconciliation user's query widths, the time ranges of the audit's queries, follow from the rate at which transaction records accrue (*Reconciliation user*, "Splitting the window"). The dock's arrival rate at the scaled times away sets that rate. The retries, back-offs, deadlines and the reconciliation user's terminal timings are timings of the software, which a compressed scenario leaves unchanged. The sim models no duration for a person's own action, such as a sign-in, a card presentation or a transcription.

### Decisions and open points

- **Configuration source and format.** Decided for the shared constants and
  the scenario parameters: the physical constants and RFID card numbers shared
  with the digital world, and the scenario parameters — the roster, the refuel
  volume range, the flow rates, the overfill
  interlock level and the fuel supplier's ordering values — are projected from
  `world.yaml` into `infra/sim/sim-constants.toml` and compiled into the binary
  by the crate's `build.rs` (sim-network.md §7; `deployment-config.md` §3), so
  `world.yaml` is their single source. The remaining sim-only parameters stay as literals in
  `main.rs` and `coupled.rs`; a runtime source (CLI, file, or env) for those is not required.
- **Coupled-mode network interfaces.** Decided: the sim binds and originates
  only on `127.0.0.1`. HAProxy fronts its loopback ports as the entities' zone
  addresses, applies each outbound flow's zone source address, and owns every
  network interface (sim-network.md §3–§5).
- **Shutdown.** Specified under *Stopping and warm start*. Entities are not
  cancelled: each comes to rest and holds its channel ends, so the "channel
  closed = fatal" rule is unchanged.

---

## Stopping and warm start

Implements sim.md §9. The checkpoint is a JSON document; the module
`src/checkpoint.rs` defines its types, reads and validates it, and writes it.

### Stop request

The orchestrator installs a `SIGTERM` handler (`tokio::signal::unix`, which
needs Tokio's `signal` feature) and holds the sending end of a
`tokio::sync::watch` channel. Each entity that begins work of its own receives
a clone of the receiver. Both ends are defined in `src/stop.rs`:

```rust
/// The orchestrator's end of the stop request.
pub struct StopSender(watch::Sender<bool>);

impl StopSender {
    pub fn stop(&self);
}

/// Resolves once the sim has been asked to stop. Cloneable; every entity that
/// begins work of its own holds one.
#[derive(Clone)]
pub struct StopRequest(watch::Receiver<bool>);

impl StopRequest {
    pub async fn requested(&mut self);
    pub fn is_requested(&self) -> bool;
}

pub fn stop_channel() -> (StopSender, StopRequest);
```

`requested()` never resolves once the sender has been dropped, so an entity
spawned by a test without an orchestrator runs until the test ends.

`orchestrator::build(clock, config, checkpoint: Option<Checkpoint>)` returns
the built sim as `SimHandles`, which exposes `stop()`, making the same request,
so that tests stop a sim without sending a signal, and `come_to_rest()`
(*Coming to rest*).

An entity checks it at each point where it would begin work: before sending an
arrival, before a poll or dispatch, before an audit or a routine check, before
taking up a job.
It is not checked inside work in progress, which runs to completion. A sleep
until a due time is raced against `requested()` with `tokio::select!`; the due
time is kept, not the remaining duration. The sleep is
`stop::sleep_until(clock, at)`, which sleeps on the sim clock until `at` and
returns at once if `at` has passed.

### Coming to rest

Each actor and the tug arrival generator receives, at spawn, its starting state
and a `oneshot::Sender` of its state type. Once stopped and idle it calls
`stop::rest(entity, report, state)`, which sends its state on that sender and
then awaits `std::future::pending()`, holding every channel end the entity owns
until the process exits. No peer observes a closed channel, and the task never
finishes, so the supervisor's rule that a finished task is a failure is
unchanged.

An entity that waits on a peer's report receives, at spawn, a
`oneshot::Receiver<()>` on which the orchestrator signals that the peer has
reported, and awaits it with `stop::after_peer(entity, peer, at_rest)`. The
receivers are: the tug arrival generator's for the tug engineer, the fuel
supplier's for the road tanker driver, the dock operator's for the tug engineer
and the road tanker driver together, and the reconciliation user's for the dock
operator. A dropped sender means the orchestrator has gone, and is fatal.

The order in which entities report follows the direction of work:

1. **The tug engineer reports first.** Once the stop is requested, the tug
   engineer finishes the tug in progress, takes up no further arrival, and
   reports. The tug engineer saves nothing: an arrival left in the arrival
   channel stays there, and its tug's departure signal stays open.
2. **The tug arrival generator reports after the tug engineer.** The generator
   reports, for each tug, the position in the tug's sequence between the draw
   of its current time away and the draw of its volume, and the tug's phase:
   `Away`, with the due time of its next arrival, or `AtDock`, with the job of
   an arrival that the tug engineer did not take up. A tug that leaves the dock
   as the sim comes to rest draws its next time away and its volume before the
   generator reports, and the generator reports the position between the two
   draws. No send to the arrival channel blocks, since the channel holds one
   job for each tug on the roster (*Tug arrival generator*).
3. **A sender awaiting completion reports after its receiver.** The fuel
   supplier, if it has sent a `FillJob` and is awaiting its `completed`
   oneshot, awaits either completion or the road tanker driver's report, and
   makes no poll meanwhile. On completion it reports no order pending. If the
   driver reports first, the driver has not taken the job up:
   the driver drains it from the channel and holds it, so its `completed`
   sender is not dropped, and the supplier reports the time it sent the job as
   the tanker's arrival. The driver's saved state carries no job: on a warm
   start the supplier sends a fresh `FillJob` at that time, as after any lead
   time.
4. **The dock operator reports after the tug engineer and the road tanker
   driver.** A refuel or fill those actors have taken up reaches the dock
   operator as a `DockJob`, which is work in progress: the dock operator takes
   up every `DockJob` it receives until both have reported, then reports. It
   begins no work of its own, so it holds no `StopRequest`; its stop is the
   signal that both peers have reported.
5. **The reconciliation user reports last.** It does not begin an audit after
   the stop; an audit in progress finishes. It then awaits the dock operator's
   report, so that every paper artefact the dock operator will post has been
   posted, drains `record_rx`, and reports its window and every artefact held.

The systems administrator waits on no peer: it receives nothing and signals
nothing. It begins no session after the stop; a session in progress runs to its
end, through the exit from `admin-01` and the disconnect, and the actor then
reports. At rest it holds no connection (sim.md §9.2).

The orchestrator routes each report it needs to the entity that waits on it,
in the same way as it pre-creates the channels at construction.
`SimHandles::come_to_rest()`, called after `stop()`, awaits every report and
returns

```rust
pub struct AtRest {
    pub checkpoint: Checkpoint,
    pub rows: FuelTankRows,
}
```

Supervision continues while it waits: an entity that finishes in that time is
fatal, and `come_to_rest()` returns its cause as the error. The orchestrator's
`run` then writes the checkpoint (*Checkpoint document*) and exits with status
0.

The passive entities — the fuel tank, the ATG probe, the ATG console and the
dispenser — keep running throughout, serving the digital world. Once every
actor has reported, the orchestrator requests their state through a request
channel each receives at spawn (`mpsc::Receiver<oneshot::Sender<…State>>`, the
pattern of `sounding_rx`). The orchestrator keeps every request sender, and
every report receiver, for the life of the process, so no passive entity
observes a closed channel after the stop. The fuel tank's reply is a
`FuelTankReport { state: FuelTankState, rows: FuelTankRows }`, where
`FuelTankRows` holds the actor-owned and appliance-owned rows of sim.md §1.2
(`esd_active`, `tanker_pump_running`, `interlock_plug_connected`,
`dispenser_solenoid_open`, `dispenser_pump_energised`), and
`FuelTankRows::at_rest()` is true when every row is false. The orchestrator
confirms the rows are at rest before writing; a sim not at rest is an invariant
violation, and fatal.

### State types

Each entity's state type is public, defined in its `mod.rs` beside `spawn()`,
and derives `serde::Serialize` and `serde::Deserialize`. The orchestrator
builds a cold-start value from the configuration; the existing parameters such
as `initial_volume_l` and `seed` remain as the cold-start values.

| Entity | State type | Fields |
|---|---|---|
| Fuel tank | `FuelTankState` | `volume_l`, `temperature_c`, `bund_wet` |
| ATG probe | `AtgProbeState` | `noise_seed`, `noise_word_pos` |
| ATG console | `AtgConsoleState` | `bund_alarm_latched` |
| Dispenser | `DispenserState` | `cumulative_pulses`, `transaction_pulses` |
| Tug arrival generator | `TugArrivalState` | `seed`, and `tugs: Vec<TugState>` in roster order. Each `TugState` holds `vessel_id`; `word_pos`, the position in the tug's sequence between the draw of its current time away and the draw of its volume; and `phase: TugPhase`, either `Away { next_arrival_at: Option<PrimitiveDateTime> }` or `AtDock { job: DispenseJob }` |
| Dock operator | `DockOperatorState` | `bdn_seq`, the number of the next BDN to issue |
| Road tanker driver | `RoadTankerDriverState` | `meter_totaliser_l`, `ticket_seq`, the number of the next delivery ticket to issue |
| Fuel supplier | `FuelSupplierState` | `next_poll_at`, `order: enum SupplierOrder { None, TankerDueAt(PrimitiveDateTime) }` |
| Reconciliation user | `ReconciliationUserState` | `window_start`, `window_end`, `records: Vec<ReconciliationRecord>` |
| Systems administrator | `SystemsAdministratorState` | `next_check_at`, `previous_check_at: Option<PrimitiveDateTime>` |

The cold-start values of `bdn_seq` and `ticket_seq` are 1. The dispenser's
preset is not saved: at rest no transaction is open, and the next transaction
writes its own preset first. A tug away from the dock has its scheduled arrival
as `next_arrival_at`, and a tug at the dock has the job of an arrival that the
tug engineer has not taken up. A cold start builds every tug `Away`, with no
`next_arrival_at`, at position 0, and draws each tug's first time away when its
task starts (*Tug arrival generator*); no checkpoint holds that state. The tug
engineer saves nothing. The reconciliation user receives
`Option<ReconciliationUserState>`: `None` on a cold start, when its first
window is `first_window(now)`. The fuel supplier's polls keep their schedule
across a stop: each poll falls due one `poll_interval` after the previous one
fell due. The systems administrator's state is `next_check_at`, the end of
its previous session plus `check_interval`, and `previous_check_at`, the start
of that session, from which the next session reads the journal (*Systems
administrator*); the checkpoint carries it as `systems_administrator`, and a
cold start has no `previous_check_at`.

`DispenseJob` carries `arrived_at`, the time the arrival occurred, which the
generator sets to the tug's scheduled arrival; the tug engineer has no other
record of when a job waiting in its channel arrived. On a warm start the jobs
of the tugs at the dock are sent to the tug engineer again, in the order of
their `arrived_at`, before any other arrival. `DispenseJob`, `BDN`,
`DeliveryTicket` and `ReconciliationRecord` derive the serde traits for this
purpose. The reconciliation user's audit is due at `window_end` plus
`settling_delay`, so it is not stored separately.

The vessel and port constants on a `DispenseJob` and its `BDN` are
`&'static str`. Serde's derive borrows a field whose type is written `&str`
from its input, which a checkpoint read from a file cannot lend, so these
fields are declared with the alias `checkpoint::StaticStr` and read with
`checkpoint::static_str`, which leaks each string to give it the static
lifetime. A warm start happens once per process, so the leak is bounded.

A random generator's position is `ChaCha8Rng::get_word_pos()`, a `u128`, stored
as a decimal string because JSON numbers do not carry 128-bit integers exactly.
A warm start calls `ChaCha8Rng::seed_from_u64(seed)`, sets the stream where
the sequence has one (a tug's IMO number), and then calls
`set_word_pos(word_pos)`, which reproduces the sequence from that point.

Every date-time is UTC and serialised in RFC 3339 with a `Z` suffix.

### Checkpoint document

```rust
pub struct Checkpoint {
    /// Incremented whenever the document's shape changes.
    pub format_version: u32,
    /// The sim time at which the checkpoint was written.
    pub written_at: PrimitiveDateTime,
    /// The compiled parameters it was written under.
    pub parameters: Parameters,
    pub fuel_tank: FuelTankState,
    pub atg_probe: AtgProbeState,
    pub atg_console: AtgConsoleState,
    pub dispenser: DispenserState,
    pub tug_arrivals: TugArrivalState,
    pub dock_operator: DockOperatorState,
    pub road_tanker_driver: RoadTankerDriverState,
    pub fuel_supplier: FuelSupplierState,
    pub reconciliation_user: ReconciliationUserState,
    pub systems_administrator: SystemsAdministratorState,
}
```

`Parameters` holds the values of `Config` that decide the simulator's
behaviour, which `Config::parameters` in `orchestrator.rs` fills. From the
constants that `world.yaml` projects (`sim-network.md` §7, image, build, and
supervision) it holds `tank_capacity_l`, `inflow_rate_l_per_min`,
`outflow_rate_l_per_min`, `flow_meter_k_factor`, `overfill_interlock_pct`,
`vessels`, with each vessel's IMO number, name, card and mean time away,
`refuel_volume_min_l`, `refuel_volume_max_l`, `poll_interval`,
`reorder_threshold_l`, `delivery_volume_l`, `lead_time`, `audit_period`,
`settling_delay`, `boundary_margin` and `admin_check_interval`. From the
literals of `coupled.rs` it holds `k_l_per_mm`, `initial_temperature_c`,
`probe_noise_enabled`, `driver_id`, `tanker_registration`,
`clipboard_deadline`, `desktop_settle`, `terminal_settle`, `key_interval`,
`connect_deadline` and `admin_step_deadline`. `Parameters` also holds
`initial_volume_l`, the start state's 35,000 litres (*Start state*), and
`initial_window`, the width of the reconciliation audit's first retrieval
query, not the first audit window. `coupled.rs` derives `initial_window` from
the roster's arrival rate, about 55.8 days at the roster in operation
(*Reconciliation user*, "Splitting the window"). The seeds, the addresses and
transports, `width_floor`, which follows from the roster, and the other values
of the start state are not recorded: a seed in the checkpoint is the sequence's
own state, and a warm start replaces the start state with the checkpoint's
states. The sim's own parameters are compared field by field; any difference
refuses the checkpoint, naming the first field that differs.

**Writing.** `checkpoint::write_at_rest(run_dir, rows, checkpoint)` confirms
that `rows` are at rest, serialises the checkpoint to
`/run/sim/checkpoint.json.tmp`, calls `sync_all`, and renames it to
`/run/sim/checkpoint.json`, so that a reader never sees a partial file. A
failure to write is fatal, and the sim exits non-zero.

**Reading.** At start `main` calls `checkpoint::read(state_dir, run_dir, now,
parameters, max_start_gap)`, which returns `Ok(None)` for a cold start,
`Ok(Some(checkpoint))` for an accepted checkpoint, and an error naming the
reason for a refusal. It reads
`/var/lib/sim/state/checkpoint.json` (`sim-network.md` §7). An absent file is a
cold start. A file that does not parse, a different `format_version`, a
different parameter, `written_at` later than `clock.now_datetime()`, or
`written_at` more than `max_start_gap` earlier than it refuses the checkpoint as
a startup fault; the message gives both times. `max_start_gap` is a literal in
`main.rs`, five minutes, and is not one of the compared `Parameters`, since it
governs the start and not the state. Once the checkpoint is accepted, the orchestrator creates
the empty file `/run/sim/checkpoint-loaded`, on which the deployment waits
before removing the checkpoint's mount (`deployment/deployment.md` §8.1).

**Reseed.** `/var/lib/sim/state/reseed.json`, if present, is an object with
optional fields `tug_arrival_seed` and `atg_probe_noise_seed`, each a `u64`. A
field present replaces that sequence's seed and sets its position to zero;
`tug_arrival_seed` replaces the seed every tug's stream derives from and sets
each tug's position to zero. An
unknown field refuses the file as a startup fault, so a misspelled field is not
ignored. A reseed file without a checkpoint is a startup fault.

### Warm start

Each entity receives its saved state in place of the cold-start value. A due
time later than the start is converted to a sleep of `due − now` on the sim
clock; a due time already passed is a sleep of zero. Each tug's task first
draws the tug's requested volume again from its saved `word_pos` (`sim.md`
§9.3, warm start). A tug that is away then logs its scheduled arrival again
(*Logging*) and sleeps until its `next_arrival_at`. A tug at the dock has its
arrival sent to the tug engineer again, before any other arrival, and awaits
its departure; the volume drawn again only moves its sequence, since its job
carries the volume. The fuel supplier logs a pending order again (*Logging*),
dispatches a saved tanker at its due time and polls at its saved
`next_poll_at`.
The reconciliation user's first window is its saved window, not
`first_window(now)`. The systems administrator's first session is due at its
saved `next_check_at` and reads the journal from its saved
`previous_check_at`.

---

## Physical

### Fuel tank

**Interactions:**
- Responds to: sounding requests from the ATG probe via `mpsc`/`oneshot`; and overfill interlock plug-in requests from the road tanker driver, each establishing a per-plug-in `InterlockCircuit` lease (see below)
- Publishes: `bund_wet` to the bund leak sensor and `outflow_rate` to the dispenser PD flow meter via `watch` channels on state change; and drives the `InterlockCircuit` state on each live interlock lease
- Reads (externally-owned state): `DispenserControlState` from dispenser; `esd_active` from dock operator (for `outflow_active` and the interlock lease); `tanker_pump_running` from road tanker driver

**Interface:**

```rust
/// Level and temperature signal returned to the ATG probe in response to
/// an on-demand sounding request. Computed lazily at the moment of request.
pub struct TankSounding {
    /// Fuel level in millimetres. Derived: `volume_L / k_L_per_mm`.
    pub level_mm: f64,
    /// Fuel temperature in degrees Celsius.
    pub temperature_c: f64,
}

/// State of the overfill interlock circuit at the fill cabinet socket, as
/// sensed by a plugged-in tanker's pump controller. Latches Dead: once broken
/// for a lease it is never driven back to Live; the fill terminates and any
/// subsequent fill re-plugs for a fresh lease.
pub enum InterlockCircuit {
    /// Healthy: `volume_L < overfill_interlock_pct / 100 × tank_capacity_l`
    /// and the ESD is not pressed. The pump controller may run the tanker pump.
    Live,
    /// Broken: `volume_L ≥ overfill_interlock_pct / 100 × tank_capacity_l`, or
    /// the ESD is pressed.
    /// The pump controller cuts the tanker pump. Once a lease reaches this
    /// state it remains Dead until the lease ends.
    Dead,
}

/// Sent by the road tanker driver when it plugs the overfill cable into the
/// fill cabinet socket. The tank replies on `reply` with a receiver carrying
/// the live `InterlockCircuit` state, seeded with the value at the instant of
/// plugging in. The circuit exists only while the driver holds the receiver;
/// dropping it models unplugging and ends the lease.
pub struct InterlockPlug {
    pub reply: oneshot::Sender<watch::Receiver<InterlockCircuit>>,
}

/// Spawn the fuel tank physical model task.
///
/// Waits concurrently on: changes to any inbound watch channel, sounding
/// requests on `sounding_rx`, and a self-scheduled `clock.sleep` wake at the
/// next physical boundary crossing (see below). On any wake: calls
/// `clock.now()`, computes the elapsed duration since the previous update,
/// advances `volume_L` by `net_rate × elapsed` (the inflow and outflow rates
/// in effect during that interval), clamps `volume_L` to
/// `[0, tank_capacity_l]`, records the new `SimInstant` as the last-update
/// time, then recomputes and publishes the derived signals (`inflow_active`,
/// `outflow_active`, `overfill_interlock_open`, `bund_wet`, `outflow_rate`)
/// where changed. Sounding requests additionally receive a reply on the
/// `oneshot::Sender<TankSounding>` carried in the message.
///
/// `inflow_active` is simply `tanker_pump_running`: the tanker pump running is
/// the inflow. The overfill cable and the pump controller live in the road
/// tanker driver (§Road tanker driver), which cuts the pump when the circuit
/// goes `Dead`. The tank's role is the fill-cabinet contact itself: on an
/// `InterlockPlug` request it seeds and hands back a
/// `watch::Receiver<InterlockCircuit>`, seeded `Dead` when
/// `volume_L` is at or above the interlock level
/// (`overfill_interlock_pct / 100 × tank_capacity_l`) or the ESD is pressed and `Live`
/// otherwise, then drives that lease `Dead` on either condition. The lease
/// latches: once `Dead` it is never driven back to `Live`, so a fill that
/// trips the interlock stays cut even if a concurrent drain (not reachable in
/// normal operation) takes the level back below the threshold; a subsequent
/// fill re-plugs for a fresh lease. The instantaneous derived quantity
/// `overfill_interlock_open` (sim.md §1.3) still tracks the level directly —
/// it is the per-lease circuit presented to the driver that latches. The lease
/// lives only while the driver stays plugged in; the state is meaningless when
/// unplugged, so no channel carries it then.
///
/// Boundary scheduling (continuous physics). The interlock is hardwired and
/// independent of ATG polling, so the tank does not wait for the next channel
/// event to notice a crossing: while a net flow is active it computes the time
/// to the next boundary — the interlock level (while a lease is live), the overflow point (`tank_capacity_l`), or empty (0 L) —
/// and schedules a
/// `clock.sleep` wake for that instant, so each signal changes at the true
/// physical moment. With no net flow, no wake is scheduled.
///
/// Overflow and cavitation. There is no inflow cap: if the pump is not cut
/// (a defeated controller, or a unit test with no driver present), `volume_L`
/// reaches `tank_capacity_l` and `bund_wet` latches — fuel has escaped to the
/// bund. `bund_wet` starts dry and is never written back to dry: the escaped
/// fuel physically remains in the bund until drained, and no drying mechanism
/// exists yet (the `bund_wet_tx` channel is retained for one). Sustained
/// outflow at an empty tank is a physical impossibility (the
/// ~10% cavitation guard is FMS software only, narrative §5): reaching 0 L
/// while `outflow_active` is a fatal invariant violation and panics the sim
/// (see Error taxonomy). All three boundary comparisons — overflow, the
/// interlock level, and empty — carry a litre tolerance (`TOL_L`) so that
/// a scheduled wake landing the volume on a boundary is not lost to
/// floating-point error.
pub fn spawn(
    /// Logical clock used to obtain the current `SimInstant` via `clock.now()`
    /// on every update, and to schedule `clock.sleep` wakes at the next
    /// physical boundary crossing (see above). Wakes are driven by both inbound
    /// channel events and these self-scheduled boundary deadlines.
    clock: SimClock,

    /// Maximum tank volume in litres. The overflow point at which `bund_wet`
    /// is set, and, with `overfill_interlock_pct`, the interlock level.
    tank_capacity_l: f64,

    /// Overfill interlock level as a percentage of `tank_capacity_l`
    /// (`process.overfill_interlock_pct`, `sim.md` §1.1). A live interlock
    /// lease goes `Dead` at `overfill_interlock_pct / 100 × tank_capacity_l`.
    overfill_interlock_pct: f64,

    /// Tank geometry constant: litres per millimetre of level. Converts
    /// `volume_L` to the `level_mm` returned in `TankSounding`.
    k_l_per_mm: f64,

    /// Inflow rate in litres per minute applied while `inflow_active`.
    inflow_rate_l_per_min: f64,

    /// Outflow rate in litres per minute applied while `outflow_active`, and
    /// the value published on `outflow_rate_tx` when outflow is active.
    outflow_rate_l_per_min: f64,

    /// Initial fuel volume in litres at sim start (*Start state*).
    initial_volume_l: f64,

    /// Fuel temperature in degrees Celsius. Held constant; the interface
    /// supports a richer thermal model later.
    initial_temperature_c: f64,

    /// On-demand sounding requests from the ATG probe. On each request the
    /// tank computes the current `TankSounding` lazily and replies on the
    /// oneshot sender carried in the message.
    sounding_rx: mpsc::Receiver<oneshot::Sender<TankSounding>>,

    /// Wet/dry signal to the bund leak sensor. Latching: initialised dry by the
    /// caller and driven wet once on overflow; the tank never writes it back to
    /// dry (see "Overflow and cavitation" above).
    bund_wet_tx: watch::Sender<bool>,

    /// Overfill interlock plug-in requests from the road tanker driver. On each
    /// `InterlockPlug` the tank seeds a watch channel with the current
    /// `InterlockCircuit` state, keeps the sender (driving it while the lease
    /// lives), and replies with the receiver; it discards the sender when the
    /// driver drops the receiver (unplugs).
    interlock_plug_rx: mpsc::Receiver<InterlockPlug>,

    /// Outflow rate to the dispenser PD flow meter. Published on every
    /// state change; `None` when outflow is inactive.
    outflow_rate_tx: watch::Sender<OutflowRate>,

    /// Current solenoid and pump state from the dispenser appliance.
    /// Used to compute the derived quantity `outflow_active`.
    dispenser_ctrl_rx: watch::Receiver<DispenserControlState>,

    /// True when the dock operator has depressed the ESD button. Forces
    /// `outflow_active` to false, and drives any live interlock lease `Dead`
    /// (the ESD breaks the fill-cabinet circuit), which the driver's pump
    /// controller obeys.
    esd_rx: watch::Receiver<bool>,

    /// True when the road tanker pump is running. This is `inflow_active`.
    tanker_pump_rx: watch::Receiver<bool>,
)
```

---

### Logical clock

**Interactions:**
- Passed as `clock` to any sim entity whose update loop is paced by simulated time
- In `WallTimeMultiple` mode: `clock.sleep(duration)` delegates to
  `tokio::time::sleep(duration / multiplier)`. `now()` is derived from Tokio's
  clock — elapsed Tokio time since a start instant captured lazily on first
  use, scaled by the multiplier. Clones share that start instant and the
  multiplier via `Arc`, so `now()` is coherent across clones.
- In `Synthetic` mode: `clock.sleep(duration)` parks on a `tokio::sync::Notify`
  registered in a shared priority queue. The runtime's `on_thread_park` hook
  advances simulated time and fires the relevant notifiers when all tasks are
  `Poll::Pending`. All clones share state via `Arc<Mutex<...>>`.

**Notes:**
- The time model is fully internal to `SimClock`; entity code is identical in all
  modes.
- `Synthetic` mode requires the runtime to be constructed via `SimClock::isolated()`,
  which installs the `on_thread_park` hook internally and returns the configured
  runtime alongside the clock.
- `spawn_blocking` must not be used with `Synthetic` mode. Blocking tasks run outside
  the async worker threads and are invisible to `on_thread_park`; the clock may advance
  while blocking work is still in progress.
- **I/O-guard discipline.** Real boundary I/O is likewise invisible to
  `on_thread_park`, so each round-trip is bracketed by an `IoGuard`
  (`clock.io_guard()`) that inhibits `Synthetic` advance while it is in flight —
  making the exchange instantaneous in simulated time rather than skipped over.
  A sim *client* guards the round-trips it initiates (dock operator, fuel
  supplier); a sim *server* guards each exchange it serves (ATG console,
  dispenser), leaving its idle wait for the next request unguarded. Only sim
  entities guard — a mocked digital component never does, because the sim server
  it reaches guards the exchange. See sim-clock.md "Real Boundary I/O".
- `isolated()` takes the sim-start epoch (a `time::PrimitiveDateTime`);
  `coupled()` takes none, and uses the current UTC time. `now_datetime()`
  returns the epoch plus the elapsed simulated duration — a calendar
  wall-clock time for protocol timestamps and paper artefacts — while `now()`
  (a `SimInstant`) remains the elapsed-duration view. The value is UTC; in
  coupled mode it agrees with the digital hosts' clocks to within their time
  synchronisation.
- See [sim-clock.md](specs/host_specs/sim/sim-clock.md) for the detailed design of the `Synthetic` mode
  mechanism.

**Interface:**

```rust
/// Specifies how simulated time advances in isolated mode.
pub enum TimeModel {
    /// Simulated time advances at a fixed multiple of wall-clock time.
    /// `clock.sleep(d)` delegates to `tokio::time::sleep(d / multiplier)`.
    /// A multiplier of 1.0 matches wall-clock pace; 2.0 runs the sim at twice
    /// real-time speed. Must be finite and positive (> 0.0); the constructor
    /// panics otherwise.
    WallTimeMultiple(f64),

    /// Simulated time advances only when all tasks are `Poll::Pending`.
    /// `clock.sleep(d)` parks on a `tokio::sync::Notify`; the runtime's
    /// `on_thread_park` hook advances the clock and fires the relevant
    /// notifiers when the scheduler reaches quiescence. All clones share
    /// state via `Arc<Mutex<...>>`.
    Synthetic,
}

/// A point in simulated time.
///
/// Represented internally as a `Duration` elapsed since sim start.
/// Obtained from `SimClock::now()`. Supports elapsed-time computation via
/// `duration_since()`. Used by the fuel tank to compute the time elapsed
/// between state transitions for lazy quantity evaluation.
pub struct SimInstant { /* ... */ }

impl SimInstant {
    /// Returns the duration elapsed since `earlier`.
    pub fn duration_since(&self, earlier: SimInstant) -> Duration;
}

/// Logical clock handle shared across sim entities.
///
/// Cheap to clone: every clone shares the same underlying state via an `Arc`,
/// so a sleep on one clone advances the simulated time observable through any
/// other clone. The shared state includes the sim-start epoch (both modes),
/// plus the multiplier and a lazily-captured start instant in `WallTimeMultiple`
/// mode, or the time counter and sleeper queue in `Synthetic` mode.
#[derive(Clone)]
pub struct SimClock { /* ... */ }

impl SimClock {
    /// Construct a `SimClock` for coupled mode: 1:1 wall-clock pace, with the
    /// calendar starting at the current UTC time (sim.md §2.1). The only
    /// constructor valid against the digital world — sim time and digital time
    /// are then one time base, so a date the sim writes on paper is the date
    /// the digital world records.
    ///
    /// The system clock is read once, here, and never again: `now_datetime()`
    /// is that start time plus the monotonic elapsed duration. Re-reading it
    /// would let anything able to set the shared host clock move the sim's time
    /// (sim.md §2.1). A step in the host clock after construction is therefore
    /// not followed, and the sim must be restarted after one.
    pub fn coupled() -> (Self, tokio::runtime::Runtime);

    /// Construct a `SimClock` for isolated mode with the given `TimeModel` and
    /// sim-start epoch — the calendar date-time that simulated t=0 represents
    /// (e.g. `datetime!(2025-01-01 0:00)` from the `time` crate). Not for a run
    /// against the digital world: a fixed epoch puts every sim date-time out
    /// of step with the digital hosts' clocks.
    ///
    /// Returns the clock and the tokio `current_thread` runtime configured
    /// for the chosen mode. For `Synthetic`, the `on_thread_park` hook is
    /// installed on the runtime internally; the caller never handles it.
    ///
    /// ```rust
    /// let (clock, runtime) =
    ///     SimClock::isolated(TimeModel::Synthetic, datetime!(2025-01-01 0:00));
    /// runtime.block_on(run_sim(clock));
    /// ```
    pub fn isolated(model: TimeModel, start: PrimitiveDateTime)
        -> (Self, tokio::runtime::Runtime);

    /// Returns the current simulated time as a `SimInstant` (elapsed duration
    /// since sim start).
    pub fn now(&self) -> SimInstant;

    /// Returns the current simulated wall-clock date and time: the sim-start
    /// epoch plus the elapsed simulated duration (`time::PrimitiveDateTime`).
    /// Used by the ATG console for the `YYMMDDHHmm` timestamp in TLS responses.
    pub fn now_datetime(&self) -> PrimitiveDateTime;

    /// Suspend the caller for `duration` of simulated time.
    ///
    /// `WallTimeMultiple`: delegates to `tokio::time::sleep(duration / multiplier)`.
    /// `Synthetic`: registers a sleeper in the priority queue and parks on a
    /// `tokio::sync::Notify` until the `on_thread_park` hook fires it.
    pub async fn sleep(&self, duration: Duration);

    /// Acquire an `IoGuard` for a boundary I/O round-trip, held across the whole
    /// exchange. While any guard is alive, `Synthetic` time does not advance, so
    /// the real round-trip is drained before the clock moves. Only sim entities
    /// may call this (see the I/O-guard discipline note above).
    pub fn io_guard(&self) -> IoGuard;
}
```

---

### ATG probe

**Interactions:**
- Receives: reading requests from the ATG console via `mpsc` channel; replies on the `oneshot` sender carried in each request
- Requests: a sounding from the fuel tank via `mpsc`/`oneshot` on each reading request; the tank computes the current `TankSounding` lazily and replies immediately
- Optional noise model: std dev 0.25 mm when enabled (seeded; see `ProbeReading`)

**Interface:**

```rust
/// The reading returned by the ATG probe in response to an on-demand request.
pub struct ProbeReading {
    /// Fuel level in millimetres. When the noise model is enabled, independent
    /// Gaussian noise with σ = 0.25 mm is added to each reading. This is
    /// derived from the Veeder-Root Mag-FLEX tall-tank probe published
    /// repeatability of ±0.5 mm, treating that figure as the ±2σ bound.
    pub level_mm: f64,

    /// Fuel temperature in degrees Celsius.
    pub temperature_c: f64,
}

/// Spawn the ATG probe task.
///
/// Waits for reading requests on `reading_rx`. On each request: sends a
/// fresh oneshot sender to the fuel tank via `sounding_tx`, awaits the
/// lazily-computed `TankSounding` reply, applies the noise model, and
/// responds to the ATG console via the oneshot sender carried in the
/// original request.
pub fn spawn(
    /// Inbound reading requests from the ATG console. Each message carries
    /// a oneshot sender on which the probe returns the completed `ProbeReading`.
    reading_rx: mpsc::Receiver<oneshot::Sender<ProbeReading>>,

    /// Channel to the fuel tank for on-demand sounding requests. The probe
    /// sends a oneshot sender; the tank computes the current sounding lazily
    /// and replies immediately.
    sounding_tx: mpsc::Sender<oneshot::Sender<TankSounding>>,

    /// When `true`, each reported `level_mm` has independent Gaussian noise
    /// added with σ = 0.25 mm. Derived from the Veeder-Root Mag-FLEX
    /// tall-tank probe published repeatability of ±0.5 mm (±2σ bound).
    /// Source: Veeder-Root Mag-FLEX product page,
    /// https://www.veeder.com/us/automatic-tank-gauging-products/probes-and-float-kits/magnetostrictive-mag-flex-tall-tank-probe
    noise_enabled: bool,

    /// Seed for the probe's noise PRNG (`rand_chacha::ChaCha8Rng`). Fixing it
    /// makes the noise reproducible across runs. Unused when `noise_enabled`
    /// is `false`.
    noise_seed: u64,
)
```

---

### Bund leak sensor

**Interactions:**
- Reads: `bund_wet` from physical model
- Exposes reading to: ATG console via `watch` channel
- Alarm latches on ATG console until physically cleared and manually acknowledged

**Interface:**

```rust
/// The reading produced by the bund leak sensor on each sampling.
pub struct BundReading {
    /// True when fuel is detected in the secondary containment bund.
    /// In the real installation this is a continuous resistance signal;
    /// in the sim it is updated on each change to `bund_wet` in the
    /// physical model.
    pub wet: bool,
}

/// Spawn the bund leak sensor task.
///
/// Waits for changes on `bund_wet_rx`. On each change, publishes a fresh
/// `BundReading` via `bund_tx`. Alarm latching is the ATG console's
/// responsibility, not the sensor's.
pub fn spawn(
    /// Wet/dry signal from the fuel tank. A bool carrying `bund_wet`;
    /// true means fuel is present in the secondary containment.
    bund_wet_rx: watch::Receiver<bool>,

    /// Sender for the bund reading channel. The ATG console holds
    /// the corresponding receiver.
    bund_tx: watch::Sender<BundReading>,
)
```

---

### Dispenser PD flow meter

**Interactions:**
- Driven by: `SimClock` — derives pulse interval from `OutflowRate` and `k_factor`; blocks while rate is `None`
- Reads: `OutflowRate` from the fuel tank via `watch` channel
- Sends: `Pulse` events to the dispenser appliance via `mpsc` channel

**Interface:**

```rust
/// A single pulse emitted by the flow meter.
///
/// Carrying `k_factor` on every pulse guarantees that the dispenser uses
/// exactly the K-factor the meter is operating with. This eliminates the
/// risk of a construction-site mismatch between the two entities.
pub struct Pulse {
    /// K-factor of this meter, in pulses per litre.
    pub k_factor: f64,
}

/// Spawn the dispenser PD flow meter task.
///
/// Waits for a non-zero `OutflowRate` on `outflow_rate_rx`. Once the rate
/// is non-zero, concurrently waits for either the next pulse interval
/// (`60.0 / (k_factor × rate_L_per_min)` seconds of simulated time) or a
/// change to `outflow_rate_rx`: emits one `Pulse` on each interval expiry
/// and recomputes the interval immediately on any rate change. When the
/// rate is `None`, blocks until the next change.
///
/// Models the open-collector NPN pulse train produced by a real PD meter:
/// each send on `pulse_tx` represents one physical pulse on the meter's
/// output wire.
pub fn spawn(
    clock: SimClock,

    /// K-factor of this meter, in pulses per litre. Used to derive the
    /// pulse interval from the received `OutflowRate`. Carried on every
    /// emitted `Pulse` so that the dispenser's volume calculations are
    /// always consistent with the meter's own operating parameters.
    k_factor: f64,

    /// Outflow rate from the fuel tank. `None` means no outflow;
    /// `Some(rate_L_per_min)` causes the meter to emit pulses at the
    /// interval `60.0 / (k_factor × rate_L_per_min)` seconds. The meter
    /// reacts immediately to rate changes on this channel.
    outflow_rate_rx: watch::Receiver<OutflowRate>,

    /// Pulse event channel to the dispenser. Each send represents one
    /// physical pulse on the meter's NPN open-collector output wire.
    pulse_tx: mpsc::Sender<Pulse>,
)
```

---

### ESD button

**Interactions:**
- `esd_active` state set by: dock operator actor (physical action)
- Hardwired: forces `outflow_active` to false in the fuel tank, and — while a
  tanker is plugged in — drives the interlock lease `Dead`, cutting the tanker
  pump in the road tanker driver's pump controller (narrative §line 209)

**Notes:**
- No own task or module. Modelled by an `esd_rx: watch::Receiver<bool>` held by
  the fuel tank, with the sender held by the dock operator actor. The tank folds
  ESD into the `InterlockCircuit` state it drives on each live interlock lease,
  so the driver's pump controller obeys a single circuit signal and does not
  observe ESD directly.

---

### Overfill probe and interlock circuit

**Interactions:**
- Derived from: `volume_L` and `esd_active` in the fuel tank — the circuit is `Dead` when `volume_L ≥ overfill_interlock_pct / 100 × tank_capacity_L` or the ESD is pressed, `Live` otherwise
- Presents: the fill-cabinet dry contact as an `InterlockCircuit` state
- Consumed by: the road tanker driver's pump controller, which cuts the tanker pump when the circuit goes `Dead`

**Notes:**
- No own task or module. The contact derives from `volume_L` (and `esd_active`),
  which the fuel tank owns, so it is computed there. It is not a permanent
  channel: plugging in sends the tank an `InterlockPlug`, and the tank replies
  with a `watch::Receiver<InterlockCircuit>` seeded with the current state and
  driven for the lease's lifetime; unplugging drops the receiver. This enforces
  that the signal exists only while the cable connects the tanker's pump
  controller to the socket. The interlock no longer gates `inflow_active`
  directly; its effect reaches inflow only by stopping the tanker pump — the
  hardwired path in narrative §line 214.

---

### Tug arrival generator

**Interactions:**
- Driven by: `SimClock` — each tug's task calls `clock.sleep(time_away)` between the tug leaving the dock and its next arrival
- Signals: tug engineer task via arrival channel on each tug arrival
- Receives: each tug's departure from the tug engineer, via the `departed` oneshot carried on its `TugArrival`

**Interface:**

```rust
/// A tug on the roster, compiled in from the numbered vessel keys of
/// `sim-constants.toml` (`deployment-config.md` §4.1).
pub struct Vessel {
    /// IMO number of the vessel. Also selects the vessel's random stream.
    pub vessel_id: VesselId,
    /// Name of the vessel, written on the BDN alongside its IMO number.
    pub name: &'static str,
    /// RFID card number for the vessel.
    pub rfid_card: RfidCardNumber,
    /// Mean time between the tug leaving the dock and its next arrival
    /// (`roster.vessels[].mean_time_away_s`).
    pub mean_time_away: Duration,
}

/// A tug refuelling job.
pub struct DispenseJob {
    /// The instant the tug arrived (*Stopping and warm start*, "State
    /// types").
    pub arrived_at: PrimitiveDateTime,
    /// IMO number of the arriving vessel.
    pub vessel_id: VesselId,
    /// Name of the vessel, written on the BDN alongside its IMO number.
    pub vessel_name: &'static str,
    /// Volume of fuel requested, drawn uniformly between the refuel volume
    /// bounds.
    pub volume_l: f64,
    /// RFID card number for the vessel, carried on the job so the tug
    /// engineer can present the correct card at the dispenser reader.
    pub rfid_card: RfidCardNumber,
}

/// A tug's arrival at the dock: its job, and the signal by which the tug
/// engineer reports that the tug has left. The tug engineer forwards the job
/// to the dock operator and keeps the signal.
pub struct TugArrival {
    pub job: DispenseJob,
    /// Fired by the tug engineer once the tug has left the dock: after it has
    /// handed back the co-signed BDN, or once the tug departs unserved where
    /// the FMS refuses the job. The tug's next time away starts on receipt.
    pub departed: oneshot::Sender<()>,
}

/// Spawn the tug arrival generator: one task per roster vessel.
///
/// Each task runs for the duration of the sim. On each cycle it draws the
/// tug's time away, an Exp(1) sample multiplied by the vessel's
/// `mean_time_away`, and the requested volume from
/// Uniform(`refuel_volume_min_l`, `refuel_volume_max_l`); logs one `INFO`
/// event with the vessel's IMO number, its scheduled arrival, one time away
/// after the draw, and the requested volume, as a warm start does again for a
/// tug that is away; calls `clock.sleep(time_away)`;
/// sends a `TugArrival` to the tug engineer; and awaits its `departed` signal
/// before drawing the next time away. A tug therefore has at most one arrival
/// outstanding, and a tug that is waiting or refuelling does not arrive again
/// (`sim.md` §6).
///
/// Random streams. Each task's generator is a `ChaCha8Rng` seeded from
/// `seed`, with its stream set to the vessel's IMO number. Each tug's
/// sequence of times away and volumes is therefore reproducible from `seed`
/// and independent of the other tugs, so adding or removing a vessel leaves
/// the others' sequences unchanged. Drawing a unit exponential and scaling it
/// keeps the sequence of draws independent of the mean.
///
/// Start-up faults, refused at spawn: an empty roster; a vessel whose
/// `mean_time_away` is zero; refuel volume bounds that are not positive and
/// finite, or whose minimum is not below its maximum. A dropped `departed`
/// sender is fatal: the task can no longer tell when its tug left.
pub fn spawn(
    clock: SimClock,

    /// The roster, one task per vessel. Projected from `world.yaml`
    /// (`roster.vessels`).
    vessels: &'static [Vessel],

    /// Bounds of each tug's requested volume in litres, the same for every
    /// tug (`roster.refuel_volume_min_l`, `roster.refuel_volume_max_l`;
    /// narrative §2.2, "~500–3,000 L", by default).
    refuel_volume_min_l: f64,
    refuel_volume_max_l: f64,

    /// PRNG seed from which every tug's stream is derived. The same seed
    /// produces the identical sequence for each tug across runs.
    seed: u64,

    /// Channel to the tug engineer task. Its capacity is the roster size
    /// (orchestrator), so a send never blocks.
    tug_arrival_tx: mpsc::Sender<TugArrival>,
)
```

---

## Appliances

### ATG console

**Interactions:**
- Requests: probe readings from the ATG probe via `mpsc`/`oneshot` on each inbound TCP poll requiring level or temperature data
- Watches: bund sensor via `watch` channel; latches bund alarm internally on first wet reading
- External network: TCP port 10001, Veeder-Root TLS serial-over-TCP protocol
- Serves inbound connections from: FMS (digital world); fuel supplier actor (routed via `atg-relay-01`)
- Function codes served: `i101` (System Status Report), `i201` (In-Tank Inventory Report), `i205` (In-Tank Status Report)

**TLS wire protocol.** The exact request/response framing, checksum, ASCII float
encoding, timestamp, and the `i101`/`i201`/`i205` layouts are specified in
[tls-protocol.md](specs/host_specs/sim/tls-protocol.md), which is the single source of truth (taken
from the Veeder-Root serial interface manual). This section records only the
console's testbed-specific behaviour; it does not restate the wire format.

**Testbed-specific behaviour:**

- **Computer format, no terminator.** The console serves computer-format
  responses only and reads the fixed-length six-character function code after
  `SOH`; there is no line terminator (tls-protocol.md §2).
- **`i201`** — on each request the console requests a `ProbeReading` via
  `probe_request_tx`, awaits it, and emits one per-tank record for tank `01`
  (`TT p ssss NN` + seven floats, tls-protocol.md §6.2). The seven fields are
  derived from the reading and geometry: Volume = `level_mm × k_l_per_mm`;
  TC Volume = Volume (temperature correction is identity at constant 15 °C);
  Ullage = `tank_capacity_l − Volume`; Height = `level_mm`; Water = `0.0`
  (no water sensor); Temperature = `temperature_c`; Water Volume = `0.0`.
  Product code `p` = `D`; status bits `ssss` = `0000` (no delivery/leak-test
  awareness); field count `NN` = `07`.
- **`i205`** — In-Tank Status Report: the per-tank active tank-alarm list
  (tls-protocol.md §6.3). The console models no tank alarms, so it returns
  `0100` (tank `01`, zero alarms). The bund alarm is a *sensor* alarm reported
  via `i101`, not here.
- **`i101`** — System Status Report (`AANNTT` entries, tls-protocol.md §6.1).
  The bund leak sensor (sensor 1) is the only alarm the console emits: while the
  alarm is latched the body is `030301`; otherwise it is the all-normal entry
  `000000`. The alarm latches on the first wet `BundReading` and is retained
  until an `alarm_clear_rx` signal clears it (re-latching on any subsequent wet
  reading) — matching Veeder-Root TLS default behaviour.

**Interface:**

```rust
/// Spawn the ATG console emulator task.
///
/// Concurrently waits for inbound TCP connections on `listen_addr`
/// and changes on `bund_rx`.
///
/// On each TCP connection: serves Veeder-Root TLS function codes; for i201
/// requests, sends a reading request to the probe via `probe_request_tx`
/// and awaits the reply before responding. The FMS and the fuel supplier
/// actor (routed via `atg-relay-01`) both connect here; the console does
/// not distinguish between callers.
///
/// On each change to `bund_rx`: if wet, latches a bund alarm internally,
/// retained until manually cleared — matching Veeder-Root TLS default
/// behaviour.
pub fn spawn(
    /// Logical clock. Queried via `clock.now_datetime()` for the
    /// `YYMMDDHHmm` timestamp in every TLS response (tls-protocol.md §3).
    clock: SimClock,

    /// Tank geometry constant: litres per millimetre of level. Used to
    /// convert `ProbeReading.level_mm` to volume for i201 responses.
    /// Mirrors the strapping table configured in a real ATG console at
    /// installation.
    k_l_per_mm: f64,

    /// Maximum tank volume in litres. Used to compute ullage
    /// (`tank_capacity_l − volume`) for i201 responses.
    tank_capacity_l: f64,

    /// Channel to the ATG probe for on-demand reading requests. On each
    /// inbound i201 poll the console sends a oneshot sender; the probe
    /// returns a freshly computed `ProbeReading`.
    probe_request_tx: mpsc::Sender<oneshot::Sender<ProbeReading>>,

    /// Receiver end of the bund sensor's watch channel. The console watches
    /// this continuously and latches a bund alarm internally once a wet
    /// reading is observed, retaining the alarm state until it is manually
    /// cleared — matching Veeder-Root TLS default behaviour.
    bund_rx: watch::Receiver<BundReading>,

    /// Manual alarm clear signal. Each received message represents one
    /// acknowledgement action at the console — matching the physical clear
    /// button on a real ATG console. Clears the internally latched bund alarm
    /// immediately on receipt; any subsequent wet reading will re-latch it.
    alarm_clear_rx: mpsc::Receiver<()>,

    /// TCP address and port to bind the TLS-over-TCP listener. Must be the
    /// IP assigned to this appliance's dedicated NIC on vnet-ot, so that
    /// the ATG console appears as a distinct network host to the digital
    /// world. IP assignment is an infrastructure concern (VM or container
    /// config); the binary receives this value at startup. Conventionally
    /// `<IP_ATG>:10001`.
    listen_addr: SocketAddr,
)
```

---

### Dispenser

The Modbus TCP wire protocol, addressing, function codes, register
encodings, and exception handling are specified in
[dispenser-modbus-tcp-spec.md](specs/host_specs/sim/dispenser-modbus-tcp-spec.md); the
register map below is the testbed dispenser's own minimal layout.

**Interactions:**
- Receives: `Pulse` events from the dispenser PD flow meter via `mpsc` channel
- External network: Modbus TCP port 502
- Receives from FMS (write): preset volume (register 0x0001), pump enable (0x0002)
- Exposes to FMS (read): cumulative totaliser (0x0003–0x0004), transaction totaliser (0x0005–0x0006), status flags (0x0007)
- Publishes solenoid and pump state to: physical model via `watch` channel
- Auto-stops (closes solenoid, disables pump) when transaction totaliser reaches preset

**Notes — register encoding:**

The two totaliser values are each a 32-bit quantity stored as a big-endian high/low word pair across two consecutive 16-bit Modbus registers — the standard Modbus convention for 32-bit quantities. The FMS reconstructs the full value as `(high_word << 16) | low_word`.

The transaction totaliser resets to zero each time a new preset is written to register 0x0001. This is the natural "start of a new transaction" boundary: the FMS writes the preset first, then enables the pump. The reset therefore occurs before any new pulses arrive.

Register 0x0007 is a 16-bit bitfield (positive logic, LSB = bit 0). Only bit 0 is meaningful in this implementation; all other bits are always zero.

| Bit | Name | 0 | 1 |
|-----|------|---|---|
| 0 | Running | Pump not dispensing | Pump actively dispensing |
| 1–15 | Reserved | 0 | — |

Bit 0 tracks `solenoid_open && pump_energised` exactly. Real forecourt dispensers (e.g. Bennett Pump Modbus interface, CiA 402 drive profile) use a bitfield for status to allow concurrent boolean conditions; the reserved bits are present so the register conforms to that convention.

A real dispenser would expose an "Authorised" bit (FMS has enabled the pump but the operator has not yet lifted the nozzle). That intermediate state is absent from this model — writing 1 to register 0x0002 opens the solenoid immediately — so the authorised bit is omitted.

**Interface:**

```rust
/// Solenoid and pump state published by the dispenser to the physical model.
/// The physical model uses these to compute the derived quantity `outflow_active`.
pub struct DispenserControlState {
    /// True when the dispenser solenoid is open. Set to true when the FMS
    /// writes pump_enable = 1 (register 0x0002); set to false on auto-stop
    /// or when the FMS writes pump_enable = 0.
    pub solenoid_open: bool,

    /// True when the dispenser pump is energised. Transitions in lockstep
    /// with `solenoid_open`; both are controlled by the single pump_enable
    /// register and both are cleared together on auto-stop.
    pub pump_energised: bool,
}

/// Spawn the dispenser emulator task.
///
/// Concurrently waits for incoming Modbus TCP connections from the FMS on
/// `listen_addr` and for `Pulse` events from the flow meter on `pulse_rx`.
///
/// On each pulse: increments the accumulated pulse counter; checks whether
/// the transaction pulse count has reached `preset_L × pulse.k_factor`;
/// triggers auto-stop if so (clears solenoid_open and pump_energised,
/// publishes updated `DispenserControlState` via `dispenser_ctrl_tx`).
///
/// On Modbus register read: returns the cumulative totaliser
/// (0x0003–0x0004), transaction totaliser (0x0005–0x0006), or status
/// flags (0x0007) as raw pulse counts or a bitfield respectively.
/// On Modbus register write: loads a new preset volume (0x0001) —
/// resetting the transaction totaliser to zero — or enables/disables
/// the pump (0x0002).
///
/// The dispenser's behaviour is fully reactive — it has no internal timing and
/// acts only on incoming pulses and FMS register operations — yet it takes a
/// `clock`. The clock is *not* used to read or sleep on simulated time; it is
/// used solely to hold an `IoGuard` while serving each Modbus exchange. Under
/// `Synthetic` time a sim server must guard the exchanges it serves so
/// simulated time cannot advance mid-round-trip, because the FMS client may be
/// a mocked component, which is not permitted to guard (see the I/O-guard
/// discipline under "Logical clock"). The guard is a property of the clock, so
/// even a timing-free appliance needs a clock handle to participate.
pub fn spawn(
    /// The simulated clock, used only for its `io_guard` while serving Modbus
    /// exchanges (above); the dispenser does not otherwise depend on time.
    clock: SimClock,

    /// Pulse events from the dispenser PD flow meter. Each received
    /// `Pulse` represents one physical pulse on the meter's output wire.
    /// The dispenser accumulates these continuously, modelling the hardware
    /// interrupt counter of a real dispenser controller. The K-factor
    /// carried on each pulse is used for all volume conversions.
    pulse_rx: mpsc::Receiver<Pulse>,

    /// Sender for the dispenser control state channel. Published whenever
    /// solenoid or pump state changes. The fuel tank holds the
    /// corresponding receiver to compute `outflow_active`.
    dispenser_ctrl_tx: watch::Sender<DispenserControlState>,

    /// TCP address and port to bind the Modbus TCP listener. Must be the
    /// IP assigned to this appliance's dedicated NIC on vnet-ot so that
    /// the dispenser appears as a distinct network host to the digital
    /// world. Conventionally `<IP_DISP>:502`.
    listen_addr: SocketAddr,
)
```

---

### Vessel RFID reader

**Interactions:**
- Triggered by: tug engineer actor sending card number via `mpsc` channel
- Presents: card number to FMS host via `SerialConnection`

**Notes — serial wire format:**

On each card event the reader writes the card number as a decimal integer string followed by a newline (`\n`). For example, `RfidCardNumber(12345)` is written as `"12345\n"`. This matches the output of a standard Wiegand-to-serial decoder: the full 26-bit value is emitted as a single unsigned decimal token with no facility-code separation.

**Interface:**

```rust
/// Spawn the vessel RFID reader task.
///
/// Waits for card presentation events from the tug engineer actor.
/// On each event, writes the card number to `serial`, which delivers it
/// to the FMS host as a Wiegand-decoded serial string. The FMS reads the
/// card number and validates it against the vessel ID on the pending job.
///
/// Purely event-driven — no clock needed.
pub fn spawn(
    /// Card number events from the tug engineer actor. Sent once
    /// per vessel arrival when the engineer presents the RFID tag at the
    /// dispenser reader.
    card_rx: mpsc::Receiver<RfidCardNumber>,

    /// Serial connection to the FMS host. The underlying transport
    /// (PTY, TCP, or other) is provided by the serial connection
    /// abstraction; the reader has no knowledge of it.
    serial: SerialConnection,
)
```

---

### Operator RFID reader

**Interactions:**
- Triggered by: dock operator actor sending card number via `mpsc` channel
- Presents: card number to HMI host via `SerialConnection`

**Notes — serial wire format:**

Identical to the vessel RFID reader: decimal integer string followed by `\n`. The same `SerialConnection` abstraction is used; the transport differs (HMI host instead of FMS host) but the byte-level format is the same.

**Interface:**

```rust
/// Spawn the operator RFID reader task.
///
/// Waits for card presentation events from the dock operator actor.
/// On each event, writes the card number to `serial`, which delivers it
/// to the HMI host as a Wiegand-decoded serial string. The HMI passes
/// the card number to the FMS for validation against the operator card
/// table.
///
/// Purely event-driven — no clock needed.
pub fn spawn(
    /// Card number events from the dock operator actor. Sent once per
    /// login when the operator presents their RFID card at the HMI reader.
    card_rx: mpsc::Receiver<RfidCardNumber>,

    /// Serial connection to the HMI host. Same abstraction as the vessel
    /// reader; transport details are hidden.
    serial: SerialConnection,
)
```

---

## Actors

### Dock operator

**Interactions:**
- Digital: login to HMI (username/password + RFID); enter tug refuelling and fill job requests; set dispenser preset and start pump; transcribe delivery ticket fields via HMI; enter BDN number via HMI
- Physical: press ESD button (sets `esd_active`); sign paper BDN; sign paper delivery ticket
- Receives: `DockJob` entries via job channel; a `DispenseJob` forwarded by the tug engineer task, or a delivery volume and tanker registration forwarded by the road tanker driver. A waiting fill is taken before a waiting dispense (Order of jobs, below)
- Signals: tug engineer to proceed or stand down, via the `oneshot::Sender<JobAdmission>` carried in `DockJob::Dispense`, once the HMI has answered the create; signals the road tanker driver to proceed via the `oneshot::Sender<()>` carried in `DockJob::Fill`, a fill having no refusal
- Receives: signed delivery ticket from the road tanker driver via channel (paper artefact handover)
- Spawned at sim startup; runs for the duration of the sim

**Notes — HTTP client and constants:**

The digital interface uses `reqwest` over plain HTTP, source-bound to
`local_addr` (the dock operator's address on `vnet-ot`, the source of all
requests to the HMI). TLS is terminated by a reverse proxy in front of the
sim when run against the digital world (digital.md §2.3); the actor and its
tests speak HTTP directly.

Values not derivable from a job are module-private constants, extensible
later:

- Operator identity — the username, password and RFID card number are **read from the compiled world constants**, not written as literals: the FMS verifies all three, so the sim and the directory must hold one authored value between them (`deployment-config.md` §3, §6). The BDN's `operator_id` is the same username: the audit compares it with the `operator` on the job's log events, and a disagreement between paper and record is fatal;
- The fuel's properties — grade, density at 15 °C, sulphur content and flashpoint — **read from the compiled world constants** (`process` in `world.yaml`): the grade and density are also held by the FMS, and none is written as a literal;
- The port name and the supplier's name, address and telephone number written onto the BDN (`sim.md` §8.2), the fuel supplier name transcribed with a delivery ticket, the expected tanker registration used on fill-job creation, and a tug-engineer id written onto the BDN;
- sequential BDN and (where the operator originates them) record numbers;
- job-status and totaliser **poll interval**, **read from the compiled world constants** (`intervals.dock_operator_poll_s`, 30 s in operation), which `render.py` scales in a compressed scenario (*Compressed scenarios*).

The BDN's `start`/`end` totaliser fields record the transaction totaliser
delta the HMI exposes (`GET /api/jobs/current/dispenser/totaliser` →
`dispensed_litres`):
start `0`, end `dispensed_litres`. Absolute meter readings are not surfaced
by the HMI API.

**Interface:**

```rust
/// A tug refuelling job. Carried on a `TugArrival` from the tug arrival
/// generator to the tug engineer, which forwards it to the dock operator once
/// the tug before it has left.
pub struct DispenseJob {
    /// IMO number of the vessel.
    pub vessel_id: VesselId,
    /// Name of the vessel, written on the BDN alongside its IMO number.
    pub vessel_name: &'static str,
    /// Volume of fuel requested, drawn uniformly between the refuel volume
    /// bounds (Tug arrival generator).
    pub volume_L: f64,
    /// RFID card number for this vessel, copied from the vessel roster.
    /// Carried on the job so the tug engineer can present the correct
    /// card at the dispenser reader without a separate roster lookup.
    pub rfid_card: RfidCardNumber,
}

/// A tank fill job sent by the fuel supplier to the road tanker driver when it
/// dispatches a delivery.
pub struct FillJob {
    /// Volume of fuel the arriving tanker carries.
    pub delivery_volume_L: f64,
    /// Fired by the road tanker driver once the delivery has ended, however it
    /// ended: the full volume delivered, the delivery stopped part-way by the
    /// interlock circuit, or the pump never started. It ends the supplier's
    /// pending order. The supplier keeps polling meanwhile but places no new
    /// order until this fires (`sim.md` §5.3).
    pub completed: oneshot::Sender<()>,
}

/// Inbound job for the dock operator.
///
/// Both variants carry a oneshot sender that the dock operator fires once the
/// FMS has answered, allowing the counterpart actor to proceed with its physical
/// actions. A fill is always admitted, so its oneshot carries no value; a
/// dispense may be refused, so its oneshot carries the FMS's admission.
pub enum DockJob {
    Dispense(DispenseJob, oneshot::Sender<JobAdmission>),
    /// The delivery volume (litres) and the arriving tanker's registration.
    /// The driver keeps the supplier's completion oneshot and forwards only
    /// what the dock operator needs.
    Fill(f64, String, oneshot::Sender<()>),
}

/// Whether the FMS admitted a submitted dispense job. `Refused` means the tug
/// departs unserved: the engineer presents no vessel card and no BDN follows.
pub enum JobAdmission {
    Active,
    Refused,
}

/// The outcome of a dispense, handed from the dock operator to the tug engineer:
/// a signed BDN to co-sign, or notice that the FMS refused the dispense after the
/// job was already active, in which case the tug departs unserved.
pub enum DispenseHandoff {
    Sign(BDN),
    Refused,
}

/// Paper delivery ticket created by the road tanker driver on fill completion.
/// Fields per narrative §2.1. The driver signs and hands to the dock operator,
/// who counter-signs and transcribes the fields into the FMS via the HMI.
pub struct DeliveryTicket {
    pub ticket_number: String,
    /// Instant the fill began, from `clock.now_datetime()` at transfer start.
    /// Carried forward by the driver to artefact creation — **not** read at
    /// creation, which is completion time (`sim.md` §8.3).
    pub delivery_commenced_at: PrimitiveDateTime,
    pub grade: String,
    pub gross_volume_L: f64,
    /// Observed fuel temperature at the transfer, in °C.
    pub temperature_C: f64,
    /// `gross_volume_L` corrected to 15 °C (`sim.md` §8.4).
    pub temp_corrected_volume_L: f64,
    /// Fuel density at 15 °C. A property of the grade, not a measurement.
    pub density_kg_per_L: f64,
    /// Tanker meter totaliser reading at transfer start.
    pub start_totaliser_L: f64,
    /// Tanker meter totaliser reading at transfer end.
    pub end_totaliser_L: f64,
    pub tanker_registration: String,
    pub driver_id: String,
    pub signed_by_driver: bool,
    pub signed_by_operator: bool,
}

/// Bunker Delivery Note (BDN) created by the dock operator on dispense completion.
/// Fields per narrative §2.2 and the ten items of MARPOL Annex VI Appendix V
/// (`sim.md` §8.2; item numbers in brackets below). The bracketed numbers are
/// Appendix V's item numbers as amended by resolution MEPC.362(79), which added
/// the flashpoint as item 9; a field without one is not an Appendix V item.
/// `sim.md` §8.2 states the provenance. The dock operator signs and
/// passes to the tug engineer for co-signing; the co-signed BDN number is then
/// entered via the HMI.
pub struct BDN {
    pub bdn_number: String,
    /// Instant the dispense began: the FMS's `opened_at`, read from the HMI
    /// after completion (`narrative.md` §2.2, `sim.md` §8.3) — **not** the dock
    /// operator's own clock, and not the completion time. Equal to `opened_at`
    /// on the job's `transaction_open` and `transaction_closed` events. [3]
    pub delivery_commenced_at: PrimitiveDateTime,
    /// IMO number of the vessel, as validated by the FMS against the RFID card number. [1]
    pub vessel_id: VesselId,
    /// Name of the vessel. Appendix V requires both the name and the IMO number
    /// of the receiving ship. [1]
    pub vessel_name: &'static str,
    /// Port of delivery, `"Ravensport"`. [2]
    pub port: &'static str,
    /// The supplier is the port: the Ravensport Harbour Authority, whose
    /// representative is the dock operator (`sim.md` §8.2). Not the fuel
    /// supplier actor, which supplies the port. [4]
    pub supplier_name: &'static str,
    pub supplier_address: &'static str,
    pub supplier_telephone: &'static str,
    /// Product name, from the compiled world constant. [5]
    pub grade: String,
    pub volume_L: f64,
    /// Observed fuel temperature at the transfer, in °C. Not an Appendix V
    /// item; carried so the corrected volume is checkable against its inputs
    /// (`sim.md` §8.4).
    pub temperature_C: f64,
    /// `volume_L` corrected to 15 °C (`sim.md` §8.4). The audit corrects the
    /// meter side only — the ATG volumes on the log events are already
    /// compensated (`hist.md` §4.2).
    pub temp_corrected_volume_L: f64,
    /// `temp_corrected_volume_L` × density at 15 °C, in tonnes: the mass in
    /// vacuum. [6]
    pub quantity_t: f64,
    /// Fuel density at 15 °C, in kg/m³. A property of the grade, not a
    /// measurement. [7]
    pub density_kg_per_m3: f64,
    /// Sulphur content, % m/m, from the compiled world constant. [8]
    pub sulphur_pct_mm: f64,
    /// Flashpoint in °C, from the compiled world constant; a chosen value, not
    /// a measurement. [9]
    pub flashpoint_C: f64,
    /// The declaration that the fuel conforms with regulation 14.1, the global
    /// 0.50% m/m sulphur limit. [10]
    pub sulphur_limit_declared: bool,
    /// Dispenser meter totaliser reading at transfer start.
    pub start_totaliser_L: f64,
    /// Dispenser meter totaliser reading at transfer end.
    pub end_totaliser_L: f64,
    /// The dock operator's username, the `operator` on the job's log events.
    pub operator_id: String,
    pub engineer_id: String,
    /// The supplier's representative's signature on the declaration. [10]
    pub signed_by_operator: bool,
    pub signed_by_engineer: bool,
}

/// Spawn the dock operator actor.
///
/// All HTTP interactions use the HMI API defined in digital.md §2.3.
/// Session tokens are obtained at startup and reused for the duration of
/// the sim; `Authorization: Bearer <token>` is sent on every request.
///
/// **Order of jobs.** One job at a time, each completed before the next is
/// taken (narrative §2.2; `sim.md` §6). Before taking a job, the operator
/// moves every job already waiting on `job_rx` into two queues of its own,
/// one for fills and one for dispenses, then takes a waiting fill if there is
/// one, otherwise the oldest waiting dispense; with both queues empty it
/// awaits `job_rx`. A road tanker waiting to deliver is therefore handled
/// before any waiting tug, and tugs in the order in which they arrived. The
/// road tanker driver sends at most one fill at a time, and the tug engineer
/// one dispense at a time.
///
/// **Login sequence** (startup):
/// 1. `POST /api/auth/login` with username and password.
/// 2. On receipt of `challenge_id`, sends the operator card number via
///    `operator_card_tx` (delivering it to the HMI serial port).
/// 3. `GET /api/auth/challenge/{challenge_id}` (long-poll) — blocks until
///    the HMI reads the card from the serial port and the FMS validates it.
///    Stores the returned session token.
///
/// **Dispense job** (`DockJob::Dispense`):
/// 1. `POST /api/jobs/refuel` with the vessel's IMO number, grade, and requested volume.
/// 2. Fires the `oneshot::Sender<JobAdmission>` from `DockJob::Dispense`:
///    `Active` once the 201 response confirms the job is active in the FMS,
///    or `Refused` if the FMS refused the create, in which case no job
///    exists and the remaining steps are skipped.
/// 3. Polls `GET /api/jobs/current` until status is `rfid_validated`
///    (the tug engineer has presented the vessel RFID tag at the dispenser
///    reader and the FMS has validated it). The live job is the singleton
///    `current` job; the operator checks the returned `job_id` matches the
///    one created in step 1.
/// 4. `POST /api/jobs/{job_id}/dispenser/start` with the preset volume. The
///    actor does not read its own clock here: the commencement is the FMS's
///    (step 6).
/// 5. Polls `GET /api/jobs/current/dispenser/totaliser` until status is
///    `complete`.
/// 6. Once, after the dispense is complete and before the note is written:
///    `GET /api/jobs/current`, checking the returned `job_id` matches, reads
///    `opened_at`, the instant the FMS started the dispense; and
///    `GET /api/tanks/01/level` reads `temperature_c`. A `null` `opened_at`
///    on a completed dispense is a malformed response.
/// 7. Creates and signs a `BDN`, writing the `opened_at` read in step 6 into
///    `delivery_commenced_at` (`sim.md` §8.3); sends it as
///    `DispenseHandoff::Sign` via `bdn_sign_tx` for the tug engineer to
///    co-sign; receives the co-signed BDN on `bdn_signed_rx`.
///
/// **The BDN's measurement fields** (`sim.md` §8.4.1):
/// - `temperature_C` — the value read in step 6. The port has one temperature
///   sensor and the HMI is the operator's only route to it (`sim.md` §5.2), so
///   this is the same value the FMS writes onto the transaction row.
/// - `density_kg_per_m3` — the compiled world constant
///   (`process.density_kg_per_l`) × 1000, a property of the grade rather than
///   a measurement. Read from the constant, never written as a literal: the FMS
///   holds the same value as `FMS_DENSITY_KG_PER_L` and the two must agree.
/// - `temp_corrected_volume_L` — `volume_L` corrected to 15 °C. Equal to
///   `volume_L`: the fuel is held at 15 °C and the correction factor is 1
///   (`sim.md` §1.3).
/// - `quantity_t` — `temp_corrected_volume_L` × `process.density_kg_per_l`
///   ÷ 1000.
///
/// **The BDN's remaining Appendix V fields** are constants: the port and the
/// supplier's details are the dock operator's own; the grade, sulphur content
/// and flashpoint are compiled world constants; `sulphur_limit_declared` is
/// `true`, signed by the operator as the supplier's representative.
/// 8. `PATCH /api/jobs/{job_id}` with the BDN number.
///
/// If the FMS refuses the start (step 4), the flow ends there instead:
/// `DELETE /api/jobs/{job_id}` frees the job for a successor, and
/// `DispenseHandoff::Refused` stands the tug engineer down in place of the
/// BDN exchange of steps 7 and 8.
///
/// **Fill job** (`DockJob::Fill`):
/// 1. `GET /api/tanks/01/level` — checks there is sufficient ullage.
/// 2. `POST /api/jobs/tank-fill` with the arriving tanker's registration,
///    from `DockJob::Fill`, grade, and delivery volume. The registration is
///    the job's counterparty, so it is the one the driver prints on the
///    ticket (`narrative.md` §2.1): an operator constant would name a
///    different tanker from the one delivering.
/// 3. Fires the `oneshot::Sender<()>` from `DockJob::Fill` once the 201
///    response confirms the job is active in the FMS.
/// 4. Awaits `ticket_rx` for the delivery ticket from the road tanker driver.
/// 5. Counter-signs the ticket.
/// 6. `POST /api/delivery-tickets` to transcribe the ticket fields (`hmi.md`
///    §7.4): ticket number, `delivery_commenced_at`, delivered volume,
///    temperature, density, temperature-corrected volume, supplier and tanker
///    registration. `delivery_commenced_at` is written as RFC 3339 in UTC with
///    a `Z` designator — every sim date-time is UTC (`sim.md` §2.1) — and the
///    FMS records it as the fill's `opened_at` (`fms.md` §7).
pub fn spawn(
    clock: SimClock,

    /// Inbound job channel. `DockJob::Dispense` entries forwarded by the tug
    /// engineer; `DockJob::Fill` entries forwarded by the road tanker driver.
    /// Processed one at a time, a waiting fill before a waiting dispense
    /// (Order of jobs, above).
    job_rx: mpsc::Receiver<DockJob>,

    /// ESD button state. Set to `true` when the button is depressed,
    /// `false` when released. Consumed by the fuel tank's `inflow_active`
    /// and `outflow_active` derived quantities.
    esd_tx: watch::Sender<bool>,

    /// Card number events to the operator RFID reader. Sent once per
    /// login when the operator presents their RFID card at the HMI reader.
    operator_card_tx: mpsc::Sender<RfidCardNumber>,

    /// Delivery ticket from the road tanker driver, sent after the fill
    /// completes and the driver has signed it.
    ticket_rx: mpsc::Receiver<DeliveryTicket>,

    /// Channel on which the dock operator sends a completed, operator-signed
    /// BDN to the tug engineer for co-signing.
    bdn_sign_tx: mpsc::Sender<DispenseHandoff>,

    /// Channel on which the dock operator receives the BDN back from the tug
    /// engineer after co-signing. Safe as a single channel because dispense
    /// jobs are processed sequentially.
    bdn_signed_rx: mpsc::Receiver<BDN>,

    /// Channel to the reconciliation user. The dock operator posts a
    /// `ReconciliationRecord::Bdn` on each completed dispense and a
    /// `ReconciliationRecord::DeliveryTicket` on each completed fill.
    /// Backed by an unbounded channel; the reconciliation user only drains
    /// it once per audit period.
    recon_tx: mpsc::UnboundedSender<ReconciliationRecord>,

    /// The sim host's IP address on `vnet-ot`. All outbound HTTP connections
    /// to `hmi-01` originate from this address.
    local_addr: SocketAddr,

    /// HTTP address of the HMI workstation (`hmi-01`) on `vnet-ot`. All
    /// digital interactions — login, job entry, preset setting, BDN
    /// transcription — are sent here; the HMI communicates with the FMS
    /// on the operator's behalf.
    hmi_addr: SocketAddr,
)
```

---

### Road tanker driver

**Interactions:**
- Digital: none
- Physical: plug/unplug overfill cable (an `InterlockPlug` lease to/from the fuel tank); start/stop tanker pump (sets `tanker_pump_running`); hand over signed delivery ticket to dock operator
- Reads: the leased `InterlockCircuit` state from the fuel tank, which folds in both the overfill trip and the ESD — the single fill-cabinet contact the pump controller obeys
- Receives: `FillJob` from the fuel supplier; forwards its volume and the tanker's registration as `DockJob::Fill` (with a oneshot proceed channel) onto the dock operator's job channel, keeping the supplier's completion oneshot
- Signals: the fuel supplier on delivery completion, via the `FillJob`'s `completed` oneshot
- Spawned at sim startup; runs for the duration of the sim

**Interface:**

```rust
/// Spawn the road tanker driver actor, which also models the tanker's pump
/// controller.
///
/// Runs for the duration of the sim. On each iteration: receives a `FillJob`
/// from the fuel supplier, creates a oneshot channel, and forwards the delivery
/// volume and its own registration as
/// `DockJob::Fill(delivery_volume_L, tanker_registration, proceed_tx)` to the dock
/// operator (keeping the `FillJob`'s `completed` oneshot). Awaits the proceed
/// signal (fired by the dock operator once the HMI confirms the job is active
/// in the FMS).
///
/// Pump controller. On the proceed signal the driver plugs in — sending the
/// tank an `InterlockPlug` and holding the returned
/// `watch::Receiver<InterlockCircuit>`. The tanker pump runs only while that
/// circuit is `Live` (the tank is below the overfill interlock level and the ESD is not
/// pressed). If it is `Live`, the driver starts the pump and runs it until
/// *either* `delivery_volume_L` has been delivered *or* the circuit goes
/// `Dead`. It then stops the pump and unplugs (dropping the receiver).
///
/// Delivered volume. On normal completion the ticket records the ordered
/// `delivery_volume_L`. On a `Dead`-circuit cut the fill auto-stopped early, so
/// the ticket records the *actual* delivered volume — `pump_rate_L_per_min ×
/// elapsed_pump_minutes`, the tanker meter reading. If the circuit is already
/// `Dead` at plug-in (tank already at or above the overfill interlock level, or ESD pressed), the pump
/// never starts and nothing is delivered. The driver then generates and signs a
/// `DeliveryTicket`, sends it to the dock operator via `ticket_tx`, and fires
/// `completed`, ending the supplier's pending order.
///
/// Commencement. The driver records `clock.now_datetime()` when the pump starts
/// and writes that instant into `delivery_commenced_at` at ticket creation,
/// rather than reading the clock again there (`sim.md` §8.3). Where the pump
/// never starts, the instant of the attempted start is recorded, so the
/// artefact still falls in a window.
///
/// **The ticket's measurement fields** are the road tanker's own, taken by its
/// sealed meter at the tanker's temperature rather than the shore tank's
/// (`sim.md` §8.4.1). That independence is what gives the ±1% check force on a
/// fill, so these values must not be derived from the tank the fuel is being
/// delivered into. The tanker's product temperature is the driver's own
/// constant, 15 °C (`sim.md` §8.4.1).
pub fn spawn(
    clock: SimClock,

    /// Rate at which the tanker's onboard pump delivers fuel, in litres per
    /// minute. Used to compute `clock.sleep(delivery_volume_L /
    /// pump_rate_L_per_min)` — the duration the pump runs before the
    /// driver stops it and generates the delivery ticket. The tanker pump is
    /// the tank's only inflow source, so this is the sim's configured
    /// `inflow_rate_L_per_min`, sourced from that single `Config` field so the
    /// physical model and the driver's elapsed-time calculation are consistent
    /// by construction.
    pump_rate_L_per_min: f64,

    /// Identity of this driver, written onto every `DeliveryTicket` generated
    /// during the sim run. Mirrors the driver ID printed on a real tanker
    /// delivery docket.
    driver_id: String,

    /// Road registration of the tanker, written onto every `DeliveryTicket`.
    tanker_registration: String,

    /// Inbound jobs from the fuel supplier. One per delivery dispatch.
    arrival_rx: mpsc::Receiver<FillJob>,

    /// Dock operator's inbound job channel. Each job is forwarded as
    /// `DockJob::Fill(delivery_volume_L, tanker_registration, proceed_tx)`, where `proceed_tx` is a oneshot
    /// sender the dock operator fires once the HMI confirms the job is
    /// active in the FMS.
    dock_job_tx: mpsc::Sender<DockJob>,

    /// Tanker pump running state. Set to `true` only while the pump controller
    /// holds the circuit healthy; set `false` on completion or an interlock/ESD
    /// cut. Consumed by the fuel tank as `inflow_active`.
    tanker_pump_tx: watch::Sender<bool>,

    /// Overfill interlock plug-in channel to the fuel tank. On plugging in the
    /// driver sends an `InterlockPlug` and holds the returned
    /// `watch::Receiver<InterlockCircuit>` for the fill; the tank drives it
    /// `Dead` on the overfill trip or ESD. Dropping the receiver unplugs.
    interlock_plug_tx: mpsc::Sender<InterlockPlug>,

    /// Delivery ticket sent to the dock operator after the fill completes
    /// and the driver has signed it.
    ticket_tx: mpsc::Sender<DeliveryTicket>,
)
```

---

### Tug engineer

**Interactions:**
- Digital: none
- Physical: present vessel RFID card at dispenser reader; sign paper BDN
- Receives: `TugArrival` from the tug arrival generator, in arrival order; forwards its `DispenseJob` as `DockJob::Dispense` (with a oneshot admission channel) onto the dock operator's job channel once the tug before it has left
- Signals: the tug arrival generator, via the arrival's `departed` oneshot, once the tug has left the dock
- Spawned at sim startup; runs for the duration of the sim

One actor acts for the engineer of whichever tug is being served, one tug at a
time. Tugs that arrive while another is served wait in the arrival channel,
which delivers them in the order they arrived (`sim.md` §6). Because a tug's
job is forwarded only once the tug before it has left, the dock operator holds
at most one tug job, and a road tanker waiting at that point is handled first
(Dock operator).

**Interface:**

```rust
/// Spawn the tug engineer actor.
///
/// Runs for the duration of the sim, serving one tug at a time. On each
/// iteration: receives the next `TugArrival`, keeps its `departed` sender, and
/// forwards the job as `DockJob::Dispense(job, admission_tx)` to the dock
/// operator. Awaits the admission, which the dock operator sends once the HMI
/// has answered the create:
///
/// - `JobAdmission::Refused`: the tug departs unserved; no card is presented.
/// - `JobAdmission::Active`: sends the vessel card number to the RFID reader,
///   then blocks on `bdn_rx` for the dispense's outcome. On
///   `DispenseHandoff::Sign(bdn)` it co-signs the BDN and returns it via
///   `bdn_tx`; on `DispenseHandoff::Refused` the tug departs unserved.
///
/// In every case the tug then leaves the dock: the actor fires `departed`
/// before taking the next arrival. A `departed` receiver already dropped is
/// fatal, since the tug arrival generator has stopped.
pub fn spawn(
    /// Inbound arrivals from the tug arrival generator, one per tug arrival,
    /// in arrival order.
    job_rx: mpsc::Receiver<TugArrival>,

    /// Dock operator's inbound job channel. Each job is forwarded as
    /// `DockJob::Dispense(job, admission_tx)`, where `admission_tx` is a
    /// oneshot sender the dock operator fires with the FMS's admission once
    /// the HMI has answered the create.
    dock_job_tx: mpsc::Sender<DockJob>,

    /// Card number events to the vessel RFID reader. Sent once per job,
    /// after the proceed signal is received, to trigger RFID validation
    /// at the dispenser.
    card_tx: mpsc::Sender<RfidCardNumber>,

    /// Channel on which the dock operator sends the outcome of a dispense:
    /// the operator-signed BDN for the tug engineer to co-sign, or notice
    /// that the FMS refused the dispense after the job was already active.
    bdn_rx: mpsc::Receiver<DispenseHandoff>,

    /// Channel on which the tug engineer returns the co-signed BDN to
    /// the dock operator.
    bdn_tx: mpsc::Sender<BDN>,
)
```

---

### Reconciliation user

> **Status: built, and proven on the deployed system for a clean audit.** With a
> day period, deployed runs have completed audits against `recon-01` and
> `hist-dmz-01`, including one covering a fill, each recording an `agreed` entry
> (`test_08_audit.py`). A retrieval split across several spans, the failure
> handling, and findings on deployed records are proven in-module only.

**Interactions:**
- Digital: a remote desktop session from the sim host's `vnet-it` interface into `recon-01`, authenticated with the workstation credential; within the session, a terminal opened by keyboard binding, a `curl` retrieval against Grafana on `hist-dmz-01` through its datasource proxy, authenticated with the separate log-server credential, and clipboard reads returning the result
- Physical: none
- Receives: completed `BDN` and `DeliveryTicket` records from the dock operator over an unbounded channel; accumulates them between audits and drains on each cycle

**Notes:**
- The sim host requires a `vnet-it` interface (`digital.md §10`). The remote desktop session is the only channel by which information travels between `recon-01` and this actor.
- **The client stack is IronRDP, asynchronous**, the stack `tests/integration/recon-probe` uses: `ironrdp` 0.16 (connector, session, input and clipboard), `ironrdp-tokio` 0.9 for the session loop on the sim's Tokio runtime, and `ironrdp-cliprdr` 0.6 for the clipboard virtual channel. That probe drives a real session against a built `recon-01`, and the clipboard ceiling `recon.md §11.2` records was measured through it, so its versions are the ones whose compatibility with the workstation's remote-desktop service is established. The audit path uses no pointer (`recon.md §5.4`).
- **The TLS upgrade is the actor's own**, with `tokio-rustls` and the `ring` crypto provider, not `ironrdp-tls`, whose upgrade verifies nothing: it accepts any certificate and returns its public key. The probe uses it, and the actor does not (see "Connecting").
- **The sim gains TLS.** It no longer speaks plain HTTP only: the remote desktop connection is TLS, terminated by the workstation's remote-desktop service, with HAProxy passing it through in TCP mode (`sim-network.md`). The configuration names `ring`'s crypto provider itself, so nothing depends on a provider installed for the whole process. `ring` compiles C, so building the sim for the musl target needs a C compiler targeting musl.
- **Memory and `tracing` only.** The actor writes nothing to disk, in parity with the dock operator and satisfying `sim.md §5.4`; its artefacts reach the checkpoint through its report to the orchestrator at a stop (see *Stopping and warm start*). A month's accumulated artefacts and retrieved text are a small number of megabytes, so memory is not a binding constraint.

---

#### Cadence and window

The audit covers one **audit period**, set by the port (`narrative.md` §3):
a calendar month, a week or a day, each with fixed boundaries, or in a compressed scenario a fixed interval measured in hours (*A period in hours*, below). All are UTC. A
day runs from 00:00 to the end of that day, including the whole of 23:59:59; the
next begins at 00:00. A week (the ISO week) runs likewise from 00:00 on Monday to
the end of Sunday, and a month from 00:00 on its first day to the end of its last.
One mechanism serves every calendar period: the period gives the
window containing an instant and the window after it, and the audit of a window
runs `settling_delay` after the window ends. Both bounds are computed from
`clock.now_datetime()` and passed explicitly in the query string; no relative
expression is used, because the workstation's clock would then decide which
records are returned (`recon.md §9`).

**A settling delay into the following period, to allow the FMS's records to arrive.** `narrative.md §5` "Temporal & procedural" condition 4 has the FMS write its own record first and forward a copy afterwards, retrying with increasing delay.
A transfer closing near the end of the period may not reach `hist-01`, or the replica, until after the period ends, and the `bdn_linked` event is later still because it waits on the operator transcribing the number.
An audit running at the instant the period ends could find records still incomplete or absent, every period, at the same boundary.

**The settling delay lies between the margin and the period:** `boundary_margin ≤ settling_delay` and `settling_delay` shorter than the shortest period of its kind (28 days for a month, 7 for a week, 1 for a day, and the interval itself for a period in hours). The upper bound keeps the audits in step: the actor audits one window at a time, so a delay of one period or more would start each audit after the following window had already ended, and the audits would fall progressively behind. The lower bound is the settling delay's own purpose: a transfer commencing just before the window ends must have completed, and its paper artefact been posted, before the audit drains. The margin is already bounded by the longest credible transfer, so it is the lower bound. With both bounds, every artefact drained by an audit is dated within its window or after it; one dated before the window is an invariant violation, and fatal. A delay outside the bounds is refused: at validation of the configuration, and by `spawn` as a startup fault.

**On a cold start, the first window starts no earlier than the sim.** The paper artefacts are held in memory only, and without a checkpoint those of earlier windows do not exist. The log-server's records persist when the sim restarts. A window reaching back before the sim started would find records whose paper artefacts no longer exist, and a record with no paper is fatal on clean data. The first window therefore runs from the later of its period's start and the instant the actor was spawned. A restart is the prescribed remedy after the host has slept (`sim.md` §2.1, operating modes), and a restart after a crash is a cold start too. A cold start is therefore an ordinary case, not an edge case; a day period meets it within hours.

**On a warm start, the first window is the saved window** (`sim.md` §9.3, warm start), with the artefacts held for it, so the audit covers the whole window across the stop.

**A period in hours.** In a compressed scenario `render.py` projects the audit period as a number of hours (`deployment-config.md` §4, projection targets), and windows of that length follow one another without a gap. On a cold start, the first audit comes `first_audit_after` the actor's spawn (*Start state*), and each later audit runs one period after the one before. Each audit covers the window that ends `settling_delay` before it, with bounds computed from `clock.now_datetime()` and passed explicitly, as for a calendar period. On a cold start, the first window starts no earlier than the sim (above), and `spawn` refuses, as a startup fault, a first window that would end at or before the spawn.

#### Accumulation and partitioning

Artefacts arrive continuously and are drained once per cycle. Each is assigned to a window by **its own `delivery_commenced_at`**, not by the cycle that drained it (`sim.md §8.3`). 
An artefact dated in a later window is held for that window rather than counted as unmatched.
An artefact dated before the window being audited cannot occur: the settling delay is at least the longest credible transfer (above), so every artefact of an earlier window was drained by that window's audit. One that does is an invariant violation, and fatal.

This is an additional defence against reporting a discrepancy on correct data at a period boundary, behind the settling delay above.

#### Session

One audit is one session. Each step originates as a property of the workstation
rather than of this actor's design, and `recon.md §8` records why.

1. **Connect** over the remote desktop protocol from `local_addr` to `recon_addr`, verifying the workstation's certificate before the workstation credential is sent (below).
2. **Open a terminal** with the `Ctrl+Alt+T` binding (`recon.md §5.4`), then wait `terminal_settle` before typing: the actor has no reading of the screen, so it cannot see the terminal appear, and the confirmation that follows is what establishes that it did. Never reuse a terminal a previous audit may have left: the workstation tears no session down, so what a previous audit left is unbounded and its closure cannot be confirmed.
3. **Confirm the session is live** before any credential is typed (below).
4. **Retrieve.**
5. **Record the summary entry** (see "Reporting").
6. **Close the terminal** by typing `exit`, then disconnect.

**Connecting.** The client negotiates TLS without the Credential Security Support Provider, which is what the workstation offers (`recon.md §4`), and advertises the United Kingdom keyboard layout, `0x0809`, and the client name `audit-client`, which the workstation logs with each connection. The TLS upgrade verifies the certificate the workstation presents against the deployment's root, as every other client of an internal service does (`digital.md §2.7`), and against the name `recon-01`, which the client gives to TLS itself. The address dialled is HAProxy's loopback port and names nothing, and the leg is passed through in TCP mode, so the name cannot be taken from the connection and the certificate need carry no address. A certificate that does not verify ends the session before the workstation credential is sent, and is not retried, on the ground the retrieval's own `--insecure` refusal rests on (below). The workstation credential is then sent in the client's logon information, inside the verified session.

**The session loop** runs the IronRDP session on the sim's runtime: it reads each frame, offers every chunk on the clipboard channel to the reassembly guard first (see "Reading the clipboard"), and passes the frame to IronRDP only if the guard accepts it. A refused chunk ends the session with the guard's failure: a message partly withheld from IronRDP cannot be resumed. The loop answers the workstation's request for the client's formats during setup with an empty list — the client never holds a selection — and sends the format requests a read makes. It decodes screen updates, because the session requires it, and reads nothing from them.

**Keyboard.** Input is injected as PS/2 Set 1 scancodes, not characters. Three
things must agree: the layout pinned in the session (`setxkbmap gb`), the actor's
scancode table, and the layout identifier the client advertises at connect. The
table must carry `0x56`, the extra key beside the left `Shift` that `gb` gives to
`|`, because the audit command pipes into the clipboard tool and cannot be
composed without it. A layout mismatch presents as a malformed query returning an
error from `hist-dmz-01`, not as a rejected credential (`recon-tests.md §1`).

The table covers every printable ASCII character, not only those the audit's commands use today, so that a change to a command needs no change to the table. A line is checked against the table before any of it is typed: a character the table lacks refuses the whole line, since a character typed wrongly or not at all is the failure the table exists to prevent. Each character is a key press and release, with `Shift` held around it where the layout requires; a line ends with `Enter`. Keys are separated by `key_interval`, slept on the sim clock — a terminal drops characters typed faster than it reads them, and 30 ms is the interval the probe proved.

**Every typed command is built in one place and tested as the exact line typed.** A change to a command — a wrapper around the retrieval, for instance — is then a change to one function and one test.

**Confirming the session: the latch value** is `latch-` followed by the window's start as fourteen digits and the attempt number within the audit — `latch-20250101000000-1` — so it differs between audits and between the attempts of one. The command is `echo <value> | xclip -selection clipboard`.

**Credentials.** Both are read from the compiled world constants, as the dock operator's are: `identities.reconciliation_user.workstation_password` for the workstation login and `.password` for the Grafana account (`deployment-config.md §3`, `deployment-config.md §6`). 
They are distinct values for distinct systems (`recon.md §7`).

#### Confirming the session

The actor receives a framebuffer it has no text reading of, and input injection
carries no acknowledgement of what consumed it. **The clipboard is the only thing
that returns**, so anything not confirmed through it is assumed rather than known.

Before the first command carrying the log-server credential:

```
clear the latch  →  type a command echoing a unique per-audit value to xclip  →  read back
```

A match establishes four things at once: 
  - The window manager delivered the keystrokes to a shell
  - The shell is executing commands rather than a dialogue absorbing them
  - The clipboard channel is live in *this* session rather than just initialised 
  - The latch discipline below works

No match, or nothing, is a **session-state failure** — reported as one, and never as an audit that found no records. This matters because the log-server password is typed on the command line: if focus is not where the actor believes it is, the credential goes somewhere else on the host.

#### Retrieval
Two distinct log records are within scope: 
The event store holds the bunkering events of `hist.md §4.2`, which the FMS pushes as JSON lines, and the authentication and card-table events of `hist.md §4.3`, which it sends as syslog over a separate connection. 

Both arrive in the same store and both replicate to `hist-dmz-01`; the audit retrieves both (`digital.md §4.5`).

The metric store is provisioned as a datasource on the replica and is not read:
the tank volumes each transfer opened and closed at are contained within the bunkering events themselves.

**A bunkering event references its login** The `session_id` on `hist.md §4.2`'s envelope matches the `session_id` carried on a `LOGIN` record of `hist.md §4.3`. The bunkering event holds no username, role, source address or result of its own.

The `LOGIN` record and the bunkering event are written by separate code paths, over separate connections, at different times: the first by the FMS's authentication handling as syslog (`fms.md §4`), the second by its log-server pusher as a JSON line (`fms.md §5.4`), onto the separate listeners of the log-server `hist.md §5`, which fail independently of one another (`fms.md §5.4`). 
The check depends on that separation. 

**Filtering is sensitive to absent fields.** A filter naming a field a bunkering event carries and the logging event doesn't returns one and drops the other without an error. 

The same applies to the join: 
`session_id` is unprefixed on the bunkering events and `fms@99999.session_id` on the syslog records, because `hist.md §4.3` stores structured-data parameters under their identifier. 
A correlation addressing one name alone matches nothing from the other kind and reports a clean month.

**Command form.** One line, with `-G` and `--data-urlencode` so that the client, not the actor, encodes the expression:

```
bash -c '{ curl -sS --fail-with-body -u <user>:<pass> -G https://hist-dmz-01/api/datasources/proxy/uid/monthly-audit-logs/select/logsql/query --data-urlencode "query=_time:[<span start>, <span end>)" 2>&1; echo "retrieval-end $?"; } | xclip -selection clipboard'
```

For the span from 18:00 on 31 December 2024 to 00:00 on 7 January 2025:

```
bash -c '{ curl -sS --fail-with-body -u <user>:<pass> -G https://hist-dmz-01/api/datasources/proxy/uid/monthly-audit-logs/select/logsql/query --data-urlencode "query=_time:[2024-12-31T18:00:00Z, 2025-01-07T00:00:00Z)" 2>&1; echo "retrieval-end $?"; } | xclip -selection clipboard'
```

Each part is there for a reason:

- **Standard error is folded into the pipe.** It is what distinguishes a failed retrieval from a span with no records (`recon.md §6.1`).
- **`--fail-with-body` makes an HTTP error a `curl` failure.** Without it, `curl` exits successfully on a refused login or a proxy error and prints Grafana's error body, a JSON object such as `{"message":"invalid username or password"}`. That parses as a record and is set aside with the authentication events, so a refused login would read as an empty span. With it, the body is accompanied by `curl: (22) The requested URL returned error: 401` and the exit status is 22.
- **The completion line** `retrieval-end <status>` carries `curl`'s exit status and ends every response. A response without it did not finish. It also means a span with no records still places a line on the clipboard, rather than an empty selection.
- **The bounds are in the expression**, as LogsQL's half-open range: the start included and the end excluded, as for the audit window. Consecutive spans therefore neither overlap nor leave a gap, without depending on whether the `start` and `end` parameters include their end. Each bound is RFC 3339 in UTC with a `Z` designator, with a fractional second only where the instant has one; the first window of a run starts at the spawn instant, which can.
- **There is no filter.** The replica's event store holds the FMS's records alone (`hist.md §4`), and a filter naming fields drops a class of record without an error (above). Records without an `event_type` are set aside before grouping (below), and the checks select from them what they read.

The `bash -c` wrapper keeps the complete retrieval, including its credential, in the wrapper's argument vector for the request's lifetime; `curl` itself scrubs the value of `-u`. This is the live `/proc` residue described in `recon.md §7.2`, in addition to the durable shell-history residue. Every character of the line is printable ASCII and so on the scancode table; the credentials are held to that, and to no single quote, at validation (`deployment-config.md §5`).

| Decision | Value | Reason |
|---|---|---|
| Format | Newline-delimited JSON, the default | Self-describing per line, so a `curl` diagnostic folded in by `2>&1` is unambiguously not a record, and one malformed line is reportable rather than silently shifting every subsequent field |
| Projection | None — whole records | The actor sees what is on the replica, including fields no check reads but a human auditor would notice |
| Ordering | Sorted locally after reassembly | Results are unsorted by default; ordering server-side disables streaming and carries a known memory cost over wide ranges. The actor holds the window anyway |
| `limit` | Not set | `limit=N` returns the N most recent matches and silently drops the oldest. Server-side truncation then cannot occur at all |

**Certificate verification is never disabled.** `--insecure` is rejected on the
same ground as clicking through a browser interstitial, and a verification
failure is not retried (`recon.md §6.4`).

**Reading a response.** The last line must be the completion line; without it the response is incomplete. A status of zero makes every line before it a record, each of which must be a JSON object; none at all is a span with no records. Any other status is a retrieval failure, classified by the status and, for an HTTP error, by the code in `curl`'s diagnostic, `curl: (22) The requested URL returned error: <code>`. The diagnostic is found on any line before the completion line, and wherever it starts in that line. Standard error and standard output share the pipe in the order `curl` writes them: measured against the deployed Grafana, the diagnostic arrives first and the error body after it, but nothing fixes that order, and a body without a final line feed written first would run into the diagnostic on one line.

| Status | Meaning | Failure |
|---|---|---|
| 6, 7, 28, 52, 55, 56 | The name did not resolve, the connection failed or timed out, or the server sent nothing or broke off before responding | Unreachable |
| 22, with HTTP 401 or 403 | Grafana refused the credential | Authorisation refused |
| 22, with HTTP 5xx | Grafana answered and the store behind its proxy did not | Unreachable |
| 35, 51, 58, 60, 77, 83, 90, 91 | The TLS handshake failed or the certificate did not verify | Certificate verification, not retried |
| 18 | The transfer ended before the response did | Incomplete |
| Any other, or 22 with any other code | Including a query the server refused, which a mistyped character produces | Malformed response |

**Records lost in retrieval are detected by the response's shape, not by counting.** A count of the events the drained artefacts imply cannot separate records lost in retrieval from records the store never received, which the narrative permits (`narrative.md §5`, Temporal 4): a log-server outage removes exactly the events a count would expect. The completion line, the status and the requirement that every record line parses are what establish that a response arrived whole; the clipboard's deadline and size bound cover what does not arrive at all.

#### Windows, margins and grouping

**A window is assigned to transactions, not to events.** A transfer's four events are emitted at four different instants, so a retrieval bounded on event timestamps alone could split a transfer spanning a period boundary: `job_opened` and `transaction_open` would fall in one window and `transaction_closed` and `bdn_linked` in the next. 

Two rules, both properties of the actor rather than of the query, to mitigate the risk:

1. **The retrieval takes a margin either side of the window**, wide enough that a transfer beginning just inside the window is retrieved whole. The margin is bounded by the longest credible transfer, not by the settling delay.
2. **Events are grouped by `job_id` into transactions before any window test**, and each transaction is assigned by its `opened_at` — the same instant the paper artefact carries as `delivery_commenced_at`. Transactions outside the audited window are discarded after grouping.

The margin widens what is retrieved, not what is audited.

**The retrieval reaches back one session lifetime further.** A transaction's `LOGIN` precedes its job's creation by as much as one FMS session lifetime, `intervals.session_timeout_s` in `world.yaml`, which runs from login and is not extended by use (`fms.md §4`). A job created early in the window can therefore belong to a session that began in the previous window. The boundary margin is bounded by the longest transfer, not by a session, so the retrieval's lower bound is the window start less the margin and less the session lifetime. Without it, the audit reports clean sessions as missing their `LOGIN`. The lifetime is the compiled world constant, not a literal: the FMS reads the same value as `FMS_SESSION_TIMEOUT`.

**Records are held as retrieved.** Each line is parsed as a JSON object and held whole. The authentication and card-table records carry no `event_type` and no `job_id`, so they are set aside by `event_type` before grouping, for the checks that read them; grouped, every one would be unassignable. A line that is not one is a malformed response, a retrieval failure. A field that is absent or unreadable is a finding about that record, reported by the check that reads it, so fields are read through accessors that return the value or the reason it cannot be read, converting from text, as which the store returns every value. A typed structure per event type would instead reject the whole line.

**The instant that assigns a transaction.** Grouping produces one group per `job_id`, and each group is assigned to a window by the first readable of:

1. `opened_at` on the group's `transaction_closed`: the instant `sim.md §8.3` names, carried by dispenses and fills alike;
2. `opened_at` on its `transaction_open`: a dispense that began and has no close;
3. for a group of `job_opened` events, alone or with a `job_cancelled`, `created_at`: a job that never began a transfer, such as a dispense the FMS refused, and which the dock operator then cancelled if the refusal came at its start (`hist.md` §4.2). It is assigned so that it is reported once, as context. It is not a transfer, it has no paper artefact, and no check runs on it. A `job_cancelled` without its `job_opened` has no instant, and is unassignable.

`created_at` never assigns a group that began a transfer. A job is created before its transfer starts, by as long as the dock operator and tug engineer take to clear the RFID gate, so a transfer assigned by it could fall in a different window from its paper.

On a dispense the close repeats the open's `opened_at`, so the two sources agree on a correct run. Where they differ, the close's is used and the difference is a discrepancy for the checks: assignment decides which audit examines a record, never whether the record is correct. Should a group hold more than one close, the earliest is used.

**A record that cannot be assigned** is a finding, reported and never fatal: an event without a readable `job_id`, or a group with none of the instants above, such as a `bdn_linked` whose other events did not arrive. It is reported by the audit whose window contains its earliest `_time`, so that it is reported once; one with no readable `_time` cannot be placed, and is reported by every audit that retrieves it rather than by none.

#### Splitting the window

At a high arrival rate, a month is not expected to fit into one retrieval; a week or a day may. What is split is the retrieval's bounds — the window widened by the margins and the session lifetime — into consecutive spans, each from the previous one's end, the last ending at the bounds' end. The split is adaptive:

- Start at `initial_window`, derived from the arrival rate (below), or at the whole of the bounds if they are narrower. At a high arrival rate a whole-month attempt is known in advance to fail, so the initial width is sized to the clipboard to minimise unnecessary and predictable retries.
- On a read that reaches its deadline, halve the width and retry the same span from its start.
- **Floor.** Halving never takes the width below `width_floor`, derived from the arrival rate (below). A span at the floor that still reaches its deadline exhausts the clipboard budget, which gives the whole-session restart once and is fatal thereafter (see "Failure handling"). The floor's expected records fill a sixteenth of the ceiling, so no legitimate span at the floor approaches it. Where a period's bounds are narrower than the floor, as a day's are at the roster's default rate, the retrieval is one span, narrower than the floor. A deadline on that span exhausts the budget at once. Wider bounds can be split, into spans at the floor and the remainder.
- **Hold the working width** for the rest of the window once one succeeds, rather than re-widening speculatively.

Each sub-retrieval is separately latched — the latch is cleared before each command, not once per audit — separately bounded by its span and separately confirmed by its completion line. Any other failure, and a deadline on a span at the floor, stops the retrieval at that span with the failure, for the budgets under "Failure handling"; the retrieval keeps the spans already retrieved and the width it holds, so that a retry, or the session that replaces this one, continues from the span that failed. The actor assembles the window from the spans' records and sorts them by `_time`, a record with no readable `_time` after every one with, in the order received; it counts every read it issued as a retrieval used.

**The widths follow from a measured size of records.** The capacity figure in `recon.md` §11.2 (obligations on the actor) counts four bunkering events per arrival and predates both the fields added to those events and the authentication and card-table records now retrieved alongside them. Measured on the deployed system, a day's records at one arrival an hour, `LOGIN` records included, are 62.6 KB, about 2.6 KB for each arrival. The actor holds that size as `RECORD_BYTES_PER_ARRIVAL`, and multiplies it by the dock's arrival rate for the expected size of an hour's records. The dock's arrival rate is the sum over the roster of one over each tug's `mean_time_away` (*Tug arrival generator*). `initial_window` is the time whose expected records fill half the clipboard's ceiling, and `width_floor` the time whose expected records fill a sixteenth of it. At one arrival an hour, `initial_window` comes to about 8.4 days and `width_floor` to about 1 day. At the roster's default rate of 0.15 arrivals an hour, the two come to about 56 days and 7 days, and at a compression factor of 5 to about 11.2 days and 1.4 days. `initial_window` and `width_floor` follow the arrival rate, so a compressed scenario scales both through the tugs' scaled times away (*Compressed scenarios*).

#### Reading the clipboard

Four obligations, stated in `recon.md §11.2` and belonging to this actor:

1. **Every read is bounded in time**, and a read that does not complete within its deadline resolves to a typed error. Past the ceiling the workstation neither delivers nor refuses, so the deadline is what ends an oversized read.
2. **The actor refuses a transfer that declares or exceeds `clipboard_ceiling_bytes`, before any of it is accumulated.** The figure is the workstation's own measured ceiling, 1,048,575 bytes (`recon.md §11.2`), not a limit this actor imposes: a larger selection never arrives at all. A larger *declared* length is therefore either a fault or a compromised workstation. It is the actor's own constant rather than a projected one — only the sim reads it, and `deployment-config.md §2` keeps a single-consumer constant with its component — and it must be changed together with the figure in `recon.md §11.2`, where the measurement is recorded.
3. **Only text formats are accepted**; file-descriptor formats are refused as a property of the actor rather than of what the far end advertises.
4. **Each read clears the latch before the copy it waits for**, and requires a fresh announcement. Without this a read returns the previous audit's records and reports success.

**A read, in order.** Clear the latch; the command that causes the copy is typed only after it is cleared. Wait for a fresh announcement of the selection's formats. Refuse it if any advertised format is a file-descriptor format, and if Unicode text (format 13, `CF_UNICODETEXT`) is not among them. Request Unicode text and nothing else: the workstation's service always offers it, and the single-byte text format does not raise the ceiling, because the ceiling applies before that conversion (`recon.md §11.2`). Receive the data, or the workstation's failure response. Data arriving without a request made in this read is content from before the read, and is refused. The client advertises no file-transfer capability when the channel is set up.

**The deadline** is the configured `clipboard_deadline`, slept on the sim clock, as the retry backoffs are. A read that has not completed when it expires is abandoned with `DeadlineReached`.

**Where the bound is enforced: in the actor's session loop, before IronRDP reassembles.** The clipboard channel's data arrives in chunks, the first of which carries the total length of the message it begins. IronRDP reassembles the chunks internally, into a buffer with no limit, discards that declared length, and hands the clipboard backend only the finished message; a check in the backend would come after the cost the bound exists to avoid. The session loop therefore reads the header of each chunk on the clipboard channel before passing the frame to IronRDP. A first chunk declaring more than the channel bound is refused before any of the message is accumulated, and a message whose chunks carry more than they declared is refused as soon as they do.

**The channel bound** is stated in the channel's terms, because the declared length counts the clipboard message rather than the selection. A selection of `clipboard_ceiling_bytes` UTF-8 bytes is sent as UTF-16 with a terminating null unit, inside an 8-byte clipboard header. Each UTF-8 byte becomes at most two bytes of UTF-16, the most for ASCII text, so the largest message a deliverable selection produces is

```
8 + 2 × clipboard_ceiling_bytes + 2  =  2,097,160 bytes
```

A declaration above it cannot come from a selection the workstation would deliver.

**The text** is decoded as UTF-16, little-endian, up to the terminating null unit, with line endings as they arrived: the workstation's line feeds arrive unconverted (`recon.md §11.2`). A payload of an odd number of bytes, or one that is not valid UTF-16, is a malformed response.

#### Pairing

Each artefact is paired with a transaction before any check runs, by the document number the FMS records against it (`narrative.md §5`, Temporal & procedural 1):

- a BDN with the dispense whose `bdn_linked.bdn_number` is its number;
- a delivery ticket with the fill whose `transaction_closed.ticket_number` is its number.

**A dispense whose link did not arrive** is paired by its content. `bdn_linked` is the one event that carries the BDN number, and it is pushed separately from the close, so it can be lost while the close arrives. A dispense with a `transaction_closed` and no `bdn_linked` is paired with an unpaired BDN whose vessel and commencement equal the close's `counterparty` and `opened_at`, and whose volume is within ±0.5% of the close's totaliser delta. The commencement is the FMS's own instant, copied to the nanosecond (`sim.md §8.3`), so at most one BDN can match. A match is a lost link, reported; the paired BDN and transaction are then checked as any other pair. No match is a record with no paper.

**Only a completed transfer requires paper.** The dock operator writes a BDN once the dispense completes, so a dispense with a `transaction_open`, no close and no link has no paper by design. It is reported as an open with no close, never as a record with no paper.

What remains after pairing is a finding: an artefact with no transaction is a paper artefact with no log record; a completed transaction with no artefact, or a link naming a document that was not drained, is a log record with no paper.

#### Checks

Each check runs over the paired transactions of the window. The grounding column names the condition the check exists to uphold: `narrative.md §5` where the narrative states one, and `hist.md §4.2` for the two reconciliations the narrative does not name but the event schema provides for. The outcome column applies the rule under Outcome; a check whose field is absent or unreadable reports that as a finding about the record.

| Check | Grounding | Outcome | Notes |
|---|---|---|---|
| Paper agrees with the record | Counterparty identity matches the authorisation | Fatal on any difference | Compared exactly, since each value is a copy, never a second measurement: vessel ↔ `counterparty`, grade, operator ↔ `operator`, commencement ↔ `opened_at`; on a BDN also temperature, density and corrected volume; on a delivery ticket every transcribed field. A value written as JSON and read back as text is the same value |
| BDN volume within ±0.5% of the totaliser delta | Measurement integrity 1 | Fatal outside the tolerance | The delta is (`end_totaliser_pulses` − `start_totaliser_pulses`) ÷ K-factor, read from the compiled world constant rather than written as a literal |
| ATG delta consistent with the meter within ±1%, temperature-corrected | Measurement integrity 2 | Reported | On fills and dispenses both. The ATG volumes are already compensated (`hist.md §4.2`), so the correction is applied to the meter side only; applying it to both adds the factor twice. Reported because the close reading may be the FMS's cached one (`fms.md §8.4`) and the probe carries noise, both benign causes |
| Preset against delivered volume, within ±0.5% | `hist.md §4.2` | Reported | `preset_volume_l` is carried on `transaction_open` and repeated on the dispense `transaction_closed`. The dispenser stops at the preset rounded to a whole pulse, plus an overshoot, and the recorded preset is held at the dispenser register's 0.1 L resolution (`fms.md` §7.4, jobs) |
| A `transaction_open` with no matching close | `hist.md §4.2` | Reported | The record of a dispense that began and did not complete; not recoverable from the other events |
| The totaliser does not decrease | Measurement integrity 4 | Fatal | Within a transaction, and from one transaction's end to the next one's start, in `opened_at` order. The FMS has no path to reset the totaliser and it cannot wrap within the testbed's life, so a decrease has no benign cause |
| The totaliser has no gap | Measurement integrity 4 | Reported | A next start above the previous end. A lost dispense record produces one |
| No concurrent transactions | Measurement integrity 3 | Reported | Via `opened_at`/`closed_at` overlap. A backwards step of the FMS's clock is a benign cause, and an injected record is already fatal as a record with no paper |
| Every transaction's `session_id` resolves to a `LOGIN` | Only authenticated transactions occur | Reported | Against `LOGIN` records from one session lifetime before the window (see "Windows, margins and grouping") |
| Card-table changes falling in the window | Counterparty identity matches the authorisation | Reported, as context | No paper counterpart exists to compare against |

A meter falsified consistently with its paper is therefore reported, through the ATG check, and not fatal.

#### Outcome

**Three classes are fatal**, by the mechanism cavitation uses: `panic!` with a formatted message, which the orchestrator recovers via `into_panic()`, logs as one ERROR attributed to the entity, then `abort_all()` and a non-zero exit. 
No typed payload and no distinct exit code. The message names the window, every fatal finding with the document or job it concerns, and the count of each reported kind: an audit that ends this way emits no WARN, so the reported findings would otherwise go unrecorded.

| Case | Disposition | Grounding |
|---|---|---|
| Record present but disagreeing with paper | Fatal | The FMS log is append-only and corrections are new entries, so there is no benign cause |
| Log record with no paper artefact | Fatal | Every completed transfer produces a signed paper document, so the record was injected |
| A totaliser decrease | Fatal | The FMS cannot reset the totaliser and it cannot wrap, and a lost record cannot lower a later reading, so the readings were written falsely |
| Paper artefact with no log record | **Reported** | `narrative.md §5 Temporal 4` permits an undeliverable copy to be lost and gives availability precedence over completeness |
| A dispense paired with its BDN by content, its link not having arrived | **Reported** | The same benign cause: the `bdn_linked` event was lost |
| A record inconsistent with another record: the ATG against the meter, preset against delivered, an open with no close, a totaliser gap, concurrent transactions | **Reported** | Each has a benign cause in the modelled port, named under Checks |
| A field a check reads that is absent or unreadable | **Reported** | The FMS writes no value where it has none to record, such as the closing ATG volume and the temperature when the ATG cannot be read at the close, and the store keeps no empty field; the check that needed the field cannot be made, so neither agreement nor disagreement is established |
| A record that cannot be assigned to a window | **Reported** | With no `job_id` or no instant it cannot be paired with paper, so neither a disagreement nor a missing artefact can be established; a lone `bdn_linked` requires only that the transaction's other events did not arrive |
| Any finding from the authentication or card-table events | **Reported** | A missing `LOGIN` can have the same benign cause as any lost forwarded event, and a card-table change has no paper counterpart to disagree with |

The three fatal classes all require something to have been **written**. 
An absence requires only that something did not arrive, which the narrative allows.
From the recon user's available information there is nothing  to distinguish a log-server outage from a suppression.

#### Reporting

Reporting levels follow the convention above, and the split matters because this is the only entity whose ordinary output is a judgement about the environment.

- **WARN, one event per audit** — an audit with findings and none fatal: the window; for each kind of finding, its count and the documents, jobs or cards it names; the shape of the unmatched artefacts; the margins; and the count of refused jobs. One event rather than one per finding, since a long outage can leave hundreds of artefacts unmatched.
- **INFO** — a clean audit: window, artefacts reconciled, records retrieved, retrievals used, refused jobs, and the margin on each numeric check, so a near miss is visible before it becomes a fatal.

**A margin** is the largest deviation a numeric check found in the window, as a fraction of its tolerance: 1 is at the tolerance, and above 1 is beyond it. There is one for each numeric check: the BDN volume against the totaliser delta (±0.5%), the ATG delta against the corrected meter volume (±1%), and the delivered volume against the preset (±0.5%). A check that ran on no transaction has no margin.

**The shape of the unmatched artefacts.** Taken in commencement order, unmatched artefacts with no matched artefact between them form one run. The WARN gives the number of runs and the length of the longest: a single long run is the observation an outage of the log server produces, and many runs of one are not.

**Refused jobs** — jobs of `job_opened` events, alone or with a `job_cancelled`, which the FMS refused — are counted in both reports as context. They are not findings: no transfer took place and no paper exists.
- **ERROR** is not used for a finding. It is the single fatal event before teardown.

These three reports would reach whoever is running the environment. **The audit also records a summary inside the modelled world**, which `narrative.md` §3 requires: before closing the terminal, the actor appends one entry to a file in the account's home directory carrying the period audited, whether the records agreed, and the document numbers of any that did not.

It is appended by shell redirection in the terminal already open, so no transfer channel is introduced and `recon.md §11.1` is unaffected. 
It names identifiers and never record contents — the workstation holds no copy of the period's records (`recon.md §1`).

**The entry is one line**: the window's start and end, each in RFC 3339 with a `Z` designator; `agreed` or `disagreed`; then the document numbers concerned, in sorted order, each once, separated by single spaces:

```
2025-01-01T00:00:00Z 2025-02-01T00:00:00Z disagreed BDN-000004 DT-000012
```

- **The records disagreed** if the audit made any finding other than a card-table change, which is context rather than a comparison of records.
- **Only document numbers are named**, as `narrative.md` §3 states: those of a paper artefact with no record, and of a BDN paired by content. A finding that names a job and no document, such as a missing `LOGIN`, makes the entry `disagreed` and adds no identifier; the WARN names the job.
- **A fatal audit writes no entry**: the sim ends at the `panic!`, before the terminal is closed.
- **A single line** cannot be left partly written across two entries, and every character in it is one a shell passes unchanged in an `echo` argument: letters, digits, `-`, `:` and the space. No quoting is needed, so none can be got wrong.

**Appending and confirming it.** The actor types `echo <entry> >> ~/reconciliation-audits.txt`, then confirms the append through the clipboard, which is the only thing that returns: it clears the latch, types `tail -n 1 ~/reconciliation-audits.txt | xclip -selection clipboard`, and reads. The last line equal to the entry confirms it; anything else, or a failed read, is the failure to write reported at WARN above.

A failure to write the entry is reported at WARN with the audit's own result. An audit that completed and left no trace of itself inside the environment has not done the whole of its job.

#### The audit loop

`spawn` validates its configuration on the synchronous path — a settling delay outside its bounds is a startup fault — builds the connector that opens an IronRDP session to `recon_addr` from `local_addr`, and starts `run`, which takes the connector as a parameter so that the tests drive it with doubles.

`run` audits one window after another, from the first window (see "Cadence and window"):

1. Sleep until the window's end plus `settling_delay`, on the sim clock.
2. Drain every record waiting on `record_rx` — a closed channel means the dock operator has died, which is fatal (see the error taxonomy) — add them to those held from earlier cycles, and partition the lot by commencement (see "Accumulation and partitioning"): those within the window are audited, and those after it are held for the next.
3. Conduct the audit (see "Failure handling") over the window's retrieval bounds, `initial_window` wide at first, reconciling the window's artefacts with what is retrieved.
4. Emit the result, as below, and take the next window.

The records are drained after the settling delay and not before, so that every transfer commencing in the window has completed and posted its paper.

**Emission.** A clean audit is one INFO event and an audit with findings one WARN event, each carrying the report under "Reporting"; a summary entry that was not written adds a WARN naming the window. A fatal outcome, and a failure the budgets do not absorb, `panic!` with their messages, which the orchestrator logs as the one ERROR. The sim's `EnvFilter` defaults to `warn`, so a clean audit is not visible in a run's output unless the filter is raised; whether the healthy case should be visible by default is a decision for the fleet's logging, not this actor's, and is left there.

**Configuration.** The audit period, the settling delay and the boundary margin are the port's procedure, scenario values projected from `world.yaml` (`audit.period`, `audit.settling_delay_s`, `audit.boundary_margin_s`) and compiled in (`deployment-config.md` §3, structure of `world.yaml`). `initial_window` and `width_floor` are derived from the arrival rate (see "Splitting the window"). The rest are the sim's own and are literals in `coupled.rs`:

| Parameter | Value | Ground |
|---|---|---|
| `audit_period` | `month` (`world.yaml`); in a compressed scenario, an interval in hours | The narrative's usual practice (`narrative.md` §3) and its compressed scenario (`narrative.md` §6) |
| `settling_delay` | 6 hours (`world.yaml`) | At least the margin and shorter than a day, so valid for every period; long enough for the FMS's forwarding retries and the transcription of a BDN number |
| `boundary_margin` | 2 hours in operation (`world.yaml`) | The longest credible transfer: a fill of 18,000 L at 600 L/min runs half an hour, and the RFID gate and the transcription add to it |
| `initial_window` | Derived; about 56 days at the roster's default rate of 0.15 arrivals an hour, 11.2 days at a compression factor of 5 | Its expected records fill half the clipboard's ceiling, with headroom for a busier span (see "Splitting the window") |
| `width_floor` | Derived; about 7 days at the roster's default rate, 1.4 days at a compression factor of 5 | Its expected records fill a sixteenth of the ceiling, so a span at the floor that still fails shows a failing clipboard (see "Splitting the window") |
| `clipboard_deadline` | 10 seconds | A read of 876 KB, near the clipboard's ceiling, completed in 87 ms on the deployed system; ten seconds is more than a hundred times that read, and bounds the cost of each halving and of each deadline at the floor |
| `terminal_settle` | 3 seconds | Provisional; confirmed by the latch, which a terminal not yet ready fails |
| `key_interval` | 30 milliseconds | Proved by the probe (see "Keyboard") |
| `connect_deadline` | 30 seconds | The connection, the TLS handshake, the logon through the proxy and the clipboard channel's setup, together |

In a compressed scenario `render.py` divides `audit_period`, as hours, `settling_delay` and `boundary_margin` by the compression factor, and the derived widths follow the scaled arrival rate (*Compressed scenarios*). The table's other parameters are timings of the software, which a compressed scenario leaves unchanged.

#### Failure handling

Three sites, each with its own budget: terminal start (no terminal opens, or the latch does not return), retrieval (the client returns a diagnostic, or the response did not arrive whole), and clipboard (a read that delivers nothing usable). A failure is counted against the site it belongs to and no other. The sim's retry policy for transient digital-world I/O (above) applies wherever a case is of that kind, so that this actor recovers as the dock operator does.

**Fatal at once, neither retried nor restarted:**

| Failure | Ground |
|---|---|
| The workstation's certificate does not verify | Retrying a failed verification is clicking through an interstitial (`recon.md §6.4`), and the workstation credential has not been sent |
| The log server's certificate does not verify | The same ground; `--insecure` is never used |
| Grafana refuses the credential | A 4xx, which the sim does not retry: a password refused once is refused again |
| A command holds a character the scancode table lacks | The commands and the credentials in them are the sim's own, and validation holds the credentials to printable ASCII (`deployment-config.md §5`), so this is the sim's fault: an invariant violation |

**Connecting** is transient digital-world I/O: a connection that fails is retried up to three times, after 1, 2 and 4 seconds on the sim clock, and exhausting the retries is fatal. It is no site's budget, since there is no session to restart; it applies to the restart's own connection alike.

**Terminal start: three attempts in each session.** An attempt opens a fresh terminal with `Ctrl+Alt+T`, waits `terminal_settle`, and confirms the latch; a mismatch or nothing read back ends the attempt. Nothing further is typed into a terminal that did not confirm: the next attempt opens another. The latch value carries the attempt's number within the audit, so it differs between every attempt, across a restart too.

**Clipboard: three failures in each session.** Content from before the read, a file-descriptor format offered, and the workstation's failure response each end the read; the span's command is typed again, the latch cleared first, and read again. A refusal by the reassembly guard, a declared length over the channel bound or chunks carrying more than they declared, has already ended the session (see "The session loop"), and exhausts the budget at once. So does a connection lost once the session is established, for the same reason.

A deadline is the split's, not the budget's, while the span exceeds `width_floor`: the span is halved and retried (see "Splitting the window"). **A span at the floor that reaches its deadline exhausts the budget.** No legitimate span at the floor approaches the ceiling, but a clipboard channel that no longer delivers presents in the same way, and only a fresh session distinguishes the two. Its read is therefore not repeated in the same session.

**Retrieval: three retries of each span.** Unreachable, incomplete and malformed responses are retried under the transient policy: the span's command is typed again after 1, 2 and 4 seconds on the sim clock, the latch cleared first. A malformed response is among them, since the query a dropped keystroke produces is refused by the server, and the next typing may not drop it. A span that succeeds resets the count for the next. Exhausting the retries is fatal, as exhausted transient I/O is throughout the sim: the retrieval site buys no restart, because a fresh session does not repair the log server. A replica that cannot be reached when the audit runs is therefore a fatal end to the run, which is a modelled outcome of an attack on it and not one to conceal.

**Exhausting the terminal or clipboard budget gives one whole-session restart** — disconnect, a new connection, a new terminal, a new latch — before a second exhaustion is fatal. 
A dialogue holding focus is cleared by a fresh session and never by retrying into a dead one. The restart resumes where the audit stopped: spans already retrieved are kept, and the retrieval continues from the span that failed, at the width last held. One restart is allowed in each audit, whichever budget used it.

**A fatal failure ends the run** by the mechanism of the fatal findings (see "Outcome"): `panic!` with a message naming the window, the site, the failure that ended the audit, and whether the restart had been used. No summary entry is written: the sim ends before the terminal is closed.

**The audit's procedure** is written against a connector that yields a workstation, so that the tests drive it with doubles: connect; open a terminal and confirm the latch; retrieve; reconcile the retrieved records; unless the outcome is fatal, record the summary entry, then close the terminal and disconnect. It returns the outcome and whether the summary entry was written, or the fatal message of a failure; the actor emits the outcome at INFO or WARN, or panics with the message of a fatal outcome or failure.

**Everything arriving at the clipboard must be classifiable**: a window's records, an empty window, a client diagnostic, an authorisation refusal, and all content. These are all data the actor reads and acts on. The restriction is on what reading can do to the sim, not on what it may say.

**No transfer channel.** No secure copy, no device redirection, no clipboard file transfer; each would give potential write onto the sim's filesystem, against `sim.md §5.4`. The clipboard is the only return path.

#### Internal types

Private to `internals`; not part of the interface.

- **`AuditWindow { start, end }`** — the period one audit covers, `start` included and `end` excluded, both UTC, so a transfer commencing exactly at a boundary belongs to the window it opens. `retrieval_bounds(margin)` widens it by the margin on both sides and further back by the session lifetime, for retrieval only.
- **`ReconciliationRecord::delivery_commenced_at()`** — the instant that assigns either artefact to a window.
- **`AuditPeriod`'s calendar** — `containing(instant)`, the window of that period containing an instant; `next(window)`, the window after it, with no gap across a month or year end; `first_window(spawned_at)`, the window containing the spawn instant, starting at that instant; and `accepts_settling_delay(delay, boundary_margin)`, true only for a delay of at least the margin and shorter than the shortest period of its kind. For an interval, the audit loop's schedule fixes the windows. Each window ends `settling_delay` before its audit, and the first window starts at the later of the spawn and one period before its end. `accepts_settling_delay` takes the interval as the shortest period of its kind.
- **`partition(artefacts, window)`** — splits the held and newly drained artefacts into those dated within the window, for this audit, and those dated after it, held for the next; panics, naming the document, on one dated before it.
- **`AuditFailure`** — a failure that prevents an audit completing, grouped by the site whose budget it consumes: `Terminal` (the connection failed, which is retried under the transient policy and consumes no budget; the workstation's certificate did not verify, which is fatal at once; a character the scancode table lacks, also fatal at once; no terminal; the latch returned a different value or nothing — the session-state failures), `Retrieval` (unreachable, authorisation refused, certificate verification, malformed response, incomplete response) and `Clipboard` (deadline reached, declared length over the channel bound, chunks exceeding their declared length, a non-text format, content from before the read, the workstation's failure response). A failure is never a finding.
- **The workstation** — the session as the audit drives it: open a terminal, clear the latch, type a line, read the clipboard, disconnect. The IronRDP session implements it, and so does the double the tests drive; the audit's session procedure is written against it.
- **The connector** — yields a connected workstation, or the connection's failure. Connecting to the workstation with IronRDP implements it; the tests' connector hands out doubles and counts the connections made.
- **`run(clock, record_rx, connector, settings)`** — the audit loop under "The audit loop", taking the connector so that the tests drive it with doubles; `spawn` builds the IronRDP connector and starts it.
- **The audit procedure** — one audit through the connector: the session steps, the three budgets, the retries and the one restart under "Failure handling", returning the outcome and whether the summary entry was written, or the fatal message of a failure.
- **The scancode table** — each printable ASCII character to its key and whether `Shift` is held, under the `gb` layout; and a line to the key events that type it, or a refusal naming the first character the table lacks.
- **The frame inspector** — takes a frame the session reads and, if it carries the clipboard channel, the header of its chunk, for the reassembly guard.
- **The TLS configuration** — the deployment's root as the only trust anchor and `recon-01` as the name verified.
- **The clipboard read's parts** — the backend, which records each announcement, the formats offered and the data received and never acts on the channel itself; the reassembly guard, which takes each chunk's header and accepts or refuses it against the channel bound; and the read, which takes the backend's record through the sequence under "Reading the clipboard" to the decoded text or a `Clipboard` failure.
- **`FindingKind`** — the kinds of finding, one per case under Outcome, with `is_fatal()` true for exactly the three fatal classes.
- **`reconcile(artefacts, records, window)`** — the audit of one window: sets the authentication and card-table records aside, groups the bunkering events, pairs, and runs the checks, returning every finding with the document or job it names, the margins, the runs of unmatched artefacts, and the counts the reports carry.
- **`outcome(reconciliation, retrievals)`** — the audit's result: clean, findings, or fatal, carrying the report under "Reporting" or the fatal message under "Outcome". The actor emits it at INFO or WARN, or by `panic!`.
- **`summary_entry(outcome)`** — the line the actor appends on the workstation (see "Reporting"), or none for a fatal outcome.
- **The retrieval** — the command for a span, built in one function; the reading of a response into its records or a `Retrieval` failure; the split, which gives the next span and takes each read's result, halving on a deadline and ending at the floor; and the retrieval's progress — the split and the spans retrieved — which is driven through one workstation and then another, and assembles the window in `_time` order.
- **`pair(artefacts, transactions)`** — pairs each artefact with its transaction by document number, then each unpaired completed dispense with an unpaired BDN by content (see "Pairing"), returning the pairs with how each was made, and the artefacts and transactions left unpaired.

**Interface:**

```rust
/// A paper artefact posted by the dock operator for the reconciliation user
/// to cross-check against the log-server replica at the end of each audit period.
pub enum ReconciliationRecord {
    Bdn(BDN),
    DeliveryTicket(DeliveryTicket),
}

/// The period one audit covers, set by the port (`narrative.md` §3), in UTC:
/// a calendar month; an ISO week, 00:00 on Monday to the end of Sunday; or a
/// day, 00:00 to the end of that day, including the whole of 23:59:59. Each
/// period's successor begins at 00:00.
pub enum AuditPeriod {
    Month,
    Week,
    Day,
    /// In a compressed scenario, a fixed interval measured in hours
    /// (`narrative.md` §6, deviations from real-world practice), whose
    /// windows' boundaries the first audit fixes (*A period in hours*).
    Interval(Duration),
}

/// Spawn the reconciliation user actor.
///
/// Sleeps until `settling_delay` past the end of each audit period, then audits
/// that period. On each cycle: drains all pending records from `record_rx` and
/// partitions them by their own `delivery_commenced_at`, holding any belonging
/// to a later window; establishes a remote desktop session to `recon-01`
/// through HAProxy's egress port; opens a terminal, confirms the session
/// through the clipboard latch, retrieves the window in one or more bounded
/// reads, records the audit's summary entry, closes the terminal and
/// disconnects; then emits the result, or panics on a fatal one.
///
/// All interaction with `recon-01` occurs within the session, and results
/// return only through the clipboard. Both credentials are read from the
/// compiled world constants rather than passed here, as the dock operator's
/// are.
///
/// `record_rx` must be backed by an unbounded channel: the dock operator posts
/// records continuously, and this actor drains them once per cycle.
pub fn spawn(
    clock: SimClock,

    /// Completed BDNs and delivery tickets posted by the dock operator.
    record_rx: mpsc::UnboundedReceiver<ReconciliationRecord>,

    /// The loopback address the session is dialled from, unbound
    /// (`127.0.0.1:0`): HAProxy applies the sim host's `vnet-it` address,
    /// 10.30.0.11, as the connection's source (`sim-network.md` §5), as for
    /// the dock operator.
    local_addr: SocketAddr,

    /// The loopback egress port HAProxy forwards to `recon-01:3389` in TCP
    /// mode, `127.0.0.1:20003` (`sim-network.md` §5). Names nothing, so the
    /// workstation's certificate is verified against the name `recon-01`.
    recon_addr: SocketAddr,

    /// The period each audit covers: a calendar month, an ISO week or a day,
    /// all UTC. Set by the port (`narrative.md` §3). In a compressed scenario,
    /// an interval in hours.
    audit_period: AuditPeriod,

    /// From the actor's spawn to its first audit (*Start state*). `Some`
    /// exactly when `audit_period` is an interval, or `spawn` refuses the
    /// configuration as a startup fault.
    first_audit_after: Option<Duration>,

    /// How far into the following period the audit runs, allowing records
    /// still in flight at the boundary to settle. At least `boundary_margin`
    /// and shorter than the shortest period of its kind, or `spawn` refuses it.
    settling_delay: Duration,

    /// Width of the first sub-retrieval, derived from the arrival rate (see
    /// "Splitting the window"). Halved on a read that reaches its deadline,
    /// down to `width_floor`, and held once a width succeeds.
    initial_window: Duration,

    /// The width below which halving stops, derived from the arrival rate
    /// (see "Splitting the window").
    width_floor: Duration,

    /// Margin retrieved either side of the window so that a transfer
    /// spanning the boundary is retrieved whole. Bounded by the longest
    /// credible transfer. Projected from `audit.boundary_margin_s`.
    boundary_margin: Duration,

    /// Deadline on every clipboard read. A read that does not complete within
    /// it resolves to a typed error.
    clipboard_deadline: Duration,

    /// After `Ctrl+Alt+T`, before the latch is typed.
    terminal_settle: Duration,

    /// Between keys: a terminal drops characters typed faster than it reads
    /// them.
    key_interval: Duration,

    /// On the connection, the TLS handshake, the logon and the clipboard
    /// channel's setup together.
    connect_deadline: Duration,
)
```

The clipboard's ceiling is not a parameter: it is the actor's own constant, the workstation's measured 1,048,575 bytes (see "Reading the clipboard"), and the reassembly guard is tested against it in-module.

---

### Systems administrator

**Interactions:**
- Digital: a Secure Shell (SSH) session into `admin-01` through this actor's egress (`sim-network.md` §5), authenticated with the workstation password; within it, an SSH connection to each of `fms-01`, `hist-01` and `hmi-01` in turn through `dmz-bastion-01`, using the key held on `admin-01`; the routine check on each
- Physical: none
- Receives: nothing
- Signals: nothing
- Spawned at sim startup; runs for the duration of the sim

**Notes:**
- The session into `admin-01` is the only channel between this actor and the digital world (`digital.md` §2.9). The key for the managed hosts stays on `admin-01` (`deployment-config.md` §6).
- The recommended client is the `russh` crate, which supports password authentication, pseudo-terminal requests and interactive shells.
- **Memory and `tracing` only.** The actor keeps its state in memory and logs each command's output at DEBUG, satisfying `sim.md` §5.4.

#### Cadence

Each session begins `check_interval` of simulated time after the previous session ends (narrative §3); after a cold start the first begins as the start state places it (*Start state*). The actor keeps the due time, `next_check_at`, and sleeps until it with `stop::sleep_until`, raced against the stop request (*Stopping and warm start*); a warm start continues from the saved due time. The duration is projected from `intervals.admin_check_interval_h` (`deployment-config.md` §3, structure of `world.yaml`), which a compressed scenario divides by the compression factor with the other process timings (*Compressed scenarios*).

#### Session

1. **Connect** to the egress port, verify `admin-01`'s host key against the public key in the compiled world constants (`deployment-config.md` §3), and sign in with the workstation credential, requesting a pseudo-terminal and an interactive shell. A different host key ends the session before the credential is sent.
2. **Check each managed host in order**: `fms-01`, `hist-01`, `hmi-01`. Type `ssh <host>`; the workstation's client configuration supplies the jump through `dmz-bastion-01` (`digital.md` §4.6). Run the check, then type `exit`.
3. **Exit** the shell on `admin-01`, then disconnect.

The actor types each command and waits for the shell prompt before typing the next. A prompt missing `step_deadline` is a step failure.

#### The check

The check is read-only (narrative §3). On each managed host it runs these commands in order.

| Command | Confirms |
|---|---|
| `systemctl --failed` | services are running, by listing any failed unit |
| `df -h` | disks have space |
| `chronyc tracking` | the clock is synchronised to `corp-it` (`digital.md` §1.3) |
| `sudo journalctl --priority=err --since <previous check>` | the journal shows no new errors |

The journal command uses `sudo` because the administration account can read only its own journal without it. `<previous check>` is the start time of the previous session, `previous_check_at`, which a warm start takes from the checkpoint; the first session after a cold start uses `--boot`. The account's `sudoers` rule carries `NOPASSWD` (`digital.md` §4.6), so the actor types only the command. Each use of `sudo` leaves a record in the host's journal, which the security monitoring collects (`siem.md` §3).

#### Credentials

`identities.systems_administrator.username` and `.workstation_password`, read from the compiled world constants as the other actors' credentials are (`deployment-config.md` §3, §6).

#### Containment

The session must request only a pseudo-terminal and a shell. It must not request agent forwarding, X11 forwarding, port forwarding or a file-transfer subsystem (`sim.md` §5.4).

#### Failure handling

A failure to reach `admin-01`, or a rejected credential, ends that session with a WARN record. A managed host that cannot be reached, or a step failure on it, is recorded at WARN, and the session moves to the next host. The next scheduled session runs as normal.

**Interface:**

```rust
/// Spawn the systems administrator actor.
///
/// Sleeps until `next_check_at`, then runs one session:
/// connects to `egress_addr`, which HAProxy forwards to `admin-01:22` from
/// the `it-sysadmin` address (`sim-network.md` §5); signs in with the
/// workstation credential; connects to each of `managed_hosts` in turn
/// through the bastion and runs the check; then disconnects. The next
/// session is due `check_interval` after the session ends. Begins no session
/// once a stop is requested; a session in progress runs to its end
/// (*Stopping and warm start*).
///
/// The credential is read from the compiled world constants, as the other
/// actors' are.
pub fn spawn(
    clock: SimClock,

    /// Loopback egress port for this actor (`sim-network.md` §3, port 20004).
    egress_addr: SocketAddr,

    /// Managed hosts in check order, by the names the workstation's SSH
    /// client configuration resolves: `fms-01`, `hist-01`, `hmi-01`.
    managed_hosts: Vec<String>,

    /// Simulated duration from the end of one routine check to the start of
    /// the next, projected from `intervals.admin_check_interval_h`.
    check_interval: Duration,

    /// Deadline on each expected shell prompt. A prompt missing it resolves
    /// to a typed error.
    step_deadline: Duration,
)
```

---

### Fuel supplier

**Interactions:**
- Digital: initiates TCP connections to `atg-relay-01:10001`; polls ATG state via `i201` and `i101`
- Physical: on dispatch, sends a `FillJob` to the road tanker driver after `lead_time` elapses; receives the driver's `completed` signal once the delivery has ended
- Loop: every `poll_interval`, reads volume and alarm state, whether or not an order is pending; if no order is pending and the reorder conditions are met, places an order, which stays pending until the driver signals that the delivery has ended — so at most one order is pending at a time

**Interface:**

```rust
/// Spawn the fuel supplier actor.
///
/// Runs for the duration of the sim as a single `async fn`, polling at every
/// `poll_interval` whether or not an order is pending (`sim.md` §5.3). It
/// waits concurrently on the next poll, the end of a pending order's lead
/// time, and the `completed` signal of a dispatched `FillJob`:
///
/// - On each poll: initiates a real TCP connection to `atg_relay_addr` and
///   reads current volume (i201) and alarm state (i101). If no order is
///   pending, the volume is at or below `reorder_threshold_L` and no critical
///   alarm is active, it places an order: the order becomes pending and its
///   lead time starts. The supplier logs one `INFO` event with the poll's
///   time, the reported volume and the road tanker's scheduled arrival, the
///   poll's time plus `lead_time`, and logs the same event again at any start
///   with the order pending. A poll while an order is pending places no order.
/// - When the lead time ends: sends a `FillJob`, carrying a fresh `completed`
///   oneshot, to the road tanker driver.
/// - When `completed` fires: the order is no longer pending. The driver fires
///   it on every outcome of the delivery, so an order cannot remain pending
///   indefinitely. A dropped `completed` sender is fatal.
///
/// Polls keep their interval through the lead time and the delivery: the
/// order does not move the poll schedule. Each poll falls due one
/// `poll_interval` after the previous one fell due, not after it ended, so
/// neither a poll's retries nor its length move the schedule. The supplier
/// polls one at a time: a poll that outlasts the next due time delays that
/// poll, which starts as soon as the slow one ends. The supplier then makes
/// one poll for each due time that has passed, at once, before it returns to
/// the schedule, so no poll is skipped.
pub fn spawn(
    clock: SimClock,

    /// The sim host's IP address on `vnet-ext`. All outbound TCP connections
    /// to `atg-relay-01` originate from this address. Unused in isolated
    /// mode (no digital world present).
    local_addr: SocketAddr,

    /// TCP address of `atg-relay-01` on `vnet-dmz`. The supplier initiates
    /// real connections here and uses Veeder-Root TLS function codes i201
    /// and i101. Unused in isolated mode.
    atg_relay_addr: SocketAddr,

    /// How often the supplier polls the ATG for current volume and alarm state
    /// (`fuel_supplier.poll_interval_s`).
    poll_interval: Duration,

    /// Reported volume at or below which an order is placed, in litres
    /// (`fuel_supplier.reorder_threshold_l`).
    reorder_threshold_L: f64,

    /// Volume of fuel the arriving tanker carries, written into every `FillJob`
    /// (`fuel_supplier.delivery_volume_l`).
    delivery_volume_L: f64,

    /// Elapsed time between an order being placed and the tanker's arrival at
    /// the dock, when the `FillJob` is sent (`fuel_supplier.lead_time_s`).
    lead_time: Duration,

    /// Channel to the road tanker driver task. A `FillJob` is sent after
    /// `lead_time` elapses following a dispatch decision.
    tanker_arrival_tx: mpsc::Sender<FillJob>,
)
```
