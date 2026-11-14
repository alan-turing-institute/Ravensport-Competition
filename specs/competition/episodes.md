# Running episodes

This document specifies how the episode runner, a component of the RavensPort competition, runs each episode, and how episodes are seeded, paced, measured and reproduced.

**Contents:**

1. [Scope](#scope)
2. [Terms](#terms)
3. [Episode lifecycle](#lifecycle)
4. [Episode inputs and seeds](#seeds)
5. [Time and the compression factor](#time)
6. [Start state, living ports, forks and settle period](#start)
7. [Step protocol](#steps)
8. [Query interface](#query)
9. [Suppressed episodes for Competition Rule 5](#rule5)
10. [Adversarial episodes](#adversarial)
11. [Horizon and termination](#horizon)
12. [Terminated and discarded episodes](#discarded)
13. [Episode record](#record)
14. [Reproducibility](#reproducibility)
15. [Episode mix](#mix)
16. [Measurement episodes and episode cost](#measurement)
17. [Parameters](#parameters)
18. [Limitations](#limitations)
19. [Open items](#open-items)

---

## 1. Scope<a id='scope'></a>

`episode-runner-impl.md` gives the implementation detail of the episode runner and of the competition images, and `episodes-tests.md` gives the test design.

The attack chains and the adversary host are specified in the attack-chain documents, `specs/attack_chains/` and `specs/host_specs/adversary/adversary.md`, which this document cites by path. Section 10 (adversarial episodes) states how the episode runner uses the attack chains and the adversary host.

---

## 2. Terms<a id='terms'></a>

- **Episode.** One run of RavensPort from a fork of a living port to the reset, scored as one unit.
- **Episode type.** Benign, with the port's operation alone, or adversarial, with one attack chain added.
- **Episode mix.** The numbers of benign and adversarial episodes in a run.
- **Track A and Track B.** The competition's two tracks. Track A scores detection through the Track A alarm. Track B scores mitigation through defensive actions.
- **Entry.** What an entrant submits. An entry comprises a modified Security Information and Event Management (SIEM) host, any added host tooling, a telemetry specification declaring the data the entry consumes, and self-reported performance. Self-reported performance gives the scores that the entrant measured in its own episodes in evaluation mode.
- **Competition Rules 3, 4 and 5.** Three of the Competition Rules, which every entry follows. Rule 3 allows an entry only the data that its telemetry specification declares, read through the query interface. Rule 4 limits added host tooling to collecting data on the digital world's hosts and transmitting it along the collection paths that the entry declares, and forbids any other change to the environment. Section 9 states Rule 5, the check of an entry's tooling through suppressed episodes.
- **SIEM host.** `siem-01`, the host of the security monitoring (`siem.md`).
- **Wazuh agent.** The software of the security monitoring on each monitored host, collecting the host's records and forwarding them to the Wazuh manager (`siem.md` §2.1, the agent on every monitored host).
- **Wazuh manager.** The software of the security monitoring on `siem-01` that receives every Wazuh agent's records and performs all detection (`siem.md` §2.2, the manager on `siem-01`).
- **Defender.** The policy that acts from the SIEM host during an episode, with any tooling that serves the policy on the SIEM host or other hosts of the digital world.
- **Idle defender.** The defender whose decision at every step is no action, in both tracks.
- **Launcher.** The program `/usr/local/bin/defender` on `siem-01`, which the step session runs, and which runs the defender's program (§7, step protocol).
- **RavensPort node.** A container or virtual machine that `deploy/topology.yaml` defines.
- **Incus host.** A virtual machine that runs Incus, the manager of the containers and virtual machines, such as the Lima virtual machine of `general-deployment.md` §1.1 (release profile). A living port and each fork run on Incus hosts of their own.
- **Episode runner.** The component that deploys each living port, makes each fork from fork 1 on, and runs each episode from the episode's Incus host, outside every RavensPort node.
- **Living port.** A deployment of RavensPort that runs continuously, and from which the episode runner starts each episode at a fork (§6.3, the living port).
- **Living port's start.** The start of a living port's simulator, at the instant of the simulator's `start_state` event, which the episode runner records as `started_at` (§4.3, start-up input).
- **Fork.** A deployment that holds a living port's state at one instant, the fork instant, in a virtual machine of its own, and runs one episode (§6.4, how a fork is made). A living port's forks are numbered from fork 1. Fork 0 is the episode on the living port's own deployment, which occurs at the living port's start (§6.3, the living port).
- **Age of a fork.** The time from the living port's start to the fork instant.
- **Fork wait.** The drawn time that fork 1 adds to the end of the living port's settle period, and that each later fork adds to 14 minutes after the fork before it (§6.3, the living port).
- **Living port seed.** The seed from which the episode runner derives a living port's tug seed, the seed of its fork waits, and the episode seed of each of its forks.
- **Training volume.** The entry's storage volume for training, attached read-write to the fork's `siem-01` for each episode in training mode (§3.2, modes).
- **Training lineage.** The succession of versions of the training volume that one living port's forks leave in training mode, each episode starting from the version that the previous episode left (§3.2, modes).
- **Snapshot cache.** The store of a run's snapshots of its living ports, kept for replays (§6.3, the living port).
- **Held clock.** The clock of a living port or of a fork, which no time source sets, and which stops whenever its Incus host is paused (§6.4, how a fork is made). The clock stops because the Incus host's kernel uses a clock source that stops during the pause.
- **Step.** An interval of simulated time of fixed length. The first step is step 0.
- **Step boundary.** The instant at which one step ends and the next begins.
- **Horizon.** The largest number of steps in an episode, written T.
- **Decision.** What the defender returns in a step. In Track A it is no action or the Track A alarm. In Track B it is no action or one defensive action.
- **Defensive action.** A Track B decision that acts on the environment.
- **Track A alarm.** The Track A decision that reports an attack. Carried out, it ends the episode (§7, step protocol).
- **Reward.** The value the episode runner computes for each step, defined by the rewards of Track A and Track B (§7, step protocol).
- **Ground truth.** The physical and operational state as the simulator holds it, and the attack chain's state as the episode runner records it: the stages that the episode runner released and their stage events.
- **Operational correctness conditions.** The conditions that define correct operation of the port, which `narrative.md` §5 (operational correctness conditions) states.
- **Ground-truth evaluator.** The component that judges each step against the operational correctness conditions, and supplies scoring with the ground truth that the simulator holds.
- **Terminal state.** A state at which no further decision could change the score (§11, horizon and termination).
- **Terminated episode.** An episode that a simulator stop with a cause inside it ends early, with its score applied (§12, terminated and discarded episodes).
- **Discarded episode.** An episode that a failure of the testbed, or an entry's `defender` that cannot run, ends, and that scoring omits (§12, terminated and discarded episodes).
- **Seed.** A value that fixes a sequence of random draws. `attack-chain-design.md`, "Seeding the inventory", uses "seed" for the facts that an adversary holds before its first stage.
- **Episode seed.** The seed from which the episode runner derives the seed of each generator of the episode, other than a living port's own generators, `sim_tug_arrivals` and `fork_wait` (§4.2, derivation).
- **Generator.** A source of random draws, named by the function that draws from it. §4.2 (derivation) lists the generators of a living port and of an episode.
- **Tug stream.** A tug's sequence of times away and requested volumes, which the simulator draws from the generator `sim_tug_arrivals` in a stream of its own for each tug (`sim.md` §6, tug arrival generator).
- **Tug seed.** The seed of the generator `sim_tug_arrivals`, from which the simulator draws every tug stream (§4.2, derivation).
- **Scheduled arrival.** The time at which a tug away from the dock arrives, which the simulator logs with the tug's requested volume as it draws them (§6.4, how a fork is made).
- **Compression factor.** The one factor by which a compressed scenario shortens every interval and duration in the port's operation, and raises every rate of fuel flow (`sim.md` §2.2, compressed scenarios).
- **Value in operation.** A timing or a rate as the port runs it, before the compression factor applies (`sim.md` §2.2, compressed scenarios).
- **Compressed scenario.** A scenario whose compression factor exceeds 1.
- **Compressed interval.** An interval in operation divided by the compression factor (§6.2, first occurrences).
- **Delivery state.** Whether an order for a road-tanker delivery has been placed at an instant, and with an order placed, the remaining lead time.
- **Remaining lead time.** The time from an instant until the road tanker arrives, for an order placed before that instant.
- **Start state.** The state of the port's operation at the fork instant, such as the tank's volume, the delivery state and the tugs waiting. At fork 0 the start state is the one that §4.3 (start-up input) gives.
- **Attack chain.** The ordered stages of one attack, run as one binary on the adversary host.
- **Port activity.** An event of the port's operation that a stage of an attack chain needs during its run, such as the fuel supplier's poll of the tank gauge (§10.1, the competition library).
- **Competition library.** The attack chains that start on the adversary host and whose stages all appear in the episode runner's table of stages (§10.1, the competition library).
- **Poll chain.** An attack chain with a stage that waits for the fuel supplier's poll (§10.1, the competition library).
- **Refuel chain.** An attack chain with a stage that needs a refuel during a peer scan of the chain's inventory stage, `enumerate-siem-agent-inventory` (§10.1, the competition library).
- **Predicted episode events, or predicted events.** The episode runner's prediction of when the fuel supplier polls, each tug arrives and is refuelled, and each stage of the attack chain runs during the episode. The prediction starts from the start state at the fork instant (§10.2, choosing the chain). Each predicted time is a range, from earliest to latest (`episode-runner-impl.md` §8, ranges of the predicted events).
- **Onset step.** The step at which the episode runner starts the attack chain.
- **Onset window.** The steps from which the onset step is drawn.
- **Wait.** A pause of the attack chain before its first stage, or between two stages.
- **Stage event.** A line that the attack chain writes to its standard output when a stage begins or ends.
- **Settle period.** The interval from the start of `sim-host`, or of a fork, to the instant at which step 0 can begin (§6.6, settle period).
- **Settle delay.** The pause that ends the settle period of a living port's deployment, once the settle conditions hold. A fork has no settle delay (§6.6, settle period).
- **Settle timeout.** The longest time the episode runner waits for the settle conditions of an episode (§6.6, settle period).
- **Settle conditions.** The four conditions that must hold before step 0 (§6.6, settle period).
- **Query interface.** The interface through which the defender reads the outputs of the security monitoring.
- **Record of reads.** The query interface's log of every read.
- **Outcome.** The episode record's account of how an episode ended: `horizon`, `alarm`, `last_stage`, `terminated`, `discarded`, `not admitted` or `interrupted` (§12, terminated and discarded episodes). Track A's outcome is the category of a Track A episode by its alarm and the stages released before the alarm, which the record holds apart (§13, episode record).
- **End cause.** In Track A, the end of an episode as the record's Track A fields name it: `horizon`, `alarm`, `last_stage` or `simulator_stop` (§13, episode record).
- **Dated file.** The file into which the Wazuh manager writes one day's alerts or archived records. `alerts.json` and `archives.json` are second names for the current day's dated files (`siem.md` §7, retention).
- **Episode record.** The directory that holds everything recorded about one episode.
- **Training mode and evaluation mode.** The episode runner's two modes (§3.2, modes).
- **Suppressed episode.** An episode of a replay for Competition Rule 5, in evaluation mode, in which the episode runner records each decision and suppresses it (§9, suppressed episodes for Competition Rule 5).
- **Measurement episode.** An episode run only to measure (§16, measurement episodes and episode cost).
- **Step session.** The long-lived Incus session into `siem-01` that runs the defender and carries the step messages.
- **Step message.** A decision request, an end notice or a decision message, each one JavaScript Object Notation (JSON) object on one line of the step session.
- **Decision request.** The episode runner's step message at the start of each step, which asks the defender for a decision.
- **End notice.** The episode runner's step message after the last step.
- **Decision message.** The defender's step message, which carries one decision.
- **Invalid line.** A line on the step session's standard output that is not a decision message holding one of the decisions of the episode's track (§7, step protocol).
- **Start-up input.** The file that carries a living port's tug seed and the time of its first audit to the simulator, which reads the file as it starts (§4.3, start-up input).
- **Scan request.** The episode runner's request to the Wazuh manager for a file-integrity scan on one host.
- **Allowance.** The time the horizon leaves after the last stage begins.
- **Flow record.** The packet inspection's summary of one network connection (`siem.md` §4.2, flow records).
- **Flow timeout.** The time for which the packet inspection holds a flow after the flow's last packet, before it writes the flow record.
- **Node image.** The image from which a RavensPort node is deployed.
- **Operational image.** The node image that `deployment.md` §6 (image build) builds for a node.
- **Competition image.** The image from which the deployment of a living port creates a node that has one. That deployment creates every other node from its operational image (§3.4, competition image).
- **Competition firewall image.** The competition image of a firewall node.
- **Competition profile.** The deployment profile that `general-deployment.md` §1.3 (competition profile) specifies for the competition, on which every living port and every fork runs.
- **Additional override file.** A file of patches to the node definitions in `deploy/topology.yaml`, applied after the patches that choose between containers and virtual machines (`deployment.md` §7.3, additional override file).

---

## 3. Episode lifecycle<a id='lifecycle'></a>

### 3.1 Phases

1. **Plan.** The episode runner fixes the episode's inputs (§4.1, inputs). At fork 0 the episode runner chooses the attack chain of an adversarial episode in this phase. In a fork the episode runner chooses the attack chain once the settle conditions hold, from the start state at the fork instant and the instant of step 0 (§10.2, choosing the chain).
2. **Fork.** At the fork instant, the episode runner makes the fork from the living port (§6.4, how a fork is made). At fork 0 the living port's own deployment serves (§6.3, the living port).
3. **Settle.** The episode runner waits for the settle conditions, and at fork 0 for the settle delay too (§6.6, settle period).
4. **Run the steps.** The episode runner runs steps 0 to T − 1.
5. **End.** At the horizon, or at a terminal state that ends the episode (§11, horizon and termination), the episode runner ends the episode.
6. **Export.** The episode runner copies the evidence into the episode record.
7. **Reset.** The episode runner deletes the fork, and the living port continues to run. At fork 0 the reset deletes the living port's own deployment (§3.3, reset).

### 3.2 Modes

**Training mode.** Entrants train their defenders in training mode, on living ports that the entrant runs on the competition profile. Training episodes run only in forks of a living port (§6.4, how a fork is made), and a living port that makes forks runs no episode at fork 0 (§6.3, the living port). Learning stays in episodes, because the defender runs only in an episode's step session (§7, step protocol). Each fork runs the living port's `siem-01`, with its monitoring history. At the episode's start, the episode runner attaches the training volume, the entry's storage volume for training, read-write to the fork's `siem-01` at `/var/lib/entry`. The training volume passes from each fork to the next, and the reset deletes everything else of the fork. Each living port runs its episodes one at a time and keeps one training lineage. The episode record states the training volume's version at the episode's start and at its end (§13, episode record), and a lineage can start from any given volume.

**Evaluation mode.** Final scoring, the suppressed episodes for Competition Rule 5 and the entrant's self-reported performance run in evaluation mode, on the competition profile. When the episode runner creates a living port, it attaches the entry's submitted storage volume read-only to the living port's `siem-01` at `/var/lib/entry`. Every fork keeps that volume, since a fork holds the living port's whole virtual machine (§6.4, how a fork is made). The SIEM host's state as the entrant submitted it is the contents of that volume, read-only in every fork, and the entry's own image of `siem-01` where it gives one. The policy stays fixed. An entry's tooling on another host is built once into that host's image, which the entry's own version of the competition's additional override file names. The entry's living ports run from those images (§6.3, the living port). Until the entry requirements say otherwise, the tooling is a manifest of software and the entry's configuration of the host's Wazuh agent. Each piece of software is a package of the distribution at one version, or a source repository at one commit. The build applies the manifest to the host's competition image where the host has one, such as a firewall node, and to its operational image otherwise.

**Added host tooling.** Added host tooling collects data on other hosts of the digital world, and transmits the data only along flows that already carry each host's data to the SIEM host. The flows that an entry uses are the declared collection paths of Competition Rule 4. No entry may change the adversary host (§6.5, adversary host).

**The entry's own images.** An entry may give its own image of any host except the adversary host, which no entry may change (§6.5, adversary host). Each such image is built from the image that a living port would otherwise run for that host: its competition image where the competition defines one, and its operational image otherwise. The entry's own version of the competition's additional override file names each such image. That file extends the competition's file: the entry merges a patch of its own into the competition's file at each build, so every value that the patch leaves stays as the competition's file gives it. The entry's living ports then run each such host from the entry's image in either mode, as §6.3 (the living port) allows with "the competition images or an entry's". The reference models use an image of `siem-01` of their own, built from the competition image of `siem-01`. The entry's storage volume still holds the defender's program and its data (§7, step protocol).

**Learning across episodes.** A defender learns across the episodes of one living port through the training volume. In training mode, each end notice carries the last step's reward, which in Track A is `R_n`. The defender keeps what it learns in the training volume, and the next fork starts from the version that the episode left (§3.3, reset). The defender may replace its own program in the training volume, and the replacement runs from the next episode of the lineage (§7, step protocol). The defender's decisions within an episode still take no reward as an input (§7, step protocol). Living ports that run in parallel keep separate lineages, which an entrant merges outside the episodes (§19, open items). A defender can also learn offline from exported episode records. Each record keeps every step's decision and reward, the stage events, the record of reads and the security monitoring's dated files, from which each read replays exactly (§13, episode record). The exported episode records serve training and offline development alone. Scoring runs every entry live in its episodes (§7, step protocol), and never scores an entry against a recording.

### 3.3 Reset

At the reset, the episode runner deletes the fork's Incus host with every instance in it, and the living port continues to run. In training mode the episode runner first detaches the training volume and keeps it as the lineage's next version, for the next fork of the same living port (§3.2, modes).

At fork 0 the reset deletes the living port's own deployment. The teardown, `make down`, deletes every instance and network, and `make up` deploys the next living port from the images already built, rebuilding an image only when its inputs change (`deployment.md` §6.1, per-node images; §8, deployment sequence). After the teardown, the episode runner lists the instances of the deployment that remain. A reset at fork 0 that leaves an instance of the deployment, or whose result the episode runner cannot check, stops the episode runner with exit status 1, and no further episode starts.

Partial resets fail. The Fuel Management System (FMS) rejects every update and deletion in its `sessions`, `jobs` and `transactions` tables (`fms.md` §8.6, append-only triggers), and restarting `sim-host` alone refills the tank (`sim-network.md` §7, image, build and supervision).

### 3.4 Competition image

The deployment of every living port creates `siem-01`, `sim-host` and the three firewall nodes from their competition images, so every fork holds them. Every other deployment creates those five nodes from their operational images, which stay as the documents of their hosts specify. The adversary host has no competition image (§6.5, adversary host). The competition's additional override file is `competition/deploy/override.yaml`.

Each competition image is built as the node's operational image is built, and differs from the operational image as follows.

- The competition image of `siem-01` adds the launcher, the idle defender's program and the query interface (§7, step protocol).
- A competition firewall image carries the competition's flow timeouts of packet inspection (§5.3, timings).
- The competition image of `sim-host` runs the competition's entry point, which reads the tug seed and the time of the first audit from the start-up input (§4.3, start-up input). The entry point starts the simulator with those two values. The operational image of `sim-host` runs the `sim` binary, which reads no configuration file at runtime (`sim-network.md` §7, image, build and supervision).

Each operational image and its competition image carry different names and their own input hashes, so both images stay built. As `general-deployment.md` §1.3 (competition profile) states, the single Makefile target that builds all container images builds every competition image whenever the competition's additional override file applies. `episode-runner-impl.md` §1 (competition images) gives the build of each competition image.

---

## 4. Episode inputs and seeds<a id='seeds'></a>

### 4.1 Inputs

The episode runner fixes five inputs before step 0. The inputs are the episode seed, the episode type, the attack chain of an adversarial episode, the mode and the track.

The episode runner takes the mode and the track on its command line, and chooses each episode's type and attack chain as §15 (episode mix) specifies. In training mode and in measurement episodes, the command line may also give the type and the attack chain.

The episode runner draws each living port seed as 32 bytes from a secure random source. The fresh living port seeds that a chain named on the command line takes at fork 0 are drawn in the same way (§10.2, choosing the chain). Each episode seed derives from the living port seed and the fork's number (§4.2, derivation), unless the command line gives the seed of an episode to replay. A replay takes the living port seed, the fork's number and the episode's other four inputs from the episode record of the episode that it repeats. In evaluation mode every living port seed and every episode seed stays secret until final scoring ends.

The episode runner also reads the compression factor from the competition's additional override file, and the file-integrity scan's interval in operation from `world.yaml`, which describes the fictional asset. The episode record keeps the compression factor.

### 4.2 Derivation

The derivation is public. The seed of each generator is the first eight bytes of a hash-based message authentication code (HMAC) built on Secure Hash Algorithm 256 (SHA-256). The eight bytes are read as an unsigned integer with the most significant byte first. The key of the HMAC is the episode seed, and its message is the generator's name in the American Standard Code for Information Interchange (ASCII). A generator added later leaves the seeds of the existing generators unchanged.

A living port's own generators, `sim_tug_arrivals` and `fork_wait`, take the living port seed as the key of the HMAC. The episode seed of fork k, for k from 1, is the whole HMAC keyed with the living port seed, over `fork/` followed by k in decimal digits, such as `fork/12`. The episode seed of fork 0 is the living port seed itself. A fork continues the living port's tug streams (§6.4, how a fork is made), so a fork's episode seed drives only the generators `attack_start_step` and `attack_waits`.

| Generator | Consumer | Draws |
|---|---|---|
| `sim_tug_arrivals` | Simulator's tug arrival generator | The tug stream of each of a living port's tugs from its start, which each fork continues from the fork instant (§6.4, how a fork is made) |
| `fork_wait` | Episode runner | The fork wait before each fork of a living port (§6.3, the living port) |
| `wazuh_first_file_integrity_scan/` and the host's name, such as `wazuh_first_file_integrity_scan/fms-01` | Episode runner | The first occurrence of that monitored host's file-integrity scan at fork 0 (§6.2, first occurrences) |
| `attack_start_step` | Episode runner | The onset step (§10.3, onset and waits) |
| `attack_waits` | Episode runner | The waits (§10.3, onset and waits) |

Probe noise stays off, and gets a generator of its own if switched on. Of the digital world's own timers, the episode seed fixes at most the first occurrence of each file-integrity scan (§6.2, first occurrences).

### 4.3 Start-up input

The episode runner supplies each living port's start-up input, the file `startup.json`. The start-up input holds two fields, the living port's tug seed and `next_audit_at`, the time of the first audit. The first audit comes one audit period and one audit settling delay after the episode runner writes the start-up input. The first audit's window runs from the simulator's start to one audit period after the time of writing (`episode-runner-impl.md` §2, start-up input). Every other value of the start state is the simulator's default (`sim.md` §2.3, start state). The default start state holds 35,000 litres in the tank, no order placed, both totalisers at zero, the first bunker delivery note and the first delivery ticket numbered 1, and no paper held. Every tug starts away from the dock, and arrives one drawn time away after the simulator's start. The fuel supplier's first poll and the systems administrator's first routine check come one compressed interval after the simulator's start.

The deployment sequence attaches the directory holding `startup.json` to the stopped `sim-host` as the read-only disk device `startup-input` at `/var/lib/sim/startup`, before starting `sim-host` (`deployment.md` §8, deployment sequence). The disk device stays attached for the life of the instance, so a restarted simulator reads the same start-up input.

The competition's entry point (§3.4, competition image) reads `startup.json` once, before it starts any entity of the simulator, and passes each field's value to the simulator as a parameter (`episode-runner-impl.md` §2, start-up input). A missing file, a missing or unknown field, or a value out of range stops the simulator at start-up, with an error that names the reason. A `next_audit_at` that has passed when the entry point reads `startup.json` is out of range. With a valid start-up input, the entry point logs one `INFO` event, `start_state`, with the seed and the start state, and the episode runner takes the time of that event as the simulator's start.

`sim.md` §1.1 (parameters) states that "Parameters are supplied at startup via configuration and are immutable during a run", and §6 (tug arrival generator) that the tug seed "Must be supplied at startup". `general-deployment.md` §1.3 (competition profile) therefore admits the start-up input, which enters `sim-host` after its image is built and before the instance starts.

---

## 5. Time and the compression factor<a id='time'></a>

### 5.1 Simulated time

The simulator runs in coupled mode, at one-to-one wall-clock pace, and its calendar starts at the current Coordinated Universal Time (UTC), read once at construction (`sim.md` §2.1, operating modes). Steps and the onset window are measured on the simulator's clock, and the attack chain's waits on the clock of the episode's Incus host (§10.4, the attack chain's session).

No time source sets the clock of a living port's Incus host, or of a fork's, during its run, and each clock stops whenever its Incus host is paused. A fork's clock continues from the fork instant (§6.4, how a fork is made), so a fork's dates follow on from the living port's without a gap. The living port's dates lag real time by the sum of its pauses.

### 5.2 The compression factor

`narrative.md` §6 (deviations from real-world practice) defines a compressed scenario, which shortens every interval and duration in the port's operation by one factor, and raises every rate of fuel flow by the same factor.

A timing scales with the compression factor when it belongs to the port's operation (`sim.md` §2.2, compressed scenarios; `digital.md` §9, deviations from real-world practice, item 13).

The compression factor is provisionally 5. The FMS and the Human-Machine Interface (HMI) read their four intervals in §5.3 (timings) as whole seconds, with a floor of 1 second. Of the whole-number compression factors, only 1 and 5 therefore divide all four exactly. A compression factor above 5 needs changes to code, configuration checks and tests.

The competition's additional override file defines the compression factor. A render without that file uses a compression factor of 1, so a deployment outside the competition runs at the values in operation (`deployment.md` §7.3, additional override file). `world.yaml` holds every value in operation that the compression factor scales for the FMS, the HMI, the simulator or the Wazuh agents. `render.py` projects each scaled value into the configuration of the component that uses it (`deployment-config.md` §4, projection targets). The compression factor is published with the environment and held fixed at final scoring, because the competition keeps the environment fixed and known to entrants.

### 5.3 Timings

| Timing | Value in operation | Compression factor 5 | Source |
|---|---|---|---|
| Each tug's mean time away | 20 hours | 4 hours | `roster.vessels[].mean_time_away_s` in `world.yaml` (`sim.md` §6, tug arrival generator) |
| Inflow rate | 600 litres a minute | 3,000 litres a minute | `fuel_supplier.tanker_pump_rate_l_per_min` in `world.yaml` (`sim.md` §1.1, parameters) |
| Outflow rate | 300 litres a minute | 1,500 litres a minute | `process.dispenser_flow_rate_l_per_min` in `world.yaml` (`sim.md` §1.1, parameters) |
| Fuel supplier's poll | 15 minutes | 3 minutes | `fuel_supplier.poll_interval_s` in `world.yaml` (`sim.md` §5.3, fuel supplier) |
| Delivery lead time | 6 hours | 72 minutes | `fuel_supplier.lead_time_s` in `world.yaml` (`sim.md` §5.3, fuel supplier) |
| Systems administrator's routine check | 24 hours | 4.8 hours | `deployment-config.md` §3 (structure of `world.yaml`) |
| Audit period | 720 hours | 144 hours | A month of 30 days (`narrative.md` §3, people and shifts); `audit.period` in `world.yaml` |
| Audit settling delay | 6 hours | 1.2 hours | `audit.settling_delay_s` in `world.yaml` (`deployment-config.md` §3, structure of `world.yaml`) |
| Time either side of the audit period over which the audit also retrieves transaction records | 2 hours | 24 minutes | `audit.boundary_margin_s` in `world.yaml` (`sim-impl.md`, "Reconciliation user") |
| Dock operator's poll of a job's status | 30 seconds | 6 seconds | `intervals.dock_operator_poll_s` in `world.yaml` (`sim-impl.md`, "Dock operator") |
| FMS poll of the tank gauge | 30 seconds | 6 seconds | `digital.md` §4.5 (log-server ingestion and replica sync) |
| FMS poll of the dispenser | 5 seconds | 1 second | `deployment-config.md` §3 (structure of `world.yaml`) |
| FMS session lifetime | 1,800 seconds | 360 seconds | `fms.md` §4 (authentication and session management) |
| HMI challenge timeout | 30 seconds | 6 seconds | `hmi.md` §4 (authentication bridge) |
| Inventory scan | 1 hour | 12 minutes | `intervals.inventory_interval_h` in `world.yaml` (`siem.md` §2.1, the agent on every monitored host) |
| File-integrity scan | 12 hours | 2.4 hours | `intervals.file_integrity_interval_h` in `world.yaml` (`siem.md` §2.1, the agent on every monitored host) |
| Listening-port inventory | 360 seconds | 72 seconds | `intervals.listening_port_interval_s` in `world.yaml` (`siem.md` §2.1, the agent on every monitored host) |
| Bound for detecting and acknowledging a leak | 12 hours | 2.4 hours | `narrative.md` §5 (operational correctness conditions) |

In a compressed scenario the audit period is a fixed interval in hours, which scales with the other timings of the port's operation (`narrative.md` §6, deviations from real-world practice). Each audit covers one period, with explicit bounds from the simulator's clock (`recon.md` §9, timekeeping). The time ranges in which the audit retrieves a period's records follow from the dock's arrival rate (`sim-impl.md`, "Reconciliation user", "Splitting the window"). The dock's arrival rate is approximately the sum over the tugs of one over each tug's mean time away (`sim.md` §6, tug arrival generator). The simulator treats a person's own action, such as a sign-in or a transcription, as instantaneous (`sim-impl.md`, "Compressed scenarios").

`sim.md` §5.3 (fuel supplier) sets the reorder threshold at 20,000 litres, and the road tanker arrives one lead time after the poll that finds the level at or below it.

The timings below stay fixed, because each belongs to the software and networks.

- The actors' retries and back-offs, and the FMS's push back-off and read limits, are the software's own fault handling. The software decides how many times to retry, how long to back off and how much to read at once.
- The settings of HAProxy, the proxy on `sim-host`, and the deployment's readiness probes detect a dead backend and a host that is up, and depend on how fast the software runs.
- The dock operator's three time limits on the HMI and the fuel supplier's limit on a poll attempt bound the wait for a response from the digital world, and await specification (§19, open items). The digital world's speed stays the same at every compression factor. The values awaiting specification are 5 seconds for the dock operator to connect, 15 seconds for an exchange and 45 seconds for the login challenge, and 15 seconds for the fuel supplier's poll attempt. The HMI's 30-second challenge timeout belongs to the port's operation and scales, staying shorter than the fixed 45-second limit at every compression factor.
- The terminal timings of the reconciliation user, the flow timeouts of packet inspection, the security monitoring's inspection of traffic at the zone boundaries, and the log server's 30-second query offset stay fixed. The terminal timings pace a terminal session so that the software has processed each input before the next arrives. The flow timeouts are settings of the packet inspection software, and the query offset allows for the delay before a record can be queried. Each of the four items depends on how fast the software runs.
- Every measured delay, every calendar quantity, and the 1-second clock bound stay fixed. A measured delay is a property of the software and the network. A calendar quantity is a definition. The clock bound is the precision of the simulator's clock.
- The competition's own timings, the step duration, the attack chain's waits, the allowance, the settle delay, the settle timeout and the fork wait, stay fixed. The episode runner, the defender and the adversary are software outside the port's operation.

The competition's additional override file sets four flow timeouts of packet inspection. A closed connection of the Transmission Control Protocol (TCP) is held for 20 seconds. An established flow of the User Datagram Protocol (UDP), and an exchange of the Internet Control Message Protocol (ICMP), each with traffic both ways, are held for 20 seconds. The packet inspection counts a TCP connection as new until it sees the connection open. A new TCP connection is held for 30 seconds. Suricata, the packet inspection software, holds the same four for 60, 300, 300 and 60 seconds as packaged. Every other flow timeout keeps its packaged value, so an open TCP connection that carries no packets is held for 600 seconds after its last packet. A connection that pauses for longer continues as a new TCP connection, because the packet inspection's pickup of a connection in mid-stream stays off as packaged. The 30-second timeout stays above the Wazuh agents' 20-second keep-alive, so a Wazuh agent's new TCP connection stays one flow from its first packet to its last, across every pause between keep-alives.

---

## 6. Start state, living ports, forks and settle period<a id='start'></a>

### 6.1 Frequency requirement

Across episodes, each event of the port's operation occurs per hour of simulated time as often as it does in operation, multiplied by the compression factor. Tug arrivals, deliveries and scheduled scans are such events.

### 6.2 First occurrences

Every periodic event of the simulator, and every schedule of the security monitoring, runs on its compressed interval, which is its interval in operation divided by the compression factor. The simulator's periodic events are the fuel supplier's poll, the systems administrator's routine check and the audit. In a fork, each continues the living port's schedule from the fork instant (§6.4, how a fork is made). The forks occur at instants spread over each living port's operation, so the episodes contain each event in the proportion that a span of operation of the same length does. At fork 0 the fuel supplier's first poll and the systems administrator's first routine check are due one compressed interval after the simulator's start. The first audit begins at the time that the start-up input gives (§4.3, start-up input).

**The file-integrity scan.** Each Wazuh agent scans its host's files when it starts, and then every 12 hours in operation. The Wazuh agent's start fixes the time of every scheduled scan in Wazuh 4.14.7, the security monitoring product in the version the build installs. A fork keeps the living port's scan times. At fork 0 the Wazuh agents start with the episode's hosts, and the episode seed places each monitored host's first file-integrity scan uniformly within the compressed interval from the simulator's start.

Where the time of a host's first file-integrity scan is inside the episode, the episode runner requests the scan from the Wazuh manager on `siem-01` at that time (`episode-runner-impl.md` §3, scan requests). The requested scan and its alerts are the same as those of a scheduled scan. The request itself leaves records on the Wazuh manager that only a requested scan leaves. Every submitted SIEM host must keep the scan request working. At a compression factor of 5 and a horizon of 12 minutes, the first file-integrity scan occurs inside an episode at fork 0 for 8.3% of hosts. With twelve monitored hosts (`siem.md` §1, purpose and scope), an average episode at fork 0 requests about 1.0 scan.

**The inventory scan and the listening-port inventory.** Wazuh 4.14.7 runs an inventory scan only on its schedule, because every inventory endpoint of its server's application programming interface (API) is read-only. The listening-port inventory comes from a command that the Wazuh agent's log collector runs on its own schedule alone. The phases of both therefore stay fixed from each Wazuh agent's start in the living port, and a fork keeps them.

### 6.3 The living port

A living port is a deployment of RavensPort that runs continuously at the compression factor, with no attack chain and no step session. The fictional port keeps the records of every fuel transfer for 3 years, as the International Convention for the Prevention of Pollution from Ships (MARPOL) requires (`narrative.md`, introduction). A living port's hosts therefore hold the records of the operation before any episode, and at each fork they hold the living port's records since its deployment. A living port takes no snapshot set and is never restored from one (`deployment.md` §10, snapshot sets). A living port's history comes from its own operation, and each fork copies its whole virtual machine (§6.4, how a fork is made).

**Start.** The episode runner deploys a living port by the deployment sequence of `deployment.md` §8 (deployment sequence), with the competition's additional override file (§3.4, competition image). The simulator starts from the living port's start-up input (§4.3, start-up input). Every record of a living port begins with its deployment.

**Images.** A living port runs from one set of images, the competition images or an entry's, and every fork of the living port runs from the same images. Each entry therefore has living ports of its own, so that its forks carry its tooling (§3.2, modes). In evaluation, every entry runs over the same set of living port seeds, each on a living port of its own, so that every entry meets the same operation of the port.

**Forks.** Fork 1 occurs one fork wait after the living port's settle period. Each later fork occurs 14 minutes and one fork wait after the instant at which the fork before it was due, on the living port's clock. The 14 minutes are the settle timeout and the horizon, which bound the settle period and the steps of the earlier fork's episode. A fork instant can come before the previous fork has been deleted. The episode runner then saves the living port's state at the fork instant, and starts the new fork once the previous fork has been deleted. The new fork's clock continues from the fork instant. A fork for which the machine that runs the Incus hosts lacks the disk space or the memory waits until both are free, and the episode runner records how late the fork came. The living port's generator `fork_wait` draws each fork wait uniformly from 0 to 6 minutes (§4.2, derivation), so a fork occurs about every 17 minutes. A drawn fork instant gives step 0 a random phase of every timer of the port. Each fork provisionally runs one episode (§17, parameters). In training, an episode can start from a living port of any age, even one that has run for minutes (§18, limitations). The episode record states each fork's wait and age.

**Fork 0.** The episode of fork 0 runs on the living port's own deployment, from the settle period after the deployment. The reset deletes the deployment, so a living port that runs fork 0 makes no fork. A living port that makes forks runs no episode at fork 0, and numbers its forks from fork 1.

**Snapshots for replay.** Each living port keeps snapshots of its whole state, saved as for a fork (§6.4, how a fork is made), for replays (§14, reproducibility). A snapshot holds the simulator's whole state, with the position of each tug stream. The snapshots are taken every 15 minutes for the living port's first hour, every 30 minutes for the next 2 hours, and every hour for the next 4 hours. The interval then doubles after every four snapshots. A run keeps the latest snapshots that fit its snapshot cache, whose size the entrant sets in training (§17, parameters). The snapshots belong to the run.

**A stopped simulator.** A living port's forks end when its simulator stops.

### 6.4 How a fork is made

A fork runs on an Incus host of its own, because the fork keeps every host's fixed address and hardware address, which the living port's networks already use. The fork's start is the instant at which the fork's Incus host resumes from the living port's saved state.

**Requirements.** Every fork meets five requirements.

1. **Continuity.** The fork starts in the living port's state at the fork instant. Every host of the fork holds the living port's records. The fork's simulator holds the living port's tank volume, delivery state, totalisers, next document numbers, tugs waiting and schedules. The fork's simulator also holds the open audit window and the paper held for it, so the fork's next audit covers the whole window from its start in the living port.
2. **The living port continues.** Making the fork leaves the living port's operation, timed by the living port's own clock, as it would have run without the fork.
3. **Held clocks.** The fork's clock continues from the fork instant at the rate of real time. The living port's clock stops for the pause at each save, so a save leaves no gap in the living port's records, and its dates lag real time by each pause. No time source sets either clock during a run (§5.1, simulated time).
4. **The living port's tug streams.** The fork's simulator continues each of the living port's tug streams from the fork instant, at the position that the stream then holds, with each tug's scheduled arrival (§4.2, derivation). The fork's simulator keeps the parameters that it took at the living port's start (`sim.md` §1.1, parameters).
5. **Nothing scored before step 0.** Every record that making the fork leaves on the fork's hosts is written before step 0, and the score counts the steps alone (§7, step protocol).

The episode runner derives the start state at the fork instant from the fork's records, for the choice of an attack chain (§10.2, choosing the chain). The database of the FMS gives the tank gauge's readings, the jobs and the transactions. The journal of the tank-gauge relay on `atg-relay-01` gives the fuel supplier's polls. The relay records each connection in the journal as it accepts the connection, and the faults below compare those connections with the polls that the simulator logged. The episode runner's look-back reaches back from the fork instant one lead time, the fill of one delivery and two poll intervals, with 600 seconds more. The look-back is 5,640 seconds at a compression factor of 5, about 1.6 hours, and about 7.2 hours in operation. The look-back reaches the poll that placed any order whose road tanker can still be at the dock. The journal must keep at least the polls of the look-back. The default retention of the system journal on `atg-relay-01`, a cap on its size with no limit of age, keeps them. The simulator's log gives the refused dispenses, the scheduled arrivals, the fuel supplier's orders, and each road tanker's arrival and the end of its delivery. The reconciliation user's home directory gives the completed audits. For each tug, the episode runner takes the scheduled arrival that the simulator logged last before the fork instant. The simulator draws a tug's next time away only as the tug leaves the dock, so the episode runner predicts no arrival that follows a departure inside the episode. Where such an arrival could reach a refuel that a chain needs, the episode runner excludes that chain alone, and the episode record states the reason (§10.2, choosing the chain). The reads run through Incus before step 0, in benign and adversarial episodes alike, and leave one session in the records of the FMS's database, or two when a read of the FMS is retried.

**Inferences at the fork.** The fork holds the living port's whole memory, so each procedure under way at the fork instant continues in the fork as it would have in the living port. The episode runner reads only the records written up to the fork instant, and those records do not show how such a procedure ends. The episode runner therefore infers each procedure's end from the records (`episode-runner-impl.md` §8, ranges of the predicted events). In the four cases below, the records can seem to disagree.

1. **A delivery note being linked.** The tug engineer hands back the co-signed bunker delivery note, and the tug departs at once, so the simulator logs its next scheduled arrival. The dock operator links the delivery note to the FMS's job afterwards. A job with its dispense and no linked delivery note, whose tug's latest scheduled arrival was logged after the dispense, is a delivery note being linked. The dock is busy for the link and its retries alone.
2. **The next job created before a departure is logged.** A served tug, its delivery note linked, or a refused tug, its job cancelled, is leaving and does not hold the dock.
3. **The same between a tug and the road tanker.** A tug or a road tanker that is leaving does not hold the dock for the other.
4. **A poll under way.** The fuel supplier makes one poll at a time on its fixed schedule, and logs each poll's event when the poll ends. A poll that has fallen due, and whose event the simulator has not logged, is under way. Until 12 seconds after it fell due, the poll is a procedure under way whose end the episode runner infers.

The bounds that follow also cover the other procedures under way:

- a refused job awaiting its cancel;
- a dispensed tug awaiting its departure;
- a refuel or a fill under way;
- a tug or the road tanker waiting at an idle dock;
- a fill transcribed before its delivery's end is logged.

**Bounds on the inferences.** Each inference allows its procedure a bounded time to end. Past the bound, the episode runner presumes the inference wrong, and discards the episode as a fault of the testbed. Every bound is provisional. Several bounds derive from the dock operator's exchange, one request to the HMI with its retries after 1, 2 and 4 seconds. The exchange is provisionally limited to 15 seconds, four attempts of 2 seconds each and the three waits between them, and its limit awaits the specification of the actors' time limits (§19, open items).

- A leaving tug departs within 5 seconds of its delivery note's link or its refused job's cancel. The departure crosses two of the simulator's channels and no exchange.
- A refused job is cancelled within one exchange, 15 seconds, of its refusal.
- A dispensed tug departs within one dock operator's poll of a job's status, three exchanges and 5 seconds of its dispense's close. The bound is 56 seconds at a compression factor of 5, and 80 seconds in operation.
- A refuel's dispense closes within 12 seconds for its start, the preset's pumping at the dispenser's rate and 15 seconds, after its job opens.
- The road tanker's fill is transcribed within 12 seconds, the delivery's pumping at the tanker's rate and 15 seconds, after its job opens.
- A delivery note is linked within one exchange, 15 seconds, of its tug's departure.
- A tug or the road tanker waiting at an idle dock has its job opened within 87 seconds of the later of its arrival and the end of the previous job. The bound is 12 seconds for the start and 75 seconds for the dock operator to log in again, whether or not it does.
- A delivery's end is logged within 5 seconds of its fill's transcription, longer by the lateness of a poll under way that fell due no later than the transcription. The road tanker driver signals the delivery's end as it hands over the delivery ticket, before the dock operator transcribes the fill. The fuel supplier logs the end at once, unless it is in a poll that began before the signal.

The rates are those of `world.yaml` at the compression factor. The dock operator logs a login after its warning that the HMI session expired. A login logged since a procedure began adds 75 seconds to the bound of a refused job's cancel, a delivery note's link, a dispensed tug's departure, a refuel and a fill. A login adds nothing to the 5 seconds of a leaving tug, and the bound of an idle dock already includes the 75 seconds. The 75 seconds are one exchange for the login, at most the login challenge's 30 seconds in operation, and one exchange for the challenge's answer. A further exchange makes again the request that the login interrupted. For a dispensed tug, the logins counted are those logged from 75 seconds before its dispense's close. The poll of the job's status under way at the close can have begun a login before the close.

**Faults.** A disagreement between the fork's records and the simulator's log that no procedure under way at the fork instant explains is a fault of the testbed. A fault discards the episode with its cause in the episode record (§12, terminated and discarded episodes). The faults are the following.

- A read that fails after one retry, of the FMS's database, the relay's journal, the simulator's log or the reconciliation user's files.
- No reading of the tank gauge before the fork instant.
- A tug with no scheduled arrival logged. The simulator's log is a file in memory that nothing rotates, and the simulator is never restarted (`sim-network.md` §7, image, build and supervision).
- A dispense whose preset differs from the request that the simulator logged.
- More than one job holding the dock.
- A tug's job and the road tanker's both holding the dock.
- More than one fill under way.
- An open dispense that no tug at the dock accounts for.
- A procedure that has not ended within its bound (above):
  - a refused job with no cancel;
  - a tug with no departure after its refused job's cancel or its delivery note's link;
  - a delivery note not linked within its bound of the tug's departure;
  - a dispensed tug with no departure after its dispense's close;
  - a refuel whose dispense has not closed;
  - a fill not transcribed;
  - a tug or the road tanker waiting at an idle dock with no job opened;
  - a fill transcribed with no delivery's end logged, since a fuel supplier that never ends its order never orders again.
- An order with no scheduled arrival of its road tanker.
- A road tanker more than 12 seconds past its scheduled arrival with no poll under way that fell due no later than that arrival, since only such a poll can hold the arrival back.
- A poll logged within the look-back whose connection the relay's journal lacks, as when journal entries are lost.
- A poll whose connections are followed by a connection of a later due time, with no event logged for the earlier poll. The episode runner groups the journal's connections into polls by due time. A poll takes the connections from the start of its range, 0.3 seconds before its due time, until the start of the next poll's range. A poll can start that much early (`episode-runner-impl.md` §8, ranges of the predicted events). The fuel supplier makes one poll at a time, so the earlier poll has ended. Connections of two due times or more with no event of the fuel supplier logged are such a fault.
- A poll under way more than one poll interval past its due time, which means that the relay or the fuel supplier has stopped. A retry that crosses into the next due time is then a fault in either reading of the grouping, and needs no rule of precedence.

Several connections within one due time are a poll that the fuel supplier retried after a failed attempt, at 1, 2 and 4 seconds (`sim-impl.md`, "Retry policy (transient digital-world I/O)"), and are no fault. Connections of one due time with no event logged are a first poll under way or overdue, and no connection at all is a first poll that has not fallen due or is overdue.

**An overdue poll or road tanker.** A poll under way more than 12 seconds after it fell due, and no more than one poll interval, is overdue, 10 seconds beyond the lateness that the ranges of the predicted events allow it. The relay may, for example, hold the poll's connection without answering. The start state is then uncertain, and the episode runner excludes the poll chains alone, with the reason in the episode record (`episode-runner-impl.md` §8, ranges of the predicted events). The fuel supplier serves its polls and the road tanker's arrival in turn, so a poll under way holds the road tanker's arrival back. Only a poll under way that fell due no later than the road tanker's scheduled arrival can hold the road tanker back. The fuel supplier serves a road tanker that is due before it starts the next poll. A road tanker more than 12 seconds past its scheduled arrival, with such a poll under way, leaves the dock's order of service uncertain, and the episode runner excludes the refuel chains. That poll is then more than 12 seconds overdue too, and the poll chains are excluded with the refuel chains. A poll that holds the road tanker back more than one poll interval past its due time is the fault above. With no such poll under way the road tanker's delay is a fault. An overdue poll and an overdue road tanker are the only cases that leave the start state uncertain.

**Method.** A fork is made by copying the living port's whole virtual machine, its Incus host. The episode runner pauses the living port's Incus host, saves its memory and the state of its devices, takes a copy-on-write snapshot of its disks, and resumes it. No directory of the machine that runs the Incus hosts is mounted in the living port's Incus host at the save. The fork's Incus host starts from the saved state, with every process of the living port and its state in memory, the simulator's included. A fork can therefore occur at any instant of operation, with a dispense or a fill under way. Every host of the fork continues from the fork instant, with the Wazuh agents on their schedules and the fork's clock continuing from that instant. Each Incus host uses a clock source that stops during each pause of its virtual machine, as requirement 3 (held clocks) needs (`episode-runner-impl.md` §5, forks).

### 6.5 Adversary host

The adversary host is present in every episode, as the attack-chain documents place it in every deployment (`adversary.md` §1, placement), so benign and adversarial episodes share one topology. The adversary host boots with its network interfaces down and unaddressed, with no service, no other host's name to resolve and no time client (`adversary.md` §1, placement; §2, contents). The adversary host sends traffic only once a stage needs it, and a stage that needs a segment brings that segment's interface up for its own duration only.

The competition relies on the adversary host's own image to hold every attack chain's binary, so the adversary host holds the same files in benign and adversarial episodes. The image holds the binaries of its own build, or, without the attack chains' source, the binaries of the competition's public release for the adversary host's machine architecture (§10.1, the competition library). The competition gives the adversary host no competition image, and the competition's additional override file neither adds nor patches the node `adversary` (`episode-runner-impl.md` §1, competition images). No entry may change the adversary host.

### 6.6 Settle period

After a deployment, the settle period ends once the episode runner has seen four settle conditions hold and has then waited the settle delay. Step 0 of fork 0 begins at that instant, and fork 1 of a living port that makes forks occurs one fork wait later (§6.3, the living port). In a fork, the episode runner confirms the four settle conditions, after attaching the training volume in training mode (§3.2, modes), and step 0 follows at once.

1. The Wazuh agent on every monitored host is connected to the Wazuh manager and past the restart that applies the Wazuh manager's shared configuration.
2. Every Wazuh agent's file-integrity scan at its start, and its first listening-port inventory, are complete.
3. Packet inspection is running on all three firewall nodes, the nodes between the zones that carry it.
4. The simulator is running, and so is each service whose failure would stop it.

Changes to a host's files and listening ports are reported from that file-integrity scan and that listening-port inventory onwards. Each Wazuh agent forwards the journal lines written after its restart.

In a fork, the settle conditions held in the living port long before the fork instant, so the first three conditions accept events from any time. Condition 1 holds once every Wazuh agent is connected. Condition 2 holds once each Wazuh agent's listening-port inventory is among the most recent records of `archives.json`, an output file of the security monitoring (§8, query interface). Condition 3 holds once the packet inspection's service is active on all three firewall nodes. The episode record states each expected log line that log rotation has removed.

The flow-record lag is the time from the end of a network connection to the arrival of its flow record at the Wazuh manager. The settle delay is provisionally 45 seconds, above the longest flow-record lag measured (§18, limitations).

The fuel supplier, the dock operator and the reconciliation user are the actors of the simulator that stop it when a service that they call fails. The fuel supplier calls the tank-gauge relay on `atg-relay-01`. The dock operator calls the HMI on `hmi-01`, and the FMS on `fms-01` behind it. The reconciliation user's audit calls the workstation `recon-01` and the log server's replica on `hist-dmz-01`. The FMS signs the dock operator in against the directory on `corp-it`, and the workstation's directory client signs the reconciliation user in against the same directory. HAProxy on `sim-host` carries every connection of an actor out of the simulator. The deployment sequence checks the HMI and the FMS, with its database, before it starts `sim-host`. The settle period waits for the tank-gauge relay, the workstation, the log server's replica, the directory, the workstation's directory client and HAProxy on `sim-host`.

The episode runner reads each settle condition through Incus, so its checks stay off the deployment's networks and out of the records that the defender reads. A deployment or a fork whose settle conditions still fail at the settle timeout, or whose simulator stops before step 0, has failed, and the episode is discarded. The settle timeout is provisionally 120 seconds from the start of `sim-host`, or from the fork's start. A living port that makes forks waits up to 900 seconds from the living port's start for the settle conditions, and makes no fork if they do not hold by then.

The Wazuh manager's databases, one for each Wazuh agent, "fill over several minutes" (`siem-tests.md` §9, file integrity and inventory) and may still be filling at step 0. The databases serve the Wazuh manager's management interface alone, on TCP port 55000 (`siem.md` §6, what the system produces). The query interface reads the security monitoring's two output files alone, so every episode's observations come from them.

---

## 7. Step protocol<a id='steps'></a>

Each step lasts 5 seconds on the simulator's clock, and begins as the previous one ends. A step covers 25 seconds of operation at a compression factor of 5.

1. At the start of step t, the episode runner sends the defender a decision request.
2. The defender reads the latest data it has requested through the query interface, and returns one decision.
3. The episode runner carries out the decision as soon as it arrives, even before earlier defensive actions have completed. A defensive action runs until it completes, even past the end of its step. The software and the simulator of RavensPort, on which the actions act, decide how the actions interact. Carrying out the Track A alarm ends the episode.
4. At the end of step t, the episode runner computes the step's reward from the ground truth at that moment, and opens step t + 1.

In training mode, the decision request for step t carries the reward for step t − 1, and the end notice carries the last step's reward. In evaluation mode, no step message carries a reward, so no decision of an evaluation episode can depend on the ground truth through a reward. In both modes, the end notice says that the episode has ended. The episode runner writes each step message to the step session's standard input or reads it from the step session's standard output (`episode-runner-impl.md` §6, step messages). `general-deployment.md` §1.1 (release profile) rules out networking between the Incus host and the RavensPort nodes, so the step session alone carries the step messages. The end notice closes the exchange of step messages.

A decision is valid when it is one of the decisions of the episode's track: no action or the Track A alarm in Track A, and no action or one defensive action in Track B. The Track A alarm is therefore invalid in Track B, and a defensive action is invalid in Track A. The defensive actions, and how a decision message names each one, await specification (§19, open items). The episode runner drops each invalid line with a logged warning and counts it, and an invalid line takes no step's place.

The first decision message with a valid decision that arrives during a step is that step's decision, and the episode runner drops each further one with a logged warning. When a step ends before any such message arrives, its decision is no action in both tracks. A decision message that arrives after the end of the step whose decision request it answers therefore counts for the step during which it arrives.

**The Track A alarm.** In Track A the episode runner carries out the Track A alarm at its arrival, in training mode and in evaluation mode. The episode runner then releases no further stage of the attack chain, sends the end notice within 1 second of the alarm's arrival, and stops the attack chain as §10.5 (stopping the chain) states. The step during which the alarm arrived is the episode's last step. A suppressed episode records the alarm and runs on (§9, suppressed episodes for Competition Rule 5).

**Reward.** A step's reward can depend on decisions taken much earlier and on defensive actions still running. Track B's reward is defined per step, and awaits specification with Track B scoring (§19, open items). Track A's reward is 0 at every step until the episode ends, and the final step carries the episodic reward, `R_n` (§13, episode record). The episode runner computes `R_n` itself, from the Track A alarm and the stages that the episode runner released before the alarm (§10.4, the attack chain's session). The episode runner returns every step's reward in training mode, and none in evaluation mode. The episode record holds every step's reward in both modes, and with them the episodic reward (§13, episode record). Before it sends the end notice, the episode runner reads the simulator's log once, waiting at most 0.5 seconds. A stop that the log places before the alarm's arrival ends the episode as a simulator stop (§11, horizon and termination). The episode record then drops the alarm, and in training mode the end notice carries the reward of an episode with no alarm. A stop that the read misses, and that the episode runner finds after the end notice, drops the alarm from the record in the same way. In training mode, the record's `R_n` can then differ from the end notice's, and scoring uses the record's.

**Rule on the reward.** The reward signal may be used during training, for optimisation, but must not be used for inference during evaluation. In evaluation mode no step message carries a reward, so no decision there can use one.

**Concurrency.** RavensPort runs continuously. The adversary, the port's operation and the defender act concurrently, each at its own pace.

**Judgement of each step.** Computing each step's reward in Track B needs the ground-truth evaluator to judge each step against the operational correctness conditions by the step's end. Track A's reward needs no such judgement: the episode runner computes it from ground truth that it records itself, the stages of the attack chain that it released before the alarm.

**The defender's program.** The step session runs the launcher, `/usr/local/bin/defender` on `siem-01`, which the competition image of `siem-01` installs. The launcher runs the defender's program from the entry's storage volume, which `siem-01` always mounts at `/var/lib/entry`, in both modes (§3.2, modes). The program is `/var/lib/entry/defender` when that is a regular file that `siem-01` can read and execute, and the idle defender's program when the volume holds no `defender`. A volume that `siem-01` cannot read and search is a fault of the entry, which the launcher tells apart from a volume with no `defender`. So is a `defender` of any other kind, such as one that is not readable, not executable or a directory. At a fault the launcher runs no program, and the episode runner discards the episode with the fault as its cause in the episode record (§12, terminated and discarded episodes). The episode record names the program that ran, the entry's `defender` or the idle defender. The launcher chooses once, as the step session opens. In training mode the defender may replace `/var/lib/entry/defender` in the training volume, and the replacement runs from the next episode of the training lineage. In evaluation mode the volume is read-only, so the program stays as the entrant submitted it.

**The program's start and environment.** The episode runner opens the step session at the start of step 0, before the query interface records the offsets at which a request that names no start begins (§8, query interface). Step 0's decision request follows that recording. When the recording fails, the episode runner discards the episode and closes the program's standard input with no step message (`episode-runner-impl.md` §12, query interface). The program runs as root, in the working directory `/root`, which the launcher sets. The entry's files are at `/var/lib/entry`, read-only in evaluation mode and read-write in training mode. The unprivileged container `siem-01` maps none of the volume's owners. The episode runner therefore opens each of the volume's files for reading by every user, and for execution by every user where the file's owner may execute it. The program can use the interpreters of the operational image of `siem-01`, which is built on Ubuntu 24.04 and holds `sh`, `bash` and `python3`. The competition image adds no interpreter.

**Idle defender.** The idle defender's program answers each decision request at once, from the request alone, with a decision message that holds the idle defender's decision for the request's track. The program exits after the end notice.

---

## 8. Query interface<a id='query'></a>

The query interface runs on `siem-01`, beside the entry, and reads the security monitoring's two output files, `alerts.json` and `archives.json`, directly, because a read must be fast. `siem.md` §6 (what the system produces) calls them "the interface that anything consuming this data reads".

A request selects records by output file, originating host and input, the log file or command the Wazuh agent collected them from. A request may also name its start, a time as far back as the security monitoring's first record. A request that names none starts at the offsets that the query interface takes a fraction of a second into step 0 (`episode-runner-impl.md` §12, query interface). Each read returns the matching records written since the defender's previous read with the same request, or, for a request's first read, since its start. Requests that differ only in their start keep separate positions. No read returns a record written after the read. `siem.md` §7 (retention) keeps both output files indefinitely, so a read can reach every record back to the first.

The record of reads, kept on `siem-01`, notes each read's step, time on the clock of `siem-01`, request, and each dated file with the byte range read. The export copies each dated file of the episode's days, and every other dated file that a read touched, whole into the episode record under the file's own name. The ranges therefore refer to the exported files (§13, episode record). From the episode record, every read can be replayed exactly and checked against the entry's telemetry specification, as Competition Rule 3 (reads only through the query interface) requires.

The record of reads stays open to the defender, because the entry submits `siem-01`. The entry's task is to get the data its policy needs into `alerts.json` and `archives.json`, and to choose what to act on. The deployment omits the search store of `siem.md` §9 (extension for storage and search), and each entry decides whether and how to store, parse or vectorise what it reads. Compliance with Competition Rule 3 (reads only through the query interface) is checked by reviewing each submitted SIEM host, and the organisers contact the entrant before disqualifying an entry for a breach of uncertain intent.

Data from an entry's added host tooling reaches the defender only once it lands in `alerts.json` or `archives.json`.

**First version.** The first version of the query interface is a program on `siem-01` that the defender runs once for each read. The program serves a defender in either track as Competition Rule 3 requires (`episode-runner-impl.md` §12, query interface).

- **A read by output file.** The defender names `alerts` or `archives`, for `alerts.json` or `archives.json`, the two files that `siem.md` §6 (what the system produces) makes the security monitoring's interface. The program writes the matching records as the file holds them.
- **Selection by originating host and input.** Either, both or neither may be given. A detector can then read only the data its telemetry specification declares, against which Competition Rule 3 checks each read.
- **A start for each request.** A request may name its start as an instant in UTC. A defender can then read the history from before the episode, as a real defender can, and a request that names no start begins at step 0.
- **Records since the previous read with the same request.** A request's start is part of the request, so each start keeps its own position. A detector that reads once in each step receives each record once, and misses none when the Wazuh manager starts writing the next day's dated file (`siem.md` §7, retention).
- **The record of reads.** Each read enters the record of reads with its request's start. The export copies the record of reads into the episode record with every dated file that a read touched, so that every read replays and checks as this section states.

A long-running form of the query interface, and selections beyond output file, host and input, follow later (§19, open items).

---

## 9. Suppressed episodes for Competition Rule 5<a id='rule5'></a>

The reference deployment is RavensPort as the competition deploys a living port, with the SIEM host as released and no entry's tooling. Competition Rule 5 requires that an entry's tooling leave the environment as the reference deployment runs it. Benign operation must stay as it is, and adversaries must reach the same stages as in the reference deployment. The competition checks the requirement by replaying a sample of evaluation episodes as suppressed episodes, with the entry's images deployed and its defender running. The episode runner suppresses decisions only in a run that its command line marks as such a replay (`episode-runner-impl.md` §6, step messages). In every other episode, in either mode, the episode runner carries out each decision. A Track A alarm leaves a suppressed episode running, and a defensive action reaches the episode record alone. The episode ends only at its horizon or when the simulator stops (§12, terminated and discarded episodes).

The check of Competition Rule 5 applies two direct checks in either track.

- **Benign episode.** Track B scoring finds every condition held at every step, so the episode stays benign.
- **Adversarial episode.** The attack chain executes as expected and reaches every stage, as its stage events show.

---

## 10. Adversarial episodes<a id='adversarial'></a>

An adversarial episode runs one attack chain from the adversary host. The episode runner chooses the chain, starts its binary at the onset step, releases each stage after a drawn wait, records each stage's beginning and end, and stops the binary at the episode's end.

### 10.1 The competition library

The attack chains' generator builds one binary for each chain, and lists the chains in its catalogue, `chains.json`. For each chain the catalogue gives its binary, foothold and impact, each stage with the facts that the stage requires and provides, and the conditions that the chain's stages declare. The episode runner refuses the catalogue of a mock build, whose binaries take fixed facts in place of the scan of the information technology (IT) segment.

A stage's target is the host or segment that the stage acts on, which the episode runner derives from the stage's required facts (`episode-runner-impl.md` §7, targets of stages).

The episode runner holds a table of every stage, reviewed against the stage bodies. For each stage, the table gives the port activity that the stage needs, whether the stage restarts the Wazuh manager, and the stage's run time where episodes measured it. For the one stage that no episode has measured, the stage that reads what a completed audit left, the table derives the run time from the stage's body, provisionally until episodes measure it. Any other stage takes a run time of 0 to 30 seconds. Four port activities appear. Two stages wait for the fuel supplier's next poll of the tank gauge. The inventory stage, `enumerate-siem-agent-inventory`, runs three peer scans of 75 seconds each, of the network peers of the FMS, of the tank-gauge relay and of the HMI, in that order. The stage finds the relay's Wazuh agent before it scans the relay's peers. In two measured runs, the stage's scans of the FMS, the relay and the HMI ran from 0 to 74.8, 74.8 to 149.8 and 149.8 to 225.8 seconds after the stage began. The stage ran for 226.1 seconds in both, inside its band of 225.5 to 233.5 seconds. One stage that denies a service needs a refuel during the inventory stage's peer scan of the FMS, from 1 to 74 seconds after the inventory stage begins. Another needs a refuel during the peer scan of the HMI, from 151 to 225 seconds after the inventory stage begins. Each window lies 1 second inside its measured scan, since the measurements come from the API log of `siem-01` in whole seconds. One stage needs an audit to be running. The table also marks the stage that reads what a completed audit left, whose run time, derived from its body, is 15 to 110 seconds. The episode runner holds, as reviewed, the digest of the attack chains' source. The source is every Rust file of the attack chains' crates outside their tests, examples and benchmarks, with every manifest and lock file of those crates. The digest covers the manifests and lock files, since the build settings that the manifests hold and the dependency versions that the lock files record decide what the binaries do. When the source's digest differs, the episode runner refuses adversarial episodes until the table is reviewed again. The episode runner also refuses binaries built before the last change to any of those files.

The competition's public release holds the attack chains' binaries without their source. For each machine architecture that it supports, the public release holds the binaries built from the reviewed source and their `chains.json`. The public release lists the SHA-256 digest of each of those files, and records the digest of the source that the binaries were built from. Without the source, the episode runner checks that the recorded digest equals the digest held as reviewed, and that every binary and `chains.json` match their listed digests. The episode runner refuses adversarial episodes when either check fails. Without the source, the episode runner makes no check of when the binaries were built.

The competition library holds the chains that start on the adversary host and whose stages the table all holds. Each declared condition names a running audit, a completed audit, or one of the two refuels. A chain stays out of the competition library when the table gives none of its stages the need that one of its declared conditions names. Every chain of the competition library leaves the Wazuh manager running, and the only port activities that its stages need are the fuel supplier's poll and the two refuels. A stage that reads what a completed audit left needs only a living port that completed an audit before the fork instant (§10.2, choosing the chain).

### 10.2 Choosing the chain

The episode runner takes the episode seed first, then the chain. At the fork, the episode runner derives the start state, with any transfer under way and the tugs waiting (§6.4, how a fork is made). At fork 0 the start state is the one that §4.3 (start-up input) gives, with every tug away from the dock. The episode runner then takes each tug's scheduled arrival. At fork 0 the episode runner chooses the chain before anything is deployed (§3.1, phases). The episode runner then draws again each tug's first time away and requested volume from the tug seed, on that tug's stream (`episode-runner-impl.md` §8, ranges of the predicted events). The tug's first arrival occurs that time after the simulator's start. The volume sets the length of the tug's refuel, and so the timing of later refuels. In a fork the episode runner takes each tug's scheduled arrival from the simulator's log (§6.4, how a fork is made).

From the start state and the scheduled arrivals, the episode runner computes when the fuel supplier polls and when each refuel's connections exist. From the episode's onset step, its waits and the stage run times, the episode runner computes when each stage of a chain runs. Each time is a range, earliest to latest, from times measured in episodes and from the stage run times of §10.1 (the competition library). `episode-runner-impl.md` §8 (ranges of the predicted events) gives the ranges.

A chain is admitted to the episode when each condition below holds, the first four at every time that the ranges allow.

1. A refuel that a stage needs overlaps the inventory stage's peer scan of the FMS or of the HMI, as the stage needs, by at least 15 seconds.
2. No tug leaves the dock inside the episode before the latest start of a refuel that a stage needs. Such a tug's return could come first in the dock's queue, and the episode runner predicts no such return (§6.4, how a fork is made).
3. For a stage that waits for the fuel supplier's poll, the poll that counts is the first whose range starts at least 2 seconds after the stage's latest beginning. That poll's range ends at least 15 seconds before the earliest end of the horizon.
4. The chain's last stage begins at least the allowance of §11 (horizon and termination), 45 seconds, before the horizon.
5. For a chain with a stage that reads what a completed audit left, the living port has completed an audit before the fork instant.

The episode runner draws the chain uniformly from the admitted chains. A chain whose stages need neither a port activity nor a completed audit meets conditions 1, 2, 3 and 5 in every episode, and only condition 4 can exclude it (§11, horizon and termination). At fork 0, a chain named on the command line takes fresh living port seeds until one admits it, and the episode record keeps the number of seeds drawn. The episode runner stops after 1,000 living port seeds that do not admit the chain. In a fork, a chain named on the command line takes the fork's own episode seed (§4.2, derivation). A fork that does not admit a named chain ends its episode before step 0 with the outcome `not admitted`. The episode record states the reason, and the chain passes to the next fork. A seed given with the chain is kept, and the episode record gives the reason when the seed does not admit the chain.

### 10.3 Onset and waits

The generator `attack_start_step` gives the onset step, drawn from a geometric distribution with a chance of 1 in 7 per step from step 6, and cut off after step 24 by drawing again. The onset therefore occurs 30 seconds to 2 minutes into the episode.

The generator `attack_waits` gives one wait for each stage, as a lower bound of 5 seconds plus a value from an exponential distribution with a mean of 5.0 seconds. A draw beyond the upper bound of 20 seconds is drawn again. A longer chain draws the same first waits as a shorter one.

### 10.4 The attack chain's session

At the onset step, the episode runner starts the chain's binary on the adversary host with the option `--paced`, through an Incus session whose standard input and standard output it holds. The binary's standard error goes to `attack-chain.log` in the episode record.

With `--paced`, the binary waits before each stage for the line `begin` on standard input, and writes a stage event to standard output as each stage begins and ends (`episode-runner-impl.md` §9, the attack chain's session).

The episode runner pauses for each wait and then writes `begin`. The first wait runs from the session's start, and each later wait from the end event of the stage before. The episode runner times each stage event on the clock of the episode's Incus host as the event arrives, and adds the stage's target to each beginning event in the episode record. Each write of `begin` releases a stage, and the releases give Track A's count `k_n`. The number of stages K in Track A's reward (§13, episode record) is the attack chain's own number of stages, `total_num_stages`.

The count `k_n` is the number of stages that the episode runner released before the Track A alarm arrived, and K when no alarm arrives. Outside a suppressed episode, the episode runner releases no stage after the alarm's arrival. The episode runner reads the step session and writes to the attack chain's session in one order of events, so each release occurs either before or after the alarm. When the alarm has arrived by the instant at which a release falls due, the episode runner reads the decision message first, and the stage is not released. A stage released before the alarm counts even when its beginning event arrives after the alarm.

### 10.5 Stopping the chain

The binary stops by itself after its last stage, at a failed stage, or before a stage whose required facts are missing. Otherwise the episode runner stops the binary at the end of the episode, after the end notice. In Track A, outside a suppressed episode, the episode runner ends the episode at the instant at which it would release the last stage (§11, horizon and termination). The binary, still waiting for `begin`, exits once the episode runner closes its standard input. During a stage, the episode runner sends the termination signal, `SIGTERM`, to the binary inside the adversary host. A stage that holds an impact, such as a denial of the directory, then restores what it changed and ends as completed. The episode runner then closes the session's standard input, so a binary still running exits before its next stage. `SIGTERM` is the only signal that the episode runner sends to the binary. `episode-runner-impl.md` §9 (the attack chain's session) gives the binary's exit statuses.

---

## 11. Horizon and termination<a id='horizon'></a>

One horizon serves both tracks. The horizon covers the onset window, an attack chain's waits and stage run times up to its last stage, and an allowance of at least the longest flow-record lag (§6.6, settle period). The allowance lets the monitored hosts' records of the last stage reach the Wazuh manager.

The horizon is 12 minutes, or 144 steps. A null route is a route that the Wazuh manager adds on a monitored host to drop that host's traffic to one address. The horizon covers the onset window and the slowest chain of the competition library with every wait at its bound, except two groups of six chains. In the six chains that end with a null route, the scan of the IT segment takes 266 to 270 seconds, and ran for 266.0 and 266.1 seconds in two measured runs. The inventory stage ran for 226.1 seconds in both runs, inside its band of 225.5 to 233.5 seconds (§10.1, the competition library). At the latest onsets with the longest waits, those chains begin their last stage up to 700.1 seconds after step 0. The allowance of 45 seconds before the 720-second horizon sets a limit of 675 seconds. Condition 4 of §10.2 (choosing the chain) therefore excludes such a chain at 1 of 10,000 seeds, 0.01%, and at 0.012% of 1,000,000 seeds. The six chains that need a completed audit run eight stages, and begin their last stage up to 796 seconds after step 0, beyond the horizon. At a fork after the living port's first audit, condition 4 excludes such a chain at 1,331 of 10,000 seeds, 13.3%. A chain that waits for the fuel supplier's poll begins its last stage later when the poll is late. The choice of §10.2 (choosing the chain) ensures that the allowance holds in every adversarial episode.

**Ending an episode.** At a terminal state or the horizon, the episode runner sends the end notice, then stops the attack chain as §10.5 (stopping the chain) states. The episode runner then records the episode's outcome (§12, terminated and discarded episodes), exports and resets.

**Terminal states.** Each terminal state below ends an episode other than a suppressed episode, which ends only at its horizon or when the simulator stops (§9, suppressed episodes for Competition Rule 5).

- A terminal state that the ground-truth evaluator identifies, such as cavitation, ends an episode in either track.
- The Track A terminal states are the Track A alarm and the start of the attack chain's last stage. A Track A episode ends at the alarm's arrival (§7, step protocol), or at the instant at which the episode runner would release the last stage. The episode runner then never releases that stage (§10.5, stopping the chain). The last stage realises the attack's consequences, so its start is the last moment at which an idealised defender could still avert those consequences. Outside a suppressed episode the last stage therefore never runs in Track A, and Track A's outcome 2, an attack detected at or after the last stage, cannot occur (§13, episode record).
- The ground-truth evaluator identifies the Track B state in which no remaining action could remedy the violated condition. The outstanding steps to T then count as violated (§7, step protocol).
- A simulator stop with a cause inside the episode is a violated operational correctness condition beyond remedy. In Track B the outstanding steps to T then count as violated. The episode record ends with the step in which the simulator stopped, and a decision message that arrives after the stop counts as dropped. At the Track A alarm, a read of the simulator's log, which waits at most 0.5 seconds, finds a stop that came before the alarm before the end notice goes (§7, step protocol).

---

## 12. Terminated and discarded episodes<a id='discarded'></a>

**Terminated.** A simulator stop with a cause inside the episode terminates it. Such a cause is a request to the digital world that gets an unexpected response, or that still fails after its retries. An audit's finding of a falsified or injected record, and a terminal state such as cavitation, are such causes too. The simulator's dock operator stops the simulator on an unexpected response, or on a request that still fails after its retries. A refused dispense sends the tug away unserved and leaves the simulator running (`sim-impl.md`, "Non-idempotent job creation (reconciliation)" and "Non-idempotent job-state transitions (reconciliation)"). The simulator's fuel supplier stops the simulator when a poll of the tank gauge fails after its retries, or returns a response that the fuel supplier cannot read. The simulator's reconciliation user stops the simulator at an audit that fails in the digital world, and at an audit with a fatal finding (`sim-impl.md`, "Reconciliation user", "Outcome"). An attack that denies a service, or a defensive action that isolates one, can therefore stop the simulator.

**Discarded.** Only a failure of the testbed, or an entry's `defender` that cannot run, discards an episode. The failures of the testbed are a failed deployment or fork (§6.6, settle period), a failed call to Incus and a failed export. A simulator stop caused solely by a defect in the simulator's own code is a failure of the testbed too. So are a fault in the start state at the fork (§6.4, how a fork is made) and a failure of the query interface to record its offsets at step 0 (§7, step protocol). An adversarial episode whose deployment holds no adversary host, or whose adversary host holds no executable binary of its chain, is a failure of the testbed too (§6.5, adversary host). So is an attack chain's session that fails to start, or that writes no process identifier (§10.4, the attack chain's session). A volume that `siem-01` cannot read and search is a fault of the entry, and so is a `defender` that is not a regular file that `siem-01` can read and execute (§7, step protocol).

**Continued.** An attack chain that stops before its last stage leaves the episode running to its horizon, because the defender's later decisions can still change the score.

**After a discard.** The episode record states the cause of the discard, and the living port continues with its next fork, so each fork runs its episode once (§6.3, the living port). After a discard at fork 0, the episode runner continues with the next living port.

**Outcomes.** The episode record gives each episode one outcome. An episode that reaches its horizon has the outcome `horizon`. A Track A episode that its alarm ends has the outcome `alarm`, and one that the start of its last stage ends has the outcome `last_stage` (§11, horizon and termination). A terminated episode has the outcome `terminated`, which only a simulator stop gives, and a discarded episode the outcome `discarded`. A fork that admits no chain gives the outcome `not admitted` (§10.2, choosing the chain). An episode that the operator of the episode runner interrupts has the outcome `interrupted`.

The competition's results report how often terminations, discards and early stops of an attack chain each happened. In Track A, the results also report how often each of the outcomes `alarm` and `last_stage` occurred. The simulator logs the message of the first of its entities to fail as the cause of its shutdown (`sim-network.md` §7, image, build and supervision). The episode runner distinguishes the causes by matching that message against the messages in the simulator's source, and discards the episode at any other message.

---

## 13. Episode record<a id='record'></a>

The episode record lies on the episode's Incus host, beyond the reach of every RavensPort node. Every node sits only on the zone networks, which are isolated from the episode's Incus host (`deployment.md` §8, deployment sequence). The episode runner moves the episode record off a fork's Incus host before the reset deletes that host. The ground truth, the stage events among it, therefore stays beyond the defender's reach during an episode. The episode record has the same layout in both modes, and keeps each source in files of its own, with timestamps from that source's own clock.

| Content | Source | Section |
|---|---|---|
| Living port seed, the living port's start-up input, and the fork's number, fork wait, instant and age | Episode runner | §4.3 (start-up input), §6.3 (the living port) |
| The start state at the fork instant, the fork's start, and the offset of the fork's clock from the clock of the machine that runs the Incus hosts | Episode runner | §6.4 (how a fork is made) |
| Episode seed, episode type, mode and track | Episode runner | §4.1 (inputs) |
| In training mode, the training volume's version at the episode's start and at its end | Episode runner | §3.2 (modes) |
| Attack chain, its binary's SHA-256 digest, the admitted chains with the reason for each exclusion, the onset step, the seed of the generator `attack_waits`, the bounds on the waits and the waits drawn, and for a chain named on the command line, the number of seeds drawn | Episode runner | §10 (adversarial episodes) |
| Scenario parameters, including the compression factor, and a hash of each node image (`deployment.md` §6, image build) | Episode runner | §5 (time and the compression factor), §14 (reproducibility) |
| Time taken by each phase, the exit status of the deployment or the fork, and at fork 0 the exit status of the reset and the instances of the deployment that remain after it, or `null` when the episode runner cannot list them | Episode runner | §3.1 (phases), §3.3 (reset), §16 (measurement episodes and episode cost) |
| Settle conditions, with the time at which each held | Episode runner | §6.6 (settle period) |
| First occurrence of each monitored host's file-integrity scan, and each scan request with its time | Episode runner | §6.2 (first occurrences) |
| For each step, the step boundaries, the time the decision request was sent, the decision and its arrival time, the numbers of dropped decision messages and of invalid lines, and the reward | Episode runner | §7 (step protocol) |
| The program that the step session ran: the entry's `defender` or the idle defender | Episode runner | §7 (step protocol) |
| In Track A, the Track A alarm's step and arrival time, or its absence; K, `k_n` and `R_n`; and Track A's outcome | Episode runner | §7 (step protocol), §10.4 (the attack chain's session), §11 (horizon and termination) |
| In Track A, the episode's end cause: the horizon, the Track A alarm, the start of the attack chain's last stage, or a simulator stop | Episode runner | §11 (horizon and termination), §12 (terminated and discarded episodes) |
| Record of reads, and each dated file of `alerts.json` and `archives.json` from the episode's days or named by a read, whole and under its own name | `siem-01`, by export | §8 (query interface) |
| Stage events with their times, each beginning event with its stage's target | Episode runner | §10.4 (the attack chain's session) |
| The episode's outcome, with the cause of a termination or a discard, and the early stops of the attack chain with their stages | Episode runner | §12 (terminated and discarded episodes) |
| Simulator log | `sim-host`, by export | §12 (terminated and discarded episodes) |
| Attack chain's process identifier, releases, standard output, exit status and stop, and its standard error in `attack-chain.log` | Adversary host, through Incus | §10.4 (the attack chain's session), §10.5 (stopping the chain), `episode-runner-impl.md` §9 (the attack chain's session) |
| Ground truth that the ground-truth evaluator reads, in its own formats | Ground-truth evaluator, by export | §11 (horizon and termination) |
| Verdict of the ground-truth evaluator for each step | Ground-truth evaluator | §7 (step protocol), §9 (suppressed episodes for Competition Rule 5) |

The export runs after the end notice and takes from the hosts exactly what scoring, the rule checks and replay need, copying each source as it stands. A source can therefore hold records written in the seconds after the end notice (`episode-runner-impl.md` §10, export).

Each defensive action names its target, as the episode record gives each stage's beginning event its target (§10.4, the attack chain's session).

**Track A's outcome.** The episode runner sorts each Track A episode into one of the outcomes below.

- **Adversarial episode.** The outcome is 1 when the alarm arrived after the first stage's release, with `k_n` from 1 to K − 1 and `R_n` = −(`k_n` − 1)/(K − 1). The outcome is 3 when the alarm arrived before the first stage's release, a false alarm with `R_n` = −1. For outcome 3, `k_n` is 0, since `k_n` counts the stages released, and the formula of outcome 1 does not apply. The outcome is 4 when no alarm arrived, with `k_n` = K and `R_n` = −1.
- **Benign episode.** The outcome is a false alarm, with `R_n` = −1, or none, with `R_n` = 0.
- **Suppressed episode.** The episode record keeps the same fields, computed from the first alarm as if it had ended the episode. A suppressed episode's last stage runs, so its outcome can also be 2, an alarm at or after the last stage's release, with `k_n` = K and `R_n` = −1.

---

## 14. Reproducibility<a id='reproducibility'></a>

Every episode of an entry shares the same scenario parameters and the same images, built once and reused through final scoring. `deployment.md` §9.1 (deterministic build) commits to byte-identical builds from the same inputs. Builds start from an unpinned Ubuntu image and install most packages at their latest versions, so a later rebuild can change the images, and the episode record's image hashes show when it does.

A living port seed reproduces the living port's draws, which are its tug streams, continued in each fork, its fork waits and the episode seed of each fork (§4.2, derivation). An episode seed reproduces the onset step and the waits, and at fork 0 the first occurrences of the file-integrity scans. The simulator's tug arrival generator draws each tug's next time away as the tug leaves the dock, so a tug arrives one drawn time away after its last departure (`sim.md` §6, tug arrival generator). A tug's arrival times therefore depend on how long the tug waited and was refuelled. Other times vary between runs, because stages run for as long as they take, and the digital world runs on its own timers. Two runs of one seed therefore agree in distribution in their event times, the times of their events from the episode's start.

**Replays.** A replay starts from the living port's latest snapshot before the fork instant (§6.3, the living port). The living port runs from the snapshot to the fork instant, and the episode runner makes the fork again with the same episode seed. The snapshot holds the position of each tug stream, so the living port reaches the fork instant through the same tugs, requested volumes, refusals and audits. The records' identifiers, the timings to the millisecond and an occasional race between two polls differ (§18, limitations). A replay of fork 0 deploys a new living port with the same living port seed.

The replays with decisions suppressed that Competition Rule 5 requires reproduce the draws. `digital.md` §10 ("Testbed orchestration") and `deployment.md` §9 (reproducibility) point here.

---

## 15. Episode mix<a id='mix'></a>

In evaluation mode the episode runner runs fixed numbers of benign and adversarial episodes, from the published ratio and the published episode count. The ratio is published before final scoring. Track A scores an entry as the mean rewards over benign and over adversarial episodes, weighted by the published proportions. Any order of episodes gives the same expected scores, because each evaluation episode starts from the submitted state with a fixed policy.

In training mode the episode runner draws each episode's type at random, benign with the published proportion of benign episodes or, until the ratio is published, with a chance of one half. A type given on the command line replaces the draw (§4.1, inputs).

Only the episode record stores each episode's type. Until the competition fixes a distribution of attack chains, the episode runner draws each adversarial episode's attack chain uniformly from the competition library's chains that the episode admits (§10.2, choosing the chain).

---

## 16. Measurement episodes and episode cost<a id='measurement'></a>

Measurement episodes run before the competition and again whenever the images or the scenario change. In training and scored episodes, the episode record is the only measurement.

A measurement episode records the time of the deployment or the fork, settle, the steps, export and reset, and the processor and memory use of each Incus host and of each node.

The measurement episodes determine the values below.

- **The compression factor.** Benign episodes run at rising values of the compression factor over the horizon. The largest value at which every episode runs cleanly within the evaluation compute budget, the computing resources that the organisers can spend on scoring, becomes the compression factor. An episode runs cleanly when it reaches its horizon, Track B scoring finds every condition held at every step, and it is kept.
- **The horizon.** The stage run times of the attack chains, measured in episodes, set the horizon of §11 (horizon and termination). The same episodes measure the ranges that §10.2 (choosing the chain) uses, for step 0, refuels, polls and stage run times. Measurement episodes measure the stage run times and the ranges again whenever the attack chains change.
- **The settle timeout.** The settle timeout is three times the longest time that measurement episodes record for the settle conditions to hold.
- **Episode cost.** The wall-clock time and the resources of an episode at the chosen compression factor and horizon are the inputs of the precision calculation that sizes the episode count. The calculation targets two quantities, false-alarm performance over benign episodes and detection performance over adversarial episodes. Each attack chain runs in the share of adversarial episodes that its admissions give it, so a result reported for one rarely admitted chain, such as a refuel chain, carries a wide interval.
- **The delay of each decision request and the end notice.** Computing a step's reward delays the step message that carries the reward. The measured delay shows whether the step messages can keep waiting for the reward.
- **The fork wait.** The bounds of the fork wait and the episodes at each fork follow from two measurements. One is the time that making a fork takes (§6.4, how a fork is made). The other is the processor, memory and storage that a living port and its forks use.

The same measurement episodes set the settle delay above the longest flow-record lag measured on a build of the competition profile, and the allowance at least as long as that lag. The measurement episodes also refine the step duration from the time that reads and decisions take.

For episode cost, the measurement episodes are equal numbers of benign and adversarial episodes across the competition library's attack chains, with the SIEM host as released, each repeated under independent seeds. Results report means with confidence intervals. Measurement episodes run on two configurations, containers on one macOS or Linux machine and the virtual machine configuration, which the project runs on Microsoft Azure.

---

## 17. Parameters<a id='parameters'></a>

The episode's own parameters belong to the competition, so this document and the episode runner's configuration hold them. The competition's additional override file defines the compression factor and the four flow timeouts of packet inspection. `world.yaml` holds only facts about the fictional asset (`deployment-config.md` §3, structure of `world.yaml`), among them the values in operation of §5.3 (timings) other than the bound for detecting and acknowledging a leak. `narrative.md` §5 (operational correctness conditions) states that bound.

| Parameter | Value | Unit | Status | Set by | Held in |
|---|---|---|---|---|---|
| Compression factor | 5 | none | Provisional | §16 | Competition's additional override file |
| Flow timeouts of packet inspection | 20 for a closed TCP connection, 30 for a new TCP connection, 20 for an established UDP flow, 20 for an ICMP exchange with traffic both ways | seconds | Set | §5.3 | Competition's additional override file |
| Step duration | 5 | seconds of simulated time | Set | §7 | Episode runner |
| Horizon T | 144 | steps, 12 minutes at the step duration of 5 seconds | Set | §11 | Episode runner |
| Onset window | 6 to 24 | steps | Provisional | §10.3 | Episode runner |
| Chance of onset per step | 1/7 | none | Provisional | §10.3 | Episode runner |
| Bounds on the waits | 5 and 20 | seconds | Provisional | §10.3 | Episode runner |
| Mean of the exponential part of a wait | 5.0 | seconds | Provisional | §10.3 | Episode runner |
| Allowance after the last stage begins | 45 | seconds | Provisional | §11 | Episode runner |
| Margin of admission of an attack chain | 15 | seconds | Set | §10.2 | Episode runner |
| Ranges of the predicted episode events | `episode-runner-impl.md` §8 (ranges of the predicted events) | seconds | Set | §16 | Episode runner |
| Time past due after which a poll or the road tanker is overdue at a fork | 12 | seconds | Set | §6.4 | Episode runner |
| Bounds on the inferences at the fork | §6.4 (how a fork is made) | seconds | Provisional; the dock operator's exchange of 15 seconds awaits the actors' time limits (§19, open items) | §6.4 | Episode runner |
| Run time of a stage that no episode measured | 15 to 110, derived from its body, for the stage that reads what a completed audit left (§10.1, the competition library), and 0 to 30 otherwise | seconds | Provisional | §10.1 | Episode runner |
| Refuel windows of the inventory stage's peer scans, after the stage begins | 1 to 74 for the scan of the FMS, and 151 to 225 for the scan of the HMI, each 1 second inside its measured scan | seconds | Set | §10.1 | Episode runner |
| Living port seeds drawn at fork 0 for a chain named on the command line | 1,000 at most | seeds | Set | §10.2 | Episode runner |
| Settle delay | 45 | seconds | Provisional | §16 | Episode runner |
| Settle timeout | 120 | seconds | Provisional | §16 | Episode runner |
| Episode seed | 32 | bytes | Set | §4.1 | Episode runner |
| Living port seed | 32 | bytes | Set | §4.1 | Episode runner |
| Fork wait | Uniform from 0 to 6 | minutes | Provisional | §16 | Episode runner |
| Episodes at each fork | 1 | episodes | Provisional | §16 | Episode runner |
| Minimum age of a fork in training | None | hours | Set | §6.3 | Episode runner |
| Times of a living port's snapshots | Every 15 minutes for the first hour, the interval doubling after every four snapshots | minutes | Set | §6.3 | Episode runner |
| Size of the snapshot cache | Set by the entrant in training | gigabytes | Set | §6.3 | Configuration of the run |
| Ratio of benign to adversarial episodes | Open | none | Open | §15 | Published with the environment |
| Episode count | Open | episodes | Open | §16 | Published with the environment |

---

## 18. Limitations<a id='limitations'></a>

- **Measured delays.** Every measured delay stays fixed, so its ratio to the scaled intervals grows with the compression factor. The longest flow-record lag measured with the competition's flow timeouts is 42.8 seconds. The lag is about 5% of the fuel supplier's 15-minute poll interval in operation, and about 24% of the 3-minute interval at a compression factor of 5.
- **Timings that depend on the machine.** The settle delay, the settle timeout and the allowance derive from delays measured on one machine. The ranges of §10.2 (choosing the chain) derive from delays measured on three kinds of machine (`episode-runner-impl.md` §8, ranges of the predicted events). A faster or slower machine changes those delays, so each machine measures them again when RavensPort is first built on it, by a method left to the competition's packaging.
- **Fault handling.** Retries and time limits stay fixed, so fault handling weakens as the compression factor rises. Above 2, the FMS's 15-second limit on a poll of the tank gauge exceeds the poll interval, so a silent gauge stalls the poller for more than one interval. From about 4.3, the dock operator's retries at 1, 3 and 7 seconds overrun the HMI's challenge timeout. From 12, the FMS's fallback to a cached reading at most two poll intervals old always fails, because two intervals are then shorter than the 5-second read timeout that precedes the fallback.
- **Events of operation per episode.** At a compression factor of 5 a 12-minute episode covers about 60 minutes of operation. With three tugs, each away for 20 hours on average, about 14% of episodes contain a tug arrival. The road tanker arrives in about 1.5% of episodes, those that start with an order placed and a remaining lead time inside the episode. The road tanker's 6-minute fill then runs at least partly inside the episode. A larger compression factor or a longer horizon raises the number of such events per episode.
- **Scan requests.** A scan request leaves records on the Wazuh manager that only a requested scan leaves (§6.2, first occurrences). The duration of a requested scan awaits a measurement episode.
- **Inventory scan and listening-port inventory.** In Wazuh 4.14.7 each Wazuh agent's start fixes the time of the first inventory scan and the first listening-port inventory (§6.2, first occurrences). At fork 0 the Wazuh agents start with the episode's hosts, and a 12-minute episode at a compression factor of 5 spans one 12-minute interval of the inventory scan. Every episode at fork 0 therefore holds one inventory scan near its end, and the listening-port inventory runs 10 times per episode at fixed steps. The requirement of §6.1 (frequency requirement) then holds in count only for the inventory scan and the listening-port inventory.
- **Fixed phases.** At fork 0 the phases of the digital world's other timers, such as the FMS's polls, stay fixed from each host's start, so those events occur at similar steps in every episode. In a fork, each timer's phase at step 0 varies with the fork instant.
- **Episodes at fork 0.** An episode at fork 0 starts in the simulator's default start state (§4.3, start-up input). The episode holds no road tanker's delivery, no routine check, no audit and nothing that a completed audit left. The tank starts at 35,000 litres and stays above the reorder threshold for an episode's length. At a compression factor of 5, the first routine check runs one check interval after the simulator's start, 17,280 seconds. The first audit begins one audit period and one audit settling delay after the episode runner writes the start-up input, 145.2 hours at the same compression factor. The fuel supplier's first poll is due one interval after the simulator's start in every such episode.
- **A living port's first hours.** At a compression factor of 5, the forks of about the first 11 hours of a living port start with no order placed, and with the tank falling from 35,000 litres towards the reorder threshold. The living port's first audit comes 145.2 hours after the episode runner writes its start-up input, at a compression factor of 5. No earlier fork holds a completed audit, so no chain with a stage that reads what a completed audit left is admitted to it (§10.2, choosing the chain). The first audit's retrieval reaches back before the living port's start, where no transaction records exist.
- **Separate training lineages.** Each living port keeps its own training lineage, so concurrent episodes, and living ports that run in parallel, learn separately. Entrants may merge lineages themselves (§19, open items).
- **Refused dispenses.** At the values in operation the FMS refuses about 2.4 dispenses in 1,000 years of operation, about one in 420 years (`fms.md` §5.1, the poller of the automatic tank gauge). One delivery cycle in about 55,000 holds a refusal. Every refusal comes with an order pending, 3.8 to 5.8 hours into the lead time, after four to eight tug arrivals where fewer than one is expected. The tank's level stays between 5,001 and 38,000 litres, with a 1st percentile of about 16,300 litres, against the overfill interlock's 47,500 litres. A compression factor of 5 leaves the rate unchanged within counting noise, so an episode holds a refused dispense with a probability of about 3 in 10 million. A test that needs a refusal induces one, as the dock operator's tests do with an FMS double that refuses (`sim-tests.md`, "Dock operator").
- **History.** Every record of a fork comes from the living port's own operation since its deployment, on every host, the outputs of the security monitoring included. At a compression factor of 5 an audit occurs inside about 0.14% of 12-minute episodes.
- **State of the management interface.** Each Wazuh agent's inventory, file-integrity baseline and connection state sit in the Wazuh manager's databases, which the management interface alone serves, on TCP port 55000. Only their changes reach `archives.json`, so the first version of the query interface gives a defender those changes and not the state (§8, query interface).
- **Identifiers of the FMS.** The FMS draws the identifiers of its jobs, transactions and sessions as version 4 universally unique identifiers (UUID), from the operating system's random source. A replay's records therefore carry identifiers that differ from those of the episode that the replay repeats (§14, reproducibility).
- **Timing of events.** The event times of two runs of one seed agree only in distribution, and two polls that are due close together can occur in either order (§14, reproducibility).
- **Finite horizon.** The score measures only the consequences that arrive within the horizon, which favours rewards that arrive early.
- **The allowance in Track B.** The allowance counts from the last stage's start, which serves Track A, whose episodes end as the last stage begins. In Track B the last stage runs, and the records of a last stage that runs for up to 30 seconds can reach the Wazuh manager up to about 28 seconds after the horizon. The 28 seconds follow from the longest flow-record lag measured, 42.8 seconds.
- **Port activities of attack chains.** A poll chain or a refuel chain runs only in episodes whose predicted events admit it (§10.2, choosing the chain). Each poll chain was admitted at all 143 forks measured. Only a poll overdue at the fork instant excludes a poll chain, and no fork measured had one. Each refuel chain was admitted at 1.0 to 2.8% of the forks measured, and at about 2% in a sample of 200,000 random fork instants. A refuel chain needs a refuel during the inventory stage's peer scan, and three tugs, each away for 20 hours on average, give 0.75 arrivals an hour at a compression factor of 5. At the forks before a living port's first audit, the uniform draw of §10.2 (choosing the chain) then runs each refuel chain in about one adversarial episode in 900.
- **Stages of an attack chain.** Track A's reward takes K as each attack chain's own number of stages (§10.4, the attack chain's session).
- **Representation of attack methods.** The competition ranks each entry by the mean episodic reward over evaluation episodes sampled such that each held-out method is equally represented. A held-out method is a stage's attack method that the competition withholds from entrants until final scoring. Under the uniform draw of §10.2 (choosing the chain), a chain that few episodes admit, such as a refuel chain, runs in fewer evaluation episodes than a chain that every episode admits.

---

## 19. Open items<a id='open-items'></a>

- **Provisional values.** Measurement episodes replace seven provisional values (§16, measurement episodes and episode cost). The first four are the compression factor, the allowance, the settle delay and the settle timeout. The other three are the run time of a stage that no episode measured, whether derived from its body or taken as 0 to 30 seconds, the fork wait and the episodes at each fork. The onset window, the bounds on the waits, the chance of onset and the mean of a wait are also provisional.
- **Forks on Microsoft Azure.** A fork copies a whole Incus host, and the snapshots of Microsoft Azure hold disks only, so a fork on Microsoft Azure needs nested virtualisation. Forks on Microsoft Azure await a test.
- **Evaluation on living ports.** The number of living ports in evaluation, the ages of their forks and the size of the snapshot cache await specification. The suppressed episodes for Competition Rule 5 on living ports also await specification.
- **Merging training volumes.** An interface through which an entry merges the training volumes of separate training lineages awaits specification.
- **Operational correctness conditions and scoring.** The ground-truth evaluator's judgement of each step against the operational correctness conditions, Track B scoring and the Track B terminal state await specification. So do the end cause and the outcome of each terminal state that the ground-truth evaluator identifies. The ground-truth evaluator implements them.
- **Distribution of attack chains.** The distribution of attack chains across adversarial episodes, beyond the uniform draw of §10.2 (choosing the chain), awaits specification, and with it the representation of the held-out methods (§18, limitations).
- **Defensive actions.** The defensive actions of Track B await specification.
- **Entry requirements.** The entry requirements, the reference models, which are the organisers' baseline detectors and defenders, and the check of Competition Rule 5 await specification. The entry requirements include the scope of added host tooling, meaning the hosts of the digital world it may run on, its software and the flows it may declare as collection paths. Until the entry requirements are specified, §3.2 (modes) limits added host tooling to a manifest for each host. The same section lets an entry give its own image of any host except the adversary host, beside its storage volume, which the entry requirements confirm or change.
- **Realism of the port's operation.** The realism of the port's operation awaits specification.
- **Ratio and episode count.** The published ratio of benign to adversarial episodes and the episode count await specification.
- **Time limits of the actors.** The dock operator's time limits on the HMI and the fuel supplier's limit on a poll attempt await specification.
- **Attack-chain documents.** The attack-chain documents that §1 (scope) cites by path are drafts awaiting merge.
- **Paced mode.** The attack chains' source that the episode runner's implementation merges builds each binary with the paced mode of §10 (adversarial episodes). The same source writes `chains.json` with `schema_version` 2, and its design (`attack-chain-design.md`) states the paced mode. The paced mode reaches the specification with the merge of the attack-chain documents.
- **Adversary host.** The competition relies on the attack-chain documents to place the adversary host in every deployment, and to install every attack chain's binary in the adversary host's own image (§6.5, adversary host). An adversarial episode that lacks either is discarded (§12, terminated and discarded episodes).
- **Query interface beyond its first version.** A long-running form of the query interface, should a read by the first version's program take longer than a step, and selections beyond output file, originating host and input await specification (§8, query interface).
- **Interface to the ground-truth evaluator.** The interface through which the episode runner gives the ground-truth evaluator the episode's origin, step duration, horizon and compression factor awaits specification.
- **Clock of the Incus host.** A speed-up of the Incus host's clock could multiply the compression factor, once a run of the full topology has tested it.
- **Loss during the scan of the IT segment.** During the stage `scan-it-segment`, the Wazuh agents of `fw-dmz-it`, `admin-01`, `recon-01` and `corp-it` drop records. Each of those agents has the default buffer of 5,000 events, sent at 500 a second. The defender's files then lack most of the scan's records and a share of the port's own records. The buffer's size in operation, and a count of the loss in the episode record, await specification.
