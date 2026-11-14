# Sim Test Plan

One section per simulated entity. Each section describes the test harness
and lists individual tests, each targeting a distinct behavioural
requirement.

**Convention:** All tests use `#[tokio::test]`. Each test exercises the
entity under test through its public `spawn()` interface only, holding the
channel ends that would otherwise be owned by neighbouring entities. The
channel ends play the role of the neighbouring entity without instantiating
it.

**SimClock in tests:** Entities that receive a `clock` argument are tested
using `SimClock::isolated(TimeModel::Synthetic)`, which returns a configured
`current_thread` Tokio runtime alongside the clock handle. Each such test is
a plain `#[test]` function (not `#[tokio::test]`, since nesting runtimes is
not permitted) and runs its body via `runtime.block_on(async { ... })`.

Time advances in `Synthetic` mode automatically: whenever all tasks are
`Poll::Pending`, the runtime's `on_thread_park` hook fires, advances
simulated time to the nearest pending deadline, and wakes the corresponding
tasks. The test body drives simulated time forward by calling
`clock.sleep(duration).await` — the same API used by the entities under
test. No separate test-clock mechanism or manual advance primitive is needed.

---

## Physical

### Logical clock

**Test harness — `Synthetic` mode:**

Tests use `#[test]` + `runtime.block_on(async { ... })` where the runtime is
obtained from `SimClock::isolated(TimeModel::Synthetic)`. No channels or
external entities are required.

**Test harness — `WallTimeMultiple` mode:**

Tests obtain the runtime from `SimClock::isolated(TimeModel::WallTimeMultiple(multiplier))`.
Inside `block_on`, `tokio::time::pause()` is called immediately to take
control of Tokio's timer. `tokio::time::advance(duration).await` is then
used to drive the underlying `tokio::time::sleep` calls without consuming
real wall-clock time.

Where a test spawns a task that sleeps and then advances the clock, it first
awaits `tokio::task::yield_now()` so the spawned task runs and arms its
`tokio::time::sleep` timer *before* the clock moves. On the `current_thread`
runtime a spawned task is not polled until the spawning task yields; without
the yield the timer would arm relative to the already-advanced instant and the
sleep would never fire.

---

#### Synthetic mode

##### `now_is_stable_without_sleep`

Record `t0 = clock.now()`. Record `t1 = clock.now()` immediately after,
with no `sleep()` in between. Assert `t1.duration_since(t0) ==
Duration::ZERO`. Confirms simulated time does not advance unless a sleep
is awaited.

---

##### `sleep_advances_simulated_time`

Record `t0 = clock.now()`. Await `clock.sleep(Duration::from_secs(60))`.
Assert `clock.now().duration_since(t0) == Duration::from_secs(60)`.

---

##### `sequential_sleeps_accumulate`

Record `t0 = clock.now()`. Await `clock.sleep(Duration::from_secs(30))`,
then await `clock.sleep(Duration::from_secs(45))`. Assert
`clock.now().duration_since(t0) == Duration::from_secs(75)`.

---

##### `sleep_does_not_consume_wall_clock_time`

Record `wall_start = std::time::Instant::now()`. Await
`clock.sleep(Duration::from_secs(3600))`. Assert
`wall_start.elapsed() < Duration::from_millis(100)`. Confirms the
Synthetic mechanism runs the sim at maximum host speed rather than
wall-clock pace.

---

##### `concurrent_sleeps_resolve_in_deadline_order`

Spawn three tasks, registering their sleeps in the order 30 s, 10 s, 60 s
— an arbitrary non-reverse order chosen specifically to exercise the
priority queue rather than a trivially ordered structure. Each task sends
`clock.now()` on a shared channel immediately on wakeup. Await all three
task handles (the `Synthetic` runtime drives them to completion
automatically). Drain the channel and assert the three reported instants
are in strictly increasing order and match 10 s, 30 s, and 60 s elapsed
from the common start instant respectively.

---

##### `clone_shares_simulated_time`

Clone the clock into `clock2`. Record `t0 = clock.now()`. Spawn a task
that awaits `clock2.sleep(Duration::from_secs(30))`. Await the task
handle. Assert `clock.now().duration_since(t0) == Duration::from_secs(30)`
— confirming that a sleep on one clone advances the simulated time
observable via any other clone.

---

#### `WallTimeMultiple` mode

##### `sleep_completes_when_tokio_time_advances_by_scaled_duration`

Construct the clock with multiplier 3.0. Call `tokio::time::pause()`.
Spawn a task that awaits `clock.sleep(Duration::from_secs(3))`.
Call `tokio::time::advance(Duration::from_secs(1)).await`. Await the
spawned task. Assert it completes — confirming that the underlying Tokio
sleep is for `d / multiplier` (1 s), not `d` (3 s).

---

##### `simulated_now_advances_by_full_sleep_duration`

Construct the clock with multiplier 3.0. Call `tokio::time::pause()`.
Record `t0 = clock.now()`. Spawn a task that awaits
`clock.sleep(Duration::from_secs(3))`. Call
`tokio::time::advance(Duration::from_secs(1)).await`. Await the task.
Assert `clock.now().duration_since(t0)` lies in `[3 s, 3 s + 50 ms)` —
confirming `clock.now()` tracks the full simulated duration (~3 s), not the
scaled Tokio duration (1 s). The upper bound absorbs Tokio's 1 ms timer
granularity: the 1 s sleep fires at 1.001 s, so `now()` reads 3.003 s.

---

##### `multiplier_scales_tokio_sleep_proportionally`

Construct the clock with multiplier 6.0. Call `tokio::time::pause()`.
Spawn a task that awaits `clock.sleep(Duration::from_secs(6))`. Call
`tokio::time::advance(Duration::from_secs(1)).await`. Await the task and
assert it completes. This independently confirms the scaling relationship
with a different multiplier value: 6 s simulated / 6.0 multiplier = 1 s
Tokio advance required.

---

#### Constructor validation

##### `zero_multiplier_panics`

Assert that `SimClock::isolated(TimeModel::WallTimeMultiple(0.0))` panics.

---

##### `negative_multiplier_panics`

Assert that `SimClock::isolated(TimeModel::WallTimeMultiple(-1.0))` panics.

---

##### `infinite_multiplier_panics`

Assert that `SimClock::isolated(TimeModel::WallTimeMultiple(f64::INFINITY))` panics.

---

##### `nan_multiplier_panics`

Assert that `SimClock::isolated(TimeModel::WallTimeMultiple(f64::NAN))` panics.

---

### Fuel tank

**Construction parameters used in tests:**

| Parameter | Value |
|-----------|-------|
| Tank capacity | 10 000 L |
| `k_l_per_mm` | 7.0 L/mm |
| Inflow rate | 200 L/min |
| Outflow rate | 100 L/min |
| `overfill_interlock_pct` | 95: an interlock level of 9 500 L (tests that do not state another) |
| Initial temperature | 15 °C |
| Default initial volume | 5 000 L (tests that do not state one) |

**Test harness:**

The test obtains a `SimClock` and runtime via
`SimClock::isolated(TimeModel::Synthetic)` and runs its body under that
runtime. It creates all channels, then calls
`fuel_tank::spawn(clock, tank_capacity_l, overfill_interlock_pct, k_l_per_mm, inflow_rate_l_per_min,
outflow_rate_l_per_min, initial_volume_l, initial_temperature_c, sounding_rx,
bund_wet_tx, interlock_plug_rx, outflow_rate_tx, dispenser_ctrl_rx, esd_rx,
tanker_pump_rx)`, holding its returned `JoinHandle` (awaited by the cavitation
test). The test body holds the counterpart ends: `sounding_tx` (to send
sounding requests), `interlock_plug_tx` (to establish an interlock lease, in
the manner of a sounding request — send an `InterlockPlug` carrying a oneshot
and await the `watch::Receiver<InterlockCircuit>` reply), `bund_wet_rx`,
`outflow_rate_rx`, `dispenser_ctrl_tx`, `esd_tx`, `tanker_pump_tx`.

All input watch channels are initialised to their default-inactive state
before `spawn()` is called:
- `dispenser_ctrl_rx`: `solenoid_open = false`, `pump_energised = false`
- `esd_rx`: `false`
- `tanker_pump_rx`: `false`

No interlock lease exists until the test sends an `InterlockPlug`.

**Note on volume dynamics tests:** When an input watch channel changes, the
tank wakes, calls `clock.now()`, and records the current instant as the
last-update time. Volume dynamics tests therefore follow this sequence: (1)
set the desired input state and yield to allow the tank to process the
change and record the last-update instant; (2) await `clock.sleep(T)` in
the test body — in `Synthetic` mode, once all tasks are pending the runtime
advances simulated time by `T` instantly; (3) send a sounding request —
this wakes the tank a second time, causing it to compute `elapsed = T`,
advance `volume_L`, and reply.

---

#### `outflow_active_when_solenoid_and_pump_open`

Set `dispenser_ctrl_tx` to `solenoid_open = true`, `pump_energised = true`.
Await `outflow_rate_rx.changed()`. Assert `outflow_rate_rx.borrow().is_some()`.

---

#### `outflow_inactive_when_esd_pressed`

Set dispenser control to active (solenoid + pump open). Await
`outflow_rate_rx.changed()` to confirm the active state. Then set `esd_tx`
to `true`. Await `outflow_rate_rx.changed()`. Assert
`outflow_rate_rx.borrow().is_none()`.

---

#### `outflow_resumes_after_esd_released`

Set dispenser control to active. Press ESD (`esd_tx = true`). Await
`outflow_rate_rx.changed()` to confirm inactive. Release ESD (`esd_tx =
false`). Await `outflow_rate_rx.changed()`. Assert
`outflow_rate_rx.borrow().is_some()`.

---

#### `outflow_inactive_when_dispenser_clears`

Set dispenser control to active. Await confirmation of active state. Then
set `dispenser_ctrl_tx` to `solenoid_open = false`, `pump_energised =
false`. Await `outflow_rate_rx.changed()`. Assert
`outflow_rate_rx.borrow().is_none()`.

---

#### `interlock_live_when_plugged_in_below_threshold`

Plug in at the default 5 000 L (send an `InterlockPlug`, await the reply
receiver). Assert the received `InterlockCircuit` is `Live`.

---

#### `interlock_dead_at_plug_in_above_threshold`

Construct the tank at 9 600 L, above the 9 500 L threshold. Plug in. Assert
the received `InterlockCircuit` is `Dead` immediately: a tanker cannot begin a
fill into an already-full tank.

---

#### `interlock_trips_dead_at_threshold`

Construct at 9 400 L and plug in (the receiver reads `Live`). Set
`tanker_pump_tx = true`. Await `interlock_rx.changed()`: the boundary
scheduler wakes the tank at the 9 500 L crossing and drives the lease `Dead`.
Assert the value is `Dead`.

---

#### `interlock_level_taken_from_configuration`

Construct with `overfill_interlock_pct` = 90, an interlock level of 9 000 L,
at 8 900 L, and plug in (`Live`). Set `tanker_pump_tx = true` and await
`interlock_rx.changed()`; assert `Dead`. Clear `tanker_pump_tx` and request a
sounding: assert `level_mm` corresponds to 9 000 L (about 1 285.7 mm at
7.0 L/mm), within the boundary tolerance. The lease trips below the default
9 500 L, so the level is read from the configuration rather than fixed.
Then construct a second tank at 9 200 L with the same setting and plug in:
assert `Dead` at once.

---

#### `interlock_dead_when_esd_pressed`

Plug in at 5 000 L (`Live`). Set `esd_tx = true`. Await
`interlock_rx.changed()`. Assert the value is `Dead` — the ESD breaks the
fill-cabinet circuit.

The pump controller's response to a `Dead` circuit (cutting the pump, and
declining to start it) is tested in the road tanker driver section.

---

#### `interlock_stays_dead_after_draining_below_threshold`

Plug in at 9 400 L (`Live`) and set `tanker_pump_tx = true`; await
`interlock_rx.changed()` to reach `Dead` at the 9 500 L threshold. Holding the same
lease, clear `tanker_pump_tx` and set dispenser control active, then advance the
clock so the level falls back below the threshold and request a sounding to
force a re-evaluation. Assert `level_mm` is below the threshold level yet
`interlock_rx` still reads `Dead`. The interlock latches: once broken it never
returns to `Live` within a lease (a concurrent fill and drain is not reachable
in normal operation, but the latch is enforced regardless). Only a fresh
plug-in resets it.

---

#### `sounding_reflects_outflow_over_elapsed_time`

Set dispenser control to active. Yield. Advance clock by 60 seconds. Send
a sounding request. Assert `level_mm` has decreased by the amount
corresponding to 100 L/min × 1 min = 100 L, within floating-point
tolerance.

---

#### `sounding_reflects_inflow_over_elapsed_time`

Set `tanker_pump_tx = true`. Yield. Advance clock by 60 seconds. Send a
sounding request. Assert `level_mm` has increased by the amount corresponding
to 200 L/min × 1 min = 200 L, within floating-point tolerance.

---

#### `sounding_reflects_net_rate_with_simultaneous_flows`

Set dispenser control to active and `tanker_pump_tx = true`. Yield. Advance
clock by 60 seconds. Send a sounding request. Assert `level_mm` reflects a net
increase of (200 − 100) L/min × 1 min = 100 L, within tolerance.

---

#### `sounding_unchanged_when_all_flows_inactive`

Leave all input channels at their default-inactive state. Advance the test
clock by 5 minutes. Send a sounding request. Assert `level_mm` equals the
initial value.

---

#### `bund_wets_on_overflow`

Hold no interlock lease (no pump controller present to cut the pump). Set
`tanker_pump_tx = true` and leave it; the tank fills at 200 L/min from the
default 5 000 L. Await `bund_wet_rx.changed()`: the boundary scheduler wakes
the tank at the 10 000 L overflow point. Assert `bund_wet_rx.borrow()` is
`true`. The overflow comparison is `volume_L ≥ tank_capacity_l - TOL_L`, so it
fires whether the scheduled wake lands the volume exactly on, or a
floating-point fraction below, the capacity boundary.

---

#### `bund_wets_when_filling_an_already_full_tank`

Construct the tank at `tank_capacity_l` and set
`tanker_pump_tx = true`. Await `bund_wet_rx.changed()` and assert
`bund_wet_rx.borrow()` is `true`. The tank is already on the capacity boundary,
so the first `step` the pump input triggers latches the bund at `t = 0` with no
elapsed fill time and no boundary-crossing wake to reach it first — the mirror
of `cavitation_panics_when_dispensing_from_empty_tank`. The clamp holds the
volume at capacity; the observable consequence of inflow into a full tank is the
bund wetting.
---

#### `bund_stays_wet_after_draining_below_capacity`

Overflow the tank as in `bund_wets_on_overflow` and confirm the bund is wet.
Then clear `tanker_pump_tx` and set dispenser control to active, draining the
primary tank; advance the clock and request a sounding to force a step at the
new time. Assert `level_mm` is below the capacity level yet `bund_wet_rx` still
reads `true` — the bund latches, retaining the escaped fuel after the primary
level falls, and nothing writes it back to dry.

---

#### `bund_wets_when_defeated_tanker_ignores_dead_interlock`

Models a tanker whose pump controller ignores the `Dead` interlock: hold the
lease receiver and keep `tanker_pump_tx = true` throughout. Plug in at 9 400 L
(`Live`), set `tanker_pump_tx = true`, and await `interlock_rx.changed()` to
`Dead` at the 9 500 L threshold. Keep the pump active after the trip. Await
`bund_wet_rx.changed()` and assert `true`: inflow continues past the threshold,
the tank re-schedules its wake from the threshold to the capacity boundary, and
the bund latches wet. Exercises the two-boundary re-scheduling with a live
lease held.

---

#### `bund_stays_dry_when_pump_obeys_tripped_interlock`

The positive counterpart to
`bund_wets_when_defeated_tanker_ignores_dead_interlock`: the pump controller
obeys the `Dead` interlock. Plug in at 9 400 L (`Live`), set
`tanker_pump_tx = true`, and await `interlock_rx.changed()` to `Dead` at the
9 500 L threshold. Then clear `tanker_pump_tx` — modelling the controller
cutting the pump on the trip — advance the clock, and request a sounding.
Assert `level_mm` is below the capacity level and `bund_wet_rx.borrow()` is
`false`: the tank stopped short of capacity and no fuel escaped. The clock
advance gives any overflow a chance to occur, so the dry assertion is
meaningful; the distinction from the defeated-tanker case is the pump being
cut, not the elapsed time.
---

#### `cavitation_panics_when_outflow_drains_empty_tank`

Capture the tank's `JoinHandle`. Set dispenser control to active (outflow at
100 L/min) from the default 5 000 L. The boundary scheduler wakes the tank at
the 0 L crossing; with outflow still active this is cavitation. Await the
`JoinHandle` and assert it resolved to a panic whose message contains
`cavitation`. A spawned-task panic is not caught by `#[should_panic]`, so the
handle is awaited directly, as in the supervision tests.

---

#### `cavitation_panics_when_dispensing_from_empty_tank`

As above, but start the tank at 0 L and set dispenser control active. The pump
draws air immediately, so the panic fires on the boundary condition at `t = 0`
rather than after a drain. Await the `JoinHandle` and assert a `cavitation`
panic.

---

### ATG probe

**Test harness:**

The test creates the two channel pairs and calls
`atg_probe::spawn(reading_rx, sounding_tx, noise_enabled)`. The test body
holds `reading_tx` (ATG console role: sends reading requests) and
`sounding_rx` (fuel tank role: receives sounding requests and replies with
controlled `TankSounding` values).

Each test interaction follows this sequence: (1) the test sends a oneshot
sender via `reading_tx`; (2) the probe wakes, creates its own oneshot, and
sends it via `sounding_tx`; (3) the test receives that oneshot on
`sounding_rx` and replies with a fixed `TankSounding`; (4) the probe
applies its noise model and sends a `ProbeReading` back on the original
oneshot; (5) the test asserts on the received `ProbeReading`.

The noise model is a construction-time parameter. Tests that verify
noise-off behaviour construct the probe with noise disabled; tests that
verify the noise distribution construct it with noise enabled.

---

#### `reading_request_forwarded_to_tank`

Send a reading request via `reading_tx`. Assert that a sounding request
arrives on `sounding_rx` — confirming the probe forwards each inbound
reading request to the fuel tank rather than caching or short-circuiting.
Reply on the sounding oneshot to unblock the probe.

---

#### `probe_reading_reflects_tank_sounding_with_noise_disabled`

Construct the probe with noise disabled. Send a reading request. Service
the sounding request with a `TankSounding` of known `level_mm`. Assert that
the returned `ProbeReading.level_mm` equals the `TankSounding.level_mm`
exactly.

---

#### `noise_disabled_gives_deterministic_readings`

Construct the probe with noise disabled. Send two sequential reading
requests, servicing each sounding request with an identical `TankSounding`.
Assert that both `ProbeReading.level_mm` values are equal.

---

#### `noise_enabled_approximates_correct_distribution`

Construct the probe with noise enabled. Send N = 1 000 sequential reading
requests, servicing each sounding request with an identical `TankSounding`
of known `level_mm = L`. Collect all returned `ProbeReading.level_mm`
values. Assert that the sample mean is within 0.05 mm of `L` and that the
sample standard deviation is within 10% of 0.25 mm.

