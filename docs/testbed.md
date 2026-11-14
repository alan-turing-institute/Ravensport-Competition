# Ravensport

A small, realistic OT environment built around a fictional onshore marine
fuel buffer tank at a modern port.

## Implementing `narrative.md`
Two documents, `digital.md` and `sim.md`, describe a software-only world that models a cyber-physical implementation of the activities in `narrative.md`.

The digital world runs real, deployable open-source software on virtualised hosts (virtual machines or containers). The simulated-physical world models the fuel tank, the equipment on the dock and the sealed vendor appliances, and produces the signals that the digital world consumes, in realistic protocols and formats. The people listed in narrative §3 (people and shifts), such as the dock operator and the fuel supplier, are actors in the simulated-physical world.

The design is specified in [`specs/sim.md`](../specs/sim.md) (the simulated-physical world), [`specs/digital.md`](../specs/digital.md) (the digital world) and [`specs/host_specs/sim/sim-clock.md`](../specs/host_specs/sim/sim-clock.md) (the logical clock that keeps the simulated-physical world's time).

## Planning documents

| Doc | Owns |
|---|---|
| **narrative.md** | The narrative. The fictional asset, people, processes, and what "operating correctly" means. Product-agnostic. The source of truth for *what* exists. |
| **sim.md** | Build plan for the **simulated-physical** world: fuel tank model, instrument behaviour, and the sealed vendor appliances (ATG console, dispenser, RFID reader) that the port does not install or configure. |
| **digital.md** | Build plan for the **fully-realistic-digital** world: the software and systems a port's IT/OT team actually deploys — FMS, log-server, HMI, workstations, networks, IdPs, firewalls. |

## Deployment

The deployment tooling lives in `deploy/`: `topology.yaml` is the single
source of truth for networks, firewall policy, and nodes, and `make`
targets render it and apply it to an
[Incus](https://linuxcontainers.org/incus/) host. Two profiles are
defined in `deploy/overrides/`:

- **`ci-minimal`** (default) — every node runs as a container, built from
  the same image as under `full`. Runs on any host, including hosted
  continuous-integration runners.
- **`full`** — the firewall, FMS, HMI, and historian nodes are virtual
  machines provisioned with real installers. Requires virtualisation
  support (KVM) on the Incus host.

### Host preparation with Lima

On macOS the Incus host is a [Lima](https://lima-vm.io) virtual machine
described by `deploy/lima.yaml`: Ubuntu 24.04 with Incus and the
Rust toolchain installed at first boot. The Python renderer, test, and lint
dependencies install into a project-local virtualenv built by `make install`,
which the deploy and test targets create automatically on first use.

```sh
brew install lima
limactl start --name=ravensport deploy/lima.yaml
limactl shell ravensport
```

The first start provisions the virtual machine and takes several
minutes. Lima mounts the host home directory, so a repository checkout
under the home directory is visible inside the virtual machine at the
same path; `limactl shell` preserves the working directory, so a shell
started from inside the repository opens at the matching path. For the `full` profile, uncomment the `vmType` and
`nestedVirtualization` lines in `deploy/lima.yaml` before the first
start (Apple Silicon M3 or later with macOS 15 is required; this is the
nested virtualisation the profile's KVM requirement implies).

Provisioning also adds 4 GB of swap memory (`/swapfile`, with
`vm.swappiness` set to 10). The running deployment uses about half of the
virtual machine's 8 GB, and without swap space the Rust test builds can
exhaust the remainder, resulting in out-of-memory errors. The `swappiness`
setting ensures that the slower swap memory is not used during normal
operation, only during tests.

### Deployment

RavensPort deployment is orchestrated by `deploy/Makefile`. Run all targets
from inside the Lima virtual machine.

Deploy with the single command:
```sh
make -C deploy up
```
and tear down with:
```sh
make -C deploy down
```

The `make -C deploy up` command combines the following steps, which can also
be run individually (use `make -C deploy help` to list all targets):

**Render:** Reads config files and writes build scripts (and other generated
artefacts) to the `deploy/build` directory: `make -C deploy render`.

**Build:** Builds all Incus images for the network hosts and the sim, with
all rendered config parameters and compiled binaries baked in: `make -C deploy build`.

The first build takes several minutes. Subsequent builds skip any image whose
SHA-256 content (plus config) hash is unchanged. Add `PROFILE=full` for the
full profile (requires KVM — Apple Silicon M3 or later).

**Deploy:** Applies the rendered topology to Incus: creates the zone networks
and launches each node from its pre-built image: `make -C deploy deploy`.

`make -C deploy up` also runs `make -C deploy stage-attacks`, which copies the
attack chains' binaries for the host's architecture, x86_64 or aarch64, from
`attack-chains/prebuilt/` into `attack-chains/dist/` and checks them against
their digests in `SHA256SUMS`. The competition's public release holds the
binaries without their source.

### Restoring a snapshot file

A snapshot file holds a stopped environment together with the operational
history it accumulated while running, so that a restored run starts from that
history rather than from an empty deployment. Its name records the profile
and architecture it was produced on:
`ravensport-snapshot-<label>-<profile>-<arch>.tar`. It restores only on a
host of the same architecture (`uname -m` inside the Lima virtual machine).
A snapshot file contains the root
filesystem of every RavensPort instance as its producer built it, and the
restore runs those instances inside the Lima virtual machine; the virtual
machine and the host are not changed. Restore only files from a producer you
trust.

Place the file anywhere under the host home directory, which Lima mounts
into the virtual machine, and from inside the virtual machine run:

```sh
make -C deploy down FORCE=1                            # removes any existing RavensPort instances
make -C deploy restore SNAPSHOT=<path to snapshot file> [SEED=<n>]
```

The restore uses only the file: the instances, their configuration, the
networks and the profile are the producer's, and the local `world.yaml`,
topology and `PROFILE` are not read. Nothing is rendered and no image is
built. The target checks the file before changing anything, and refuses it if
its format version is unknown, its architecture does not match, any member is
corrupted or truncated, a RavensPort instance already exists, the storage
pool has too little free space, or a different snapshot with the same label
has already been restored on this host. If the file was produced from a different
RavensPort commit than the checkout, the target warns and continues.

The restore imports every instance, sets the virtual machine's clock to the
time the snapshot was taken, starts the environment, and starts the sim from
the state it was stopped in. The sim refuses its saved state if it starts more
than five minutes after the snapshot time, so do not interrupt the restore.
The restore waits 30 seconds after starting the sim, and fails, showing the
end of the sim's log, if the sim has stopped.
Without `SEED` the restored run repeats the snapshot's own continuation; with
`SEED`, tug arrivals after the restore follow a different sequence. Each
restore records its time and seed in
`deploy/snapshots/<label>/runs.log`, and the same snapshot restored
with the same `SEED` reproduces the run.

For the whole restored run the virtual machine's clock runs from the snapshot time,
and its time synchronisation is disabled.

End the restored run with:

```sh
make -C deploy stop
```

which stops the instances and returns the clock to real time, and keeps the
instances and their snapshots for later restores. By contrast, running
`make -C deploy down` while a restored run is in progress changes nothing, and
reports that the run is in progress. With no restored run in progress, it still changes nothing
while any instance holds a snapshot, and lists each snapshot and whether it
has been exported. This prevents accidental deletion of snapshots, which are
removed along with their instances when the environment is torn down. The
teardown can be forced with `make -C deploy down FORCE=1`, which deletes all
instances and snapshots and returns the clock to real time.

The first restore keeps a local copy of the snapshot in `deploy/snapshots/`,
so later restores of the same snapshot use its label and do not read the file
again:

```sh
make -C deploy restore SNAPSHOT=<label> [SEED=<n>]
```

A restore refuses to start while any RavensPort instance is running; end the
current restored run first.

### Producing a snapshot file

To produce a snapshot, deploy the environment with `make -C deploy up` and let it run in real time. 
Do not run any commands inside the instances
while it runs unless you want them to become part of the history, because each command is recorded in that host's logs and by the
security monitoring.

Then take a snapshot:

```sh
make -C deploy snapshot LABEL=<label>
```

The label is made of letters, digits, `.`, `_` and `-`, beginning with a
letter or digit. The target stops the
environment in the order a planned maintenance window shuts the port down,
starting with the sim, which saves its state once it has come to rest; this
can take up to an hour. It then snapshots every instance and keeps the sim's
state, the stop time, and the rendered files and configuration the instances
came from in `deploy/snapshots/<label>/`. The snapshot can be restored on the
same host by its label, as described above.

If the target fails after the sim has saved its state, the snapshot is kept,
marked incomplete, and running the same command again completes it once the
cause of the failure is fixed. An incomplete snapshot cannot be restored or
exported, and while it exists no other snapshot can be restored and
`make -C deploy down` refuses to delete the instances. It cannot be completed
if the environment has run since the failure, or if the stop took too long for
a restore to start the sim; the target then says so.

The target warns if the fuel supplier's next check of the tank level falls due
at the start of a restored run. Such a snapshot is kept, but the sim is likely
to stop within seconds of every restore of it, so do not export it.

Write the snapshot to a snapshot file with:

```sh
make -C deploy export-snapshot LABEL=<label>
```

which writes
`deploy/snapshots/ravensport-snapshot-<label>-<profile>-<arch>.tar`. The
directory is on the mount Lima shares with the host, so the file can be
distributed from the host. Export a snapshot before tearing the environment
down with `FORCE=1`, which deletes the local snapshot.

The full procedure is specified in
[`specs/deployment/deployment.md` §10 (snapshot sets)](../specs/deployment/deployment.md#snapshot-sets).

### Testing

Always run test suites via the `Makefile` from the repository root.

The full suite runs inside the Lima virtual machine, which provisions
PostgreSQL, Incus, slapd, and the Python tooling automatically (see
`deploy/lima.yaml`), so nothing is installed by hand.

The `make test` command runs test suites against a deployed RavensPort instance.
It assumes the network is already up, so must be run after `make -C deploy up`.

Test suites can also be run individually:
```sh
make test              # run every suite
make test-<suite>      # one suite: test-sim, test-hmi, test-fms, test-sim-hmi-fms, test-deploy, test-integration
```

The snapshot test, `make -C deploy test-snapshot`, is not part of `make test`:
it stops the environment, steps the virtual machine's clock, and can take up
to an hour while the sim saves its state.

**Without a Lima VM — on a dev machine.**

Tests that do not require a running RavensPort instance can be executed on any
machine with the following command:
```sh
make test-local        # every suite that needs neither Incus nor PostgreSQL
```

This suite includes unit tests for the renderer and individual hosts, and three-way
integration tests across the sim, FMS and HMI. It needs only the Rust toolchain
and Python 3 (with `venv`).

## Design problems

### Sim time vs. digital-world time

**Problem.** Time progression in the physical sim should not be unnecessarily tied to the wall-clock.


**Decision**
The physical sim has a logical clock and can advance
arbitrarily fast. The digital world is real software (log-server, FMS,
firewalls) deployed under an open-source orchestrator, running in time
with its host clocks.

- **Isolated mode.** The sim runs on a logical clock, as fast as the
  host allows. Used for: unit-testing the physical model, replaying
  attack scenarios, reproducible CI runs. The digital world is not
  connected.
- **Coupled mode.** The sim logical clock runs at the same speed as the clock in the digital world.

**What this rules out.** Full-system time acceleration (driving host
clocks via a fake NTP source) was considered and rejected as fragile and
complex. Multi-day log-server retention tests cannot be fast-forwarded in
coupled mode; they are not cyber scenarios and are out of scope.

**Addendum — event scheduling is separate from the logical clock.**

The tug arrival generator (sim.md §6) and the fuel supplier (sim.md §5.3) are consumers of the logical clock; their scheduled arrivals
advance in logical time, not wall-clock time.

---

### FMS<->dispenser protocol: Modbus TCP instead of native RS-485

**Background.** In a real port installation, the FMS software (e.g. OPW
FSC3000) and the dispenser (hardware + firmware, sealed appliance) are
bought from the same vendor or a certified-compatible third party. The
FMS PC and the dispenser on the dock are connected by an RS-485 serial
cable. They speak a **proprietary vendor protocol** natively — OPW
Petro-Net for OPW equipment, Gilbarco DART for Gilbarco equipment. Both
ends speak the same protocol out of the box; no gateway is involved.

The pulse train from the dispenser's flow meter goes to the dispenser's
own preset counter *and* back to the FMS as a discrete hardware pulse
output — two consumers of the same pulse wire. The FMS sends a preset
volume + authorise command; the dispenser runs its own solenoid cutoff
at the preset limit.

**Problem.** Petro-Net and DART are proprietary and unpublished, so neither protocol can be implemented faithfully. Both ends of the FMS<->dispenser connection are built for the testbed. The FMS is a custom open-source build (see digital.md §2.1, fuel management system), and the dispenser is a sim-side emulator. The protocol that crosses the sim<->digital boundary must be realistic.

**Decision: Modbus TCP.**

In real installations, integrators use Modbus TCP gateway converters to connect Petro-Net and DART equipment to third-party systems, so Modbus TCP is a documented, real-world integration pattern for this boundary. In the testbed, the FMS and the dispenser emulator implement Modbus TCP natively, without a gateway converter, as a documented deviation.

- FMS issues Modbus TCP writes to set preset volume and enable the
  dispenser pump.
- Dispenser emulator exposes Modbus TCP registers for: solenoid state,
  pulse totaliser, preset remaining, fault flags.
- Pulse counting is totaliser-register-based rather than a hardware
  pulse wire; the K-factor (100 pulses/litre) is still honoured in
  the register arithmetic.

**Deviation note.** Recorded in digital.md §9. The native RS-485
proprietary protocol is absent; Modbus TCP is the stand-in. Attack
paths that exploit Petro-Net/DART framing or RS-485 bus access are not
present.

---

### Sim appliance network presence: single sim host with a proxy front

**Background.** The ATG console, dispenser, and RFID readers are sealed
vendor appliances sitting on the OT LAN in a real installation — separate
boxes, each with its own IP address, MAC address, and ARP entry. The FMS
connects to each by IP. The sim's actors also need network presence: the
fuel supplier on `vnet-ext` (to reach `atg-relay-01` in the DMZ), the dock
operator on `vnet-ot` (to reach the HMI), and the reconciliation user on
`vnet-it` (to reach the reconciliation workstation).

**Problem.** The sim must present each appliance and each networked actor
as a distinct host on the correct zone, while keeping the physical model a
single coherent in-process state machine. Options considered:

- **Separate sim hosts** — one orchestrated host per appliance and actor.
  Faithful to the real topology but requires the physical model state
  (tank volume, flow rates, sensor readings) to be shared across host
  boundaries via an inter-process protocol, adding complexity with no
  realism benefit.
- **Single sim host with a proxy front** — one orchestrated container runs
  the Rust sim process (which owns the physical model, all appliance
  emulators, and all actor tasks) alongside an HAProxy reverse proxy. The
  sim binds only loopback; HAProxy owns every zone-visible interface and
  address and bridges to the sim's loopback ports.

**Decision: single sim host with a proxy front.**

From the digital world's perspective each appliance and actor is a distinct
host at its own address: the ATG console, dispenser, and the two RFID
readers (networked readers reachable over TCP) on `vnet-ot`; the dock
operator on `vnet-ot`; the fuel supplier on `vnet-ext`; and the
reconciliation user on `vnet-it`. The physical model and all sim modules
remain a single in-process state machine with no inter-process
communication overhead. The full interface and address map, and the
proxy's inbound/outbound mappings, are specified in
[sim-network.md](../specs/host_specs/sim/sim-network.md).

Traffic from the fuel supplier actor (on `vnet-ext`) to the ATG console
(on `vnet-ot`) traverses `fw-ext-dmz`, `atg-relay-01`, and `fw-ot-dmz` —
the relay creates a new TCP connection for each forwarded session, so the
host's kernel does not short-circuit the packets internally. The firewall
and relay see genuine network traffic between distinct IP addresses.

**Known limitation.** The sim's interfaces carry synthetic MAC addresses.
In the real world, a network scan would see distinct MAC addresses and OUI
prefixes identifying each vendor (e.g. a Veeder-Root OUI for the ATG
console). In the testbed none of the interfaces carry vendor-authentic
OUIs. This matters for scenarios that fingerprint hosts by MAC or OUI.

---

### Actor implementation: async/await as implicit state machine

**Problem.** Each actor (dock operator, road tanker driver, tug
engineer, reconciliation user, fuel supplier) has a defined causal behaviour: a sequence of
digital actions (HTTP calls to the FMS, log-server replica) and physical
actions (state mutations on the sim), ordered by stimuli from the digital
world and from other actors. The question is how to
implement this behaviour in a way that is readable, correct, and auditable.

Three approaches were considered:

- **Explicit state machine (enum + transition function).** Each actor is a
  Rust enum of named states; a `handle(event)` function returns the next
  state. Every state and transition is explicit. The cost is significant
  boilerplate and awkward representation of branching (fill vs. dispense)
  and inter-actor coordination.
- **Scripted scenario as data (YAML/TOML playbook).** Steps are declared
  as structured data; a generic executor interprets them. Scenarios can be
  defined without recompiling, but expressing conditionals, loops, and typed
  channel coordination requires reinventing a programming language in the
  data format.
- **Async/await (implicit state machine).** Each actor is a Rust `async fn`.
  Every `.await` is an event boundary; the sequential body reads as a direct
  causal narrative of the actor's behaviour. Rust's compiler generates the
  state machine. Inter-actor coordination uses typed `tokio` channels, as
  specified in sim.md §5.1.

**Decision: async/await.**

Each actor in sim.md §5.2 is a Rust `async fn`. The function body is the
actor's implementation: await points correspond to blocking on a digital
response or an inter-actor signal; physical actions are direct state mutations
on the shared physical model; branching (e.g. fill vs. dispense)
is plain Rust `match`. An actor that participates in more than one
event sequence handles them within a single function — the dock operator, for
example, establishes a session at the start of the sim and loops over an ordered job
list, branching on job type. This reflects the real-world structure: one
person, one session, sequential non-concurrent jobs (narrative §5.7).

Auditability is recovered through instrumentation rather than explicit state
enums: each actor emits a structured trace event at every await boundary
(actor name, step name, timestamp), producing a complete causal log without
coupling the actor logic to a state representation.

---

## Conventions

- **Open-source rule.** Every product chosen for the digital world must
  be open-source or carry an equivalent open licence.
  Commercial trial modes and paid licences are not acceptable, even when
  they are the most realistic real-world product; the divergence is
  recorded in digital.md §9.

  *Why this constraint exists.* Ravensport is intended to be run as a
  public competition; competitors must be able to obtain and run the
  testbed without encountering licensing barriers, and the project must
  not depend on commercial licences that the organisers cannot
  redistribute. The rule is scoped to that competition use. Other
  deployments of Ravensport — for example, internal research within an
  organisation that already holds the relevant commercial licences —
  may relax this rule and substitute closed-source products that more
  closely match real-world port deployments. If the rule is relaxed,
  the relevant `[trial]` / `[paid]` candidates listed in digital.md §2
  become available again, and the corresponding entries in digital.md
  §9 cease to be deviations.
- **Open-licence tagging.** Products referenced in digital.md and sim.md
  are tagged `[free]` (community / open-source / freely downloadable),
  `[trial]` (time-limited demo or feature-restricted free tier), or
  `[paid]` (no free path). Only `[free]` products may be selected as the
  testbed's choice; `[trial]` and `[paid]` tags are retained so that the
  real-world products a port actually deploys can be cited for context.
- **Vendor products as examples.** Where a concrete vendor product is
  named in digital.md or sim.md, it is one acceptable open-source choice,
  not a fixed commitment; any open-source substitute that preserves the
  documented boundary interfaces is permitted.
