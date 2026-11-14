# Sim

Build plan for the simulated-physical world: a self-consistent physical
model of the tank and its surroundings, plus the sealed vendor
appliances (ATG console, dispenser, vessel RFID reader, operator RFID
reader) that the port does not install or configure. The simulator owns ground truth; the
digital world only sees the data that crosses the sim <-> digital boundary.

See [`docs/testbed.md`](../docs/testbed.md) for how this document relates to narrative.md and digital.md.

**Contents:**
1. [Physical model](#physical-model)
2. [Logical clock](#logical-clock)
3. [Instruments and field devices](#instruments)
4. [Appliance emulation](#appliance-emulation)
5. [Actors](#actors)
6. [Tug arrival generator](#tug-arrival-generator)
7. [Sim<->digital boundary protocols](#boundary-protocols)
8. [Paper artefacts](#paper-artefacts)
9. [Stopping and restarting](#stop-restart)
10. [Implementation](#implementation)
11. [Open decisions](#open-decisions)

---

## 1. Physical model<a id='physical-model'></a>

**Design principle.** 
Physical infrastructure is modelled with a fidelity that is sufficient to support a realistic boundary between the physical and digital world.
Each physical component is behind an interface; initial implementations are simple
and can be made more complex without changing the interface.

### 1.1 Parameters

Parameters are supplied at startup via configuration and are immutable during a run.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `tank_capacity_L` | float | 50,000 | Maximum tank volume in litres. |
| `k_L_per_mm` | float | 7.0 | Tank geometry constant: litres per millimetre of level. Encodes all geometry the model requires. |
| `rho_ref_kg_per_L` | float | 0.840 | Marine Gas Oil (MGO) reference density at 15 °C. A shared world constant: the FMS holds the same value as `FMS_DENSITY_KG_PER_L` (`host_specs/fms.md §5.2`), both projected from `process.density_kg_per_l` (`deployment/deployment-config.md §3`). |
| `inflow_rate_L_per_min` | float | 600 | Inflow rate when active. The tanker pump is the tank's only inflow source, so this same value is the road tanker driver's pump rate, from which it derives the pump-run duration. A scenario value, projected from `fuel_supplier.tanker_pump_rate_l_per_min` (`deployment/deployment-config.md §3`). |
| `outflow_rate_L_per_min` | float | 300 | Outflow rate when active: the dispenser's flow rate. A scenario value, projected from `process.dispenser_flow_rate_l_per_min` (`deployment/deployment-config.md §3`). |
| `overfill_interlock_pct` | float | 95 | Volume, as a percentage of `tank_capacity_L`, at or above which the overfill interlock opens (§1.3, §3.5). A scenario value, projected from `process.overfill_interlock_pct` (`deployment/deployment-config.md §3`). |

### 1.2 State variables

State variables are the primitive mutable values owned by the physical model. Actor physical interfaces (§5) write directly to the actor-owned rows.

| Variable | Type | Owner | Description |
|---|---|---|---|
| `volume_L` | float | Physical model | Current fuel volume in litres (0–`tank_capacity_L`). |
| `temperature_C` | float | Physical model | Fuel temperature in °C. Constant 15 °C initially; interface supports a richer model. |
| `bund_wet` | bool | Physical model | True when fuel is present in secondary containment. |
| `esd_active` | bool | Dock operator (physical interface) | True when the ESD button is depressed. |
| `tanker_pump_running` | bool | Road tanker driver (physical interface) | True when the road tanker pump is running. |
| `interlock_plug_connected` | bool | Road tanker driver (physical interface) | True when the road tanker's overfill cable is connected at the fill cabinet socket. |
| `dispenser_solenoid_open` | bool | Dispenser appliance (§4.2) | True when the dispenser solenoid is open. Set by the dispenser appliance in response to FMS Modbus register writes. |
| `dispenser_pump_energised` | bool | Dispenser appliance (§4.2) | True when the dispenser pump is energised. Set by the dispenser appliance in response to FMS Modbus register writes. |

On a cold start `volume_L`, `temperature_C` and `bund_wet` take their configured initial values; on a warm start they take the values saved in the checkpoint (§9). The actor-owned and appliance-owned rows start at rest (false) in both cases.

### 1.3 Derived quantities

Derived quantities are recomputed after each state update; none is stored persistently.

| Quantity | Type | Formula |
|---|---|---|
| `overfill_interlock_open` | bool | `volume_L ≥ overfill_interlock_pct / 100 × tank_capacity_L` |
| `inflow_active` | bool | `tanker_pump_running` |
| `outflow_active` | bool | `dispenser_solenoid_open AND dispenser_pump_energised AND NOT esd_active` |
| `level_mm` | float | `volume_L / k_L_per_mm` |
| `vol_corrected` | float | `volume_L` (correction factor 1 at the modelled constant 15 °C) |

`inflow_active` equals `tanker_pump_running`: the overfill interlock, ESD, and overfill cable gate the tanker pump itself (§3.5), so the pump runs only when all are satisfied. `overfill_interlock_open` breaking the hardwired overfill circuit cuts that pump (§3.5). Temperature-corrected volume equals `volume_L`: the fuel is held at a constant 15 °C, the reference temperature, so the correction factor is 1 and no thermal-expansion model is defined. Introducing a varying temperature requires one. The factor it must supply is `ρ(T) / rho_ref_kg_per_L`, since mass is conserved: fuel warmer than 15 °C is less dense and corrects to a smaller volume.

### 1.4 Update loop

The physical model runs in its own async loop, sleeping between updates via `clock.sleep(...)` (§2). At each update, if `inflow_active`, `volume_L` increases by `inflow_rate_L_per_min / 60` multiplied by the step duration in seconds; if `outflow_active`, it decreases correspondingly. Derived quantities are recomputed after the update.

---

## 2. Logical clock<a id='logical-clock'></a>

The logical clock is the authoritative time source for the sim. Where necessary, sim entities interact with time using a `SimClock` handle with an async primitive, `sleep(duration)`. No sim
entity reads wall-clock time directly; in coupled mode the clock itself reads
it once, at construction, as its start date. Entities run concurrently; there
is no enforced ordering between them.

### 2.1 Operating modes

- **Coupled mode.** `clock.sleep(duration)` delegates to
  `tokio::time::sleep(duration)`, maintaining 1:1 wall-clock pace, and the
  sim's calendar starts at the current UTC time, read once at construction.
  Used for: running the simulation in tandem with an emulated digital world.
  Coupled mode has no other pace and no fixed start date, because sim time
  and digital time must be one time base. The digital hosts stamp their
  records from their own clocks, which the testbed cannot accelerate, and their
  cadences — poll intervals, session lifetimes, challenge windows — are real
  time. A paper artefact and the record of the same transfer must be assigned
  to an audit window by the same instant (§8.3), so the date a sim actor
  writes on paper must be the date the digital world would record. Every sim
  date-time is therefore UTC.

  The clock is read **once**, at construction; after that the sim's calendar
  is the start time plus the elapsed time of the runtime's monotonic clock.
  It is not re-read, because the host clock is platform state shared with the
  digital world: on the container profiles every container reads the same
  kernel clock, and anything able to set it would otherwise shift the sim's
  time — a route from the digital world into the physical one outside the
  documented boundary. Unprivileged containers lack the capability to set the
  clock; the privileged exceptions are those `deployment/deployment.md` §7.2
  documents. Reading once confines any remaining exposure to start-up.

  The consequence is that the sim does not follow a step in the host clock
  made after construction. On one kernel the monotonic and wall clocks run at
  the same rate, so there is no gradual drift, but a step — most commonly the
  host resynchronising its clock on resume after it has been suspended —
  moves the digital world and leaves the sim behind by the size of the step.
  Paper artefacts are then stamped behind the records they describe, which at
  a month boundary assigns them to different audit windows. The sim must be
  restarted after any such step.
- **Isolated mode.** The digital world is not connected. Two sub-modes are
  available, selected via `TimeModel` at clock construction:
  - `TimeModel::Synthetic` — simulated time advances only when all tasks are
    `Poll::Pending`. The runtime's `on_thread_park` hook fires at that point,
    advances the clock to the earliest pending `sleep()` deadline, and wakes
    the corresponding tasks. The simulation runs as fast as the host allows.
  - `TimeModel::WallTimeMultiple(f64)` — `clock.sleep(d)` delegates to
    `tokio::time::sleep(d / multiplier)`. The simulation runs at a controlled
    multiple of wall-clock speed.

  An isolated clock starts its calendar at a date supplied by the caller, so
  that tests are reproducible. Neither a fixed start date nor a pace other than
  1:1 is valid against the digital world.

### 2.2 Compressed scenarios

`narrative.md` §6 (deviations from real-world practice) states that "A compressed scenario shortens every interval and duration in the port's operation by one stated factor, and raises every rate of fuel flow by the same factor." That factor is the compression factor, which an additional override file sets (`deployment/deployment.md` §7.3, additional override file). Without an additional override file that sets it, the compression factor is 1. A timing's or a rate's value in operation is its value when the compression factor is 1.

In a compressed scenario the clock keeps the one-to-one pace of coupled mode (§2.1, operating modes). The compression factor changes the values that the sim runs with.

- `render.py`, which projects `world.yaml` into the sim at build time, multiplies the inflow and outflow rates (§1.1, parameters) by the compression factor. `render.py` divides by the compression factor each tug's mean time away (§6, tug arrival generator), and the fuel supplier's poll interval and lead time (§5.3, fuel supplier). `render.py` also divides the interval of the systems administrator's routine check (§5.2, per-actor surface), the interval of the dock operator's poll of a job's status, and the FMS session lifetime. For the reconciliation user, `render.py` divides the audit period, the settling delay, and the time either side of the period over which the audit also retrieves transaction records. A compressed scenario measures the audit period in hours (`narrative.md` §6, deviations from real-world practice). The sim receives every value that `render.py` multiplies or divides already scaled (`deployment/deployment-config.md` §4, projection targets).
- The time ranges in which the reconciliation user retrieves a period's records follow from the rate at which transaction records accrue. The dock's arrival rate at the scaled times away sets that rate (§6, tug arrival generator). The sim therefore holds no compression factor of its own (`host_specs/sim/sim-impl.md`, "Compressed scenarios").

The retries, back-offs and time limits of the sim's requests to the digital world are timings of software and networks, outside the port's operation, and a compressed scenario leaves them unchanged.

### 2.3 Start state

The start state is the state of the port's operation from which a run starts. The sim starts with 35,000 L in the tank and with no order placed for a road tanker delivery (`host_specs/sim/sim-impl.md`, "Start state"). Every tug starts away from the dock, and arrives one drawn time away after the start (§6, tug arrival generator). The readings of the dispenser's cumulative totaliser and of the road tanker's meter start at zero. The first bunker delivery note (BDN) and the first delivery ticket of a run are numbered 1 (§8, paper artefacts), and the reconciliation user holds no paper artefact from before the start.

The fuel supplier's first poll comes one `poll_interval` after the start (§5.3, fuel supplier). The systems administrator's first routine check comes one interval of the routine check after the start (§5.2, per-actor surface). With a calendar audit period, each audit runs one settling delay after a calendar boundary (`host_specs/sim/sim-impl.md`, "Reconciliation user"). With an audit period in hours (§2.2, compressed scenarios), the time of the first audit is supplied at startup (§1.1, parameters).

---

## 3. Instruments and field devices<a id='instruments'></a>

Each instrument reads from sim state and produces a signal. Noise and
failure modes are behind interfaces; initial implementations are ideal
(no noise, no failure).

### 3.1 ATG probe

Reads `level_mm` and `temperature_C` from the physical model. Exposes
them to the ATG console appliance (§4.1). Noise interface present but off by default. When enabled, level noise
uses a standard deviation of 0.25 mm (Veeder-Root Mag-FLEX tall-tank
probe ±0.5 mm repeatability, treated as the ±2σ bound).

### 3.2 Bund leak sensor

Reads `bund_wet`. Produces a binary wet/dry signal to the ATG console.
Alarm latches on the ATG console (stays alarmed until physically cleared
and manually acknowledged), matching Veeder-Root Tank Level System (TLS) default behaviour.

### 3.3 Dispenser PD flow meter

Watches `outflow_rate` from the fuel tank. When the outflow rate is
zero, no pulses are emitted. When non-zero, pulses are emitted at the
interval derived from the rate and the configured K-factor. Behind an
interface; K-factor is configurable. Exposed to the dispenser appliance
(§4.2) which presents it to the FMS over Modbus TCP.

### 3.4 ESD button

Physical state: `esd_active` (bool). Set by the dock operator actor.
Hardwired — when `esd_active` is true, `outflow_active` evaluates to false
(§1.3), and the ESD breaks the fill-cabinet interlock circuit, cutting the
tanker pump and so stopping inflow (§3.5). Not on any network.

### 3.5 Overfill probe and interlock circuit

Reads `overfill_interlock_open` (§1.3). Presents a dry-contact signal on
the fill cabinet socket. The circuit exists only while the road tanker
driver's overfill cable is connected (`interlock_plug_connected`, §1.2):
connecting the cable establishes the circuit, disconnecting it removes it.
While the circuit exists it is live unless `overfill_interlock_open` is true
or the ESD is pressed, either of which breaks it and cuts the tanker pump,
stopping inflow. Hardwired — not on any network.

---

## 4. Appliance emulation<a id='appliance-emulation'></a>

Sealed vendor boxes whose internals the port does not configure. Each
emulator's external interface must be indistinguishable from the real
product.

### 4.1 ATG console — Veeder-Root Tank Level System (TLS) 450PLUS class

Polls the ATG probe (§3.1) and bund sensor (§3.2). Implements the
proprietary Tank Level System (TLS) serial-over-TCP protocol on TCP port 10001. The FMS polls this port
using standard Tank Level System (TLS) function codes.

Implements the following Tank Level System (TLS) function codes (subset sufficient for the
FMS to operate). The exact wire format is specified in [tls-protocol.md](specs/host_specs/sim/tls-protocol.md).

| Code | Name | Description |
|---|---|---|
| `i201` | In-Tank Inventory Report | Per-tank record: volume, TC volume, ullage, height, water, temperature, water volume |
| `i205` | In-Tank Status Report | Per-tank list of active tank alarms |
| `i101` | System Status Report | Active alarm/warning entries (the bund liquid-sensor alarm appears here) |

### 4.2 Dispenser

Receives preset volume and pump enable from the FMS over Modbus TCP.
Manages its own solenoid state and totaliser. Auto-stops (closes
solenoid, disables pump) when totaliser reaches preset. The Modbus wire
protocol, addressing, function codes, register encodings, and exception
handling are specified in
[dispenser-modbus-tcp-spec.md](specs/host_specs/sim/dispenser-modbus-tcp-spec.md).
Exposes the following Modbus registers:

| Register | R/W | Width | Description |
|---|---|---|---|
| 0x0001 | R/W | 16-bit | Preset volume (litres × 10, integer) |
| 0x0002 | R/W | 16-bit | Pump enable (1 = enable, 0 = disable) |
| 0x0003 | R | 32-bit (high word) | Cumulative totaliser, bits 31–16 |
| 0x0004 | R | 32-bit (low word) | Cumulative totaliser, bits 15–0 (pulses, non-resettable) |
| 0x0005 | R | 32-bit (high word) | Transaction totaliser, bits 31–16 |
| 0x0006 | R | 32-bit (low word) | Transaction totaliser, bits 15–0 (pulses since last preset write) |
| 0x0007 | R | 16-bit | Status flags (see sim-impl.md for bit definitions) |

Pulse arithmetic uses K-factor 100 pulses/litre (configurable via
interface). The cumulative totaliser (0x0003–0x0004) is monotonically
non-decreasing; any reset is an audited event in the FMS. The
transaction totaliser (0x0005–0x0006) resets to zero each time a new
preset is written to register 0x0001. Both totalisers are 32-bit values
stored as a big-endian high/low word pair across two consecutive
registers, following the standard Modbus convention for 32-bit
quantities.

### 4.3 Vessel RFID reader

Mounted at the dispenser. Reads the tug's RFID tag when `vessel_at_dispenser`
is set by the tug engineer actor. Presents the card number to the
FMS over a TCP connection at the reader's own OT address. The FMS reads
the card number and validates it against the vessel named on the open dispense job.


### 4.4 Operator RFID reader

Mounted at the HMI workstation. Reads the dock operator's personal RFID
card when the operator actor presents it during login. Presents the card
number to the HMI host using the same
mechanism as the vessel reader (§4.3). The FMS validates the card number against
its internal card table.

---

## 5. Actors<a id='actors'></a>

The actors named in narrative §3 are the **dock operator**, **road tanker driver**, **tug engineer**, **reconciliation user**, **systems administrator** and **fuel supplier**.
They are sim-side entities, outside the digital world. Each
actor has two distinct interfaces:

- **Digital interface.** Normal use of digital-world UIs and APIs (HMI
  login, FMS form entry, RFID tap at the HMI). From the digital world's
  perspective an actor is indistinguishable from a real human user;
  authentication, session handling, and audit logging all apply
  unchanged.
- **Physical interface.** State mutations on sim objects representing
  bodily actions. These are the only way actors affect physics directly.

Nothing flows unless both interfaces are consistent. The FMS can enable
the dispenser and the pump can energise, but if `outflow_active` is
blocked by a physical constraint the sim faithfully reflects the unsafe
outcome. This split is what lets the testbed expose scenarios like
"digital world believes a clean 500 L dispense; sim ground truth shows
a different physical state."

### 5.1 Actor model

Each actor is implemented as a single Rust `async fn`. The sequential
body encodes the actor's causal behaviour directly: each `.await` is an
event boundary — a blocking wait for a digital-world response or an
inter-actor channel signal. Rust's async runtime generates the underlying
state machine; no explicit state enum is needed.

All actors are scripted — there is no human-in-the-loop mode and no
external control interface.

The dock operator establishes one authenticated session at shift start
and loops over new jobs, branching on job type. This
reflects the real-world structure: one person, one session, sequential
non-concurrent jobs (narrative §5).

Digital-interface calls are real protocol requests to the digital world.
The dock operator's calls go to the HMI (`hmi-01`) — the interface a
real operator uses — which communicates with the FMS on the operator's
behalf. The digital world's responses are the primary event source for
subsequent actions.

Actor tasks coordinate via typed `tokio` channels when one actor's
digital confirmation must precede another actor's physical action. Direct
channels are used only where a physical action requires a specific prior
digital confirmation — for example, the dock operator signals the tug
engineer once the HMI has answered, and the engineer writes the vessel card
number to the RFID reader only if the job is active in the FMS. Where the FMS
refuses the job instead, the signal says so and the tug departs unserved.

All actors are spawned at sim startup and run until the sim is stopped (§9).

### 5.2 Per-actor surface

#### Dock operator

- **Digital:** login (user/pass + RFID at HMI); enter tug refuelling
  job request (vessel, grade, volume); enter fill job request (tanker
  registration, grade, volume); set dispenser preset and start pump
  (after RFID validation); cancel a job the FMS has refused to start, so the
  next job can be entered; read tank state from the HMI, which is the only
  route to the fuel temperature the BDN carries (§8.4.1); transcribe delivery
  ticket fields into HMI (after fill); enter BDN number into HMI (after
  dispense).
- **Physical:** press ESD button; sign paper BDN; sign paper delivery
  ticket.

#### Road tanker driver

- **Digital:** *(none — does not authenticate to port systems)*
- **Physical:** plug/unplug overfill cable; hand over signed paper
  delivery ticket.

#### Tug engineer

- **Digital:** *(none — does not authenticate to port systems)*
- **Physical:** present vessel RFID tag at dispenser reader; sign paper
  BDN.

#### Reconciliation user

- **Digital:** login to the office workstation `recon-01` (user/pass,
  validated against the directory on `corp-it`); separate login to the
  log-server replica (user/pass on its built-in user store); retrieve the
  period's transaction event records for the end-of-period audit. The two
  credentials are distinct.
- **Physical:** *(none)*

The audit retrieves log records only. The tank-volume time series is
provisioned on the replica but is not on the audit path: every check the audit
performs is reachable from the transaction events and the paper artefacts, and
the tank volumes each transaction opened and closed at are carried on the events
themselves. What the audit retrieves and what it checks are specified in
`host_specs/sim/sim-impl.md`.

#### Systems administrator

- **Digital:** sign in to `admin-01` over Secure Shell (SSH) (user/pass, validated against the directory on `corp-it`); from that session, connect through `dmz-bastion-01` to `fms-01`, `hist-01` and `hmi-01` in turn with the key held on `admin-01`; run the routine check on each, with `sudo` where it needs elevated privileges.
- **Physical:** *(none)*

Each check begins `intervals.admin_check_interval_h` simulated hours after the previous check ends, and changes nothing (narrative §3; `deployment/deployment-config.md` §3). A compressed scenario divides the check interval by the compression factor (§2.2, compressed scenarios). The start state places the first check after a cold start (§2.3, start state), and a warm start keeps the saved due time (§9). The check confirms that each host's services are running, its disks have space, its clock is synchronised and its journal shows no new errors. The commands it runs are specified in `host_specs/sim/sim-impl.md`.

### 5.3 Fuel supplier

- **Digital:** initiates real TCP connections to
  `atg-relay-01` on TCP 10001 and polls ATG state using Veeder-Root Tank Level System (TLS)
  function codes (i201, i101).
- **Physical:** on dispatch, sends a `FillJob` to the road tanker driver after `lead_time` elapses; receives the driver's signal that the delivery has ended.

The actor is implemented as a single `async fn`: a loop that awaits
each poll interval and reads current volume and alarm state. It polls at
every interval, including while an order is pending, since the supplier
monitors the tank level continuously (narrative §2.1); an order's lead time
and delivery do not delay the next poll.

**Parameters:**

Each is a scenario value, projected from `world.yaml`'s `fuel_supplier`
section (`deployment/deployment-config.md §3`).

| Parameter | Description | Default |
|---|---|---|
| `poll_interval` | Time between ATG polls. From `fuel_supplier.poll_interval_s`. | 15 min |
| `reorder_threshold_L` | ATG-reported volume at or below which a delivery is dispatched, provided no critical alarm is active. From `fuel_supplier.reorder_threshold_l`. | 20,000 L |
| `lead_time` | Elapsed sim time between dispatch decision and the road tanker's arrival. From `fuel_supplier.lead_time_s`. | 6 h |
| `delivery_volume_L` | Volume the arriving tanker carries. From `fuel_supplier.delivery_volume_l`. | 18,000 L. The narrative's 15,000–20,000 L describes the default scenario. |

A compressed scenario divides `poll_interval` and `lead_time` by the compression factor (§2.2, compressed scenarios). The fuel supplier's first poll comes one `poll_interval` after the start (§2.3, start state).

**Decision logic:**

On each poll: (1) read current volume via i201; (2) read alarm state via
i101; (3) if `reported_volume_L ≤ reorder_threshold_L` and no critical alarm
(overfill, leak) is active and no order is pending, set the delivery-pending
flag, log the order with the poll's time, the reported volume and the road
tanker's scheduled arrival, one `lead_time` after the poll, and, after
`lead_time` elapses, send a `FillJob` to the road tanker driver. The order remains pending, and the flag set, until the road tanker
driver signals that the delivery has ended: the full volume delivered, the
delivery stopped part-way by the interlock circuit breaking (§3.5), or the
tanker pump never starting. 
A poll while an order is pending does not place an order. Therefore at most one order is pending at a time, and in normal operation the tank holds no more than `reorder_threshold_L` when a tanker arrives. The render's check that a delivery "fits" below the overfill interlock level relies on this (`deployment/deployment-config.md §5`).

**Specifics:**
The actor binds to the sim host's `vnet-ext` network interface and
initiates TCP connections to `atg-relay-01:10001`. It issues i201 and
i101 requests and parses the ATG console's standard responses.

### 5.4 What the digital world may influence<a id='containment'></a>
The digital world must not reach the mechanism the sim runs on aside from the response to expected data received. 
This can be enforced by:

- **The sim initiates every connection to the digital world and accepts none.** 
    Outbound flows are enumerated in `sim-network.md` §5; the packet filter drops inbound connections on the pure-egress interfaces rather than relying on nothing listening there (`sim-network.md` §1, §8).
- **No digital-world host may cause a write to the sim's filesystem.** 
    No actor acquires a file-transfer or remote-access client in order to retrieve data, and no retrieval names a destination the far end can influence. 

These impact the reconciliation user most, because it is the only actor that retrieves bulk data rather than exchanging protocol messages, and its return path is chosen to satisfy them: the audit is bookkeeping, and no bookkeeping should justify a channel by which the digital world could act on the sim outside of the specified scope. `recon.md` §11 records the obligations that follow for that actor.

---

## 6. Tug arrival generator<a id='tug-arrival-generator'></a>

The tug arrival generator is a standalone sim entity that models the port's tugs coming to the dock to take on fuel. 
It runs one task per tug on the roster, for the duration of the sim, and each task follows its tug through a cycle: away from the dock, arrive, wait if the dock is occupied, refuel, leave (narrative §2.2).

On each cycle, for one tug: 
(1) draw a time away, exponentially distributed with the tug's `mean_time_away_s`; 
(2) draw the requested volume, uniformly between `refuel_volume_min_L` and `refuel_volume_max_L`; 
(3) log the tug's scheduled arrival, one time away after the draw, with its requested volume; 
(4) call `clock.sleep(time_away)`;
(5) send a `DispenseJob` for the tug to the tug engineer; 
(6) wait for the tug to leave the dock, which it does once the tug engineer has handed back the co-signed BDN, or once it departs unserved where
the FMS refuses the job (§5.1). 
The next time away starts when the tug leaves, so a tug that is waiting or refuelling does not arrive again.

**Order of service.** The dock operator handles one job at a time and completes it before starting the next (narrative §2.2, §5). 
Waiting tugs are handled in the order in which they arrived, and a road tanker waiting to deliver is handled before any waiting tug. 
Each tug has at most one job outstanding, so the channel to the tug engineer holds one job per roster tug and a send never blocks; one tug's waiting does not delay another tug's arrival.

**Arrival rate.** The dock's arrival rate is approximately the sum, over the roster, of one over each tug's `mean_time_away_s`. Time spent waiting and refuelling lowers it, appreciably only when that time is not small compared with the mean time away.

**Parameters:**

| Parameter | Description | Default |
|---|---|---|
| Roster | The tugs, each with its name, International Maritime Organization (IMO) number, RFID card and `mean_time_away_s`. | From `world.yaml` (`roster.vessels`, `deployment-config.md` §3): three tugs. |
| `mean_time_away_s` | For each tug, the mean time between the tug leaving the dock and its next arrival, in seconds. | From `world.yaml` (`roster.vessels[].mean_time_away_s`): 72,000 s (20 h) for each tug. |
| `refuel_volume_min_L`, `refuel_volume_max_L` | Bounds of each tug's requested volume, the same for every tug. | From `world.yaml` (`roster.refuel_volume_min_l`, `roster.refuel_volume_max_l`): 500 L and 3,000 L (narrative §2.2). |
| `seed` | PRNG seed for reproducible sequences. Must be supplied at startup. | — |

Each tug's random sequence is derived from `seed` and the tug's IMO number.
Subsequent runs with the same seed reproduce each tug's times away and requested volumes, and adding or removing a tug leaves the other tugs' sequences unchanged. A tug's arrival times also depend on when it left the dock, and therefore on how long it waited and was refuelled. A warm start continues each tug's sequence from the position saved in the checkpoint, between the draws of its time away and its volume, and draws the volume again (§9.3).

---

## 7. Sim <-> digital boundary protocols<a id='boundary-protocols'></a>

The exact protocols presented on the OT LAN. digital.md §3 is the
mirror of this section from the digital world's perspective. The Tank Level
System (TLS) wire format is specified in [tls-protocol.md](specs/host_specs/sim/tls-protocol.md);
the Modbus TCP wire format in
[dispenser-modbus-tcp-spec.md](specs/host_specs/sim/dispenser-modbus-tcp-spec.md).

| Signal | Transport | Direction | Detail |
|---|---|---|---|
| ATG level, temperature, volume, alarms | Tank Level System (TLS) serial-over-TCP port 10001 | ATG console → FMS | Veeder-Root Tank Level System (TLS) function codes i101, i201, i205 (see tls-protocol.md) |
| Dispenser preset + enable | Modbus TCP | FMS → dispenser | Registers 0x0001–0x0002 (see dispenser-modbus-tcp-spec.md) |
| Dispenser totaliser + status | Modbus TCP | Dispenser → FMS | Registers 0x0003–0x0007 (see dispenser-modbus-tcp-spec.md) |
| Vessel RFID card number | TCP, port 9001 | RFID reader → FMS | 26-bit card number; no return channel. |
| Operator RFID card number | TCP, port 9002 | Operator RFID reader → HMI | 26-bit card number; no return channel. Same mechanism as vessel reader. |
| ESD contact state | Hardwired dry contact | ESD button → pumps/valves | Not on network; internal sim state only |
| Overfill interlock circuit | Hardwired dry contact | Overfill probe → fill cabinet socket | Not on network; internal sim state only |

---

## 8. Paper artefacts<a id='paper-artefacts'></a>

Two structured data objects in the sim. Created by actor actions;
transcription into the FMS is a digital-side operation performed by the
dock operator actor.

### 8.1 DeliveryTicket

Created by the road tanker driver actor when a fill completes. Fields per
narrative §2.1:

```
ticket_number          string   Sequential, unique
delivery_commenced_at  datetime Start of the transfer, not of the artefact (§8.3)
grade                  string   e.g. "MGO"
gross_volume_L         float
temperature_C          float    Observed fuel temperature at the transfer
temp_corrected_volume_L float   Gross volume corrected to 15 °C
density_kg_per_L       float    Density at 15 °C
start_totaliser        float    Tanker meter reading at start
end_totaliser          float    Tanker meter reading at end
tanker_registration    string
driver_id              string
signed_by_driver       bool
signed_by_operator     bool
```

### 8.2 BDN (Bunker Delivery Note)

Created by the dock operator actor when a dispense completes. Fields per
narrative §2.2 and the ten items of MARPOL Annex VI Appendix V (item number in
brackets).

The bracketed numbers are the item numbers of Appendix V, the bunker delivery
note's required content, as amended by resolution MEPC.362(79). That amendment
added the flashpoint as item 9, making the supplier's declaration of conformity
item 10. A field without a number is not an Appendix V item: narrative §2.2 or
the reconciliation audit requires it.

```
bdn_number              string   Sequential, unique
delivery_commenced_at   datetime Start of the transfer, not of the artefact (§8.3) [3]
vessel_id               integer  Vessel IMO number; matches RFID-validated vessel [1]
vessel_name             string   Vessel name [1]
port                    string   "Ravensport" [2]
supplier_name           string   "Ravensport Harbour Authority" [4]
supplier_address        string   "Fuel Dock, East Quay, Ravensport RV1 4QD" [4]
supplier_telephone      string   "01632 960482" [4]
grade                   string   Product name, e.g. "MGO" [5]
volume_L                float    Dispenser meter totaliser delta
temperature_C           float    Observed fuel temperature at the transfer
temp_corrected_volume_L float    volume_L corrected to 15 °C
quantity_t              float    temp_corrected_volume_L × density at 15 °C, in tonnes [6]
density_kg_per_m3       float    Density at 15 °C [7]
sulphur_pct_mm          float    Sulphur content, % m/m [8]
flashpoint_C            float    Flashpoint [9]
sulphur_limit_declared  bool     Declaration of conformity with regulation 14.1 [10]
start_totaliser         float    Dispenser meter reading at start
end_totaliser           float    Dispenser meter reading at end
operator_id             string   The dock operator's username
engineer_id             string   Tug engineer
signed_by_operator      bool     The supplier's representative's signature [10]
signed_by_engineer      bool
```

**The supplier is the port.** Appendix V's supplier is the party delivering
the fuel to the ship, which here is the Ravensport Harbour Authority (narrative,
introduction); its representative, who signs the declaration, is the dock operator. The
fuel supplier actor (§5.3) supplies the port, not the tug, and does not appear
on the BDN. The authority's address and telephone number are fictional: `RV` is
not a United Kingdom postcode area, and 01632 960xxx is a range Ofcom reserves
for drama.

**The fuel properties are scenario values, not measurements.** `grade`,
density, sulphur content and flashpoint are the single grade's constants from
`world.yaml`'s `process` section (`deployment-config.md` §3). The flashpoint of
66 °C is a chosen value above the ISO 8217 distillate minimum of 60 °C. Item 10's
declaration is against regulation 14.1, the global 0.50% m/m limit, which a
0.10% m/m fuel meets whether or not the port lies in an emission control area.
`quantity_t` is the mass in vacuum; the commercial weight-in-air convention,
about 0.1% lower, is not modelled.

The fields without an item number are design choices rather than MARPOL
requirements. The regulation 18.8.1 fuel sample is not modelled.

### 8.3 Commencement, not creation<a id='commencement'></a>

`delivery_commenced_at` on both artefacts is the instant the fuel transfer
**began**, not the instant the artefact was constructed. MARPOL Annex VI
Appendix V requires the date of commencement of delivery on a bunker delivery
note, and the same instant serves the end-of-period audit.

Both artefacts are created at completion — the BDN by the dock operator when a
dispense completes, the delivery ticket by the road tanker driver when a fill
completes — so neither may be dated by a clock read at creation. Each start
instant is recorded once, on one side of the sim<->digital boundary, and copied
to the other:

- **Fill.** The road tanker driver reads the clock when the pump starts and
  carries the instant forward to the ticket. The dock operator transcribes it,
  and the FMS records it as the transaction's `opened_at`
  (`host_specs/fms.md` §7).
- **Dispense.** The FMS records the instant it starts the dispense as
  `opened_at`. The dock operator reads it from the HMI once the dispense is
  complete and writes it on the BDN (`narrative.md` §2.2). An instant read from
  the dock operator's own clock would precede the FMS's by the time the start
  request takes, or by longer where a lost response is retried, so a dispense
  started just before a period boundary would put its paper and its record in
  different windows.

**The same instant defines the transaction's audit window on both sides.** The
log-server events carry it as `opened_at`, which `hist.md §4.2` places on
`transaction_closed` for dispenses and fills alike. Paper and record must be
assigned to a window by the same instant: if one side used commencement and the
other completion, a transfer spanning midnight at a month boundary would appear
as a record with no paper artefact in one month and paper with no record in the
next — two discrepancies, on correct data.

A transfer's four log events are emitted at four different instants, so a window
taken on event timestamps alone splits a boundary transfer across two months.
The audit therefore groups events into transactions before assigning them to a
window; `host_specs/sim/sim-impl.md` specifies how.

### 8.4 Temperature and density<a id='temperature-density'></a>

Both artefacts carry the observed fuel temperature, the density at 15 °C, and
the meter volume corrected to that reference. MARPOL Annex VI Appendix V
requires the density at 15 °C on a bunker delivery note. It does not require the
temperature, which regulation 18.5.2 asks for only with low-flashpoint and gas
fuels; the temperature and the corrected volume are carried so that the
correction is checkable against its inputs. What the correction does for
narrative §5's ±1% check differs between the two processes, and §8.4.1 sets out
why.

**Density is a property of the grade, not a per-transfer measurement.**
`rho_ref_kg_per_L` (§1.1) is the reference density of the fuel in the tank; both
artefacts record it rather than measuring it. The BDN states it in kg/m³, the
unit Appendix V item 7 specifies; the delivery ticket states it in kg/L.
**Temperature is the fuel
temperature the physical model holds** (`temperature_C`, §1.2). The correction is
`vol_corrected` as §1.3 defines it.

**The correction is identity while the temperature is constant**, and §1.2 holds
it at 15 °C. The fields are therefore specified so the arithmetic is correct
rather than because it currently changes a result, and they stay correct when a
richer thermal model is introduced. The same applies to the ATG side: the
temperature-compensated volume the console reports equals the raw volume today
(`host_specs/sim/tls-protocol.md §6.2`).

#### 8.4.1 Where the temperature comes from, and what the correction buys

**The port has one temperature sensor.** narrative §4.1 gives the ATG probe as a
level and temperature probe, and the local indicator it carries at the tank is a
*level* indicator; the console in the office "exposes level, temperature, and
alarm state to the FMS and HMI" (§4.4). The dock operator's only route to a
temperature value is therefore the HMI, and that is a property of the modelled
port rather than a gap in this specification.

**On a dispense, the correction must still be applied — but its *value* does not
affect the outcome.** One temperature governs both sides, so once the meter side
is corrected the factor divides out of the ratio:

```
|k·ΔV_atg − k·V_meter| / (k·V_meter)  =  |ΔV_atg − V_meter| / V_meter
```

The correction is not optional. The ATG figures are recorded already compensated
(`host_specs/hist/hist.md §4.2`), so leaving the meter side uncorrected — or
correcting the ATG side a second time — leaves the factor in the ratio, and the
check then measures the temperature rather than the volumes. At a factor of 0.985 both
mistakes produce a 1.5% divergence on correct data, which fails a ±1% tolerance.

What cancels is the dependence on *what the temperature is*. Applied on exactly
one side, the check's outcome is the same for any value of `k`. That is why the
provenance of the dispense-side temperature does not matter to the check, and
transcribing it from the HMI costs the check nothing.

**On a fill it does not cancel, and the correction changes the result.** The
delivery ticket's corrected volume and density are the road tanker's own
measurement, taken by its sealed meter at the tanker's temperature (§8.1). The
ATG side is the shore tank at the shore probe's temperature. Two independent
measurements, two different factors, and the correction is what makes them
comparable. This is why §8.1 carried density and a corrected volume before §8.2
did.

**What the BDN's temperature is for**, then, is making the corrected volume
checkable against its inputs — not an input the ±1% check depends on.
Transcription still binds the value at the moment it is written, so a later
alteration of the stored event is detectable against the paper. What it cannot
detect is a compromised ATG reporting the wrong temperature in the first place,
which is true of every ATG-derived value in the environment.

**Both temperatures are a constant 15 °C.** The shore tank's is `temperature_C`
(§1.2). The road tanker's product temperature is a constant held by the road
tanker driver, in the manner of `driver_id` and `tanker_registration`, and is
never derived from the shore tank: the independence of the two measurements is
what gives the ±1% check force on a fill. At 15 °C the correction factor is 1
(§1.3), so no thermal-expansion model is defined; one is required before either
temperature is allowed to vary.

**A note on the delivery ticket's fields.** narrative §2.1 lists the ticket as
carrying grade, volume, density, totalisers, tanker registration and driver
identity, and does not name temperature. §8.1 carries it because a
temperature-corrected volume cannot be produced without one: the tanker's meter
measures the temperature it corrects to. The field is derived from what the
narrative states rather than added beside it.

---

## 9. Stopping and restarting<a id='stop-restart'></a>

The sim holds its state in memory. A stop requested by the sim host's service
manager brings every actor to rest and writes the sim's state to a
**checkpoint**. A start supplied with a checkpoint continues from it (a **warm
start**); a start without one begins from the configured initial values (a
**cold start**). A crash writes no checkpoint. In the narrative a stop and warm start
is a planned maintenance window (`narrative.md` §3): no new transfer begins,
transfers under way are completed, and arrivals during the window are served
when it ends.

The digital world's records persist across a stop of the sim: the FMS database,
the log servers and the security monitoring archives. A cold start following that
history contradicts it: the tank returns to its initial volume, an uncommanded
step change in the ATG readings (`narrative.md` §5, measurement integrity 5);
the dispenser totaliser returns to zero, a reset (measurement integrity 4); BDN
and delivery ticket numbers repeat; and the paper artefacts awaiting the next
audit are lost, so that audit finds log-server records with no paper. The
checkpoint carries every quantity whose loss would produce one of these contradictions.

### 9.1 Stop request

The stop request is `SIGTERM`, sent by the service manager
(`host_specs/sim/sim-network.md` §7). On receiving it:

1. **No new work begins.** No further tug arrives, the
   fuel supplier makes no further poll or dispatch, the reconciliation user
   begins no further audit, and the systems administrator begins no further
   check. Each records when its next action is due. A tug that leaves the dock
   as the sim comes to rest draws its next time away and requested volume, as
   at any departure (§6).
2. **Work in progress finishes.** A refuel or fill already taken up by its
   actor runs to completion, including the signing of its paper artefact and
   the transcription into the HMI. An audit already begun runs to the end of
   its session, and so does a routine check. The physical model, the instruments and the appliances continue
   to run, so the digital world's polls are answered throughout.
3. **Work offered but not taken up is recorded, not begun.** A tug's arrival
   that the tug engineer has not taken up is saved with its tug, at the dock,
   and is sent to the tug engineer again at the warm start. A tug's arrival
   that is due and has not been sent is saved with its due time. A `FillJob`
   waiting for the road tanker driver is saved as pending work, due at the time
   it arrived.
4. **The checkpoint is written once every actor is at rest**, and the sim
   exits with status 0. The reconciliation user's state is taken after the dock
   operator has come to rest, so that it includes every paper artefact the dock
   operator has posted.

At rest no flow is active and every actor-owned and appliance-owned row of
§1.2 is false. The sim confirms this before writing; a sim not at rest writes
no checkpoint and exits non-zero. A failure while coming to rest is fatal, as at
any other time, and writes no checkpoint.

The time taken to come to rest is that of the longest activity in progress: a
fill (half an hour for 18,000 L at 600 L/min, with its transcription) or an
audit session. The operator works one job at a time (narrative §5), so a refuel
and a fill are not both in progress.

### 9.2 Checkpoint contents

| Owner | Saved state |
|---|---|
| Physical model | `volume_L`, `temperature_C`, `bund_wet` |
| ATG console | The latched alarm state |
| Dispenser | The totalisers |
| ATG probe | The noise generator's seed and position in its sequence |
| Tug arrival generator | Its seed; for each tug, the position in the tug's sequence between the draw of its current time away and the draw of its volume, and either the due time of its next arrival, for a tug away from the dock, or the job of its arrival, for a tug at the dock whose arrival the tug engineer has not taken up |
| Tug engineer | Nothing: an arrival that it has not taken up is saved with its tug |
| Dock operator | The last BDN number issued |
| Road tanker driver | The tanker meter totaliser; the last delivery ticket number issued |
| Fuel supplier | The due time of its next poll; and, for a pending order whose tanker the road tanker driver has not taken up, the time at which the tanker is due or arrived |
| Reconciliation user | The current audit window; the paper artefacts drained and held for it or a later window, and those posted since the last drain |
| Systems administrator | The due time of its next check; the start time of its previous check, from which the next check reads the journal |
| The checkpoint itself | The sim time at which it was written; the checkpoint format version; the compiled parameters it was written under (§1.1, §5.2, §5.3, §6, and `host_specs/sim/sim-network.md` §7) |

Every due time is an absolute UTC date-time on the sim's calendar (§2.1).
Sessions and connections are not saved: at rest none is in use except the dock
operator's HMI session, which the dock operator opens again on start, as at
the start of a shift.

### 9.3 Warm start

The checkpoint is read before any entity is spawned, from the read-only path
given in `host_specs/sim/sim-network.md` §7. If no checkpoint is present the sim
cold-starts.

A checkpoint is refused, as a startup fault, if its format version or any of its
parameters differs from the binary's, or if the sim's start time lies outside
the interval from the time at which the checkpoint was written to
`max_start_gap` (5 minutes) after it. A start before the checkpoint would place
the digital world's history later than the sim's present. A start more than
`max_start_gap` after it would leave a gap between the history and the
restored run, and shows that the host clock was not set back to the snapshot time
before the restore (`deployment/deployment.md` §8.2). The sim cannot close
such a gap by starting its own calendar at the checkpoint time: the digital
world stamps its records from the host clock, and sim time and digital time are
one time base (§2.1). `max_start_gap` is a sim-only parameter, compiled in, and
admits no override. A refused checkpoint is not replaced by a cold start, which
would produce the contradictions the checkpoint exists to prevent.

Each entity starts from its saved state. A due time later than the start is
waited for on the sim clock; one already passed is acted on at once, pending
work first and then in order of due time. The reconciliation user audits its
saved window, not a window beginning at the start.

Each random sequence continues from its saved position, so a warm start
continues the run as if it had not stopped. A tug draws its time away and its
requested volume together, at its departure, and logs them (§6). The checkpoint
saves each tug's position between the draw of its time away and the draw of its
volume, and the warm start draws the tug's requested volume again from there.
That draw gives the volume that the uninterrupted run drew, and leaves the
tug's sequence where that run left it, so a restored run repeats the
uninterrupted run's draws. A warm start logs the scheduled arrival of each tug
that is away again, and the fuel supplier's pending order, since the sim host's
log does not survive the stop (`host_specs/sim/sim-network.md` §7, image,
build, and supervision). For a tug at the dock, whose job carries its volume,
the draw only moves the sequence past it. A **reseed file** supplied beside the
checkpoint gives a new seed for one or more sequences; each such sequence
starts again from its new seed, and a new tug arrival seed starts every tug's
sequence again, from which each tug draws its requested volume. A due time
already drawn is kept.

### 9.4 Containment

The checkpoint is consistent with §5.4. It is written only on the service
manager's stop request, to a fixed path on the sim host's in-memory filesystem,
and carries the sim's own typed values. It is read from a read-only mount that
the deployment supplies and no digital-world host can reach.

---

## 10. Implementation<a id='implementation'></a>

The sim is implemented in **Rust**.

| Concern | Approach |
|---|---|
| Async runtime | `tokio` — concurrent TCP listeners for ATG (port 10001) and Modbus (port 502) run as independent tasks |
| Modbus TCP server | Custom MBAP/PDU framer on `tokio::net` (see [dispenser-modbus-tcp-spec.md](specs/host_specs/sim/dispenser-modbus-tcp-spec.md)); `tokio-modbus` is a dev-dependency used as the test client only |
| ATG Tank Level System (TLS)-over-TCP | Custom Veeder-Root Tank Level System (TLS) framer on `tokio::net::TcpListener` |
| Physical model | Single-threaded state machine, mutated by tokio tasks via `Arc<Mutex<...>>` or message-passing channels |
| Actor implementation | Each actor is a single `async fn`; inter-actor coordination via typed `tokio` channels. See §5.1 and [`docs/testbed.md`](../docs/testbed.md), "Actor implementation". |
| Dock operator digital interface | `reqwest` HTTP client to the HMI API, source-bound to the operator's `vnet-ot` address; plain HTTP, with TLS terminated by a reverse proxy when run against the digital world |
| Tug arrival generator | One Tokio task per roster tug; draws the tug's time away, sleeps, signals the tug engineer, and waits for the tug to leave the dock. See §6. |
| Logical clock (`SimClock`) | Shared handle; provides `sleep(duration)` to all sim entities. Mode (coupled/isolated) is internal. |
| Fuel supplier actor | Tokio task; on dispatch decision, notifies the road tanker driver after `lead_time`. In coupled mode, bound to the sim host's `vnet-ext` interface. See §5.3. |

---

## 11. Open decisions<a id='open-decisions'></a>

Running list of decisions not trivially derivable from narrative.md; each
resolved item is folded back into narrative.md.

*(none)*