---

### Bund leak sensor

**Test harness:**

The test creates both watch channel pairs and calls
`bund_leak_sensor::spawn(bund_wet_rx, bund_tx)`. The test body holds
`bund_wet_tx` (fuel tank role: publishes wet/dry state) and `bund_rx`
(ATG console role: receives `BundReading` updates). No clock is needed.
The `bund_wet` watch channel is initialised to `false`.

---

#### `wet_reading_published_on_bund_wet_signal`

Set `bund_wet_tx = true`. Await `bund_rx.changed()`. Assert
`bund_rx.borrow().wet == true`.

---

#### `dry_reading_published_on_bund_dry_signal`

Set `bund_wet_tx = true` and await `bund_rx.changed()` to establish wet
state. Then set `bund_wet_tx = false`. Await `bund_rx.changed()`. Assert
`bund_rx.borrow().wet == false`. This also verifies that the sensor does
not latch: a latching sensor would not publish the transition to dry.

---

### Dispenser PD flow meter

**Construction parameters used in tests:**

| Parameter | Value |
|-----------|-------|
| K-factor | 10 pulses/L |
| Outflow rate (active) | 60 L/min |
| Pulse interval | `60.0 / (10 × 60)` = 0.1 s |

**Test harness:**

The test obtains a `SimClock` and runtime via
`SimClock::isolated(TimeModel::Synthetic)` and runs its body under that
runtime. It creates an `outflow_rate` watch channel (initialised to `None`)
and a `pulse` mpsc channel, then calls `flow_meter::spawn(clock, k_factor,
outflow_rate_rx, pulse_tx)`. The test body holds `outflow_rate_tx` (fuel
tank role: publishes the outflow rate) and `pulse_rx` (dispenser role:
receives pulse events).

---

#### `no_pulses_when_outflow_inactive`

Leave `outflow_rate_tx` as `None`. Advance the clock by 10 seconds. Assert
that `pulse_rx.try_recv()` returns nothing — confirming the meter does not
emit pulses when no outflow rate is set.

---

#### `pulses_match_outflow_rate_and_k_factor`

Set `outflow_rate_tx = Some(60.0)`. Yield to allow the meter to process the
rate change. Advance the clock by 1.0 second (10 complete pulse intervals at
0.1 s each). Drain `pulse_rx` and assert exactly 10 pulses were received.
Assert that every pulse carries `k_factor = 10.0`.

---

#### `no_partial_pulse_on_mid_interval_deactivation`

Set `outflow_rate_tx = Some(60.0)`. Yield. Advance the clock by 0.05 seconds
(half of one pulse interval). Set `outflow_rate_tx = None`. Yield. Assert
that `pulse_rx.try_recv()` returns nothing — confirming that no pulse is
emitted for an incomplete interval.

---

#### `pulses_stop_after_deactivation`

Set `outflow_rate_tx = Some(60.0)`. Yield. Advance the clock by 0.3 seconds
(3 complete intervals). Drain `pulse_rx` and assert 3 pulses received. Then
set `outflow_rate_tx = None`. Yield. Advance the clock by a further 1.0
second. Assert that `pulse_rx.try_recv()` returns nothing.

---

#### `pulses_resume_after_reactivation`

Extend `pulses_stop_after_deactivation`: after confirming no pulses with the
rate set to `None`, set `outflow_rate_tx = Some(60.0)` again. Yield. Advance
the clock by 0.2 seconds (2 complete intervals). Assert that exactly 2 new
pulses are received.

---

### Flow-meter fidelity

These tests verify that the flow meter faithfully measures what the tank
actually loses. They wire the real fuel tank → flow meter → dispenser triangle,
run a single preset dispense, and compare two independently derived quantities:

- the **tank's actual drain** — its sounding before the dispense minus its
  sounding after (level in millimetres × `K_L_PER_MM`); and
- the **meter's recorded dispense** — the dispenser's transaction totaliser
  (pulse count ÷ K-factor), read over Modbus.

A faithful meter agrees with the tank to within its resolution, one pulse
(`1/k` = 0.1 L). This holds because the flow meter counts pulses against elapsed
flow time rather than per `clock.sleep`: a late wake catches up rather than
accumulating drift, and the dispenser counts the whole catch-up batch, so the
meter records exactly the small over-flow the auto-stop latency causes — the
same volume the tank drains.

**Construction parameters used in tests:**

| Parameter | Value |
|-----------|-------|
| Tank capacity | 50 000 L |
| Level-to-volume factor `K_L_PER_MM` | 7.0 L/mm |
| Inflow / outflow rate | 600 L/min |
| Initial volume | 30 000 L |
| Temperature | 15 °C |
| K-factor | 10 pulses/L (one pulse = 0.1 L) |
| Preset dispense | 50 L (≈ 5 s of pump time at 600 L/min) |

The large tank keeps the over-drain from clamping at zero, so the disagreement
stays measurable; the small preset keeps the wall-time cases quick.

**Test harness — `assert_tank_matches_meter(clock)`:**

Spawn the fuel tank, flow meter, and dispenser on the supplied clock, wired by
their real channels; the test body holds the sounding sender (ATG probe role)
and the inert bund, ESD, tanker-pump, and overfill-plug senders. Read the
initial tank volume, then drive the dispense over real Modbus: connect to the
dispenser's TCP address and write the preset register (50 L, encoded ×10) and
the enable register. Await the dispenser's auto-stop — the pump control state
returning to not-energised — by which point the tank has integrated the whole
drain. Read the final tank volume for the tank drain, and the transaction
totaliser (pulses ÷ K-factor) for the meter's recorded volume. Assert that the
two agree to within one pulse (0.1 L).

The tests below run this body under different clock modes obtained from
`SimClock::isolated(...)`, confirming the agreement holds independent of how
simulated time is driven.

---

#### `tank_matches_meter_under_synthetic_time`

Run under `TimeModel::Synthetic`. Simulated time advances only as the entities
sleep, so there is no real-time jitter and the dispense completes essentially
instantly.

---

#### `tank_matches_meter_at_wall_time_x1`

Run under `TimeModel::WallTimeMultiple(1.0)` — simulated time tracks the wall
clock, so the 50 L dispense takes ~5 s of real time.

---

#### `tank_matches_meter_at_wall_time_x10`

Run under `TimeModel::WallTimeMultiple(10.0)` — ~500 ms of real time.

---

#### `tank_matches_meter_at_wall_time_x300`

Run under `TimeModel::WallTimeMultiple(300.0)` — ~17 ms of real time. This is
the highest multiple expected to hold reliably: the real dispense window stays
above the host's timer and scheduler granularity.

---

#### `tank_matches_meter_at_wall_time_x1000`

Run under `TimeModel::WallTimeMultiple(1000.0)` — ~5 ms of real time. Annotated
`#[ignore]`: at this multiple the real dispense window falls below the host's
timer and scheduler granularity, so the auto-stop overshoot can exceed one
pulse on a loaded host, making the test flaky. It is retained as documentation
of that limit and can be run explicitly with `cargo test -- --ignored`.

---

### Tug arrival generator

**Construction parameters used in tests:**

| Parameter | Value |
|-----------|-------|
| Roster | Two test vessels: A, IMO number 1999997, mean time away 15 min; B, IMO number 1999985, mean time away 60 min |
| Refuel volume bounds | 200 L and 800 L — a range other than the default, so the tests show the bounds are read rather than fixed |
| PRNG seed | 42 |

**Test harness:**

The test obtains a `SimClock` and runtime via
`SimClock::isolated(TimeModel::Synthetic)` and runs its body under that
runtime. It creates an `mpsc` channel with one slot per roster vessel and
calls `tug_arrival_generator::spawn(clock, vessels, refuel_volume_min_l,
refuel_volume_max_l, seed, tug_arrival_tx)`. The test body holds
`tug_arrival_rx` and plays the tug engineer: unless a test says otherwise, it
fires each `TugArrival`'s `departed` as soon as the arrival is received, so a
tug leaves at once and the interval between its consecutive arrivals is its
time away. Each `recv` lets the generator's sleeps drive simulated time
forward; the test records `clock.now()` at each arrival.

---

#### `arrival_sent_after_time_away`

Advance the clock by 10 × vessel A's mean time away (150 minutes). Drain
`tug_arrival_rx`. Assert that at least one `TugArrival` was received,
confirming the basic send mechanism.

---

#### `each_tug_keeps_its_own_mean_time_away`

Collect arrivals until each vessel has arrived 500 times. For each vessel,
compute the 499 intervals between its consecutive arrivals. Assert that each
vessel's sample mean is within 10% of its own configured mean (15 min for A,
60 min for B), and that each sample coefficient of variation (std dev / mean)
is within 0.15 of 1.0 — the defining property of an exponential distribution.

---

#### `requested_volumes_within_configured_bounds`

Collect 500 arrivals. Assert that every `volume_l` lies within [200, 800] L,
and that the sample mean is within 25 L of 500 L, the mean of
Uniform(200, 800). Both checks together confirm that the configured bounds,
not fixed ones, are respected and that the draw is approximately unbiased.

---

#### `tug_does_not_arrive_again_before_departing`

With vessel A alone on the roster, receive its first arrival and hold its
`departed` sender without firing it. Advance the clock by 20 × A's mean time
away. Assert that no further arrival was received: a tug that is waiting or
refuelling does not arrive again (`sim.md` §6). Fire `departed`; assert that
a further arrival of A follows.

---

#### `adding_a_tug_leaves_other_sequences_unchanged`

Run the generator with vessel A alone and record A's first 20 times away and
requested volumes. Run it again, with the same seed, with A and B on the
roster, and record A's first 20. Assert the two sequences are identical: each
tug's stream derives from the seed and its own IMO number.

---

#### `each_draw_logs_the_scheduled_arrival`

With a `tracing` subscriber that records `INFO` events, collect 20 arrivals of
each vessel. Assert that one `INFO` event of the tug arrival generator precedes
each arrival, logged at the start or at the vessel's previous departure. The
event carries the vessel's IMO number, a scheduled arrival equal to the
arrival's `clock.now()`, and the requested volume that the arrival's job
carries (`sim.md` §6).

---

#### `each_arrival_is_logged`

With a `tracing` subscriber that records `INFO` events, collect 20 arrivals of
each vessel. Assert that each arrival logs one `INFO` event of the tug arrival
generator at the arrival's `clock.now()`, with the vessel's IMO number, its
scheduled arrival and its requested volume.

---

#### `sequence_independent_of_clock_mode`

Record vessel A's first 10 requested volumes under `TimeModel::Synthetic`,
then, with the same seed, under `TimeModel::WallTimeMultiple(3600.0)`. Assert
the volumes are identical: the draws do not depend on the clock mode.

---

#### `invalid_configuration_refused_at_spawn`

Each case, called separately, is refused at spawn: an empty roster; a vessel
with a `mean_time_away` of zero; a minimum refuel volume equal to or greater
than the maximum; a refuel volume bound that is zero, negative, NaN or
infinite.

---

#### `dropped_departure_signal_is_fatal`

Receive vessel A's first arrival and drop it, dropping its `departed` sender.
Assert that the generator's `JoinHandle` resolves with a panic: the task can no
longer tell when its tug left.

---

## Appliances

### ATG console

**Construction parameters used in tests:**

| Parameter | Value |
|-----------|-------|
| `k_l_per_mm` | 7.0 L/mm |
| `tank_capacity_l` | 50,000 L |

**Test harness:**

The console queries `clock.now_datetime()` for the response timestamp, so each
test obtains `(clock, runtime)` from `SimClock::isolated(TimeModel::Synthetic,
start)` and runs its body under `runtime.block_on(...)` (a plain `#[test]`, not
`#[tokio::test]`), doing real TCP on `127.0.0.1`. The test creates all channels,
initialises the `BundReading` watch channel to `{ wet: false }`, and calls
`atg_console::spawn(clock, k_l_per_mm, tank_capacity_l, probe_request_tx,
bund_rx, alarm_clear_rx, listen_addr)` using a free port on `127.0.0.1` as
`listen_addr` (the console binds synchronously, so the client may connect
immediately). The test body holds: `probe_sounding_rx` (receives probe requests
from the console, each carrying a `oneshot::Sender<ProbeReading>`), `bund_tx`
(controls bund state), `alarm_clear_tx` (sends alarm clear signals), and a TCP
client socket connected to `listen_addr`. Requests are computer-format with no
terminator (e.g. `\x01i20100`); the wire format is specified in
[tls-protocol.md](specs/host_specs/sim/tls-protocol.md).

Only i201 contacts the probe. For the i201 test the console's probe request
is serviced by receiving the oneshot sender from `probe_sounding_rx` and
replying with a fixed `ProbeReading { level_mm: 5000.0, temperature_c:
15.0 }`. The expected i201 field values (a per-tank record for tank `01`
carrying seven IEEE-754 floats; tls-protocol.md §6.2), derived from the
construction parameters and this fixed reading, are:

| # | Field | Expected value |
|---|-------|----------------|
| 1 | Volume | 35,000.0 L |
| 2 | TC Volume | 35,000.0 L |
| 3 | Ullage | 15,000.0 L |
| 4 | Height | 5,000.0 mm |
| 5 | Water | 0.0 mm |
| 6 | Temperature | 15.0 °C |
| 7 | Water Volume | 0.0 L |

---

#### `i201_returns_correct_inventory_data`

Send `\x01i20100` over the TCP connection. Service the probe request. Parse
the per-tank record (`TT p ssss NN`) and decode the seven IEEE-754 hex
fields. Assert the seven decoded values match the expected field values
above (within floating-point tolerance), and that the record prefix is tank
`01`, product code `D`, status `0000`, field count `07`.

---

#### `i205_returns_no_active_tank_alarms`

Send `\x01i20500`. Assert the response per-tank record is `0100` — tank
`01` with zero active tank alarms (`nn = 00`). i205 does not contact the
probe and carries no inventory fields (tls-protocol.md §6.3).

---

#### `i101_returns_no_alarms_in_clean_state`

Send `\x01i10100`. Assert the response body between the timestamp and `&&`
is exactly the all-normal entry `000000` (tls-protocol.md §8).

---

#### `i101_contains_bund_alarm_when_latched`

Set `bund_tx` to `BundReading { wet: true }`. Yield. Send `\x01i10100`.
Assert the response body is exactly `030301` — the Liquid Sensor Fuel Alarm
on sensor 1.

---

#### `bund_alarm_latches_after_wet_signal_clears`

Set `bund_tx` to `BundReading { wet: true }`. Yield. Set `bund_tx` to
`BundReading { wet: false }`. Yield. Send `\x01i10100`. Assert the response
body is still `030301` — confirming the latch holds after the physical
signal clears.

---

#### `bund_alarm_clears_after_latch_clear_signal`

Set `bund_tx` to `BundReading { wet: true }`. Yield. Set `bund_tx` to
`BundReading { wet: false }`. Yield. Send `()` via `alarm_clear_tx`.
Yield. Send `\x01i10100`. Assert the response body is back to the
all-normal entry `000000` — confirming the latch has been cleared.

---

#### `response_checksum_is_valid`

Send `\x01i20100`. Service the probe request. Receive the complete response
bytes. Sum the 8-bit values of every byte preceding the four-character
checksum field — `SOH`, the echoed function code, the timestamp, the body,
and both `&` characters — then parse the checksum field as a 16-bit hex
value and add it. Assert the result modulo 65,536 is zero (the value-based
TLS verification, tls-protocol.md §4).

---

#### `concurrent_clients_both_receive_valid_responses`

Open a second TCP connection to `listen_addr`. Send `\x01i20100` on
both connections without waiting for either response. Service both probe
requests as they arrive on `probe_sounding_rx`. Assert that both
connections receive a well-formed i201 response. The console must handle
both connections gracefully — neither dropped nor denied — so that both
clients receive complete responses.

---

### Dispenser

**Construction parameters used in tests:**

| Parameter | Value |
|-----------|-------|
| K-factor (on each `Pulse`) | 10 pulses/L |
| Test preset volume | 100 L (written as 1000 to register 0x0001, i.e. litres × 10) |
| Pulses to auto-stop | 100 × 10 = 1000 |

**Test harness:**

The test creates a `pulse` mpsc channel and a `dispenser_ctrl` watch
channel, then calls `dispenser::spawn(pulse_rx, dispenser_ctrl_tx,
listen_addr)` using a free port on `127.0.0.1`. The test body holds
`pulse_tx` (flow meter role: sends `Pulse` events), `dispenser_ctrl_rx`
(fuel tank role: receives `DispenserControlState` updates), and a Modbus
TCP client connected to `listen_addr`.

All register interactions use standard Modbus TCP function codes: 0x06
(Write Single Register) for writes and 0x03 (Read Holding Registers) for
reads. The two 32-bit totaliser values are reconstructed from consecutive
register pairs as `(high_word << 16) | low_word`. Unless otherwise
stated, the standard preset of 1000 (100 L) is written to register
0x0001 before the pump is enabled.

No clock is required: all activity is driven by incoming pulses and
Modbus register operations.

---

#### `initial_control_state_is_inactive`

Assert that `dispenser_ctrl_rx.borrow()` has `solenoid_open = false` and
`pump_energised = false` immediately after spawn.

---

#### `pump_enable_activates_solenoid_and_pump`

Write 1000 to register 0x0001 (preset). Write 1 to register 0x0002
(pump enable). Await `dispenser_ctrl_rx.changed()`. Assert `solenoid_open
= true` and `pump_energised = true`.

---

#### `pump_disable_clears_solenoid_and_pump`

Enable the pump (write 1 to 0x0002). Await `dispenser_ctrl_rx.changed()`
to confirm active. Write 0 to 0x0002. Await
`dispenser_ctrl_rx.changed()`. Assert `solenoid_open = false` and
`pump_energised = false`.

---

#### `totalisers_accumulate_received_pulses`

Write the standard preset and enable the pump. Send 50 `Pulse {
k_factor: 10.0 }` events via `pulse_tx`. Yield. Read registers
0x0003–0x0004 (cumulative totaliser) and 0x0005–0x0006 (transaction
totaliser). Assert both 32-bit values equal 50.

---

#### `auto_stop_triggers_at_preset_pulse_count`

Write the standard preset and enable the pump. Send exactly 1000 `Pulse
{ k_factor: 10.0 }` events via `pulse_tx`. Await
`dispenser_ctrl_rx.changed()`. Assert `solenoid_open = false` and
`pump_energised = false`.

---

#### `cumulative_totaliser_retains_count_after_auto_stop`

Following `auto_stop_triggers_at_preset_pulse_count`, read registers
0x0003–0x0004. Assert the 32-bit value equals 1000 — confirming the
cumulative totaliser is not cleared by auto-stop.

---

#### `transaction_totaliser_resets_on_new_preset_write`

Write the standard preset and enable the pump. Send 200 pulses (partial
dispense). Yield. Write a new preset value to register 0x0001. Yield.
Read registers 0x0005–0x0006. Assert the 32-bit value equals 0.

---

#### `cumulative_totaliser_not_reset_on_new_preset_write`

Write the standard preset and enable the pump. Send 200 pulses. Read
registers 0x0003–0x0004 and record the 32-bit value. Write a new preset
to register 0x0001. Yield. Read 0x0003–0x0004 again. Assert it equals
the recorded value.

---

#### `cumulative_totaliser_is_monotonically_non_decreasing_across_transactions`

Run three sequential transactions: for each, write the standard preset,
enable the pump, send exactly 1000 pulses, and await
`dispenser_ctrl_rx.changed()` to confirm auto-stop. After each auto-stop,
read registers 0x0003–0x0004. Assert the three 32-bit readings are 1000,
2000, and 3000 respectively — confirming the monotonic property required
by narrative §5 measurement integrity condition 4.

---

#### `dispenser_re_arms_after_auto_stop`

Following `auto_stop_triggers_at_preset_pulse_count`, write a new preset
to register 0x0001 and write 1 to register 0x0002. Await
`dispenser_ctrl_rx.changed()`. Assert `solenoid_open = true` and
`pump_energised = true`.

---

#### `status_flag_set_when_running_cleared_when_stopped`

Write the standard preset and enable the pump. Yield. Read register
0x0007. Assert bit 0 is set and bits 1–15 are zero. Then write 0 to
register 0x0002 (pump disable). Await `dispenser_ctrl_rx.changed()`.
Read register 0x0007 again. Assert the value is 0x0000.

---

### Serial connection

`SerialConnection` (`sim::serial`) is the transport-agnostic byte link the RFID
readers write to. Its only backend today is the in-process loopback, tested
here through the public API — `SerialConnection::loopback()`, the async
`write_all`, and `LoopbackHandle::bytes()`. `#[tokio::test]`s.

---

#### `loopback_starts_empty`

Construct via `SerialConnection::loopback()`; assert the handle's `bytes()` is
empty before any write.

---

#### `write_all_is_captured_by_handle`

Write some bytes via `write_all`; assert the handle's `bytes()` returns exactly
them.

---

#### `successive_writes_accumulate_in_order`

Two `write_all` calls; assert `bytes()` is their concatenation in order.

---

#### `empty_write_is_a_no_op`

`write_all(b"")`; assert `bytes()` is still empty.

---

#### `transports_arbitrary_bytes`

Write a slice containing non-text byte values (e.g. `0x00`, `0xFF`); assert
`bytes()` returns them verbatim — confirming the link is byte-transparent, not
text-specific.

---

### RFID reader (shared)

The vessel and operator RFID reader appliances both re-export the common
`shared::rfid_reader::spawn`, so its behaviour is tested once here, against
`sim::shared::rfid_reader::spawn`. The two appliance modules each carry a single
smoke test (below) confirming their entry point is wired.

**Test harness:**

The test creates a `card` mpsc channel and a loopback `SerialConnection`
(`SerialConnection::loopback()`) that captures all bytes written to it, then
calls `rfid_reader::spawn(card_rx, serial)`. The test body holds `card_tx` (the
presenting-actor role) and the `LoopbackHandle` to inspect written output.
Purely event-driven, so these are `#[tokio::test]`s.

---

#### `card_number_written_to_serial_on_presentation`

Send `RfidCardNumber(12345)` via `card_tx`. Yield. Assert that the
loopback contains exactly the bytes `"12345\n"`.

---

#### `each_card_presentation_produces_one_serial_write`

Send three `RfidCardNumber` events sequentially — `RfidCardNumber(1)`,
`RfidCardNumber(2)`, `RfidCardNumber(3)` — yielding after each. Assert
that the loopback contains exactly three newline-terminated tokens, one
per event, in order.

---

#### `distinct_card_numbers_produce_distinct_serial_output`

Send `RfidCardNumber(11111)` followed by `RfidCardNumber(22222)`. Assert
the two newline-terminated tokens written to the loopback are not equal.

---

### Vessel RFID reader

`vessel_rfid_reader` re-exports `shared::rfid_reader::spawn`; the reader
behaviour is covered by the shared suite above. This module carries a single
smoke test confirming its vessel-context entry point is wired.

#### `spawn_delivers_card_to_serial`

Spawn via `vessel_rfid_reader::spawn` against a loopback `SerialConnection`.
Send one `RfidCardNumber`, yield, and assert it appears on the loopback as the
expected newline-terminated decimal token.

---

### Operator RFID reader

`operator_rfid_reader` re-exports the same `shared::rfid_reader::spawn`; it
carries the mirror smoke test for its own entry point.

#### `spawn_delivers_card_to_serial`

Spawn via `operator_rfid_reader::spawn` against a loopback `SerialConnection`.
Send one `RfidCardNumber`, yield, and assert it appears on the loopback as the
expected newline-terminated decimal token.

---

## Actors

**Convention — starting states and helper functions:**

Actors follow a strict causal sequence. Unlike physical entities and
appliances, an actor's later behaviours can only be reached after earlier
steps in its lifecycle have completed. Tests for later behaviours
therefore cannot be fully independent in the same way as entity tests.

The approach used across all actor test suites is:

1. A small set of **named starting states** is defined for each actor,
   covering the key points in its lifecycle.
2. Each starting state has a corresponding **async helper function** that
   drives the actor to that state from a fresh spawn, using an
   all-success mock (no deliberate delays, no scripted failures) so the
   preamble is fast and reliable. The helper is not a test — it makes no
   assertions.
3. Each test declares its **required starting state**, calls the
   appropriate helper, then scripts only the specific behaviour under
   test and makes its assertions.

If a helper breaks, every test that depends on that state will fail
together. Earlier tests, which exercise the steps the helper covers,
will identify the root cause.

---

### Dock operator

**Test harness:**

The test obtains a `SimClock` and runtime via
`SimClock::isolated(TimeModel::Synthetic)` and runs its body under that
runtime. It creates all channels, then calls
`dock_operator::spawn(clock, job_rx, esd_tx, operator_card_tx,
ticket_rx, bdn_sign_tx, bdn_signed_rx, recon_tx, local_addr, hmi_addr)`
using a free port on `127.0.0.1` as `hmi_addr`. The test body holds all
counterpart channel ends: `job_tx`, `esd_rx`, `ticket_tx`, `bdn_sign_rx`,
`bdn_signed_tx`, `recon_rx`.

The test body runs a mock HMI HTTP server on `hmi_addr` that captures
the sequence of inbound requests and returns scripted responses. In
helpers, the mock returns the minimum valid success response for every
request immediately, with no deliberate delays.

**Test jobs used in helpers and tests:**

| Name | Type | Fields |
|------|------|--------|
| `TEST_DISPENSE_JOB` | `DispenseJob` | `vessel_id: VesselId(1999997)`, `vessel_name: "Tug Alpha"`, `volume_L: 500.0`, `rfid_card: RfidCardNumber(99999)` |
| `TEST_FILL_JOB` | `DockJob::Fill` contents | `delivery_volume_L: 10000.0`, `tanker_registration: "AB12 CDE"` |

---

**Starting states:**

#### `LoggedIn`

The actor has completed the two-phase login and holds a valid session
token. All subsequent HTTP requests carry `Authorization: Bearer <token>`.

*Helper steps:*
1. Mock returns 202 + `challenge_id` to `POST /api/auth/login`.
2. Read `operator_card_tx` to drain the card number sent by the actor.
3. Mock returns 200 + token to `GET /api/auth/challenge/{challenge_id}`.
4. Yield to allow the actor to store the token.

---

#### `DispenseJobActive`

Reached from `LoggedIn`. A dispense job has been created in the FMS and
the proceed signal has been fired to the tug engineer.

*Helper steps:*
1. Reach `LoggedIn`.
2. Send `DockJob::Dispense(TEST_DISPENSE_JOB, proceed_tx)` on `job_tx`.
3. Mock returns 201 + `job_id` (status `pending_vessel_rfid`) to
   `POST /api/jobs/refuel`.
4. Await the proceed oneshot to fire.

---

#### `DispenseRfidValidated`

Reached from `DispenseJobActive`. The vessel RFID tag has been validated
by the FMS; the job status is `rfid_validated`.

*Helper steps:*
1. Reach `DispenseJobActive`.
2. Mock returns status `rfid_validated` to the first
   `GET /api/jobs/current` poll.
3. Advance the clock by one poll interval to trigger the poll.
4. Yield to allow the actor to process the response.

---

#### `DispensingInProgress`

Reached from `DispenseRfidValidated`. The preset has been set and the
pump has been started.

*Helper steps:*
1. Reach `DispenseRfidValidated`.
2. Mock returns 200 + status `dispensing` to
   `POST /api/jobs/{job_id}/dispenser/start`.
3. Yield.

---

#### `DispenseComplete`

Reached from `DispensingInProgress`. The dispenser has auto-stopped and
the totaliser reports the dispense is complete.

*Helper steps:*
1. Reach `DispensingInProgress`.
2. Mock returns status `complete` to the first
   `GET /api/jobs/current/dispenser/totaliser` poll.
3. Advance the clock by one poll interval to trigger the poll.
4. Mock returns the job, status `awaiting_bdn` and a non-null `opened_at`, to
   `GET /api/jobs/current`.
5. Mock returns a valid level reading, at 15 °C, to `GET /api/tanks/01/level`.
6. Yield to allow the actor to process the response.

---

#### `FillJobActive`

Reached from `LoggedIn`. A fill job has been created in the FMS and the
proceed signal has been fired to the road tanker driver.

*Helper steps:*
1. Reach `LoggedIn`.
2. Send `DockJob::Fill` carrying `TEST_FILL_JOB` and `proceed_tx` on `job_tx`.
3. Mock returns a valid level reading to `GET /api/tanks/01/level`.
4. Mock returns 201 + `job_id` (status `open`) to
   `POST /api/jobs/tank-fill`.
5. Await the proceed oneshot to fire.

---

#### Tests

##### `login_sends_rfid_card_after_password_confirmation`

**Starting state:** fresh spawn (no helper).

Script the mock to respond to `POST /api/auth/login` with 202 +
`challenge_id` but hold `GET /api/auth/challenge/{challenge_id}` open.
Assert `operator_card_tx` receives an `RfidCardNumber` before the
long-poll resolves — confirming the actor presents its card in response
to the challenge and not before.

---

##### `dispense_job_fires_proceed_after_hmi_confirms_active`

**Starting state:** `LoggedIn`.

Send `DockJob::Dispense(TEST_DISPENSE_JOB, proceed_tx)` on `job_tx`.
Script the mock to return 201 from `POST /api/jobs/refuel`. Assert the
proceed oneshot fires.

---

##### `pump_start_deferred_until_rfid_validated`

**Starting state:** `DispenseJobActive`.

Script `GET /api/jobs/current` to return `pending_vessel_rfid` on the
first poll. Advance the clock by one poll interval. Script the second
response to return `rfid_validated`. Assert `POST
/api/jobs/{job_id}/dispenser/start` is only sent after the
`rfid_validated` response.

---

##### `bdn_deferred_until_dispense_complete`

**Starting state:** `DispensingInProgress`.

Script `GET /api/jobs/current/dispenser/totaliser` to return
`dispensing` on the first poll. Advance the clock by one poll interval.
Script the second response to return `complete`. Assert `bdn_sign_tx`
receives a BDN only after the `complete` response.

---

##### `dispense_job_sends_bdn_for_signing_on_completion`

**Starting state:** `DispenseComplete`.

Assert `bdn_sign_tx` receives a `BDN` with `vessel_id`, `vessel_name` and `volume_L`
matching `TEST_DISPENSE_JOB`.

---

##### `bdn_number_entered_via_hmi_after_cosigning`

**Starting state:** `DispenseComplete`.

Drain `bdn_sign_rx` and send a co-signed copy back on `bdn_signed_tx`
with a known `bdn_number`. Assert the mock HMI receives
`PATCH /api/jobs/{job_id}` carrying that exact `bdn_number`.

---

##### `dispense_job_posts_reconciliation_record_after_bdn_returned`

**Starting state:** `DispenseComplete`.

Drain `bdn_sign_rx` and send a co-signed BDN back on `bdn_signed_tx`.
Assert `recon_rx` yields a `ReconciliationRecord::Bdn` containing that
BDN.

---

##### `the_bdn_records_commencement_not_creation`

**Starting state:** `DispensingInProgress`, on a clock starting at 23:58 on the
last day of a month, with the mock's current job reporting `opened_at` as
23:58:00.123456789 that day.

Script the totaliser poll to return `dispensing` until the clock has passed
midnight, then `complete`. Assert the BDN arrives on the first day of the next
month and its `delivery_commenced_at` equals the mock's `opened_at` to the
nanosecond. The BDN is constructed at completion, so an operator reading the
clock there writes the completion time into a field named for commencement; and
the nanoseconds are not the sim clock's, so an operator reading its own clock at
the start cannot pass either (`sim.md` §8.3).

---

##### `the_bdn_commencement_is_the_fms_start_after_a_retried_start`

**Starting state:** `DispenseRfidValidated`, with the mock's current job
reporting `opened_at` once the job is `dispensing`.

Script the first `POST /api/jobs/{job_id}/dispenser/start` to commit without a
response reaching the actor, and the retry to return 409 "job is not in active
state". Drive the dispense to completion and assert the BDN's
`delivery_commenced_at` equals the mock's `opened_at`: a lost start response
leaves the commencement unchanged, since it is read from the job, not from the
start response.

---

##### `a_completed_dispense_without_an_fms_start_is_fatal`

**Starting state:** `DispensingInProgress`, with the mock's current job reporting
`opened_at` as `null`.

Script the totaliser poll to return `complete`. Assert the actor panics, as for
any malformed response, and that no BDN is sent for signing.

---

##### `the_bdn_carries_the_appendix_v_fields`

**Starting state:** `DispenseComplete`.

Assert each Appendix V field (`sim.md` §8.2): `grade`, `sulphur_pct_mm` and `flashpoint_C` equal their compiled world constants; `density_kg_per_m3` equals the density constant × 1000; `quantity_t` equals `temp_corrected_volume_L` × the density constant ÷ 1000; `port` and the three supplier fields hold the "Ravensport Harbour Authority's" details; `sulphur_limit_declared` and `signed_by_operator` are `true`; `operator_id` equals the compiled operator username. 
The projected values are compared with the constants rather than with literals, so a change of fuel in `world.yaml` does not break the test and a literal in the actor does.

---

##### `the_bdn_temperature_is_read_from_the_hmi`

**Starting state:** `DispenseComplete`, with the mock's level reading reporting
17.5 °C.

Assert the BDN's `temperature_C` is 17.5 and that the mock received
`GET /api/tanks/01/level` after the `complete` totaliser response. 17.5 °C is
deliberately not the modelled 15 °C, so a constant in the actor cannot pass.
`temp_corrected_volume_L` is not asserted here: no thermal model is defined
away from 15 °C (`sim.md` §1.3).

---

##### `level_checked_before_fill_job_created`

**Starting state:** `LoggedIn`.

Send `DockJob::Fill` carrying `TEST_FILL_JOB` on `job_tx`. Assert the
mock HMI receives `GET /api/tanks/01/level` before
`POST /api/jobs/tank-fill`.

---

##### `fill_job_fires_proceed_after_hmi_confirms_active`

**Starting state:** `LoggedIn`.

Send `DockJob::Fill` carrying `TEST_FILL_JOB` on `job_tx`. Script the
mock to return a valid level and return 201 from
`POST /api/jobs/tank-fill`. Assert the proceed oneshot fires.

---

##### `the_fill_job_names_the_arriving_tanker`

**Starting state:** `FillJobActive`.

Assert the `POST /api/jobs/tank-fill` body carries `tanker_registration`
`"AB12 CDE"`, the registration `DockJob::Fill` carried. The job's counterparty
is the tanker delivering, the one whose registration is printed on its ticket
(`narrative.md` §2.1), so a registration of the operator's own would make every
clean fill disagree with its paper.

---

##### `fill_job_posts_reconciliation_record_after_ticket_received`

**Starting state:** `FillJobActive`.

Send a `DeliveryTicket` on `ticket_tx`. Assert `recon_rx` yields a
`ReconciliationRecord::DeliveryTicket` containing that ticket.

---

##### `jobs_processed_sequentially`

**Starting state:** `LoggedIn`.

Send two `DockJob::Dispense` entries on `job_tx` without waiting between
them. For the first job, script the mock to require two poll cycles each
for RFID validation and totaliser polling (clock advances required). Assert
the second proceed oneshot does not fire until the first job's
`PATCH /api/jobs/{job_id}` has been received by the mock — confirming
the actor processes one job at a time.

---

##### `waiting_fill_taken_before_waiting_dispense`

**Starting state:** `DispenseJobActive`.

With the first dispense under way, send a second `DockJob::Dispense` and then
a `DockJob::Fill` on `job_tx`, in that order. Script the mock through the rest
of the first dispense: RFID validation, completion and the BDN exchange.
Assert that the first dispense's `PATCH /api/jobs/{job_id}` reaches the mock
before any further create — the job in progress is completed first — and that
the next create is the fill's `POST /api/jobs/tank-fill`, before the second
dispense's `POST /api/jobs/refuel`, although the dispense was sent first: a
road tanker waiting to deliver is handled before a waiting tug (`sim.md` §6).

---

##### `waiting_dispenses_taken_in_order_received`

**Starting state:** `DispenseJobActive`.

With the first dispense under way, send two further `DockJob::Dispense`
entries, for different vessels, on `job_tx`. Script the mock through every
dispense. Assert that their `POST /api/jobs/refuel` requests reach the mock in
the order the jobs were sent: waiting tugs are handled in the order in which
they arrived.

---

##### `refused_create_stands_tug_down_and_operator_continues`

**Starting state:** `LoggedIn`.

Script the mock to return 422 with a cavitation-threshold body from
`POST /api/jobs/refuel`. Send `DockJob::Dispense(TEST_DISPENSE_JOB,
proceed_tx)`. Assert the proceed oneshot yields `JobAdmission::Refused`, that no
`POST /api/jobs/{job_id}/dispenser/start` reaches the mock, and that a
second dispense job then completes normally (the operator loop survives
the refusal).

---

##### `persistent_atg_unavailable_503_refuses_create_without_panic`

**Starting state:** `LoggedIn`.

Script the mock to return 503 with the code `atg_read_failed` ("cavitation check cannot be evaluated") from `POST /api/jobs/refuel` for every attempt. Assert the proceed oneshot yields `JobAdmission::Refused` after the initial attempt plus one re-POST per backoff, and that no `GET /api/jobs/current` reconciliation poll is made — a 503 is a definite no-op per `fms.md §7.4`, not an ambiguous outcome.

---

##### `refused_start_cancels_job_and_hands_refusal_to_engineer`

**Starting state:** `LoggedIn`.

Script the mock to return 422 with a projected-closing-volume body from
`POST /api/jobs/job-1/dispenser/start`. Assert the proceed oneshot
yields `JobAdmission::Active` (the job was created), that `DispenseHandoff::Refused`
arrives on `bdn_sign_rx`, that exactly one `DELETE /api/jobs/job-1`
reaches the mock (the refused job frees the single job slot), and that a
subsequent dispense completes normally.

---

##### `a_cancel_of_a_job_no_longer_current_hands_over_the_refusal`

**Starting state:** `LoggedIn`.

Script the mock to return 422 carrying `cavitation_threshold` from
`POST /api/jobs/job-1/dispenser/start`, and 404 from `DELETE /api/jobs/job-1`.
Assert that `DispenseHandoff::Refused` arrives on `bdn_sign_rx` after one
`DELETE`: a job that is no longer the current one is the state the cancel seeks.

---

##### `a_cancel_that_fails_through_the_retry_schedule_is_fatal`

**Starting state:** `LoggedIn`.

Script the mock to return 422 carrying `cavitation_threshold` from
`POST /api/jobs/job-1/dispenser/start`, and HAProxy's 503 page from
`DELETE /api/jobs/job-1` for every attempt. Assert the proceed oneshot yields
`JobAdmission::Active`, that the operator stops with no handoff on `bdn_sign_rx`,
and that the initial attempt plus one retry per backoff reaches the mock. A failed
cancel leaves the refused job holding the single job slot, so it is fatal where it
happens rather than at the next create.

---

##### `an_unexpected_status_on_the_cancel_is_fatal`

**Starting state:** `LoggedIn`.

Script the mock to return 422 carrying `cavitation_threshold` from
`POST /api/jobs/job-1/dispenser/start`, and 409 from `DELETE /api/jobs/job-1`.
Assert the operator stops with no handoff on `bdn_sign_rx`, after one `DELETE`.

---

##### `persistent_503_without_the_fms_code_on_create_is_fatal`

**Starting state:** `LoggedIn`.

Script the mock to return HAProxy's 503 page, which carries no code, from `POST /api/jobs/refuel` for every attempt, and 404 from each `GET /api/jobs/current`. Assert the operator stops after the initial attempt plus one re-POST per backoff, each followed by a reconciliation poll: the proceed oneshot is dropped without an admission.

---

##### `persistent_503_without_the_fms_code_on_start_is_fatal`

**Starting state:** `LoggedIn`.

Script the mock to return HAProxy's 503 page from `POST /api/jobs/job-1/dispenser/start` for every attempt. Assert the proceed oneshot yields `JobAdmission::Active`, that the operator stops with no handoff on `bdn_sign_rx`, and that no `DELETE /api/jobs/job-1` reaches the mock.

---

##### `no_reading_503_skips_the_ullage_check_and_the_fill_proceeds`

**Starting state:** `LoggedIn`.

Script the mock to return 503 with the code `no_atg_reading` from `GET /api/tanks/01/level` for every attempt. Assert the fill's proceed oneshot fires and `POST /api/jobs/tank-fill` reaches the mock, because an unavailable reading must not block a delivery (`digital.md` §4.3, Fill transaction lifecycle).

---

##### `ullage_503_without_the_fms_code_is_fatal`

**Starting state:** `LoggedIn`.

Script the mock to return HAProxy's 503 page from `GET /api/tanks/01/level` for every attempt. Assert the operator stops before any `POST /api/jobs/tank-fill`: the fill's proceed oneshot is dropped.

---

##### `persistent_atg_unavailable_503_refuses_start_and_cancels_job`

**Starting state:** `LoggedIn`.

Script the mock to return 503 with the code `atg_read_failed` from `POST /api/jobs/job-1/dispenser/start` for every attempt. Assert the proceed oneshot yields `JobAdmission::Active`, that `DispenseHandoff::Refused` arrives on `bdn_sign_rx` after the initial attempt plus one retry per backoff, and that exactly one `DELETE /api/jobs/job-1` reaches the mock.

---

### Road tanker driver

**Construction parameters used in tests:**

| Parameter | Value |
|-----------|-------|
| `pump_rate_L_per_min` | 600 L/min |
| `driver_id` | `"driver-01"` |
| `tanker_registration` | `"AB12 CDE"` |

**Test harness:**

The test obtains a `SimClock` and runtime via
`SimClock::isolated(TimeModel::Synthetic)` and runs its body under that
runtime. It creates all channels, then calls
`road_tanker_driver::spawn(clock, pump_rate_L_per_min, driver_id,
tanker_registration, arrival_rx, dock_job_tx, tanker_pump_tx,
interlock_plug_tx, ticket_tx)`. The test body holds `arrival_tx` (fuel
supplier role: sends `FillJob` events), `dock_job_rx` (dock operator
role: receives `DockJob::Fill` and fires the proceed oneshot),
`tanker_pump_rx`, `interlock_plug_rx` (fuel tank role: receives each
`InterlockPlug` and replies with a `watch::Receiver<InterlockCircuit>`),
and `ticket_rx`.

The `tanker_pump` watch channel is initialised to `false`. When servicing an
`InterlockPlug`, the test creates a `watch::channel(InterlockCircuit::Live)`
(or `Dead` where the scenario requires), replies with the receiver, and keeps
the sender to drive the circuit `Dead` mid-fill.

**Test fill job:** `FillJob { delivery_volume_L: 3000.0, completed }`, the
test keeping the `completed` oneshot's receiver (`completed_rx`) in the fuel
supplier's place. At 600 L/min this takes exactly 5 minutes (300 seconds) of
simulated time.

---

**Starting states:**

#### `FillJobForwarded`

A `FillJob` has been received on `arrival_rx` and forwarded as
`DockJob::Fill` on `dock_job_tx`. The actor is blocking on the proceed
oneshot.

*Helper steps:*
1. Send `FillJob { delivery_volume_L: 3000.0, completed }` on `arrival_tx`,
   retaining `completed_rx`.
2. Receive the `DockJob::Fill` from `dock_job_rx`, retaining the
   `proceed_tx` oneshot sender.
3. Yield to allow the actor to reach the await point.

---

#### `FillInProgress`

The proceed signal has been received, the driver has plugged in against a
`Live` circuit, and `tanker_pump_tx = true`. The clock is at the instant the
pump started, and the test holds the circuit's `watch::Sender<InterlockCircuit>`.

*Helper steps:*
1. Reach `FillJobForwarded`.
2. Fire the proceed oneshot.
3. Receive the `InterlockPlug` on `interlock_plug_rx`; reply with a receiver
   for a `watch::channel(InterlockCircuit::Live)`, retaining the sender.
4. Await `tanker_pump_rx.changed()` to confirm the pump started.

---

#### Tests

##### `fill_job_forwarded_to_dock_operator_on_arrival`

**Starting state:** fresh spawn.

Send `FillJob { delivery_volume_L: 3000.0 }` on `arrival_tx`. Assert
`dock_job_rx` receives a `DockJob::Fill` carrying `delivery_volume_L = 3000.0`
and the driver's own `tanker_registration`, `"AB12 CDE"`: the registration it
prints on its ticket.

---

##### `interlock_plugged_in_after_proceed`

**Starting state:** `FillJobForwarded`.

Fire the proceed oneshot. Assert an `InterlockPlug` is received on
`interlock_plug_rx` — the driver plugs in before pumping.

---

##### `tanker_pump_starts_after_proceed`

**Starting state:** `FillJobForwarded`.

Fire the proceed oneshot. Service the `InterlockPlug`, replying with a `Live`
circuit. Await `tanker_pump_rx.changed()`. Assert `*tanker_pump_rx.borrow() ==
true`.

---

##### `pump_starts_only_after_interlock_established`

**Starting state:** `FillJobForwarded`.

Fire the proceed oneshot. Assert the `InterlockPlug` arrives on
`interlock_plug_rx` before any `tanker_pump_rx` change: service the plug
(reply `Live`) only after confirming no pump change has occurred, then await
`tanker_pump_rx.changed()` — confirming the interlock circuit is established
before the pump starts.

---

##### `pump_never_starts_when_interlock_dead_at_plug_in`

**Starting state:** `FillJobForwarded`.

Fire the proceed oneshot. Service the `InterlockPlug`, replying with a `Dead`
circuit (the tank is at or above the overfill interlock level). Assert `tanker_pump_rx` never goes
`true`, and that the `DeliveryTicket` on `ticket_rx` has `gross_volume_L =
0.0` — the pump controller refuses to start into a full tank.

---

##### `tanker_pump_stops_after_delivery_volume_reached`

**Starting state:** `FillInProgress`.

Advance the clock by 300 seconds (3 000 L ÷ 600 L/min). Await
`tanker_pump_rx.changed()`. Assert `*tanker_pump_rx.borrow() == false`.

---

##### `interlock_released_after_pump_stops`

**Starting state:** `FillInProgress`.

Advance the clock by 300 seconds. Await `tanker_pump_rx.changed()` to confirm
the pump has stopped. Assert the held circuit sender then reports no receivers
(`receiver_count() == 0`) — the driver drops its interlock receiver, unplugging
the cable, only after the pump is off.

---

##### `delivery_ticket_sent_with_correct_fields`

**Starting state:** `FillInProgress`.

Advance the clock by 300 seconds. Await `ticket_rx`. Assert the received
`DeliveryTicket` has: `gross_volume_L = 3000.0`, `driver_id = "driver-01"`,
`tanker_registration = "AB12 CDE"`, `signed_by_driver = true`,
`signed_by_operator = false`.

---

##### `interlock_trip_cuts_pump_mid_fill_with_actual_volume`

**Starting state:** `FillInProgress`.

Advance the clock by 150 seconds (half the 300 s fill). Drive the held circuit
sender to `InterlockCircuit::Dead`. Await `tanker_pump_rx.changed()`; assert
`*tanker_pump_rx.borrow() == false` — the pump controller cuts the pump. Await
`ticket_rx`; assert `gross_volume_L = 1500.0` (600 L/min × 2.5 min), the actual
metered volume, not the ordered 3 000 L.

---

##### `completion_signalled_on_every_outcome`

Three cases, each from a fresh spawn: the full volume delivered
(`FillInProgress`, then advance the clock by 300 seconds); the delivery stopped
part-way (`FillInProgress`, then drive the held circuit `Dead` after 150
seconds); the pump never started (`FillJobForwarded`, then fire the proceed
oneshot and reply to the `InterlockPlug` with a `Dead` circuit). In each,
assert that `completed_rx` resolves, and only after the `DeliveryTicket` has
been sent on `ticket_rx`. The fuel supplier's pending order ends on this signal
(`sim.md` §5.3), so an outcome that did not fire it would leave the supplier
unable to order again.

---

##### `sequential_fill_jobs_processed_one_at_a_time`

**Starting state:** fresh spawn.

Send two `FillJob` entries on `arrival_tx` without waiting between them.
For the first job, receive its `DockJob::Fill` from `dock_job_rx`, fire the
proceed oneshot, and service the `InterlockPlug` with a `Live` circuit.
Advance the clock by 300 seconds. Assert `ticket_rx` receives the first
ticket. Only then receive the second `DockJob::Fill` from `dock_job_rx` —
confirming the actor does not forward the second job until the first fill is
complete.

---

### Tug engineer

**Test harness:**

The test creates all channels and calls `tug_engineer::spawn(job_rx,
dock_job_tx, card_tx, bdn_rx, bdn_tx)`. No clock is required — the tug
engineer is purely event-driven. The test body holds: `job_tx` (tug
arrival generator role: sends `TugArrival`, keeping each arrival's
`departed_rx`), `dock_job_rx` (dock
operator role: receives `DockJob::Dispense` and fires the proceed
oneshot), `card_rx` (vessel RFID reader role: receives `RfidCardNumber`),
`bdn_to_engineer_tx` (dock operator role: sends operator-signed `BDN`
into the actor's `bdn_rx`), and `bdn_from_engineer_rx` (dock operator
role: receives co-signed `BDN` from the actor's `bdn_tx`).

**Test dispense job:**
`DispenseJob { vessel_id: VesselId(1999997), vessel_name: "Tug Alpha", volume_L: 500.0, rfid_card: RfidCardNumber(99999) }`,
sent as `TugArrival { job, departed }`. Where a test needs a second vessel, it
uses `VesselId(1999985)`, `"Tug Bravo"` and `RfidCardNumber(99998)`.

---

**Starting states:**

#### `DispenseJobForwarded`

A `TugArrival` has been received on `job_rx` and its `DispenseJob` forwarded
as `DockJob::Dispense` on `dock_job_tx`. The actor is blocking on the
proceed oneshot.

*Helper steps:*
1. Send the test arrival on `job_tx`, retaining its `departed_rx`.
2. Receive the `DockJob::Dispense` from `dock_job_rx`, retaining
   `proceed_tx`.
3. Yield to allow the actor to reach the await point.

---

#### `ProceedReceived`

The proceed signal has been received and the vessel card number has been
sent to `card_tx`.

*Helper steps:*
1. Reach `DispenseJobForwarded`.
2. Fire the proceed oneshot.
3. Await `card_rx` to drain the card number, confirming it was sent.

---

#### Tests

##### `dispense_job_forwarded_to_dock_operator_on_arrival`

**Starting state:** fresh spawn.

Send the test arrival on `job_tx`. Assert `dock_job_rx` receives a
`DockJob::Dispense` whose embedded `DispenseJob` has `vessel_id =
VesselId(1999997)` and `volume_L = 500.0`.

---

##### `rfid_card_not_sent_before_proceed`

**Starting state:** `DispenseJobForwarded`.

Assert `card_rx.try_recv()` returns nothing — confirming the engineer
does not present the vessel card before the dock operator has confirmed
the job is active in the FMS.

---

##### `rfid_card_sent_after_proceed`

**Starting state:** `DispenseJobForwarded`.

Fire the proceed oneshot. Await `card_rx`. Assert an `RfidCardNumber` is
received.

---

##### `rfid_card_matches_vessel_on_job`

**Starting state:** `DispenseJobForwarded`.

Fire the proceed oneshot. Await `card_rx`. Assert the received
`RfidCardNumber` equals `RfidCardNumber(99999)` — the `rfid_card` field
from the test `DispenseJob`.

---

##### `bdn_cosigned_and_returned_after_receipt`

**Starting state:** `ProceedReceived`.

Send a `BDN` with `signed_by_operator = true`, `signed_by_engineer =
false` via `bdn_to_engineer_tx`. Await `bdn_from_engineer_rx`. Assert
the returned `BDN` has `signed_by_engineer = true`.

---

##### `bdn_fields_preserved_on_cosigning`

**Starting state:** `ProceedReceived`.

Send a `BDN` with known values for all fields via `bdn_to_engineer_tx`.
Await `bdn_from_engineer_rx`. Assert every field of the returned `BDN`
is identical to the sent value, except `signed_by_engineer` which
changes from `false` to `true`.

---

##### `waiting_tugs_served_one_at_a_time_in_arrival_order`

**Starting state:** fresh spawn.

Send two arrivals on `job_tx` without waiting between them: Tug Alpha, then
Tug Bravo. Receive the first `DockJob::Dispense` from `dock_job_rx` and assert
it is Tug Alpha's; fire its proceed oneshot. Service the card read on
`card_rx`. Send the operator-signed BDN via `bdn_to_engineer_tx` and await
`bdn_from_engineer_rx`. Assert that no second `DockJob::Dispense` has
appeared until Tug Alpha's `departed_rx` has resolved, and that the second is
Tug Bravo's — the engineer serves one tug at a time, in the order they
arrived, and forwards the next tug's job only once the tug before it has left.

---

##### `departure_signalled_after_cosigned_bdn_returned`

**Starting state:** `ProceedReceived`.

Assert `departed_rx` has not resolved. Send the operator-signed BDN via
`bdn_to_engineer_tx`. Await `bdn_from_engineer_rx`, then assert `departed_rx`
resolves: the tug leaves once both signatures are on the BDN (narrative
§2.2), and not before.

---

##### `refused_job_departs_without_presenting_card`

**Starting state:** fresh spawn.

Receive the `DockJob::Dispense` and fire its proceed oneshot with
`JobAdmission::Refused` (the FMS refused the job before activation). Assert
the arrival's `departed_rx` resolves, and the engineer advances to the next
arrival without presenting a vessel card: the tug departs unserved.

---

##### `refusal_after_activation_departs_without_cosigning`

**Starting state:** `ProceedReceived`.

Send `DispenseHandoff::Refused` via `bdn_to_engineer_tx`. Assert the
arrival's `departed_rx` resolves, and the engineer advances to the next
arrival and returns no co-signed BDN.

---

##### `dropped_departure_receiver_is_fatal`

**Starting state:** `ProceedReceived`.

Drop `departed_rx`. Complete the BDN exchange. Assert the engineer's
`JoinHandle` resolves with a panic: the tug arrival generator that awaited the
departure has stopped.

---

### Reconciliation user

Coverage is split across two locations, matching the division `recon-tests.md`
§0.2 already names:

| Location | Needs a deployment | Covers |
|---|---|---|
| `reconciliation_user/internals.rs` and `internals/` (`#[cfg(test)]`) | No | The audit logic, the transport and the audit loop: the window type, the audit calendar, the artefact timestamp, accumulation, the finding dispositions, the partitioning, grouping and checks built on them, the clipboard read, the session, the retrieval, the failure budgets, and `run` driven with the connector double |
| `infra/sim/tests/` | No | What `spawn()` does before any session: refusing a settling delay outside its bounds |
| `tests/integration/recon/` | Yes — the full digital world | The session against a real `recon-01`, and the end-to-end proof |

- The audit logic takes typed records and typed paper artefacts and returns discrepancies; it does not know its input arrived over a clipboard and so does not need the full digital stack.
- **The in-module tests are an exception to this document's convention** of testing through `spawn()` only. Through `spawn()`, every audit test needs the session and clipboard doubles, so the audit logic could not be tested until the transport exists. Testing it in-module lets it be built and verified first, and a transport failure cannot then present as a failure to reconcile.
- **The loop is driven through `run`, in-module, rather than through `spawn()`.** `spawn()` takes socket addresses and builds the IronRDP connector, so it cannot be handed the connector double; `run` takes the connector (`sim-impl.md`, "The audit loop"). The tests specified below as through `spawn()` were written before the transport existed. Each is either covered by an in-module test written since, or carried into the in-module "Audit loop" group; the table after them maps every one.

---

**Test harness:**

The actor takes a `clock`, so each test is a plain `#[test]` running its body via `runtime.block_on`, per the convention at the start of this document. 
The clock is obtained with `SimClock::isolated(TimeModel::Synthetic, start)`, where `start` is a **fixed epoch chosen per test** so that calendar arithmetic is deterministic. The window bounds are calculated from `now_datetime()` (`sim-clock.md`).
Synthetic mode advances to the nearest pending deadline whenever all tasks park, so a test reaches the audit by letting simulated time run rather than by an explicit advance.
The tests that split a retrieval set `initial_window` and `width_floor` directly, the floor at one day, in place of the widths that the actor derives from the arrival rate (`sim-impl.md`, "Splitting the window").

The exact `spawn()` parameter list follows `sim-impl.md`'s "Reconciliation user" section and this harness follows it. 
Three seams are required regardless of its final shape:

1. **A clipboard backend double.** An inert recorder of protocol callbacks — it records what was requested, and returns what the test scripts: content, a refusal, a non-text format, or nothing at all. `recon-tests.md` §9 is explicit that this is what makes three of §11.2's four obligations directly unit-testable; the fourth, the size bound, is asserted at reassembly rather than at the callback.
2. **A session double** standing in for the remote-desktop client. It records the keystrokes injected as scancodes and the order of connect, terminal-open, terminal-close and disconnect, and can be scripted to fail at each.
3. **The paper channel.** The test body holds `record_tx` and posts `BDN` and `DeliveryTicket` values, playing the dock operator's role.

Retrieved records are scripted as newline-delimited JSON lines matching
`hist.md` §4.2, returned through the clipboard double. A test that needs a
retrieval to fail returns a `curl` diagnostic on the same channel, which is what
`2>&1` folded into the pipe produces.

---

**Starting states:**

#### `Accumulated`

Paper artefacts have been posted on `record_tx` and no audit has yet run.

*Helper steps:*
1. Post the artefacts the test requires on `record_tx`.
2. Yield to allow the actor to receive them.

---

#### `SessionConfirmed`

Reached from `Accumulated`. The audit has woken, the session double has accepted
a connection, a terminal has been opened, and the latch handshake has returned a
matching value.

*Helper steps:*
1. Reach `Accumulated`.
2. Let simulated time advance to the audit wakeup.
3. Script the clipboard double to return the value the actor echoed.
4. Yield to allow the actor to accept the confirmation.

---

#### `MonthRetrieved`

Reached from `SessionConfirmed`. Every sub-retrieval has returned within its
deadline and the month has been reassembled and sorted.

*Helper steps:*
1. Reach `SessionConfirmed`.
2. Script the clipboard double to return the test's record set for each
   retrieval the actor issues.
3. Yield until the actor stops issuing retrievals.

---

#### Tests

**Audit types** (in-module; no starting state)

##### `a_window_includes_its_start_and_excludes_its_end`

A window covering January 2025 contains 00:00 on 1 January and the last
nanosecond of 31 January, and contains neither 00:00 on 1 February nor the last
nanosecond of 31 December. A transfer commencing exactly at midnight belongs to
the month it opens, and to no other.

##### `the_retrieval_bounds_widen_the_window_by_the_margin_and_the_session_lifetime`

The retrieval bounds of that window with a six-hour margin run from 18:00 on
31 December less the compiled session lifetime, so that the `LOGIN` opening a
session is retrieved with its transactions, to 06:00 on 1 February. The margin
and the lifetime widen what is retrieved, not what is audited, so the window
itself is unchanged.

##### `each_artefact_reports_its_own_commencement`

A `BDN` and a `DeliveryTicket`, each wrapped as a `ReconciliationRecord`, report
their own `delivery_commenced_at` — the instant that assigns them to a window
(`sim.md` §8.3).

##### `only_a_written_disagreement_or_an_unbacked_record_is_fatal`

Exactly three kinds of finding are fatal: a record disagreeing with its paper
artefact, a record with no paper artefact, and a totaliser decrease. A paper
artefact with no record, a dispense paired by content, an inconsistency between
records, an unreadable field, an unassignable record, a missing `LOGIN`, and a
card-table change are reported (`sim-impl.md`, "Outcome").

**Audit calendar** (in-module; no starting state)

Each test uses a fixed instant, so the calendar arithmetic is deterministic.
1 January 2025 is a Wednesday.

##### `a_month_window_runs_from_the_first_to_the_first_of_the_next_month`

The month containing 12:00 on 15 January 2025 runs from 00:00 on 1 January to
00:00 on 1 February.

##### `a_week_window_runs_from_monday_to_monday`

The week containing Thursday 16 January 2025 runs from 00:00 on Monday 13 January
to 00:00 on Monday 20 January, and the week containing 00:00 on Monday 13 January
starts at that instant.

##### `a_day_window_runs_from_midnight_to_midnight`

The day containing 14:00 on 15 January 2025 runs from 00:00 on 15 January to
00:00 on 16 January.

##### `the_next_window_follows_without_a_gap_across_a_month_and_a_year_end`

For each period, the window after a window starts at its end. Across a year end:
December 2024 is followed by January 2025; the week containing Tuesday
31 December 2024 runs from Monday 30 December to Monday 6 January and is followed
by the week from 6 January; 31 December is followed by 1 January. In a leap year,
February 2024 runs to 1 March and is followed by March.

##### `a_settling_delay_outside_its_bounds_is_refused`

With a six-hour margin: a delay one nanosecond short of the shortest period of
its kind is accepted, and one equal to it is refused (28 days for a month, 7 for
a week, 1 for a day); a delay equal to the margin is accepted, and one nanosecond
short of it is refused.

##### `the_first_window_starts_no_earlier_than_the_sim`

Spawned at 14:00 on 15 January 2025, the first window of a month period runs from
that instant to 00:00 on 1 February, and of a day period from that instant to
00:00 on 16 January. The window after it is a whole period.

**Accumulation** (in-module; no starting state)

Each test partitions artefacts against the January 2025 window, from 00:00 on
1 January to the end of 31 January.

##### `an_artefact_dated_within_the_window_is_audited`

A `BDN` commencing at 00:00 on 1 January and a `DeliveryTicket` commencing at the
last nanosecond of 31 January are both returned for this audit, and nothing is
held.

##### `an_artefact_dated_after_the_window_is_held`

Artefacts commencing at 00:00 on 1 February and at 12:00 on 7 February are held,
and none is returned for this audit.

##### `a_held_artefact_is_audited_in_the_next_window`

An artefact held from January, partitioned with February's newly drained
artefacts against the window after January, is returned for February's audit.

##### `an_artefact_dated_before_the_window_is_fatal`

An artefact commencing at the last nanosecond of 31 December panics with a
message naming its document number. The settling-delay bounds make it impossible
on a correct run (`sim-impl.md`, "Accumulation and partitioning").

**Records and grouping** (in-module; `sim-impl.md`, "Windows, margins and grouping")

Events are written as the store returns them, every value as text. Each grouping
test groups against the January 2025 window.

##### `a_line_that_is_not_a_json_object_is_a_malformed_response`

A `curl` diagnostic, a truncated object and a JSON array each parse to
`RetrievalFailure::MalformedResponse`; a JSON object parses to an event.

##### `a_field_reads_as_its_value_or_the_reason_it_cannot`

A text field reads as its value and an RFC 3339 instant with nanoseconds as that
instant in UTC. An absent field, a number where text is expected, and text that
is not an instant each report that reason, naming no other field.

##### `the_events_of_a_transfer_spanning_the_window_end_form_one_transaction`

A dispense whose `job_opened` and `transaction_open` fall before 00:00 on
1 February and whose `transaction_closed` and `bdn_linked` fall after it is
returned as one January transaction holding all four events.

##### `a_transaction_is_assigned_by_the_opened_at_on_its_close`

A fill, which has no `transaction_open`, is assigned by the close's `opened_at`:
one commencing at 23:00 on 31 January and closing on 1 February is January's.

##### `the_close_decides_where_open_and_close_disagree`

A dispense whose open carries `opened_at` 23:58 on 31 January and whose close
carries 00:02 on 1 February is not returned for January.

##### `a_dispense_without_a_close_is_assigned_by_its_open`

A dispense with `job_opened` and `transaction_open` only, commencing on
31 January, is returned as a January transaction.

##### `a_job_that_never_began_a_transfer_is_assigned_by_its_creation`

A group of a single `job_opened` created on 31 January is returned for January as
a refused job, not as a transaction.

##### `a_cancelled_job_is_a_refused_job_assigned_by_its_creation`

A group of a `job_opened` created at 23:58 on 31 January and its `job_cancelled`
at 00:01 on 1 February is returned for January as a refused job, assigned by its
creation, and is not returned for February.

##### `a_cancel_without_its_job_opened_is_unassignable`

A group of a single `job_cancelled` is returned as unassignable, not as a refused
job: without its `job_opened` it has no instant that assigns it.

##### `creation_never_assigns_a_transfer`

A dispense created at 23:59:30 on 31 January whose transfer opens at 00:00 on
1 February is not returned for January.

##### `a_record_that_cannot_be_assigned_is_reported_once`

An event without a `job_id`, and a `bdn_linked` with no other event of its job,
are returned as unassignable by the window containing their earliest `_time`,
and not by the window after it. `FindingKind::UnassignableRecord` is not fatal.

##### `a_transaction_outside_the_window_is_discarded_after_grouping_in_module`

Transactions commencing in the margins either side of January are not returned.

**Pairing and checks** (in-module; `sim-impl.md`, "Pairing", "Checks" and "Outcome")

Each test starts from one clean January window, a completed dispense and a fill
with their paper artefacts and the `LOGIN` records of their sessions, and changes
one thing. The tests assert the kind of each finding returned; the panic on a
fatal kind is tested through `spawn()`.

##### `the_clean_window_yields_no_finding`

The clean window, with a second dispense continuing the first's totaliser,
returns no finding: every artefact pairs and every check passes.

*Pairing*

##### `a_bdn_pairs_with_the_dispense_that_links_its_number`

The BDN pairs with the dispense whose `bdn_linked.bdn_number` is its number, and
not with another dispense in the window.

##### `a_ticket_pairs_with_the_fill_that_closes_with_its_number`

The delivery ticket pairs with the fill whose `transaction_closed.ticket_number`
is its number.

##### `a_dispense_whose_link_was_lost_pairs_by_content`

With its `bdn_linked` removed, the dispense pairs with its BDN by vessel,
commencement and volume. The audit returns a lost link, which is not fatal, and
the pair is still checked: altering the paired BDN's grade as well returns a
disagreement.

##### `content_pairing_requires_the_exact_commencement`

With its `bdn_linked` removed and the BDN's commencement one nanosecond later
than the close's `opened_at`, the dispense pairs with nothing: a record with no
paper, fatal. The BDN is a paper artefact with no record.

##### `a_link_naming_an_undrained_document_is_a_record_without_paper`

A `bdn_linked` naming a BDN number no drained artefact carries returns a record
with no paper, fatal.

##### `a_dispense_that_never_completed_needs_no_paper`

A dispense with `job_opened` and `transaction_open` only, and no BDN, returns an
open with no close, which is not fatal, and not a record with no paper.

##### `an_artefact_with_no_transaction_is_reported`

A BDN and a delivery ticket whose transactions were not retrieved each return a
paper artefact with no record, which is not fatal.

*Checks*

##### `every_copied_bdn_field_must_equal_the_record`

Altering each in turn of the BDN's vessel, grade, operator, commencement (by one
nanosecond), temperature, density and corrected volume returns a disagreement,
fatal. Density is compared in kg/m³ on the BDN against kg/L × 1000 on the record.

##### `every_transcribed_ticket_field_must_equal_the_record`

Altering each in turn of the ticket's commencement, grade, delivered volume,
temperature, density, corrected volume and tanker registration returns a
disagreement, fatal; so does a fill whose `counterparty` is not the ticket's
tanker registration. The supplier is not compared: the ticket does not carry it,
and the dock operator adds it at transcription (`narrative.md` §2.1).

##### `the_bdn_volume_is_within_half_a_percent_of_the_totaliser_delta`

The totaliser delta is computed with the compiled K-factor. A BDN volume 0.5%
above the delta passes; one just beyond 0.5% returns a disagreement, fatal.

##### `the_atg_delta_is_compared_with_the_meter_within_one_percent`

On a dispense and on a fill, an ATG delta 1% from the corrected meter volume
passes; one just beyond 1% returns an inconsistency between records, which is
not fatal.

##### `the_preset_is_compared_with_the_delivered_volume_within_half_a_percent`

A delivered volume 0.5% from `preset_volume_l` passes; one just beyond returns
an inconsistency between records, which is not fatal.

##### `a_totaliser_decrease_is_fatal`

A dispense whose `end_totaliser_pulses` is below its `start_totaliser_pulses`,
and a second dispense whose start is below the first's end, ordered by
`opened_at`, each return a totaliser decrease, fatal.

##### `a_totaliser_gap_is_reported`

A second dispense whose start is above the first's end returns an inconsistency
between records, which is not fatal.

##### `overlapping_transactions_are_reported`

A fill whose `opened_at`–`closed_at` interval overlaps the dispense's returns an
inconsistency between records, which is not fatal.

##### `a_session_resolves_to_a_login_under_its_prefixed_name`

The transactions' `session_id` resolves to a `LOGIN` carrying it as
`fms@99999.session_id`. With that `LOGIN` removed, a missing `LOGIN` is returned,
which is not fatal; a `LOGIN` carrying the value as an unprefixed `session_id`
does not resolve it.

##### `an_unreadable_field_is_reported`

A dispense close without its `atg_volume_at_close_l`, and one whose
`temperature_c` is not a number, each return an unreadable field, which is not
fatal, and no disagreement: the comparison that needed the field cannot be made.

##### `card_table_changes_in_the_window_are_reported_as_context`

A `CARD-CHANGE` in the window returns a card-table change, which is not fatal; one
in the margin returns nothing.

**Outcome and report** (in-module; `sim-impl.md`, "Outcome" and "Reporting")

Each test reconciles a window built as for "Pairing and checks" and takes its
outcome. The tests assert what the outcome carries; that a clean outcome is
emitted at INFO, findings at WARN and a fatal one by `panic!` is tested through
`spawn()`.

##### `a_clean_audit_reports_its_counts_and_margins`

The clean window, with one refused job added, gives a clean outcome carrying the
window, two artefacts reconciled, the records retrieved, the retrievals used, one
refused job, and a margin for each numeric check.

##### `a_margin_is_the_largest_deviation_as_a_fraction_of_its_tolerance`

Two dispenses whose BDN volumes are 501 L and 502 L against deltas of 500 L give a
BDN-volume margin of 0.8, the larger of 0.4 and 0.8. An ATG delta of 505 L
against 500 L gives an ATG margin of 1. A window with no dispense has no
BDN-volume or preset margin.

##### `reported_findings_make_one_summary_naming_each_subject`

A window with a missing `LOGIN` and two card-table changes gives a findings
outcome carrying, for each kind, its count and the jobs or cards it names, with
the margins and the refused jobs. It is one outcome, however many findings.

##### `unmatched_artefacts_are_summarised_as_runs`

Of five artefacts in commencement order, the second alone and the fourth and
fifth together without their records give three unmatched artefacts in two
runs, the longest of two. All five unmatched give one run of five.

##### `a_fatal_finding_makes_the_outcome_fatal`

A window with a disagreement, a record with no paper and a missing `LOGIN` gives
a fatal outcome whose message names the window, the disagreeing document and the
job with no paper, and counts the one missing `LOGIN`. A window with only
reported findings is never fatal.

**Summary entry** (in-module; `sim-impl.md`, "Reporting")

Each test takes the summary entry of an outcome built as for "Outcome and
report". The append itself is tested through `spawn()`.

##### `a_clean_audit_summary_states_the_period_and_agreement`

The clean window's entry is exactly
`2025-01-01T00:00:00Z 2025-02-01T00:00:00Z agreed`.

##### `a_summary_names_the_documents_that_did_not_agree`

A window whose BDN was paired by content and whose delivery ticket has no
record gives `… disagreed BDN-000001 DT-000001`: each document once, in sorted
order.

##### `a_finding_without_a_document_names_nothing`

A window with a missing `LOGIN` alone gives `… disagreed` with no identifier
after it: the job is not named.

##### `a_card_table_change_alone_leaves_the_records_agreed`

A window with a card-table change alone gives `… agreed`.

##### `a_fatal_audit_writes_no_summary`

A window with a disagreement gives no entry.

##### `a_summary_is_one_line_of_characters_a_shell_passes_unchanged`

Every entry above is one line, and holds only letters, digits, `-`, `:` and the
space.

**Clipboard** (in-module; `sim-impl.md`, "Reading the clipboard")

The backend, the reassembly guard and the read are tested without a session:
each test plays the workstation's side by calling the backend's callbacks or
offering chunk headers. The guard takes the ceiling as a parameter, so its tests
use a small one; one test fixes the deployed value. Driving them through a real
session is tested with the session.

##### `the_channel_bound_follows_from_the_ceiling`

The channel bound for a ceiling of 1,048,575 bytes is 2,097,160; for a ceiling of
10, it is 30. The deployed ceiling is 1,048,575, the figure `recon.md` §11.2
records.

##### `a_declared_length_over_the_bound_is_refused_before_accumulation`

With a ceiling of 10, a first chunk declaring 31 bytes is refused with
`DeclaredLengthOverCeiling` and nothing of it is accumulated; one declaring 30
is accepted.

##### `chunks_carrying_more_than_their_declaration_are_refused`

A message declaring 20 bytes whose chunks carry 12 and then 9 is refused at the
second chunk.

##### `the_client_advertises_no_file_transfer`

The backend's capabilities, sent when the channel is set up, include no
file-transfer capability.

##### `only_an_announcement_after_the_latch_is_cleared_is_fresh`

An announcement recorded before the latch is cleared is not one the read waits
for; one recorded after it is. The command that causes the copy is typed after
the clear, so its announcement always follows it.

##### `a_read_requests_unicode_text_only_after_a_fresh_announcement`

Before an announcement the read requests nothing. After one offering formats 13,
16, 1 and 7, as the workstation's service does, it requests format 13 and no
other.

##### `a_file_descriptor_format_is_refused_even_beside_text`

An announcement offering Unicode text and a file-descriptor format is refused
with `NonTextFormat`, and nothing is requested. So is one offering no Unicode
text.

##### `unicode_text_decodes_with_its_line_endings_unchanged`

The received data decodes up to its terminating null unit, with `\n` and `\r\n`
as they arrived.

##### `malformed_unicode_text_is_a_malformed_response`

An odd number of bytes, and an unpaired surrogate, are each a malformed
response.

##### `the_workstations_failure_response_is_a_typed_failure`

A failure response in place of the data resolves the read to a `Clipboard`
failure, not to empty text.

##### `data_without_a_request_in_this_read_is_refused`

Data arriving after the clear but before this read's request is refused as
content from before the read.

##### `a_read_that_never_completes_resolves_at_its_deadline`

Under the synthetic clock, a read whose copy is announced and whose data never
arrives resolves to `DeadlineReached` once `clipboard_deadline` has elapsed, and
not before.

**Session** (in-module; `sim-impl.md`, "Session")

The scancode table, the frame inspector, the TLS configuration and the session
procedure are tested without a workstation. The procedure runs against a
workstation double that records each line typed, each key chord, each read and
each disconnect in order, and returns the clipboard contents the test scripts.
The TLS tests complete a handshake on the loopback interface against
certificates the test issues from a root of its own; no remote desktop protocol
is involved. The connection's deadline is tested against a listener on the
loopback interface that accepts and never answers.

##### `the_scancode_table_covers_every_printable_ascii_character`

Every character from space to `~` has a key.

##### `the_table_follows_the_gb_layout`

`\` and `|` are `0x56`, unshifted and shifted; `#` and `~` are `0x2B`; `"` is
`0x03` shifted and `@` is `0x28` shifted.

##### `a_line_with_a_character_the_table_lacks_is_refused_before_any_key`

A line containing a tab is refused, naming the tab, and no key event is
produced for any of it.

##### `a_line_is_typed_as_presses_and_releases_ending_with_enter`

`a|B` is: `a` pressed and released; `Shift` pressed, `0x56` pressed and
released, `Shift` released; `Shift` pressed, `B` pressed and released, `Shift`
released; `Enter` pressed and released.

##### `the_terminal_is_opened_with_ctrl_alt_t`

`Ctrl` and `Alt` are held while `T` is pressed and released, then released.

##### `a_certificate_from_the_root_naming_recon_01_is_accepted_at_a_loopback_address`

A server on `127.0.0.1` presenting a certificate for `recon-01` issued by the
root completes the handshake.

##### `a_certificate_naming_another_host_is_refused`

A certificate for `recon-02` issued by the root is refused with
`CertificateVerification`.

##### `a_certificate_not_issued_by_the_root_is_refused`

A self-signed certificate for `recon-01` is refused with
`CertificateVerification`.

##### `a_workstation_that_never_answers_ends_at_the_connect_deadline`

A listener on `127.0.0.1` accepts the connection and sends nothing. Connecting
ends with `ConnectionFailed` when `connect_deadline` has elapsed on the sim
clock, which the transient policy then retries. One deadline covers the whole of
connecting, from the connection to the clipboard channel's setup; a stage outside
it would hold the audit until the proxy's inactivity timeout, which is generous
by design (`sim-network.md` §6).

##### `a_clipboard_channel_chunk_is_offered_to_the_guard`

A frame carrying data on the clipboard channel yields its chunk header: the
declared length, whether it is first or last, and the bytes it carries. A frame
on another channel, and one that is not channel data, yield none.

##### `the_latch_value_differs_between_audits_and_attempts`

The latch value of the January window's first attempt is
`latch-20250101000000-1`; its second attempt's and February's differ from it.

##### `the_session_is_confirmed_by_reading_back_the_latch_value`

The procedure clears the latch, types `echo latch-20250101000000-1 | xclip
-selection clipboard`, and reads. The value read back confirms the session; a
different value is `LatchMismatch`; a read that resolves to nothing is
`LatchAbsent`. Nothing is typed after a failed confirmation.

##### `the_summary_entry_is_appended_and_confirmed`

The procedure types `echo <entry> >> ~/reconciliation-audits.txt`, clears the
latch, types `tail -n 1 ~/reconciliation-audits.txt | xclip -selection
clipboard`, and reads. The entry read back confirms the append; anything else is
a failure to write the entry, which is not an audit failure.

##### `the_session_ends_with_exit_then_disconnect`

After the summary entry, the last line typed is `exit`, and the disconnect
follows it.

**Retrieval** (in-module; `sim-impl.md`, "Retrieval" and "Splitting the window")

The command, the reading of a response and the split are tested as functions;
the procedure that drives them runs against the workstation double of
"Session", scripted with a response per read. Responses are written as the
clipboard returns them: newline-delimited records, any `curl` diagnostic folded
in, and the completion line.

##### `the_retrieval_command_is_the_exact_line_typed`

For the span from 18:00 on 31 December 2024 to 00:00 on 7 January 2025, the
command is the line `sim-impl.md` gives, with the compiled username and
password. It sets no `limit` and no filter: the expression is the span alone.

##### `the_retrieval_command_is_typable`

The command for that span, carrying the compiled credentials, is accepted by the
scancode table in full.

##### `a_span_bound_with_a_fractional_second_carries_it`

A span starting at 09:30:15.25 on 3 January 2025 is written
`_time:[2025-01-03T09:30:15.25Z, …)`; a bound on a whole second carries no
fractional part.

##### `a_response_ending_in_status_zero_is_its_records`

Two bunkering events and a `LOGIN` record followed by `retrieval-end 0` read as
three records, the `LOGIN` among them: a record without an `event_type` is not
discarded as unreadable.

##### `a_response_of_the_completion_line_alone_is_a_span_with_no_records`

`retrieval-end 0` alone reads as no records, not as a failure.

##### `a_response_without_its_completion_line_is_incomplete`

Records with no completion line after them, and a response that is empty, are
each `Incomplete`.

##### `a_record_line_that_is_not_a_json_object_is_a_malformed_response`

Under status zero, a line before the completion line that is not a JSON object
makes the response `MalformedResponse`.

##### `a_failed_retrieval_is_classified_by_its_status`

Each row of `sim-impl.md`'s table, with the diagnostic `curl` prints for it:
statuses 6, 7 and 28 are `Unreachable`; 22 after `The requested URL returned
error: 401`, or 403, is `AuthorisationRefused`, the error body before it
notwithstanding; 22 with 502 is `Unreachable`; 60 is `CertificateVerification`;
18 is `Incomplete`; 22 with 400, and 3, are `MalformedResponse`. A refused
credential is never read as a span with no records. The diagnostic is found
before the error body, after it, and where a body without a final line feed
runs into it on one line: the order in the pipe is the order `curl` writes
them, which nothing fixes.

##### `the_first_span_is_the_initial_width_or_the_whole_retrieval`

With bounds of 7 days and 6 hours and an initial width of 7 days, the first span
is 7 days from the bounds' start. With an initial width of 14 days, it is the
whole of the bounds.

##### `the_spans_are_consecutive_and_cover_the_bounds`

Each span starts at the previous one's end, and the last ends at the bounds'
end, shorter than the width where the bounds require.

##### `a_span_reaching_its_deadline_is_halved_and_retried_from_its_start`

After a deadline on a 7-day span, the next span starts at the same instant and
is 3 days and 12 hours wide.

##### `halving_stops_at_one_day`

A 36-hour span reaching its deadline is retried at one day, not 18 hours.

##### `a_span_of_one_day_or_less_reaching_its_deadline_ends_the_split`

A one-day span, and a final span shorter than a day, each end the split with
the span named, for the fatal message.

##### `a_width_that_succeeds_is_held`

After a halving and a success, every later span has the halved width, the last
excepted.

##### `the_assembled_window_is_in_time_order`

Spans returned with their records out of order assemble into `_time` order
across the spans; a record with no readable `_time` follows every one with, in
the order received.

##### `each_sub_retrieval_clears_the_latch_before_its_command`

Over three spans, every command is preceded by its own clearing of the latch,
and each read follows its command.

##### `a_failed_sub_retrieval_ends_the_retrieval_with_its_failure`

A second span returning status 22 with 401 ends the retrieval with
`AuthorisationRefused`; no third command is typed.

##### `every_read_counts_as_a_retrieval`

A retrieval of two spans in which the first reached its deadline once reports
three retrievals used.

**Failure handling** (in-module; `sim-impl.md`, "Failure handling")

The audit procedure runs against a connector that hands out workstation doubles
and counts the connections made, on a synthetic clock, so that the backoffs are
measured in simulated time. Each double is scripted with its reads and, where a
test needs it, a refusal to open a terminal or to type a line. Unless a test says
otherwise the window is January 2025, retrieved in one span, and the
reconciliation of what is retrieved is clean.

##### `a_clean_audit_records_its_summary_and_closes`

One connection; a terminal opened and confirmed with `latch-20250101000000-1`;
the span retrieved; the summary entry appended and confirmed; `exit` typed; the
session disconnected. The procedure returns the clean outcome with the summary
entry written.

##### `a_fatal_outcome_writes_no_entry_and_does_not_close`

When the reconciliation is fatal, nothing is typed after the retrieval and the
session is not disconnected; the procedure returns the fatal outcome.

##### `a_summary_that_is_not_written_still_closes_the_session`

When the append is not confirmed, the outcome is returned with the summary entry
not written, and `exit` and the disconnect follow.

##### `a_failed_connection_is_retried_after_one_two_and_four_seconds`

Three failed connections then a successful one complete the audit, the fourth
connection made seven simulated seconds after the first. A fourth failure is
fatal, and no terminal is opened.

##### `a_certificate_failure_at_the_workstation_is_fatal_at_once`

A connection refused for its certificate is fatal after one connection: neither
retried nor restarted.

##### `an_untypable_character_is_fatal_at_once`

A line refused for a character the table lacks is fatal, with one connection and
nothing further typed.

##### `the_terminal_is_attempted_three_times_in_a_session`

Two latch reads that do not match, then one that does: three terminals opened in
one session, confirmed with the latch values ending `-1`, `-2` and `-3`, and the
retrieval follows the third.

##### `a_third_unconfirmed_terminal_restarts_the_session`

Three unconfirmed terminals: the session is disconnected, a second connection
made, and its first terminal confirmed with the latch value ending `-4`.

##### `clipboard_failures_retype_the_span_and_three_exhaust_the_budget`

Content from before the read twice, then the records: the span's command is
typed three times, the latch cleared before each, in one session. Three such
failures restart the session instead.

##### `a_guard_refusal_restarts_the_session_at_once`

A read refused for a declared length over the channel bound restarts the
session without the span's command being typed again in the first.

##### `a_deadline_at_the_floor_restarts_the_session`

A one-day span reaching its deadline restarts the session; its read is not
repeated in the first.

##### `the_restart_resumes_at_the_span_that_failed`

With the window in seven one-day spans and the third exhausting the clipboard
budget, the second session's first command after its latch is the third span's,
at one day's width, and the audit's records hold every span's, the first two
retrieved before the restart.

##### `a_retrieval_failure_is_retried_after_one_two_and_four_seconds`

Unreachable three times, then the records: the span's command typed four times
in one session, the last seven simulated seconds after the first failure, and
the audit completes. A fourth failure is fatal, with one connection made.

##### `a_successful_span_resets_the_retrieval_count`

Two spans that each fail three times before succeeding complete the audit.

##### `a_refused_credential_or_log_server_certificate_is_fatal_at_once`

Status 22 with HTTP 401, and status 60, are each fatal after one typing of the
span's command, with one connection made.

##### `a_second_exhaustion_is_fatal`

Three unconfirmed terminals in each of two sessions are fatal; so is a clipboard
exhaustion in the session a terminal exhaustion restarted. No third connection is
made.

##### `the_fatal_message_names_the_window_the_site_the_failure_and_the_restart`

The message of a second exhaustion names the January window, the terminal site,
the latch mismatch, and that the restart had been used; that of a refused
credential, the retrieval site, the refusal, and that it had not.

**Audit loop** (in-module; `sim-impl.md`, "The audit loop")

`run` is spawned on a synthetic clock with the connector double, a monthly
period, a six-hour settling delay and a two-hour margin unless a test says
otherwise, and the test body holds `record_tx`, posting paper artefacts as the
dock operator does. Each session double is scripted with the records its audit
retrieves. Log events are captured by one subscriber installed once for the
test binary and never dropped, which writes each event to a buffer of the thread
that emitted it; the isolated runtime is single-threaded, so a test reads its
own actor's events. A subscriber per test does not serve: `tracing` caches for
the whole process whether each call site is enabled, and recomputes it as
subscribers come and go, so tests running in parallel lose one another's events.
A panic is read from the task's join result, as the orchestrator reads it.

##### `each_audit_covers_its_calendar_window`

Spawned at 12:00 on 15 January 2025, the first audit's retrieval runs from 12:00
on 15 January, less the margin and the session lifetime, to 02:00 on
1 February; the second's covers February whole, widened alike. Neither is a
rolling interval ending at the moment it runs.

##### `an_audit_runs_the_settling_delay_after_its_window_ends`

For a day, a week and a month: no connection is made before the window's end
plus the settling delay, and one is made then.

##### `each_audit_opens_its_own_session`

Two audits make two connections, each opening its own terminal, confirmed with a
latch value carrying its own window's start.

##### `an_artefact_is_audited_in_the_window_its_commencement_falls_in`

A BDN commencing in January and one commencing in February, both posted before
January's audit: January's audit reconciles the first against its records,
clean, and does not count the second unmatched; February's audit reconciles the
second.

##### `a_transfer_spanning_the_period_end_is_audited_once_in_the_opening_window`

A BDN commencing at 23:58 on 31 January, its transfer's events either side of
midnight: January's audit reconciles it clean, and February's, whose margin
retrieves its later events, reports neither a record without paper nor paper
without a record.

##### `a_clean_audit_is_reported_at_info`

One INFO event carrying the window, the artefacts reconciled, the records
retrieved, the retrievals used and the margins; no WARN and no ERROR.

##### `findings_are_reported_in_one_warn_and_not_at_error`

Two BDNs whose records were not retrieved: one WARN carrying the kind, both
document numbers, one run of two, and no ERROR; the task does not panic, and the
summary entry reads `disagreed` with both numbers.

##### `a_summary_not_written_is_reported_at_warn`

An append that is not confirmed adds a WARN naming the window, beside the
audit's own result.

##### `a_fatal_outcome_panics_with_its_message`

A transaction retrieved with no paper: the task panics with the outcome's
message, and no summary entry is typed.

##### `an_unabsorbed_failure_panics_with_its_message`

A refused credential: the task panics with the failure's message, naming the
window, the retrieval site and the refusal.

##### `a_second_audit_appends_its_own_entry`

Two audits append two entries with `>>`, each naming its own window.

##### `the_actor_writes_nothing_to_disk`

No source file of the actor uses `std::fs`, `File::create`, `fs::write` or
`OpenOptions` (`sim.md` §5.4), in parity with the dock operator.

**Window and cadence** (`recon.md` §9; the actor's §1 obligations)

##### `window_is_the_previous_calendar_month_not_a_rolling_thirty_days`

**Starting state:** `Accumulated`, epoch set mid-month.

Assert the bounds the actor puts in the query string are the first and last
instants of the previous calendar month, not `now` minus thirty days.

##### `audit_runs_the_settling_delay_into_the_following_period`

**Starting state:** `Accumulated`, once for each audit period.

Assert no retrieval is issued at the period boundary, and one is issued after the
configured settling delay has elapsed.

##### `a_settling_delay_outside_its_bounds_is_refused_at_spawn`

**Starting state:** none.

Assert `spawn` with a day period panics as a startup fault, before any session is
attempted, for a one-day settling delay and for one shorter than the boundary
margin.

##### `bounds_are_explicit_and_never_relative`

**Starting state:** `SessionConfirmed`.

Assert the typed command carries `start` and `end` as absolute values. A
relative expression would let the workstation's clock decide which records are
returned.

---

**Accumulation and partitioning** (the actor's §2 obligations)

Both artefacts carry `delivery_commenced_at` (`sim.md` §8.3), and a transaction
is assigned to a window by that instant on both sides.

##### `an_artefact_is_reconciled_in_the_window_its_own_timestamp_falls_in`

**Starting state:** `Accumulated` with artefacts spanning two months.

Assert only the artefacts dated within the audited month are reconciled.

##### `an_artefact_dated_in_a_later_window_is_held_not_counted_unmatched`

**Starting state:** `Accumulated` with one artefact dated after the window ends.

Assert the audit completes clean. This is the second defence against terminating
the sim on clean data at a month boundary, so the assertion is that it does *not*
fire.

##### `a_held_artefact_is_reconciled_by_the_next_audit`

**Starting state:** `Accumulated`, run across two audit cycles.

Assert the held artefact is matched in the following window rather than dropped.

##### `the_artefact_records_commencement_not_creation`

**Starting state:** `Accumulated`.

Drive a transfer whose start and completion fall on different days and assert
`delivery_commenced_at` holds the start. Both artefacts are constructed at
completion, so an actor reading the clock at creation produces a field named for
commencement that holds the completion time — a substitution nothing downstream
can detect (`sim.md` §8.3).

##### `a_transfer_spanning_a_month_boundary_is_audited_once_in_the_opening_month`

**Starting state:** `Accumulated`, with a transfer commencing before midnight on
the last day of the window and completing after it.

Assert the transaction is reconciled in the opening month, that its paper
artefact is not held forward, and that the closing month reports neither a record
without paper nor paper without a record. Paper and record must agree on which
instant assigns the window; this is the test that fails if they diverge.

##### `boundary_events_are_grouped_before_the_window_test`

**Starting state:** `SessionConfirmed`, scripted so the four events of a boundary
transfer fall either side of the calendar month.

Assert the retrieval takes a margin either side of the month, that events are
grouped by `job_id` before any window test, and that the opening month's audit
holds all four events of that transfer as one transaction. Without the margin
and the grouping the opening month sees the transfer's open without its close,
on correct data (`sim-impl.md`).

##### `a_transaction_outside_the_window_is_discarded_after_grouping`

**Starting state:** `MonthRetrieved`, the margin having pulled in transfers that
commenced outside the window.

Assert they are discarded on `opened_at` after grouping, and do not reach the
checks. The margin widens what is retrieved, not what is audited.

---

**Clipboard discipline** (`recon.md` §11.2)

##### `a_read_that_does_not_complete_resolves_to_a_typed_error`

**Starting state:** `SessionConfirmed`.

Script the clipboard double to announce a copy and then never respond. Assert the
read resolves to a typed error at its deadline rather than blocking. Past the
ceiling the workstation neither delivers nor refuses, so this is the obligation
that ends an oversized retrieval.

##### `the_bound_is_enforced_at_reassembly_from_the_declared_length`

**Starting state:** `SessionConfirmed`.

Script a transfer declaring a total length above the bound. Assert it is refused
**before** chunks are accumulated — a check applied to the completed payload
satisfies the clause as written while paying the cost it exists to avoid.

##### `a_selection_at_the_ceiling_is_accepted_and_one_byte_beyond_is_refused`

**Starting state:** `SessionConfirmed`, with `clipboard_ceiling_bytes` set small.

The bound is strict. Assert both directions at the configured ceiling rather
than at the measured 1,048,575, so the test needs no megabyte fixture; a separate
assertion covers the deployed value being the measured one.

##### `a_file_descriptor_format_is_refused_rather_than_unrequested`

**Starting state:** `SessionConfirmed`.

Script the double to advertise a file-descriptor format. Assert the actor refuses
it, rather than merely not asking for it — the refusal must be a property of the
actor, not of what the far end advertises.

##### `content_already_on_the_selection_before_the_read_began_is_refused`

**Starting state:** `SessionConfirmed`.

Place content on the double's selection before the read starts. Assert it is not
returned. The positive form of this test passes whether or not the ordering is
enforced; the negative is the one that has force.

---

**The latch handshake** (`recon.md` §8.1)

##### `the_latch_value_is_unique_per_audit`

**Starting state:** `Accumulated`, two audit cycles.

Assert the echoed values differ.

##### `no_match_is_reported_as_a_session_state_failure_not_an_empty_audit`

**Starting state:** `Accumulated`.

Script the clipboard double to return a non-matching value. Assert the outcome is
a session-state failure and that no clean audit is reported.

##### `no_credential_is_typed_before_the_latch_returns`

**Starting state:** `Accumulated`.

Script the latch read to fail. Assert the session double recorded no keystrokes
carrying the log-server credential. If focus is not where the actor believes it
is, the credential goes somewhere else on the host.

---

**Retrieval and splitting** (the actor's §5 obligations)

##### `the_query_carries_no_limit`

**Starting state:** `SessionConfirmed`.

`limit=N` returns the N most recent entries and silently drops the oldest, which
under a fatal discrepancy rule terminates the sim on clean data.

##### `the_filter_retrieves_both_classes_of_event`

**Starting state:** `SessionConfirmed`.

`digital.md` §4.5 has the audit retrieving log records only — the bunkering
events of `hist.md` §4.2 **and** the authentication and card-table events of
§4.3. Script the clipboard double to return both shapes and assert both are
parsed and retained.

A filter selecting only records carrying `event_type` drops every syslog-shaped
record silently, because the two classes differ in shape rather than in value.
That is the failure this test exists to catch, so the negative matters: assert
that a returned `LOGIN` is not discarded as unparseable.

##### `a_bunkering_event_is_joined_to_its_login_by_session_id`

**Starting state:** `MonthRetrieved`, with a `LOGIN` and a `job_opened` sharing a
`session_id`.

The envelope carries `session_id` and the syslog record carries it as
`fms@99999.session_id` (`hist.md` §4.2, §4.3). Assert the join is made across
both field names — the same value under two names is what the audit reconciles
on, and a join written against one name alone finds nothing and reports a clean
month.

##### `a_window_that_hits_its_deadline_is_halved_and_retried`

**Starting state:** `SessionConfirmed`.

Script the first retrieval to exceed its deadline. Assert the next covers half
the span.

##### `a_single_day_that_still_times_out_is_fatal`

**Starting state:** `SessionConfirmed`.

No legitimate day approaches the ceiling. Assert the actor does not halve below
one day.

##### `a_width_that_succeeds_is_held_for_the_rest_of_the_month`

**Starting state:** `SessionConfirmed`.

Each probe past the ceiling costs a full deadline with no diagnostic, so widths
are not re-tested speculatively. Assert no re-widening.

##### `the_month_is_reassembled_from_its_sub_retrievals_in_time_order`

**Starting state:** `MonthRetrieved`, sub-retrievals returned out of order.

Results are unsorted by default and are sorted locally after reassembly. Assert
the assembled month is ordered.

##### `each_sub_retrieval_is_separately_latched_and_bounded`

**Starting state:** `SessionConfirmed`.

Assert the latch is cleared before each, not once per audit.

---

**The checks** (`narrative.md` §5; the actor's §6.1 obligations)

Each check is tested in-module, under "Pairing and checks" above. Through
`spawn()`:

##### `a_retrieved_window_is_paired_and_checked`

**Starting state:** `MonthRetrieved`, consistent, holding a dispense and a fill.

Assert the audit reports both artefacts reconciled and no finding: the
retrieved records reach the pairing and the checks.

##### `the_k_factor_is_read_from_the_compiled_world_constant`

`process.k_factor` is a projected world fact (`deployment-config.md` §3). The
±0.5% check crosses from pulses to litres with it, and a literal in the actor
would reintroduce the drift the compiled constants exist to prevent. Extends the
existing drift guard.

##### `each_numeric_check_reports_its_actual_margin`

**Starting state:** `MonthRetrieved`.

A near-miss must be visible before it becomes a fatal, so pass/fail alone is not
sufficient.

##### `the_temperature_correction_is_applied_to_the_meter_side_only`

**Deferred until a thermal model is defined** (see the deferral note at the end
of this section).

**Starting state:** `MonthRetrieved`, with a fuel temperature away from 15 °C.

The ATG volumes on the events are already temperature-compensated (`hist.md`
§4.2). Assert the ±1% check corrects the meter figure and takes the ATG delta as
given.

**Both neighbouring mistakes fail the same way and by the same amount.** Leaving
the meter side uncorrected, and correcting the ATG side a second time, each leave
the factor in the ratio: at 0.985 both produce a 1.5% divergence on correct data, which
fails a ±1% tolerance (`sim.md` §8.4.1). Assert correct data passes, and assert
each mistake does not.

This cannot be caught at the modelled constant of 15 °C, where the correction is
identity and all three arrangements agree. The test must set a temperature at
which they differ.

##### `the_correction_is_checked_against_its_inputs_not_taken_on_trust`

**Deferred until a thermal model is defined** (see the deferral note at the end
of this section).

**Starting state:** `MonthRetrieved`, with a `temp_corrected_volume_l` that does
not follow from the `temperature_c` and `density_kg_per_l` beside it.

Both the paper artefact and the event carry the inputs as well as the corrected
figure, which is what makes the arithmetic checkable. Assert the discrepancy is
detected rather than the corrected figure being read directly.

---

**Outcome and classification** (the actor's §6.2 and §7.2 obligations)

##### `a_summary_entry_is_written_on_a_clean_audit`

**Starting state:** `MonthRetrieved`, consistent.

Assert the session double recorded an append carrying the period and the
statement that the records agreed, before the terminal was closed
(`narrative.md` §3).

##### `a_summary_entry_names_identifiers_and_no_record_content`

**Starting state:** `MonthRetrieved` with unmatched artefacts.

Assert the entry carries the document numbers and **not** the contents of any
record. The workstation holds no copy of the period's records, and an entry
carrying finding detail would rebuild one a period at a time (`recon.md §1`).

##### `a_second_audit_appends_rather_than_replaces`

**Starting state:** `Accumulated`, run across two audit cycles.

An accumulated run of summaries is itself reconcilable, so a month absent from
the sequence is evidence. Assert the second entry does not overwrite the first.

##### `a_failed_summary_write_is_reported`

**Starting state:** `MonthRetrieved`; the session double refuses the append.

Assert the failure is reported at WARN alongside the audit's own result, and
that the audit's findings are not lost with it.

##### `a_finding_is_not_emitted_at_error`

**Starting state:** `MonthRetrieved` with one record removed.

ERROR is the single fatal event before teardown. Assert the non-fatal finding is
not recorded there, so that an ERROR in a run's output continues to mean the run
ended.

##### `a_record_disagreeing_with_paper_is_fatal`

**Starting state:** `MonthRetrieved` with one altered field.

The FMS log is append-only and corrections are new entries, so there is no benign
cause. Assert the panic, on the cavitation precedent.

##### `a_log_record_with_no_paper_artefact_is_fatal`

**Starting state:** `MonthRetrieved` with an extra record.

Every transaction produces a signed paper document, so this is an injected
record.

##### `a_paper_artefact_with_no_log_record_is_reported_and_not_fatal`

**Starting state:** `MonthRetrieved` with one record removed.

Assert the absence is reported at WARN with the artefact identified, and assert
the audit does **not** panic. WARN is what makes the finding visible in a normal
run; the fleet's `EnvFilter` default suppresses INFO, so a finding recorded there
would exist and be assertable while nobody watching a run would see it
(`sim-impl.md`, Logging). This is the one discrepancy class with a benign cause:
`narrative.md` §5 Temporal 4 permits a copy that cannot be delivered to be lost
and gives availability precedence over completeness, so terminating here would
end the run on behaviour the narrative allows.

The negative half is the assertion that matters. An implementation that treats
every absence as fatal satisfies a test that only checks the report.

##### `a_contiguous_run_of_absences_is_reported_and_not_fatal`

**Starting state:** `MonthRetrieved` with a run of consecutive records removed.

The same disposition regardless of scale. A long outage and a sustained
suppression produce the same observation, and the audit does not distinguish
them — `narrative.md` §5 Temporal 4 poses that as a detection problem rather than
resolving it.

##### `the_shape_of_an_absence_is_reported_as_detail`

**Starting state:** `MonthRetrieved`, one case isolated and one contiguous.

Assert **one** WARN per audit carrying how many artefacts went unmatched, the
number of runs they form and the length of the longest (`sim-impl.md`,
"Reporting") — not one event per artefact, which a long outage would turn into
hundreds. This is forensic detail for whoever reads the audit, on the same
principle as the per-check margins: it is recorded, and nothing in the sim acts
on it.

##### `a_clean_audit_emits_structured_detail_including_per_check_margins`

**Starting state:** `MonthRetrieved`, consistent.

Assert window, artefacts reconciled, records retrieved, retrievals used, and
per-check detail, at INFO. The test raises the `EnvFilter` above the fleet
default, which suppresses INFO in a normal run — deliberately, since this is the
healthy case and the per-check margins are forensic material rather than
something a normal run should print (`sim-impl.md`, Logging).

##### `every_clipboard_arrival_is_classified`

**Starting state:** `SessionConfirmed`.

A window's records, an empty window, a client diagnostic, an authorisation refusal
and falsified content are all data the actor reads and acts on. Assert each is
classified distinctly — in particular that an empty window and a failed retrieval
are not confused, which is what folding standard error into the pipe exists to
prevent.

---

**Failure handling** (the actor's §7.1 obligations)

##### `each_failure_site_has_its_own_budget`

Terminal start, retrieval and clipboard are three sites and one budget is not
shared between them.

##### `budget_exhaustion_buys_one_whole_session_restart`

**Starting state:** `SessionConfirmed`.

Assert a new connection, a new terminal and a new latch. A dialogue holding focus
is cleared by a fresh session and never by retrying into a dead one.

##### `a_second_exhaustion_is_fatal`

##### `the_actor_writes_nothing_to_disk`

Parity with the dock operator, which was the agreed bar, and the strongest
reading of `sim.md` §5.4. Follows the existing form: no actor source uses
`std::fs`, `File::create`, `fs::write` or `OpenOptions`.

---

**Where each test above is carried.** Each was specified through `spawn()`
before the transport existed; only
`a_settling_delay_outside_its_bounds_is_refused_at_spawn` is written that way
(see the coverage table at the start of this section).

| Test | Carried by |
|---|---|
| `window_is_the_previous_calendar_month_not_a_rolling_thirty_days` | Audit loop: `each_audit_covers_its_calendar_window` |
| `audit_runs_the_settling_delay_into_the_following_period` | Audit loop: `an_audit_runs_the_settling_delay_after_its_window_ends` |
| `a_settling_delay_outside_its_bounds_is_refused_at_spawn` | Through `spawn()`, in `infra/sim/tests/`: it fails before any session |
| `bounds_are_explicit_and_never_relative` | Retrieval: `the_retrieval_command_is_the_exact_line_typed` |
| `an_artefact_is_reconciled_in_the_window_its_own_timestamp_falls_in` | Audit loop: `an_artefact_is_audited_in_the_window_its_commencement_falls_in` |
| `an_artefact_dated_in_a_later_window_is_held_not_counted_unmatched` | The same |
| `a_held_artefact_is_reconciled_by_the_next_audit` | The same |
| `the_artefact_records_commencement_not_creation` | Audit types: `each_artefact_reports_its_own_commencement`; the dock operator's `the_bdn_records_commencement_not_creation` |
| `a_transfer_spanning_a_month_boundary_is_audited_once_in_the_opening_month` | Audit loop: `a_transfer_spanning_the_period_end_is_audited_once_in_the_opening_window` |
| `boundary_events_are_grouped_before_the_window_test` | Records and grouping: `the_events_of_a_transfer_spanning_the_window_end_form_one_transaction`; Audit types: `the_retrieval_bounds_widen_the_window_by_the_margin_and_the_session_lifetime` |
| `a_transaction_outside_the_window_is_discarded_after_grouping` | Records and grouping: `a_transaction_outside_the_window_is_discarded_after_grouping_in_module` |
| The five tests of "Clipboard discipline" | Clipboard |
| `the_latch_value_is_unique_per_audit` | Session: `the_latch_value_differs_between_audits_and_attempts`; Audit loop: `each_audit_opens_its_own_session` |
| `no_match_is_reported_as_a_session_state_failure_not_an_empty_audit` | Session: `the_session_is_confirmed_by_reading_back_the_latch_value`; Failure handling: `a_third_unconfirmed_terminal_restarts_the_session` |
| `no_credential_is_typed_before_the_latch_returns` | Session: `the_session_is_confirmed_by_reading_back_the_latch_value` |
| `the_query_carries_no_limit` | Retrieval: `the_retrieval_command_is_the_exact_line_typed` |
| `the_filter_retrieves_both_classes_of_event` | Retrieval: `a_response_ending_in_status_zero_is_its_records` |
| `a_bunkering_event_is_joined_to_its_login_by_session_id` | Pairing and checks: `a_session_resolves_to_a_login_under_its_prefixed_name` |
| `a_window_that_hits_its_deadline_is_halved_and_retried` | Retrieval: `a_span_reaching_its_deadline_is_halved_and_retried_from_its_start` |
| `a_single_day_that_still_times_out_is_fatal` | Superseded: a deadline at the floor now restarts the session (`sim-impl.md`, "Failure handling"). Failure handling: `a_deadline_at_the_floor_restarts_the_session`, `a_second_exhaustion_is_fatal` |
| `a_width_that_succeeds_is_held_for_the_rest_of_the_month` | Retrieval: `a_width_that_succeeds_is_held` |
| `the_month_is_reassembled_from_its_sub_retrievals_in_time_order` | Retrieval: `the_assembled_window_is_in_time_order` |
| `each_sub_retrieval_is_separately_latched_and_bounded` | Retrieval: `each_sub_retrieval_clears_the_latch_before_its_command` |
| `a_retrieved_window_is_paired_and_checked` | Audit loop: `an_artefact_is_audited_in_the_window_its_commencement_falls_in` |
| `the_k_factor_is_read_from_the_compiled_world_constant` | The drift guard in `deploy/tests/unit/test_sim_constants_agreement.py`, extended to the reconciliation user with the projection of the audit's cadence |
| `each_numeric_check_reports_its_actual_margin` | Outcome and report |
| The two temperature-correction tests | Deferred until a thermal model is defined, as before |
| `a_summary_entry_is_written_on_a_clean_audit` | Failure handling: `a_clean_audit_records_its_summary_and_closes` |
| `a_summary_entry_names_identifiers_and_no_record_content` | Summary entry |
| `a_second_audit_appends_rather_than_replaces` | Audit loop: `a_second_audit_appends_its_own_entry` |
| `a_failed_summary_write_is_reported` | Audit loop: `a_summary_not_written_is_reported_at_warn` |
| `a_finding_is_not_emitted_at_error` | Audit loop: `findings_are_reported_in_one_warn_and_not_at_error` |
| `a_record_disagreeing_with_paper_is_fatal` | Pairing and checks, for the disposition; Audit loop: `a_fatal_outcome_panics_with_its_message`, for the panic |
| `a_log_record_with_no_paper_artefact_is_fatal` | The same |
| `a_paper_artefact_with_no_log_record_is_reported_and_not_fatal` | Audit loop: `findings_are_reported_in_one_warn_and_not_at_error` |
| `a_contiguous_run_of_absences_is_reported_and_not_fatal` | Outcome and report, for the runs; Audit loop, as the previous |
| `the_shape_of_an_absence_is_reported_as_detail` | The same |
| `a_clean_audit_emits_structured_detail_including_per_check_margins` | Audit loop: `a_clean_audit_is_reported_at_info` |
| `every_clipboard_arrival_is_classified` | Retrieval: the reading of a response, and `a_failed_retrieval_is_classified_by_its_status` |
| `each_failure_site_has_its_own_budget` | Failure handling |
| `budget_exhaustion_buys_one_whole_session_restart` | Failure handling: `a_third_unconfirmed_terminal_restarts_the_session`, `the_restart_resumes_at_the_span_that_failed` |
| `a_second_exhaustion_is_fatal` | Failure handling: `a_second_exhaustion_is_fatal` |
| `the_actor_writes_nothing_to_disk` | Audit loop: `the_actor_writes_nothing_to_disk` |

**Integration coverage** — `tests/integration/recon/`, against the full digital
world. Gated on the log-server replica being seeded for a known period.

- A record set where all BDNs and delivery tickets have matching log-server
  entries produces no discrepancy (clean audit).
- A log-server entry that has been tampered with or deleted is detected and
  reported.
- A paper record for which no log-server entry exists is detected and reported.
- The audit command composes and executes under the pinned layout. `|` is
  `Shift` + `0x56` under `gb` and the workstation guarantees the layout only if
  the actor's scancode table agrees with it; `recon-tests.md` §1 records that a
  layout regression presents as a malformed query rather than as a rejected
  login.
- A month settles in two retrievals in a scenario with a busier dock, which
  exercises the adaptive split against the real clipboard bridge. The scenario
  sets each of the three tugs' `roster.vessels[].mean_time_away_s` to 21,600
  seconds, 6 hours, so the
  roster's arrival rate is 0.5 an hour and `initial_window` about 16.8 days,
  against a month's bounds of about 30.2 days. A scenario may set the tugs'
  times away (`narrative.md`, the opening after its scope lists). At the
  roster's default rate a month is one retrieval, since `initial_window` is
  about 56 days.
- The audit's command is found in the shell history. `recon-tests.md` §9 places
  this here because it needs an audit to have run.

**The first audit of a deployed run** — `tests/integration/recon/test_08_audit.py`.
The proof that the actor conducts a whole audit against the real workstation and
log server. It needs a deployment run with a short period, `audit.period: day`
set in the checkout and not committed, past its first audit at 06:00 UTC on the
day after the sim started; each test skips while no summary entry exists, so the
module can be run before then without failing. Everything is read through
`incus exec`.

- `the_audit_recorded_an_agreed_summary_entry` — the last line of the summary
  file in the reconciliation user's home directory, found from the directory
  entry rather than assumed, is a window from an instant to the following
  midnight, both in RFC 3339 with `Z`, then `agreed`.
- `the_session_arrived_as_audit_client` — the workstation's remote-desktop log
  names the client `audit-client`.
- `the_audit_commands_are_in_the_shell_history` — the history holds the latch
  command, the retrieval command in its `sim-impl.md` form (with
  `--fail-with-body`, the half-open `_time` range and the completion line), the
  append and its confirmation, in that order.
- `the_sim_audited_without_a_warning_or_an_error` — the sim is still running,
  and its log holds no WARN or ERROR from the reconciliation user. A clean audit
  reports at INFO, which the deployed filter does not show, so the summary entry
  is the positive evidence and the log the negative.

**Measuring the payload and the read** — `tests/integration/recon-probe/measure.sh`.
Not a test: it measures what sets two timings of `sim-impl.md`, "The audit
loop". At least 14 days of records at one arrival an hour, the rate of the
62.6 KB-a-day measurement, are seeded through `hist-01`. The seeded days end 35
days before the run, so that no audit retrieves them (`recon-tests.md` §5). The
retrieval of one day is run on `recon-01` through `incus exec` and its bytes
counted. The same retrieval is read through the clipboard by the probe inside
`sim-host`, on the actor's own route, and timed. A span of about 14 seeded
days, about 876 KB, near the clipboard's ceiling and below it, is read and
timed the same way.
`RECORD_BYTES_PER_ARRIVAL` is then the day's bytes over the day's arrivals, from
which the actor derives `initial_window` and `width_floor` (`sim-impl.md`,
"Splitting the window"). The derived widths scale with the configured arrival
rate. `clipboard_deadline` is a margin over the near-ceiling read.

---

**Two tests are deferred:** `the_temperature_correction_is_applied_to_the_meter_side_only`
and `the_correction_is_checked_against_its_inputs_not_taken_on_trust`. Each needs
a temperature at which the correction is not identity, and so a correction
defined away from 15 °C (`sim.md` §1.3), which arrives with a thermal model. At
the modelled constant of 15 °C the three arrangements of the ±1% check agree,
and checking the correction against its inputs reduces to the corrected volume
equalling the metered one, which the exact comparison of paper with record
already covers. The outcome of a correction that does not follow from its inputs
is decided with the thermal model. The other groups
that waited on prior work — partitioning on the artefact timestamp and the
disposition of a paper artefact with no matching record — have been settled.

---

### Systems administrator

Coverage is split across two locations.

| Location | Needs a deployment | Covers |
|---|---|---|
| `infra/sim/src/actors/systems_administrator/internals/tests.rs` | No | Cadence, the commands typed and their order, the channels requested, failure handling, stopping and warm start (*Stopping and warm start*) |
| `tests/integration/admin/` | Yes, the full digital world | One session against a real `admin-01`, reaching each managed host through `dmz-bastion-01` |

**Test harness:**

The actor takes a `clock`, so each test runs its body via `runtime.block_on`, per the convention at the start of this document. The clock is `SimClock::isolated(TimeModel::Synthetic, start)`. A **session double** stands in for the Secure Shell (SSH) client. It records the channels requested and every command typed, returns a prompt after each command, and can be scripted to fail at sign-in or at any managed host.

**Starting states:**

#### `Waiting`

Spawned, before the first session.

#### `SignedIn`

A session is open on `admin-01`, before the first managed host.

**Tests:**

#### `one_session_each_configured_interval`

Starting state: `Waiting`, with `check_interval` set to six hours. Advancing the synthetic clock through 24 hours produces four sessions, each beginning six simulated hours after the previous one ends.

#### `the_journal_is_read_since_boot_then_since_the_previous_check`

Starting state: `Waiting`, on a cold start. The journal command of the first session is `sudo journalctl --priority=err --boot`; that of the second gives the first session's start time after `--since`.

#### `each_managed_host_is_checked_in_order`

Starting state: `SignedIn`. The session types `ssh fms-01`, the check and `exit`, then the same for `hist-01` and `hmi-01`, then exits the shell on `admin-01`.

#### `the_check_is_read_only`

Starting state: `SignedIn`. Every command typed on a managed host is one of the four commands of the check (`sim-impl.md`, "Systems administrator").

#### `only_a_shell_is_requested`

Starting state: `Waiting`. Through one session, the session double records password authentication, one pseudo-terminal and one shell, and these are all it records (`sim.md` §5.4).

#### `an_unreachable_host_is_skipped`

Starting state: `SignedIn`. Scripted to fail at `hist-01`, the session records a WARN, checks `hmi-01` and exits normally.

#### `a_rejected_sign_in_waits_for_the_next_interval`

Starting state: `Waiting`. Scripted to reject the credential once, the session ends with a WARN, and the next scheduled session signs in.

#### `an_unexpected_host_key_ends_the_session`

Starting state: `Waiting`. Scripted to present a host key other than `admin-01`'s, the session ends with a WARN before any credential is sent, and the next scheduled session signs in.

#### `session_reaches_each_managed_host`

Integration, against a deployed environment. One session signs in to `admin-01` and completes the check on `fms-01`, `hist-01` and `hmi-01`. The security monitoring's `archives.json` then holds the SSH authentications on `admin-01`, `dmz-bastion-01` and each managed host, and one `sudo` record from each managed host.

---

### Fuel supplier

**Construction parameters used in tests:**

| Parameter | Value |
|-----------|-------|
| `poll_interval` | 1 min |
| `reorder_threshold_L` | 10 000 L |
| `delivery_volume_L` | 15 000 L |
| `lead_time` | 30 min |

**Test harness:**

The test obtains a `SimClock` and runtime via
`SimClock::isolated(TimeModel::Synthetic)` and runs its body under that
runtime. It spins up a `tokio::net::TcpListener` on a loopback port before
calling `fuel_supplier::spawn(clock, local_addr, atg_relay_addr,
poll_interval, reorder_threshold_L, delivery_volume_L, lead_time,
tanker_arrival_tx)`. The listener responds to Veeder-Root TLS i201 queries
with the current volume and to i101 queries with a no-alarm state. A
`watch::Sender<f64>` allows individual tests to update the reported volume
between `clock.sleep()` advances, and the listener records `clock.now()` at
each i201 query it answers, so tests can observe the polls. An `mpsc` channel
is created for `tanker_arrival_tx`; the test body holds `tanker_arrival_rx` to
observe dispatched `FillJob` entries, and plays the road tanker driver: it
holds each `FillJob`'s `completed` sender and fires it only where a test says
the delivery has ended.

---

#### `no_dispatch_when_level_above_threshold`

Set mock ATG volume to 20 000 L (above the 10 000 L threshold). Advance
the clock by 3 × `poll_interval` (3 min). Assert
`tanker_arrival_rx.try_recv()` returns nothing — no dispatch is triggered
when the level is above the reorder threshold.

---

#### `dispatch_triggered_when_level_below_threshold`

Set mock ATG volume to 8 000 L (below threshold). Advance the clock by
`poll_interval` (1 min) to trigger a poll and dispatch decision, then by
`lead_time` (30 min) to let the actor complete the lead-time sleep. Assert
`tanker_arrival_rx` receives a `FillJob`.

---

#### `fill_job_volume_matches_configured_value`

Set mock ATG volume to 8 000 L. Advance clock by `poll_interval` +
`lead_time`. Receive the `FillJob` from `tanker_arrival_rx`. Assert
`fill_job.delivery_volume_L == 15_000.0`.

---

#### `no_dispatch_before_lead_time_expires`

Set mock ATG volume to 8 000 L. Advance clock by `poll_interval` only
(the dispatch decision has been made but the lead-time sleep has not yet
elapsed). Assert `tanker_arrival_rx.try_recv()` returns nothing —
confirming the actor is mid-lead-time and has not yet sent the `FillJob`.

---

#### `polls_continue_while_an_order_is_pending`

Set mock ATG volume to 8 000 L. Advance clock by `poll_interval` +
`lead_time` and receive the first `FillJob`, holding its `completed` sender
without firing it. Advance clock by a further 10 × `poll_interval`. Assert
that the mock ATG answered one i201 query in each `poll_interval` from the
first poll onwards, through the 30 minutes of the lead time and the 10 minutes
after the `FillJob`: the supplier monitors the level continuously, and an
order does not move its poll schedule (`sim.md` §5.3).

---

#### `a_slow_poll_does_not_move_the_schedule`

Set mock ATG volume to 20 000 L and script the mock to refuse the first two
connection attempts of the second poll, so that poll completes 3 seconds after
it fell due, after its retries at 1 and 2 seconds. Assert that the third poll
falls one `poll_interval` after the second fell due, not after it ended: a
poll's length does not move the schedule.

---

#### `the_poll_that_places_an_order_logs_it`

With a `tracing` subscriber that records `INFO` events, set mock ATG volume to
8 000 L and advance the clock by `poll_interval` + `lead_time`. Assert that the
poll that placed the order logged one `INFO` event of the fuel supplier, with
the poll's time, the reported volume of 8 000 L and a scheduled arrival 30
minutes after the poll. Assert that the `FillJob` arrives at that scheduled
arrival, and that the later polls, with the order pending, log no order.

---

#### `each_poll_and_delivery_is_logged`

With a `tracing` subscriber that records `INFO` events, set mock ATG volume to
8 000 L and advance the clock by `poll_interval` + `lead_time`. Receive the
`FillJob` and fire its `completed` sender. Assert that each poll logged one
`INFO` event of the fuel supplier, with the volume read, the alarm state and
whether the poll placed an order. Assert one `INFO` event of the road tanker's
arrival as the `FillJob` is sent, with the delivery volume, and one of the
delivery's end as `completed` fires.

---

#### `no_second_order_while_an_order_is_pending`

Set mock ATG volume to 8 000 L. Advance clock by `poll_interval` +
`lead_time` and receive the first `FillJob`, holding its `completed` sender
without firing it. Leave mock ATG volume below threshold. Advance clock by
3 × (`poll_interval` + `lead_time`). Assert `tanker_arrival_rx.try_recv()`
returns nothing. The advance exceeds the lead time, so a second order placed
by any of the below-threshold polls during it would have produced a
`FillJob`: an order stays pending until the delivery has ended, however long
that takes.

---

#### `ordering_resumes_after_completion`

Set mock ATG volume to 8 000 L. Advance clock by `poll_interval` +
`lead_time`, receive the first `FillJob`, and fire its `completed`. Leave mock
ATG volume below threshold. Advance clock by `poll_interval` + `lead_time`.
Assert `tanker_arrival_rx` receives a second `FillJob`: once the delivery has
ended, the next below-threshold poll places an order.

---

#### `dropped_completion_signal_is_fatal`

Set mock ATG volume to 8 000 L. Advance clock by `poll_interval` +
`lead_time`, receive the first `FillJob`, and drop it, dropping its
`completed` sender. Assert the supplier's `JoinHandle` resolves with a panic.

---

## Stopping and warm start

Tests for sim.md §9 and sim-impl.md, "Stopping and warm start". The entity
tests below follow the convention of this document: each entity under test is
spawned through its public `spawn()` with an isolated synthetic clock, the test
holds the neighbouring channel ends and the `StopRequest` sender, and it
receives the entity's state on the report oneshot. The reconciliation user's
tests are unit tests of its audit loop, in
`src/actors/reconciliation_user/internals/audit_loop.rs`, run against the
workstation double of its other unit tests, which is internal to the crate. The
systems administrator's tests are unit tests in
`src/actors/systems_administrator/internals/tests.rs`, run against the session
double of its other unit tests, which is likewise internal. The
checkpoint tests call
`checkpoint.rs` directly with documents in a temporary directory. The
whole-sim tests build the orchestrator (`orchestrator::build`) in isolated mode
against the mock digital world of `tests/full_sim.rs`, and request the stop
through the built sim's `stop()` rather than a signal.

---

### Coming to rest

#### `generator_reports_next_due_time_and_sends_nothing_after_stop`

Spawn the tug arrival generator with seed 42 and the three tugs of the roster.
Request a stop before any tug's first arrival is due. Assert that the report
carries the seed and, for each tug, its first arrival's due time and the word
position between the draws of its first time away and its volume; advance the clock past
every due time and assert `tug_arrival_rx` receives nothing.

#### `generator_reports_a_tug_at_the_dock`

Let one tug arrive, and leave its `TugArrival` in the arrival channel, not
taken up. Request a stop. Once the tug engineer has reported, assert that the
generator reports that tug `AtDock` with its job, and every other tug `Away`
with its due time, each at the word position between the draws of its time
away and its volume.

#### `tug_leaving_after_stop_reports_its_next_due_time`

Let a tug arrive, request a stop, and let the tug leave the dock before the tug
engineer reports. Assert that the generator reports the tug `Away`, with the due
time that its next time away gives from its departure, and the word position
between the draws of that time away and its volume.

#### `engineer_takes_up_no_arrival_after_stop_and_keeps_its_departure_open`

Request a stop with the tug engineer idle, then send it a tug's arrival. Assert
that the engineer reports, sends no `DockJob`, presents no card and leaves the
tug's departure signal open, and that its task does not finish.

#### `engineer_finishes_the_tug_in_progress_before_report`

Request a stop as the tug engineer serves a tug. Assert that it reports only
after it has handed back the co-signed BDN and the tug has left.

#### `refuel_in_progress_completes_before_report`

Request a stop after the tug engineer has taken up a job and before the dock
operator has entered the BDN number. Assert that the mock HMI receives the
transcription, that the reconciliation user's channel receives the BDN, and
that the dock operator's report follows both and carries the BDN number used.

#### `fill_in_progress_completes_before_report`

As above for a fill: the stop arrives with the tanker pump running. Assert the
pump stops at the delivery volume, the delivery ticket is transcribed and
posted, and the road tanker driver's report carries the advanced meter
totaliser and ticket number.

#### `supplier_reports_tanker_due_during_lead_time`

Bring the supplier to an order and request a stop half-way through the lead
time, between two polls. Assert the report's order is `TankerDueAt` the
ordering poll's time plus `lead_time`, its `next_poll_at` is the next poll of
the unchanged schedule, and that no `FillJob` is sent.

#### `supplier_reports_tanker_arrived_when_driver_did_not_take_it_up`

Let the supplier send a `FillJob` that the driver does not receive, and request
a stop. Assert the supplier does not report until the driver's report is
signalled, and then reports `TankerDueAt` the time the job was sent.

#### `driver_holds_a_fill_job_it_did_not_take_up`

Send a `FillJob` to the idle road tanker driver and request a stop at the same
time. Assert that the driver's report is its unchanged starting state, that no
`DockJob` is sent, and that the job's `completed` sender is held: neither
fired nor dropped.

#### `supplier_reports_no_order_after_delivery_ends`

Request a stop while the driver has a fill in progress, and fire `completed`
30 minutes later. Assert the supplier reports no order pending, and that no
poll was made after the stop.

#### `dock_operator_serves_dock_job_arriving_after_stop`

Request a stop with the dock operator idle and a refuel taken up by the tug
engineer but not yet forwarded. Assert the dock operator serves the `DockJob`
and reports only after the engineer has reported.

#### `reconciliation_user_does_not_begin_audit_after_stop`

Request a stop before the window's audit is due. Advance the clock past it.
Assert no session is opened and that the report carries the window's bounds.

#### `reconciliation_user_finishes_audit_in_progress`

Request a stop while the connector double is mid-retrieval. Assert the audit
completes, its summary entry is written, and the report then carries the next
window.

#### `reconciliation_user_report_includes_artefacts_posted_after_its_stop`

Post a BDN on `record_rx` after the reconciliation user has stopped and before
the dock operator reports. Assert the reconciliation user's report includes it,
together with the artefacts held from earlier drains.

#### `administrator_begins_no_session_after_stop`

Spawn the systems administrator with `next_check_at` six hours after the
clock's start. Request a stop. Assert the report carries the starting state
unchanged; advance the clock past `next_check_at` and assert the session double
records no connection.

#### `administrator_finishes_session_in_progress_before_report`

Script the session double to hold its reply to the first command typed on
`hist-01`, and request a stop once that command is typed. Release the reply.
Assert that the check on `hist-01` and `hmi-01` completes, and the shell on
`admin-01` is exited and the session disconnected, before the report; and that
the report carries `previous_check_at` as the session's start and
`next_check_at` as its end plus `check_interval`.

#### `stop_does_not_close_any_channel`

Bring every entity to rest in the whole sim. Assert that no task has finished
and no panic has been logged before the checkpoint is written.

#### `whole_sim_checkpoint_carries_the_administrator_state`

Stop the whole sim before the systems administrator's first session is due.
Assert the checkpoint's `systems_administrator` holds `next_check_at` as the
start plus `check_interval`, and no `previous_check_at`.

---

### Checkpoint

#### `checkpoint_round_trips`

Serialise a checkpoint holding a value in every field, including a word
position above 2^64 and an artefact of each kind; deserialise it. Assert the
result equals the original.

#### `checkpoint_written_by_rename`

Write a checkpoint to a temporary directory. Assert `checkpoint.json` exists
and parses, and no `checkpoint.json.tmp` remains.

#### `absent_checkpoint_is_a_cold_start`

Read from an empty directory. Assert the result is a cold start, and no
`checkpoint-loaded` marker is created.

#### `accepted_checkpoint_creates_marker`

Read a valid checkpoint. Assert `checkpoint-loaded` exists in the run directory
afterwards, and does not exist after a refused one.

#### `unparseable_checkpoint_is_refused`

Read a truncated checkpoint. Assert a startup fault naming the file.

#### `format_version_mismatch_is_refused`

Read a checkpoint whose `format_version` is one more than the binary's. Assert
a startup fault naming the two versions.

#### `parameter_mismatch_is_refused_naming_the_field`

Read a checkpoint whose `tank_capacity_l` differs from the binary's. Assert a
startup fault naming `tank_capacity_l`.

#### `checkpoint_written_after_start_is_refused`

Read a checkpoint whose `written_at` is one second after the clock's start.
Assert a startup fault.

#### `checkpoint_older_than_max_start_gap_is_refused`

Read a checkpoint whose `written_at` is `max_start_gap` plus one second before
the clock's start. Assert a startup fault giving both times.

#### `checkpoint_within_max_start_gap_is_accepted`

Read checkpoints whose `written_at` equals the clock's start and is exactly
`max_start_gap` before it. Assert both are accepted.

#### `reseed_replaces_seed_and_resets_position`

Read a checkpoint with a reseed file giving `tug_arrival_seed`. Assert the
generator's state carries the new seed with every tug at word position zero,
and each tug's saved phase unchanged.

#### `reseed_with_unknown_field_is_refused`

Read a checkpoint with a reseed file containing `tug_seed`. Assert a startup
fault naming the field.

#### `reseed_without_checkpoint_is_refused`

Read a directory holding a reseed file and no checkpoint. Assert a startup
fault.

#### `sim_not_at_rest_writes_no_checkpoint`

Give the orchestrator's at-rest check a tank reply with `dispenser_solenoid_open`
true. Assert the check fails as an invariant violation and no file is written.

---

### Warm start

#### `each_tugs_draws_repeat_across_a_stop`

Run the generator with seed 42 and the three tugs of the roster until each tug
has left the dock 20 times, without a stop, recording each tug's times away and
requested volumes in the order drawn. Run it again with the same seed, stop it
once each tug has left the dock 10 times, and start a new generator from the
report. Assert that each tug's 20 times away and requested volumes are
identical to the first run's: the volume that the warm start draws again
equals the one drawn before the stop, and every later draw follows from it.

#### `passed_due_time_is_acted_on_at_once`

Start the fuel supplier with no order and `next_poll_at` one minute before the
clock's start. Assert the dispatch follows the lead time from a poll made at
the start.

#### `saved_tanker_is_dispatched_at_once`

Start the fuel supplier with `TankerDueAt` 30 seconds before the clock's start.
Assert a `FillJob` is sent without the clock advancing.

#### `saved_tugs_at_the_dock_arrive_first_in_order_of_arrival`

Start the generator for two tugs, both saved at the dock, the second having
arrived first. Assert both arrivals are in the channel at spawn, without the
clock advancing, the second tug's first.

#### `starting_state_not_matching_the_roster_is_refused`

Spawn the generator for two tugs from a state for one. Assert the spawn panics
with a message naming the roster.

#### `reconciliation_user_audits_saved_window`

Start the reconciliation user from a state whose window began one week before
the clock's start, holding a BDN and a delivery ticket from that week. Advance
the clock past the window's end plus `settling_delay`. Assert the retrieval
bounds start at the saved window's start, and the outcome is clean against
records matching the two artefacts.

#### `administrator_waits_for_its_saved_check_time`

Start the systems administrator from a state with `next_check_at` two hours
after the clock's start. Assert the session double records no connection before
two hours have passed, and one session once they have.

#### `administrator_check_already_due_runs_at_once`

Start the systems administrator from a state with `next_check_at` one minute
before the clock's start. Assert a session begins without the clock advancing.

#### `administrator_reads_the_journal_from_its_saved_previous_check`

Start the systems administrator from a state with `previous_check_at` set and
`next_check_at` already passed. Assert the first session's journal command on
each managed host gives the saved time after `--since`, not `--boot`.

#### `warm_start_continues_counters_and_totalisers`

Run the whole sim through one refuel and one fill, stop it, and start a new sim
from the checkpoint. Assert the first ATG reading equals the volume at the
stop, the dispenser totaliser read over Modbus equals its value at the stop,
and the next BDN and delivery ticket numbers follow the last ones issued.

#### `fuel_conserved_across_a_stop`

Run the whole sim to two deliveries, stopping it after the first and
warm-starting from the checkpoint. Assert, as `sim_conserves_fuel_and_only_reorders_below_threshold`
does for an uninterrupted run, that the tank volume reported after the second
delivery equals the initial volume minus every dispensed volume plus every
filled volume recorded by the mock digital world across both runs.

