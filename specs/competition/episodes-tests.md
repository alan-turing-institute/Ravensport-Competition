# Running Episodes Test Plan

Test plan for `episodes.md`, the specification of the episode runner and the episodes it runs, and for its implementation detail in `episode-runner-impl.md`. One section per behavioural area. Each section names its tests and the sections that it targets.

---

## 0  Test harness

### 0.1  Locations

| Location | Needs a deployment | Covers |
|---|---|---|
| Unit tests beside the episode runner's code, in `competition/tests/` | No | Seed derivation, the fork seeds, the draws of the fork waits, the onset step and the waits, the times of the snapshots, the idle defender's decision messages, the refusal of a changed attack-chain source, of an older binary and of a mock build's catalogue, the check of the public release's binaries, the declared conditions of a chain, the exit statuses that a stand-in binary records, the use that `render.py` makes of the competition's additional override file, the layout of the episode record, the validity of decision messages, Track A's reward over every outcome, suppression by the command line's option, the query interface's program, and the export of the dated files that its reads touched (§1, §2, §3, §5, §6, §7, §8, §9, §11 and §12) |
| Unit tests of `render.py`, in `deploy/tests/unit/test_compressed_scenario.py` | No | The values that `render.py` scales by the compression factor, and the factors that it refuses (§2) |
| The deployment's unit tests of the snapshot scripts, in `deploy/tests/unit/test_snapshot_scripts.py` (`deployment.md` §10.5, tests) | No | The refusal of snapshot sets in a deployment of the competition profile, and the paths at which `sim-host` holds the start-up input and the checkpoint (§12, reset and the competition image) |
| Tests of the simulator's crate, in `infra/sim/tests/` | No | The simulator's log of each tug's scheduled arrival (§3) |
| `tests/integration/competition_image/` | The images alone | The competition images, and the builds that change them (§12) |
| `tests/integration/living/` | Yes, living ports of the full topology | Living ports created with a given living port seed and an entry's volume, and their forks: `every_entry_runs_over_the_same_living_port_seeds` (§3). The training-mode half of `a_track_a_alarm_ends_the_episode_at_its_arrival`, `evaluation_step_messages_carry_no_reward`, `a_volume_without_a_defender_runs_the_idle_defender`, `a_volumes_defender_runs_in_place_of_the_idle_defender` and `a_defender_that_cannot_run_discards_the_episode` (§5) |
| `tests/integration/episodes/` | Yes, the full topology | The start-up input, living ports and forks, the settle period, the step protocol, the query interface, suppressed episodes for Competition Rule 5, adversarial episodes, termination, export, reset, the competition image and reproducibility (§1 to §14) |

The integration suite runs against the full deployed topology, because an episode spans every host and zone.

### 0.2  A test defender

A test defender runs on `siem-01` and answers each decision request with a decision message that holds a scripted decision. The test defender's scripts cover no action, the Track A alarm and a test defensive action. The scripts also cover silence through a whole step, two decision messages during one step, and a decision message that arrives during the next step. The test defender reads through the query interface, as an entry does. The test defender belongs to the integration suite in `tests/integration/episodes/`. Each test places it as `defender` in the entry's storage volume, from which the launcher runs it (`episodes.md` §7, step protocol). That volume is the living port's submitted storage volume in evaluation mode, and the first volume of the training lineage in training mode. At fork 0, a test gives the volume to the command `run` with `--volume`.

### 0.3  Attack chains in the tests

The unit tests drive the attack chain's session of `episodes.md` §10.4 (the attack chain's session) with a stand-in process that follows the paced mode, in `competition/tests/test_attack.py`. The stand-in's stages complete, fail, hold until `SIGTERM`, or miss a required fact, as each test sets. The integration suite runs a real attack chain on the adversary host, `it-foothold__deny-directory-availability`. In a Track B or suppressed episode, the chain's last stage holds a denial of the directory until the episode runner stops it. In Track A outside a suppressed episode, the episode ends before that stage (`episodes.md` §11, horizon and termination). The attack-chain test suite, `attack-chains-tests.md`, tests each chain's effect, and §7 (suppressed episodes for Competition Rule 5) below checks the real attack chains through suppressed episodes.

### 0.4  Reading ground truth

Tests read ground truth from the episode record, and from the simulator through `incus exec sim-host`, as the other integration suites do. Tests that judge operational conditions use the ground-truth evaluator.

### 0.5  Statistical tests

Tests of a distribution use fixed sets of 10,000 seeds, so each verdict is deterministic, and decide at the 1% level, which a correct implementation fails on one fixed set in a hundred. A chi-squared goodness-of-fit test decides for counts over categories, such as the frequency of each onset step or the events that the forks contain, where every expected count is large. A one-sample Kolmogorov-Smirnov test decides for a continuous value against a fully specified distribution, such as the fork waits or the cut-off exponential waits, where every parameter is fixed in advance. A two-sample Kolmogorov-Smirnov test decides whether the event times of two runs share a distribution.

### 0.6  What waits

Some tests wait on a part that the first version of the episode runner leaves out, or on a part that awaits specification or implementation (`episodes.md` §19, open items). Each test applies once the part that it waits on exists.

| Part | Tests that wait on it |
|---|---|
| The defensive actions, which await specification (`episodes.md` §19, open items), and Track B's rewards | `a_decision_starts_when_it_arrives`, `a_decision_starts_before_an_earlier_action_ends`, `decisions_are_recorded_and_not_carried_out`, `track_b_reward_follows_the_violated_steps`, and the Track B half of `the_step_messages_carry_the_previous_reward_and_the_end` and `evaluation_step_messages_carry_no_reward` |
| The ground-truth evaluator | `track_b_reward_follows_the_violated_steps`, `a_terminal_state_ends_the_episode`, `a_suppressed_episode_ends_only_at_its_horizon_or_a_simulator_stop`, `the_benign_check_detects_disturbing_tooling`, and the check of Track B in `a_simulator_stop_ends_the_record_at_its_step` |
| A living port and its forks in the integration suite, which await implementation. The first version of the episode runner makes forks at a living port's drawn fork instants, and keeps and replays snapshots (`episodes.md` §6.3, the living port) | `each_fork_follows_its_fork_wait`, `forks_contain_each_event_in_proportion`, `a_living_port_runs_no_attack_chain_and_no_step_session`, `a_fork_leaves_the_living_port_undisturbed`, `a_copy_continues_the_living_ports_state`, `a_copys_audit_covers_the_living_ports_open_window`, `a_copys_road_tanker_arrives_when_the_living_ports_would`, `a_copy_holds_its_clock_from_the_fork_instant`, `a_copy_continues_the_living_ports_tug_stream`, `a_fork_during_a_delivery_notes_link_is_not_uncertain`, `a_fork_during_the_next_jobs_creation_is_not_uncertain`, `a_copy_keeps_the_living_ports_addresses_on_its_own_incus_host`, `a_copy_continues_work_in_progress`, `no_directory_of_the_machine_is_mounted_at_a_save`, `making_a_copy_leaves_no_record_after_step_0`, `a_copy_has_no_settle_delay`, `a_copy_that_never_settles_is_discarded`, `an_overdue_road_tanker_excludes_the_refuel_chains`, `a_fork_that_excludes_a_named_chain_passes_it_to_the_next_fork`, `a_failed_copy_discards_the_episode`, `the_episode_record_leaves_the_copy_before_the_reset`, `the_reset_deletes_the_copy_and_leaves_the_living_port_running`, `the_training_volume_is_attached_only_for_an_episode`, `the_episode_record_states_the_training_volumes_versions`, `a_living_port_runs_its_training_episodes_one_at_a_time`, `every_evaluation_copy_keeps_the_submitted_volume_read_only`, `a_program_replaced_in_training_runs_from_the_next_episode` and the faults made in a fork in `a_fault_in_the_start_state_discards_the_episode` |
| A replay that runs a snapshot on to a fork instant, which is built after the first version (`episodes.md` §14, reproducibility) | `a_replay_reaches_the_fork_instant_with_the_same_tugs` |
| A replay that takes its inputs from the episode record of the episode it repeats, which awaits a choice between a search of the episode records by seed and an option that names the record (`episodes.md` §4.1, inputs) | `a_replay_takes_its_inputs_from_the_episode_record` |
| Test tooling on a monitored host, or tooling that blocks a stage of the attack chain | `added_tooling_data_reaches_the_defender_through_the_files`, `the_adversarial_check_detects_a_blocked_stage` and `the_benign_check_detects_disturbing_tooling` |
| Measurement episodes (`episodes.md` §16, measurement episodes and episode cost) | `a_read_takes_less_than_one_step`, `admitted_chains_complete_on_fresh_seeds`, `measurement_episodes_record_phases_and_resources`, `the_settle_delay_exceeds_the_longest_flow_record_lag`, `the_settle_timeout_is_three_times_the_longest_settle` and `event_times_of_one_seed_agree_in_distribution` |
| The entry requirements, which await specification (`episodes.md` §19, open items) | `an_entrys_tooling_is_built_into_its_hosts_images` |
| The published ratio of benign to adversarial episodes and the episode count (`episodes.md` §19, open items) | `evaluation_runs_the_fixed_numbers_of_each_type` |
| A run of the integration suite whose adversary host holds the chain's binaries in its own image (`episodes.md` §6.5, adversary host; §19, open items) | `a_fork_0_episode_holds_the_volume_read_only`, `a_track_a_episode_ends_at_the_start_of_the_last_stage`, `each_wait_starts_after_the_previous_stage_ends`, `no_stage_is_released_after_the_alarm`, `stage_events_carry_the_episode_runner_clock`, `stage_events_name_the_target_host`, `the_adversary_host_holds_every_binary_as_built`, `the_adversary_host_is_down_and_unaddressed_before_and_after`, `the_attack_chain_starts_at_the_onset_step`, `the_attack_chain_stops_gracefully_at_the_end`, `the_end_notice_carries_r_n`, `the_episode_record_holds_track_a_fields`, `the_history_before_step_0_holds_no_trace_of_the_episode_runner`, `the_reset_leaves_no_node`, `the_step_messages_carry_the_previous_reward_and_the_end` and `the_stop_releases_the_directory`; the evaluation-mode half of `a_track_a_alarm_ends_the_episode_at_its_arrival`, `a_volume_without_a_defender_runs_the_idle_defender` and `evaluation_step_messages_carry_no_reward`; the checks through the launcher alone in `a_defender_that_cannot_run_discards_the_episode`; and the comparison of the adversary host's binaries with `attack-chains/dist` in `the_episode_type_appears_only_in_the_episode_record` |
| A test that the implementation has not written | `a_failed_incus_call_discards_the_episode`, `a_living_port_starts_from_the_simulators_default_start_state`, `a_refuel_overlaps_the_peer_scan_by_15_seconds`, `a_seed_given_with_its_chain_is_kept`, `an_early_stop_of_the_attack_chain_leaves_the_episode_running`, `each_generator_has_its_own_stream`, `every_stop_records_its_cause`, `first_file_integrity_scans_are_uniform_over_the_interval`, `fork_waits_are_uniform_from_0_to_6_minutes`, `one_seed_of_sim_tug_arrivals_reproduces_the_tug_draws`, `render_keeps_fixed_timings_unchanged`, `start_up_input_is_read_once_and_validated`, `the_counted_poll_starts_2_seconds_after_the_stage` and `two_runs_of_one_seed_share_their_draws` |

---

## 1  Episode inputs and seeds

Targets `episodes.md` §4 (episode inputs and seeds) and `episode-runner-impl.md` §2 (start-up input).

### `seed_derivation_matches_hmac_sha256`

For fixed episode seeds, the seed of each generator of an episode equals the first eight bytes of a hash-based message authentication code (HMAC) built on Secure Hash Algorithm 256 (SHA-256). The generators of an episode are `attack_start_step`, `attack_waits` and each `wazuh_first_file_integrity_scan/`. Each generator's seed reads its eight bytes as an unsigned integer with the most significant byte first. Each HMAC is keyed with the episode seed and computed over the generator's name in the American Standard Code for Information Interchange (ASCII). The expected values come from an independent implementation.

### `an_added_generator_leaves_existing_seeds_unchanged`

Deriving the seed of a new generator leaves the seed of every existing generator unchanged, for every test episode seed.

### `evaluation_living_port_seeds_are_32_random_bytes`

In evaluation mode every living port seed has 32 bytes, comes from the operating system's secure random source, and differs from every other living port seed of the same run. At fork 0 the episode seed is the living port seed.

### `a_replay_takes_its_inputs_from_the_episode_record`

Given the seed of an earlier episode on its command line, the episode runner takes the living port seed and the fork's number from the episode record of that episode. The episode runner also takes the episode type, the attack chain, the mode and the track from that record. The replay's episode record holds the same living port seed, fork and five inputs.

### `a_replay_takes_its_seed`

Given the seed of an earlier episode on its command line, the episode runner runs one episode with that seed.

### `start_up_input_is_attached_before_sim_host_starts`

In the deployment of a living port, the start-up input is attached to `sim-host` as a read-only disk device before the deployment sequence starts `sim-host`, and the simulator's service user can read the start-up input. The start-up input and the storage volume on `siem-01`, the two attachments that `general-deployment.md` §1.3 (competition profile) admits, are the only data that reach a RavensPort node beyond its image. A deployment without the competition's additional override file attaches neither, and its simulator starts from its defaults (`sim.md` §2.3, start state).

### `start_up_input_is_read_once_and_validated`

With a valid start-up input, the simulator starts and records the seed and its start state. The simulator stops at start-up and records the reason when the file is missing, a field is missing or unknown, or a value is out of range. A `next_audit_at` that has passed when the entry point reads the file is out of range.

### `one_seed_of_sim_tug_arrivals_reproduces_the_tug_draws`

Two simulator runs with one seed of the generator `sim_tug_arrivals` draw, for each tug, the same sequence of times away and requested volumes.

### `each_generator_has_its_own_stream`

The simulator's tug streams depend only on the seed of the generator `sim_tug_arrivals`, and the fork waits only on the seed of `fork_wait`. Each host's scan time depends only on the seed of that host's generator `wazuh_first_file_integrity_scan/`. The onset step depends only on the seed of `attack_start_step`, and the attack chain's waits only on the seed of `attack_waits`. With the seed of one generator changed, the draws of every other generator stay the same.

---

## 2  Time and the compression factor

Targets `episodes.md` §5 (time and the compression factor), and `episode-runner-impl.md` §1 (competition images).

### `render_scales_each_value_in_operation`

For a compression factor of 5, `render.py` projects every timing of `episodes.md` §5.3 (timings) divided by 5, and every rate multiplied by 5. Each scaled value reaches the configuration of the Fuel Management System (FMS), the Human-Machine Interface (HMI), the simulator or the Wazuh agents.

### `render_keeps_fixed_timings_unchanged`

The retries, back-offs, timeouts and limits that `episodes.md` §5.3 (timings) lists as fixed have the same values for a compression factor of 1 and of 5.

### `a_compression_factor_of_one_reproduces_the_values_in_operation`

With a compression factor of 1, every projected value equals its value in operation.

### `render_rejects_a_factor_that_breaks_whole_seconds`

`render.py` rejects a compression factor whose scaled intervals for the FMS or the HMI include a fraction of a second. The rejection holds for as long as those components read whole seconds (`episodes.md` §5.2, the compression factor).

### `the_competition_override_file_sets_the_compression_factor`

A render with the competition's additional override file, `competition/deploy/override.yaml`, uses the compression factor that the file sets, and a render with a deployment profile alone uses a compression factor of 1. After either render, the committed `infra/sim/sim-constants.toml` holds the values in operation, and a render with the competition's additional override file also writes the scaled constants to `build/sim/sim-constants.toml`. A render refuses the override file of a deployment profile, `full.yaml` or `ci-minimal.yaml` in `deploy/overrides/`, when that file sets the compression factor (`deployment.md` §7, deployment profiles). A render also refuses a `world.yaml` that sets the compression factor (`deployment-config.md` §3, structure of `world.yaml`).

### `the_competition_override_file_sets_the_flow_timeouts`

A render with the competition's additional override file gives the packet inspection on the three firewall nodes the four flow timeouts of `episodes.md` §5.3 (timings). A render with a deployment profile alone keeps Suricata's packaged flow timeouts. A render refuses the override file of a deployment profile when that file sets a flow timeout (`deployment.md` §7.3, additional override file). A render also refuses an additional override file that sets a flow timeout and leaves a firewall node with its operational image name (`episode-runner-impl.md` §1, competition images).

---

## 3  Start state, living ports and forks

Targets `episodes.md` §6.1 (frequency requirement) to §6.5 (adversary host), and `episode-runner-impl.md` §3 (scan requests), §4 (living ports) and §5 (forks).

### `a_living_port_starts_from_the_simulators_default_start_state`

At a living port's start, the start-up input holds the living port's tug seed and `next_audit_at`, with the values of `episode-runner-impl.md` §2 (start-up input). The simulator's log records 35,000 litres in the tank, no order placed, both totalisers at zero, the first bunker delivery note and delivery ticket numbered 1, and each tug's first scheduled arrival and requested volume. From the same tug seed, the helper `competition/predicted_events/` draws each tug's first time away and requested volume, on that tug's stream. The simulator's start plus each time away is within 1 second of the tug's logged scheduled arrival, and each volume equals the logged one. The fuel supplier's first poll and the systems administrator's first routine check come one compressed interval after the simulator's start. The first audit's window runs from the simulator's start to one audit period after the time of writing.

### `a_living_port_runs_no_attack_chain_and_no_step_session`

Between two forks, the living port's adversary host runs no process of an attack chain, and the living port's `siem-01` holds no step session.

### `each_fork_follows_its_fork_wait`

Fork 1 of a living port occurs its fork wait after the end of the living port's settle period, within 1 second on the living port's clock. Each later fork occurs 14 minutes and its fork wait after the fork before it, within 1 second on the same clock. The episode record states each fork's number, fork wait, instant and age.

### `fork_waits_are_uniform_from_0_to_6_minutes`

Over 10,000 seeds of the generator `fork_wait`, the first fork wait spreads uniformly from 0 to 6 minutes. A Kolmogorov-Smirnov test at the 1% level decides (§0.5, statistical tests).

### `fork_seeds_derive_from_the_living_port_seed`

For fixed living port seeds, the episode seed of fork k, for k from 1, equals the HMAC built on SHA-256, keyed with the living port seed, over `fork/` and k in decimal digits. The episode seed of fork 0 equals the living port seed. The seeds of the living port's generators `sim_tug_arrivals` and `fork_wait` follow `episodes.md` §4.2 (derivation), keyed with the living port seed. The expected values come from an independent implementation.

### `every_entry_runs_over_the_same_living_port_seeds`

Two living ports created with the same living port seed and the volumes of two different entries make the same draws. The draws are the same fork waits and episode seeds, and each tug's times away and requested volumes (`episodes.md` §6.3, the living port; §14, reproducibility).

### `forks_contain_each_event_in_proportion`

A living port runs for 10 days. Over its forks, the episodes contain tug arrivals, the road tanker's deliveries, the fuel supplier's polls, the systems administrator's routine checks and the scheduled file-integrity scans. Each event occurs at its rate in operation multiplied by the compression factor. At `world.yaml`'s values and a compression factor of 5, the episodes hold about 12 of the road tanker's deliveries, the fewest of these events. A chi-squared test at the 1% level decides for each event (§0.5, statistical tests).

### `a_fork_leaves_the_living_port_undisturbed`

Through a fork, the living port's readings of the tank gauge keep their spacing on the living port's clock, with no gap at the pause. Across the fork, the offset of the living port's clock from the clock of the machine that runs the Incus hosts grows by the pause, within 0.5 seconds. The living port's simulator keeps running, and its tug arrivals follow the living port's own seed.

### `a_stopped_simulator_ends_the_living_ports_forks`

After the living port's simulator stops, the episode runner makes no further fork of that living port.

### `a_copy_continues_the_living_ports_state`

At the fork's start, the fork's simulator holds the living port's tank volume, delivery state, totalisers, next document numbers, tugs waiting and schedules at the fork instant. The dispenser's cumulative totaliser equals the end reading of the last transaction that the fork's FMS holds. The fork's next bunker delivery note and delivery ticket take the numbers after the living port's last ones. Every record that the fork's hosts hold from before the fork instant is the living port's.

### `a_copys_audit_covers_the_living_ports_open_window`

With a fork whose living port's next audit is due inside the episode, the fork's audit runs at the living port's scheduled time. The audit covers the window from its start in the living port, with the paper that the living port held. The reconciliation workstation then holds an `agreed` summary entry for that window.

### `a_copys_road_tanker_arrives_when_the_living_ports_would`

With an order pending at the fork instant, the fork's road tanker arrives at the living port's due time, within 1 second of simulated time. With no order placed and a volume above the reorder threshold, a road tanker arrives only after a tug has taken the volume to the threshold.

### `a_copy_holds_its_clock_from_the_fork_instant`

At the fork's start, the fork's clock reads the fork instant within 1 second. To the reset, the fork's clock then keeps within 0.5 seconds of that offset from the clock of the machine that runs the Incus hosts. No time source sets the clock of the fork's Incus host or of the living port's.

### `a_copy_continues_the_living_ports_tug_stream`

In a benign episode with the idle defender, each tug's times away and requested volumes after the fork instant are those of the living port over the same span. Each arrival that the living port had scheduled at the fork instant occurs within 1 second of the living port's, on the two clocks. The fork's simulator logs no `start_state` event after the fork instant.

### `each_tug_arrives_at_its_logged_scheduled_arrival`

Over a living port's first 12 hours, the simulator logs each tug's scheduled arrival and requested volume as it draws them. Each tug arrives within 1 second of the scheduled arrival logged last before it, and its job carries the logged volume.

### `a_fork_during_a_delivery_notes_link_is_not_uncertain`

A fork occurs after a tug has departed with its co-signed bunker delivery note and before the dock operator links the note to the FMS's job. The start state is not uncertain and the episode is not discarded. The dock is busy for the link alone, and the tug's next arrival is the one that the simulator logged at its departure.

### `a_fork_during_the_next_jobs_creation_is_not_uncertain`

A fork occurs after the dock operator creates the next job and before the departure of the tug served before it is logged. The start state is not uncertain and the episode is not discarded, and the leaving tug does not hold the dock. The same holds for a refused tug whose job is cancelled, and between a tug and the road tanker.

### `a_copy_keeps_the_living_ports_addresses_on_its_own_incus_host`

Each host of a fork has the living port's address and hardware address, and no packet of a fork reaches the living port's networks or another fork's.

### `a_copy_continues_work_in_progress`

A fork made during a dispense completes the dispense, with its transaction and its bunker delivery note. The fork's hosts keep the living port's boot identifiers and processes. A fork made during the road tanker's fill completes the fill, with its delivery ticket.

### `no_directory_of_the_machine_is_mounted_at_a_save`

At each save of a living port, the living port's Incus host mounts no directory of the machine that runs the Incus hosts, and no fork's Incus host mounts one.

### `making_a_copy_leaves_no_record_after_step_0`

Every record that making the fork leaves on the fork's hosts carries a time before step 0.

### `snapshots_follow_their_schedule`

A living port's snapshots are taken at 15, 30, 45 and 60 minutes after its start, then every 30 minutes to 3 hours, every hour to 7 hours and every 2 hours to 15 hours. The snapshot cache keeps the latest snapshots that fit its size, and deletes the oldest first.

### `first_file_integrity_scans_are_uniform_over_the_interval`

Over 10,000 episode seeds, the first occurrence of each monitored host's file-integrity scan spreads uniformly over its compressed interval, and the draws of different hosts are independent. A Kolmogorov-Smirnov test at the 1% level decides each spread (§0.5, statistical tests).

### `scan_draws_match_hmac_sha256`

For fixed episode seeds, the time that the episode runner draws for each monitored host's first file-integrity scan equals the time that `episode-runner-impl.md` §3 (scan requests) derives from the seed of the host's generator `wazuh_first_file_integrity_scan/`. The expected times come from an independent implementation. With one host left out of the monitored set, the times of the other hosts' first file-integrity scans stay the same.

### `a_file_integrity_scan_is_requested_at_the_drawn_time`

At fork 0, with an episode seed whose draw for one host is inside the episode, the episode runner requests that host's scan at the drawn time. The Wazuh agent's log then records a scan that starts within the Wazuh agent's check-in interval, and the episode record holds the request with its time. With a draw outside the episode, the episode runner leaves the scan to its schedule.

### `adversary_host_stays_silent_in_a_benign_episode`

In a benign episode, the adversary host runs with its interfaces down and unaddressed, and every zone network stays free of its packets.

---

## 4  Settle period

Targets `episodes.md` §6.6 (settle period).

### `step_0_waits_for_each_settle_condition`

Each settle condition is withheld in turn. The test holds one Wazuh agent before its restart, delays the file-integrity scan at one Wazuh agent's start, stops packet inspection on one firewall node, and holds the simulator. The test also stops, in turn, each service that the settle period waits for. Step 0 waits until the withheld condition holds.

### `step_0_waits_the_settle_delay`

At fork 0, step 0 begins at least the settle delay after the last settle condition holds.

### `a_copy_has_no_settle_delay`

In a fork, step 0 begins within 1 second of the episode runner's confirmation of the four settle conditions, and within the settle timeout of the fork's start. In training mode, the episode runner attaches the training volume before it confirms the settle conditions.

### `settle_checks_send_no_network_traffic`

The episode runner's checks stay out of every connection event and flow record in the episode.

### `a_deployment_that_never_settles_is_discarded`

With one Wazuh agent kept down, the settle timeout expires. The episode is discarded as a failed deployment, and the episode record states the condition that still failed at the settle timeout. A run with the tank-gauge relay on `atg-relay-01` kept down ends in the same way.

### `a_copy_that_never_settles_is_discarded`

With one Wazuh agent of a fork kept down, the settle timeout expires from the fork's start. The episode is discarded as a failed fork, and the episode record states the condition that still failed. The living port continues to run.

### `a_simulator_stop_before_step_0_discards_the_episode`

The simulator is stopped once before the settle conditions hold, and once during the settle delay. Each time the deployment fails, the episode is discarded, and the episode record states the stop.

---

## 5  Step protocol

Targets `episodes.md` §7 (step protocol), and `episode-runner-impl.md` §1 (competition images), §6 (step messages) and §11 (Track A's fields in the record).

### `step_boundaries_follow_the_step_duration`

Successive step boundaries lie one step duration apart on the simulator's clock, within the clock bound of 1 second.

### `the_step_messages_carry_the_previous_reward_and_the_end`

In training mode, each decision request carries the reward of the step before it, and the end notice carries the reward of the last step. In both modes, the end notice states that the episode has ended.

### `a_decision_starts_when_it_arrives`

A test defensive action starts within 1 second of its arrival, before the step ends.

### `a_decision_starts_before_an_earlier_action_ends`

A test defensive action that arrives before an earlier test defensive action has completed starts within 1 second of its arrival.

### `a_missing_decision_means_no_action`

A step that ends before any decision message arrives records no action in both tracks.

### `a_slow_decision_message_counts_for_the_step_during_which_it_arrives`

A decision message that answers a decision request and arrives during the next step appears in the episode record as the next step's decision.

### `a_second_decision_message_in_a_step_is_dropped`

When the test defender sends two decision messages during one step, the episode record holds only the first decision message's decision for that step. The episode record counts one dropped decision message, and the episode runner logs a warning.

### `track_a_reward_is_zero_until_the_end`

In Track A every step's reward is 0, and the final step carries the episodic reward of Track A (`episodes.md` §13, episode record), with K the attack chain's own number of stages. The episode runner computes it from ground truth that it records itself, the stages of the attack chain that it released before the alarm, with no judgement of the operational correctness conditions.

### `track_a_reward_follows_the_outcomes`

A table of episodes, with K of 2, 5 and 7, gives `k_n`, `R_n` and the outcome of each, as `episodes.md` §13 (episode record) states:

- an alarm before the first stage's release: `k_n` = 0, outcome 3, `R_n` = −1;
- an alarm after the first release: `k_n` = 1, outcome 1, `R_n` = 0;
- an alarm after the k-th release: `k_n` = k, outcome 1, `R_n` = −(k − 1)/(K − 1);
- no alarm, with the episode ending at the last stage's start: `k_n` = K, outcome 4, `R_n` = −1;
- a chain that stops after its j-th stage, with no alarm: outcome 4, `R_n` = −1; and with a later alarm: `k_n` = j;
- a benign episode with an alarm: a false alarm, `R_n` = −1; and with none: `R_n` = 0;
- a suppressed episode with an alarm after the last stage's release: `k_n` = K, outcome 2, `R_n` = −1.

### `the_end_notice_carries_r_n`

In Track A in training mode, every decision request after step 0 carries a reward of 0, and the end notice carries `R_n`, equal to the episode record's. In evaluation mode, the end notice's `previous_reward` holds `null`, and the episode record holds `R_n`.

### `a_decision_message_is_valid_only_for_its_track`

In Track A, `{"decision": "no action"}` gives no action and `{"decision": "alarm"}` the Track A alarm. In Track A, `{"decision": "continue"}` and a test defensive action are invalid. A decision message with a second key, a decision that is not a string, a line that is not JavaScript Object Notation (JSON) and an empty line are invalid too. In Track B, `{"decision": "alarm"}` is invalid. The episode runner logs a warning for each invalid line and counts it.

### `an_invalid_line_takes_no_steps_place`

When the test defender writes an invalid line and then `{"decision": "alarm"}` during one step, the step's decision is the Track A alarm, and the episode record counts one invalid line and no dropped decision message.

### `a_track_a_alarm_ends_the_episode_at_its_arrival`

In training mode and in evaluation mode, the end notice follows the test defender's Track A alarm within 1 second, with a `step` of the alarm's step plus 1. The episode record ends with the alarm's step, its end cause is `alarm`, and its outcome is `alarm`.

### `no_stage_is_released_after_the_alarm`

With a real attack chain, an alarm that arrives during a stage stops the binary with the termination signal, and no later stage begins. The episode runner writes no further `begin`, and `k_n` counts the stages released before the alarm. Unit tests cover the two other cases. An alarm that arrives during a wait leaves the next stage unreleased, and the binary exits with status 3 once the episode runner closes its standard input (`competition/tests/test_attack.py::test_a_stage_left_unreleased_never_begins`). An alarm that has arrived by the instant at which a release falls due leaves that stage unreleased and out of `k_n` (`competition/tests/test_steps.py::test_an_alarm_arriving_with_a_release_leaves_the_stage_unreleased`).

### `a_benign_false_alarm_ends_the_episode`

In a benign Track A episode, the test defender's alarm at step 20 ends the episode at step 20, with the outcome `alarm`, Track A's outcome a false alarm and `R_n` = −1.

### `an_alarm_after_an_unnoticed_simulator_stop_is_dropped`

The simulator stops before the alarm, and the read of the simulator's log at the alarm misses the stop. In training mode, the test defender's alarm then makes the episode runner send the end notice with the alarm's `R_n`. In both modes, the episode record then holds no alarm, the end cause `simulator_stop`, and the `R_n` of an episode with no alarm.

### `a_stop_found_at_the_alarm_ends_the_episode_as_a_simulator_stop`

The simulator stops before the test defender's alarm, unseen by the step schedule. The read of the simulator's log at the alarm finds the stop within 0.5 seconds, and the episode ends at the stop as a simulator stop. The end notice follows the alarm within 1 second, and in training mode carries the reward of an episode with no alarm. The episode record holds no alarm and the end cause `simulator_stop`.

### `only_a_run_marked_as_a_replay_is_suppressed`

Without the option `--suppressed`, the episode runner carries out each decision in either mode. With it, the episode runner records each decision and carries out none.

### `track_b_reward_follows_the_violated_steps`

In Track B a step's reward is −1/T when the ground-truth evaluator finds a violated condition at the step's end, and 0 when every condition holds.

### `evaluation_step_messages_carry_no_reward`

In evaluation mode, the `previous_reward` of every decision request and of the end notice holds `null`, and the episode record holds the reward of every step. In training mode, each decision request after step 0 holds the reward of the step before it, and the end notice holds the reward of the last step.

### `step_messages_send_no_network_traffic`

The step messages stay out of every connection event and flow record in the episode.

### `the_idle_defender_answers_each_decision_request`

Fed decision requests one at a time, the idle defender answers each decision request within 1 second with a decision message that holds `no action` in both tracks. The idle defender's last message answers the last decision request, and the idle defender exits after the end notice.

### `the_idle_defender_gives_a_decision_for_every_step`

In a benign episode with the idle defender as the defender, the episode record holds one decision of the idle defender for every step, and its count of dropped decision messages is 0.

### `a_volume_without_a_defender_runs_the_idle_defender`

In each mode, the launcher runs `/usr/local/bin/idle-defender` when the entry's storage volume holds no `defender`. The episode record holds the idle defender's decision for every step, and names `/usr/local/bin/idle-defender` as the program that ran.

### `a_defender_that_cannot_run_discards_the_episode`

In each mode, a `defender` in the entry's storage volume that is not an executable regular file discards the episode before step 0, with that cause in the episode record. The test makes such a `defender` as a file without execute permission and as a directory. Two other variants cannot occur in an episode: a `defender` without read permission, and a volume that `siem-01` cannot read and search. The episode runner opens the volume's files to the container's users before it attaches the volume, in a fork and at fork 0 alike (`episodes.md` §7, step protocol). The test checks those two variants through the launcher alone, on a volume attached to `siem-01` directly. For all four variants, the launcher exits with status 126 and runs no program.

### `a_volumes_defender_runs_in_place_of_the_idle_defender`

In each mode, with the test defender as `defender` in the entry's storage volume, the episode record holds the test defender's scripted decisions. During the episode the step session's process in `siem-01` runs the test defender as root, in the working directory `/root`, and no process runs the launcher. The episode record names `/var/lib/entry/defender` as the program that ran.

### `the_launcher_runs_only_the_tested_program`

Given as its argument a program that its own test of `/var/lib/entry/defender` does not give, the launcher writes the cause to its standard error, exits with status 126 and runs no program. Given the program that its test gives, the launcher runs that program. Called without an argument, the launcher exits with status 2.

### `the_entrys_volume_opens_to_the_containers_users`

Before either volume is attached, each of its files is readable by every user, and executable by every user where its owner may execute it. A `defender` of mode 0700 runs in `siem-01`, and a file of mode 0600 can be read there. The command `run` opens the directory of `--volume` before any episode, and fails when it cannot.

### `a_program_replaced_in_training_runs_from_the_next_episode`

In training mode, the test defender replaces `/var/lib/entry/defender` during an episode with a program that follows another script. The rest of that episode follows the first script, and the next episode of the training lineage follows the second.

---

## 6  Query interface

Targets `episodes.md` §8 (query interface) and `episode-runner-impl.md` §12 (query interface).

### `a_read_returns_the_records_since_the_previous_read`

Two successive reads with one request return disjoint sets of records, and together they hold every matching record written in that interval.

### `the_first_read_starts_at_step_0`

Every record that the first read of a request that names no start returns was written at or after step 0.

### `each_request_keeps_its_own_read_position`

A read with one request leaves unchanged what a read with another request returns. After a read of the records of `fms-01`, a request for the records of `hmi-01` that names no start makes its first read. That read returns every matching record written since step 0, those written before the read of `fms-01` among them.

### `a_request_selects_by_file_host_and_input`

A request for one output file, one originating host and one input returns only records that match all three.

### `every_read_enters_the_record_of_reads`

For each read, the record of reads holds the step, the time on the clock of `siem-01`, the request, and each dated file with the byte range read.

### `reads_replay_from_the_episode_record`

Replaying each read in the record of reads against the exported dated files returns, byte for byte, the records that the defender received. The same holds in an episode that spans midnight on the clock of `siem-01`.

### `added_tooling_data_reaches_the_defender_through_the_files`

Test tooling on a monitored host writes a record that the host's Wazuh agent forwards to `siem-01` along its existing flow. The record lands in `archives.json` and appears in a later read.

### `siem_read_writes_the_matching_records_as_the_file_holds_them`

Against output files of test records, `siem-read` writes the records that match its file, host and input, in the file's order, and exits with status 0. Each line is byte for byte as the file holds it. An unknown file or option exits with status 2.

### `siem_read_starts_where_step_0_found_each_file`

After `siem-read --start`, the first read of a request without `--since` returns only records written after the offsets that the option recorded. A read before the option has run exits with status 2. With `--step-0`, the record of reads numbers each read's step from the given instant. When the option runs 0.35 seconds into step 0, a read made 0.1 seconds into a later step takes that step's number.

### `step_0_waits_for_siem_read_start`

The episode runner opens the step session before it runs `siem-read --start`, and sends step 0's decision request only after the option has exited with status 0. When the option exits with another status, the episode is discarded with that cause, and the program's standard input closes with no step message.

### `siem_read_follows_the_day_change`

The second name moves to the next day's dated file between two reads. The second read then returns the rest of the previous day's file and then the new file's records, with none missed and none repeated. The same holds when the Wazuh manager has compressed the previous day's file.

### `siem_read_starts_at_the_first_record_at_or_after_its_start`

In a unit test against dated files of test records over three days, the two earlier ones compressed, `siem-read --since TIME` starts at the first record whose `timestamp` is at or after `TIME`. The search runs in file order from the dated file of the day of `TIME`. A `TIME` before the first record starts at the first record, and a `TIME` on a day without a dated file starts at the next dated file. A `TIME` after the read exits with status 2. The records of compressed files are written as their decompressed lines. The record of reads counts their byte ranges in the decompressed content, and holds the request's start under `since`.

### `each_start_keeps_its_own_position`

In a unit test, two requests that differ only in their start keep separate positions. A read with one start leaves unchanged what the next read with the other returns, and a second read with one start continues where the first stopped. Two spellings of one instant name one request.

### `a_read_with_a_start_before_step_0_returns_the_history`

In a unit test, and in the integration suite's benign episode that ends at a false alarm, a read with `--since` an instant before step 0 returns records from before step 0. The read returns the matching records whose timestamps are at or after that instant. A read with `--since` an instant before the security monitoring's first record returns every matching record from the first on.

### `the_export_holds_every_dated_file_that_a_read_touched`

A unit test makes a record of reads that names dated files from days before step 0's day. The export holds each such file whole and under its own name, beside the files of the episode's days. Every read then replays byte for byte from the episode record.

### `the_history_before_step_0_holds_no_trace_of_the_episode_runner`

An integration test runs over two episodes, a benign episode that ends at a false alarm and an adversarial episode that ends at an alarm. In each, no record of `alerts.json` or `archives.json` written before step 0 holds a trace of the check of the chain's binary, of the step session or of `siem-read --start`. No such record names `siem-read`, the launcher `/usr/local/bin/defender` or the chain's binary, and none comes from the adversary host. The test fails when an episode's export holds no record from before step 0.

### `a_read_takes_less_than_one_step`

The time to read one step's records during the port's operation in a benign episode is measured and reported, and the test fails if a read takes longer than one step.

---

## 7  Suppressed episodes for Competition Rule 5

Targets `episodes.md` §9 (suppressed episodes for Competition Rule 5).

### `decisions_are_recorded_and_not_carried_out`

Every decision of the test defender appears in the episode record, and every defensive action stays suppressed.

### `a_track_a_alarm_leaves_the_episode_running`

The episode continues to its horizon after the test defender raises the Track A alarm. A suppressed adversarial episode in Track A also continues to its horizon once its attack chain's last stage has begun. The episode record holds the first alarm's step and the `R_n` computed from it.

### `a_suppressed_episode_ends_only_at_its_horizon_or_a_simulator_stop`

A terminal state that the ground-truth evaluator identifies, injected in a test, leaves a suppressed episode running to its horizon. A simulator stop ends a suppressed episode at the step in which the simulator stops.

### `the_benign_check_detects_disturbing_tooling`

Test tooling that blocks the FMS's poll of the tank gauge fails the benign check, because Track B scoring finds a violated condition. The Security Information and Event Management (SIEM) host as released passes.

### `the_adversarial_check_detects_a_blocked_stage`

Test tooling that blocks a stage of the attack chain fails the adversarial check, because the stage events end before the beginning of the blocked stage. The SIEM host as released passes.

---

## 8  Adversarial episodes

Targets `episodes.md` §10 (adversarial episodes) and `episode-runner-impl.md` §7 (targets of stages), §8 (ranges of the predicted events) and §9 (the attack chain's session).

### `onset_steps_follow_the_cut_off_geometric_distribution`

Over 10,000 seeds of the generator `attack_start_step`, every onset step lies from step 6 to step 24. The frequencies follow a geometric distribution with a chance of 1 in 7, cut off after step 24. A chi-squared test at the 1% level decides (§0.5, statistical tests).

### `waits_follow_the_cut_off_exponential_distribution`

Over 10,000 seeds of the generator `attack_waits`, every wait lies from 5 to 20 seconds, and the waits follow the distribution of `episodes.md` §10.3 (onset and waits). A Kolmogorov-Smirnov test at the 1% level decides (§0.5, statistical tests).

### `a_departure_before_a_needed_refuel_excludes_that_chain`

A tug leaves the dock inside the episode before the latest start of the refuel that a refuel chain needs. The episode runner excludes that chain, and the episode record gives the reason. The chains that wait for the fuel supplier's poll stay admitted.

### `an_overdue_poll_excludes_only_the_poll_chains`

At a fork, a poll under way is more than 12 seconds past its due time and less than one poll interval, with its event not logged. The relay holds the poll's connection open without answering until past the 12 seconds, and releases it before one poll interval has passed. A relay that refused connections would exhaust the retries at 1, 2 and 4 seconds and stop the simulator. The start state is uncertain, the episode is not discarded, and the episode runner excludes every poll chain. The episode admits the other chains that its predicted episode events admit, and the episode record gives the reason for each exclusion.

### `a_poll_more_than_one_interval_overdue_is_a_fault`

At a fork, a poll under way is more than one poll interval past its due time, with its event not logged, and the relay holds its connection open without answering. The episode runner discards the episode with that fault as its cause, and the living port continues with its next fork.

### `an_overdue_road_tanker_excludes_the_refuel_chains`

At a fork, the road tanker is more than 12 seconds past its scheduled arrival, with a poll under way. The relay holds the poll's connection open without answering until past the 12 seconds, and releases it before one poll interval has passed since the poll fell due. The episode runner excludes every refuel chain, and every poll chain since the held poll is overdue too, and does not discard the episode. The episode admits the other chains that its predicted events admit, and the episode record gives the reason for each exclusion.

### `the_relays_journal_is_checked_against_the_logged_polls`

A poll that the simulator logged within the look-back, its connection removed from the fork's journal, discards the episode with that cause. A poll held by the relay more than 12 seconds past its due time, its event not logged and no later connection in the journal, is no fault. The held poll leaves the start state uncertain, and excludes the poll chains alone.

### `a_later_connection_with_no_event_for_the_earlier_poll_is_a_fault`

In a fork, the relay's journal holds a poll's connections followed by a connection of a later due time, and the earlier poll's event is removed from the simulator's log. The episode runner discards the episode with that fault as its cause. So does a fork whose journal holds connections of two due times and whose simulator's log holds no event of the fuel supplier.

A poll retried after a failed attempt, with several connections within its due time and one event, is no fault, and the episode runs. So is a retried first poll at fork 1, under way or overdue, with several connections and no event logged.

### `a_named_chain_takes_fresh_seeds_until_one_admits_it`

At fork 0, a chain named on the command line takes fresh living port seeds until one admits it, and the episode record keeps the number of seeds drawn. A chain that 1,000 seeds in turn fail to admit stops the episode runner, which reports the reason. In a fork, the episode runner tries only the fork's own episode seed. A fork that does not admit the chain ends its episode before step 0 with the outcome not admitted, and the episode record states the reason.

### `a_seed_given_with_its_chain_is_kept`

Given a seed and a chain together on its command line, the episode runner keeps that seed. When the seed does not admit the chain, the episode record gives the reason.

### `a_fork_that_excludes_a_named_chain_passes_it_to_the_next_fork`

At a fork whose predicted events hold no refuel during the inventory stage's peer scan, a chain named on the command line that needs one passes to the next fork. No seed is drawn at the first.

### `a_chain_that_reads_what_an_audit_left_needs_a_completed_audit`

At a fork before the living port's first audit, no chain with a stage that reads what a completed audit left is admitted, and the episode record gives the reason. At a fork after that audit, the chain is admitted.

### `a_chain_that_needs_no_port_activity_is_excluded_only_by_the_allowance`

The test covers each chain of the sample library and of the recorded library whose stages need neither a port activity nor a completed audit. Over 10,000 episode seeds, each such chain is admitted, or excluded with the reason of condition 4 of `episodes.md` §10.2 (choosing the chain).

### `the_chain_is_drawn_uniformly_from_the_admitted_chains`

Over 10,000 draws for one episode with a fixed set of admitted chains, each admitted chain is drawn equally often. A chi-squared test at the 1% level decides (§0.5, statistical tests).

### `admission_agrees_with_each_live_episode`

For each of 20 recorded live episodes, the admission of the episode's chain agrees with what became of the chain. The chains that stalled for want of a poll, or failed for want of a refuel in a scan, are excluded, and every chain that completed is admitted. The recorded episodes ran an inventory stage of two peer scans, so the test judges them with that stage's measured run time and windows. The test checks the arithmetic of admission, and `admitted_chains_complete_on_fresh_seeds` (§14, episode mix and measurement episodes) checks the ranges.

### `the_counted_poll_starts_2_seconds_after_the_stage`

For a stage that waits for the fuel supplier's poll, with one poll's range starting 1 second after the stage's latest beginning, admission counts the poll after it.

### `an_intercepted_poll_delays_only_itself`

A poll that a stage intercepts completes up to 10 seconds late, and the later polls keep their due times. A chain whose second awaited poll is due before the deadline on the schedule is admitted.

### `a_chain_whose_last_stage_begins_inside_the_allowance_is_excluded`

A chain whose last stage may begin within the allowance of the horizon is excluded, and the episode record gives the reason. The same chain is admitted when its awaited poll is due 40 seconds earlier.

### `a_refuel_overlaps_the_peer_scan_by_15_seconds`

A refuel whose range overlaps the inventory stage's peer scan by less than 15 seconds at some time that the ranges allow excludes the chain that needs it. An overlap of at least 15 seconds at every such time admits the chain.

### `a_refused_refuel_makes_no_connections`

A refuel that the FMS refuses at its start, because the tank's volume less the request is at or below the cavitation threshold, makes no connections. The refused refuel therefore admits no chain that needs a refuel during the inventory stage's peer scan.

### `a_tug_waits_for_the_road_tankers_fill`

A tug that arrives during the road tanker's fill waits for the fill's end. The fuel supplier's polls keep their interval through the fill (`sim.md` §5.3, fuel supplier).

### `a_changed_attack_source_refuses_adversarial_episodes`

With one Rust file of the attack chains' source changed, the source's digest differs from the digest held as reviewed. The episode runner then refuses an adversarial episode until the table of stages is reviewed again. A changed manifest or lock file of the attack chains' crates refuses adversarial episodes in the same way. A change to a test, an example or a benchmark leaves the digest as it was, and adversarial episodes run.

### `a_binary_older_than_its_source_is_refused`

The episode runner refuses an attack chain's binary that was built before the last change to the attack chains' source.

### `a_release_without_the_source_checks_its_prebuilt_binaries`

In the public release, without the attack chains' source, every binary and `chains.json` match their listed SHA-256 digests, and the recorded digest of the source equals the digest held as reviewed. Adversarial episodes then run. A changed binary, a missing binary, a changed `chains.json` or a recorded digest that differs from the digest held as reviewed refuses adversarial episodes.

### `a_mock_builds_catalogue_is_refused`

The episode runner refuses a catalogue that marks a mock build, and a catalogue whose `schema_version` is not 2, and runs no adversarial episode from either.

### `a_declared_condition_without_its_need_keeps_its_chain_out`

A chain that declares a condition whose need the table gives none of its stages stays out of the competition library. With a stage that has that need, the same chain belongs to the competition library.

### `the_attack_chain_starts_at_the_onset_step`

The episode runner starts the attack chain's session within 1 second of the boundary of the onset step.

### `each_wait_starts_after_the_previous_stage_ends`

The time from the session's start to the first beginning event, and from each end event to the next beginning event, equals the drawn wait, within 1 second.

### `stage_events_name_the_target_host`

Each beginning event in the episode record carries its stage's target, as `episode-runner-impl.md` §7 (targets of stages) derives it.

### `stage_events_carry_the_episode_runner_clock`

The episode record holds each stage event with its time on the clock of the episode's Incus host, the clock of the episode runner.

---

## 9  Horizon and termination

Targets `episodes.md` §11 (horizon and termination) and §10.5 (stopping the chain), and `episode-runner-impl.md` §9 (the attack chain's session).

### `an_episode_ends_at_the_horizon`

An episode that stays clear of every terminal state ends after T steps, and its end notice states the end.

### `a_terminal_state_ends_the_episode`

A terminal state that the ground-truth evaluator identifies, injected in a test, ends an episode other than a suppressed episode at the step in which it occurs.

### `a_simulator_stop_ends_the_record_at_its_step`

A simulator stopped during a step ends the episode record with that step, and a decision message that arrives after the stop counts as dropped. In Track B the outstanding steps to T count as violated.

### `a_track_a_episode_ends_at_the_start_of_the_last_stage`

In Track A, with no alarm, the episode ends at the instant at which the last stage falls due. The episode runner writes no `begin` for the last stage, the end cause and the outcome are both `last_stage`, and Track A's outcome is 4 with `R_n` = −1. With `it-foothold__deny-directory-availability`, the directory's denial never runs, and a query of the directory succeeds at step 0 and before the reset.

### `the_attack_chain_stops_gracefully_at_the_end`

At the end of a Track B or suppressed adversarial episode whose last stage holds its impact, the episode runner sends the termination signal `SIGTERM`. The stage restores what it changed and ends as completed, and the binary exits with status 0.

### `the_binary_exit_status_follows_its_stop`

The stand-in binary's exit status in the episode record follows its stop. The status is 0 when every stage completed, and 1 at a failed stage or missing facts. The status is 2 on a command line that the binary cannot use, and 3 when the episode runner stops the binary before a stage. The only signal that the episode runner sends in these tests is `SIGTERM`.

### `the_stop_releases_the_directory`

In a Track B or suppressed episode, after the episode runner stops the directory's denial, a query of the directory from its own host succeeds.

### `the_adversary_host_is_down_and_unaddressed_before_and_after`

At step 0 and after the export, the adversary host's interfaces are down and unaddressed, and no process of the attack chain remains.

### `the_adversary_host_holds_every_binary_as_built`

At step 0 of every episode, benign or adversarial, the adversary host holds every binary that `chains.json` lists, with its built SHA-256 digest.

---

## 10  Terminated and discarded episodes

Targets `episodes.md` §12 (terminated and discarded episodes).

### `an_unexpected_response_terminates_the_episode`

A request of the dock operator that gets an unexpected response stops the simulator, and the episode ends with the outcome `terminated` and its score. The FMS refuses a dispense at its start when the tank's volume less the request is at or below the cavitation threshold. Such a dispense sends the tug away unserved and leaves the simulator running (`sim-impl.md`, "Non-idempotent job-state transitions (reconciliation)").

### `a_simulator_defect_discards_the_episode`

A failure injected into the simulator's own code, as the sole cause of the simulator's stop, discards the episode.

### `a_failed_incus_call_discards_the_episode`

A call of the episode runner to Incus that fails during an episode, injected in a test, discards the episode, and the episode record states the call that failed.

### `a_failed_export_discards_the_episode`

An export that fails to copy the evidence discards the episode.

### `a_failed_attack_session_discards_the_episode`

In an adversarial episode, an attack chain's session that fails to start, injected in a test, discards the episode with that cause. A session whose first line is not a process identifier discards the episode in the same way.

### `a_failed_copy_discards_the_episode`

A fork that fails, by a failure injected in a test into the save, the restore or the start of the fork's Incus host, discards the episode. The episode record states the failure.

### `a_fault_in_the_start_state_discards_the_episode`

Each fault in the first table is made by editing a fork's records before the episode runner reads them. The episode runner discards the episode with that fault as its cause in the episode record. The living port continues with its next fork, and the episode does not run again (`episodes.md` §6.4, how a fork is made).

| Fault | Made in the fork by |
|---|---|
| A read that fails after one retry, of each of the four sources | Making each source fail in turn before the reads: stopping the FMS's database, the relay's journal, the simulator's log or the reconciliation user's files |
| No reading of the tank gauge before the fork instant | Deleting the readings from the FMS's database |
| A tug with no scheduled arrival logged | Removing that tug's events from the simulator's log |
| A dispense whose preset differs from the logged request | Editing the tug's logged request in the simulator's log |
| More than one job holding the dock | Adding a second open job to the FMS's database |
| A tug's job and the road tanker's both holding the dock | Adding an open fill beside a tug's open job in the FMS's database |
| More than one fill under way | Adding a second fill under way to the FMS's database |
| An open dispense that no tug at the dock accounts for | Adding an open dispense for a tug away from the dock to the FMS's database |
| A tug with no departure within 5 seconds of its refused job's cancel or its delivery note's link | Removing the tug's next scheduled arrival from the simulator's log, at a fork more than 5 seconds after the cancel or the link |
| A fill transcribed with no delivery's end logged within its bound | Removing the fuel supplier's event of the delivery's end from the simulator's log, at a fork more than 5 seconds after the transcription with no poll under way that fell due no later than the transcription |
| An order with no scheduled arrival of its road tanker | Removing the scheduled arrival from the order's event in the simulator's log |
| A road tanker more than 12 seconds past its scheduled arrival with no poll under way that fell due no later than that arrival | Removing the road tanker's arrival from the simulator's log, at a fork more than 12 seconds after it with no fill opened since and no such poll under way |
| A poll logged within the look-back whose connection the relay's journal lacks | Removing that poll's connection from the relay's journal |
| A poll's connections followed by a connection of a later due time, with no event logged for the earlier poll | Removing the earlier poll's event from the simulator's log |

The faults in the second table depend on how long a procedure has been under way at the fork instant. The episode runner judges that time from the records up to the fork instant, so nothing done in a fork can make these faults. Unit tests of the episode runner's check of the start state make each one. Each test constructs the records, the fork's FMS rows, the simulator's log lines and the relay's journal, and expects the fault.

| Fault | Constructed records |
|---|---|
| A refused job with no cancel within its bound | A refusal with no cancel after it for more than 15 seconds |
| A delivery note linked later than its bound | A tug's departure with no link of its delivery note for more than 15 seconds |
| A dispensed tug with no departure within its bound after its dispense's close | A dispense closed, with no departure of its tug for longer than the bound |
| A refuel whose dispense has not closed within its bound | A refuel's job opened, with no close of its dispense for longer than the bound |
| A fill not transcribed within its bound | A fill's job opened, with no transcription for longer than the bound |
| A tug or the road tanker waiting at an idle dock for more than 87 seconds | An arrival at an idle dock, with no job opened for more than 87 seconds |
| A poll under way more than one poll interval past its due time | A poll's connection, with no event of it for more than one poll interval after its due time |

With at most one connection in the relay's journal, a simulator's log with no event of the fuel supplier is no fault: the first poll has not fallen due, or is under way or overdue.

### `an_adversarial_episode_needs_its_binary_on_the_adversary_host`

Before any stage of an adversarial episode, the episode runner checks that the adversary host holds the chain's binary as an executable file in `/root`. With the binary, the episode runs its chain. Without it, the episode is discarded with the cause `the adversary host holds no executable /root/<chain id>` in the episode record. No attack chain's session starts, and no step runs.

### `a_fork_whose_adversary_host_lacks_the_binary_discards_its_episode`

In a fork, the check follows the choice of the chain and comes before step 0's decision request. A fork whose adversary host lacks the chain's binary discards its episode with the same cause, and no attack chain's session starts.

### `a_check_of_the_binary_that_incus_fails_discards_the_episode`

A check of the binary that Incus fails to run is a failed call to Incus, which discards the episode.

### `a_discarded_episode_is_recorded_and_the_next_fork_runs`

A fork that fails for a fault of the testbed ends its episode as discarded, the episode record states the cause, and the episode runner makes the next fork at the next fork instant.

### `an_early_stop_of_the_attack_chain_leaves_the_episode_running`

A test attack chain that stops before its last stage leaves the episode running to its horizon.

### `every_stop_records_its_cause`

The episode record states the cause of every termination and discard.

---

## 11  Episode record

Targets `episodes.md` §13 (episode record), and `episode-runner-impl.md` §10 (export) and §11 (Track A's fields in the record).

### `the_episode_record_holds_every_item`

The episode record holds each item of the table in `episodes.md` §13 (episode record), each source in files of its own.

### `the_episode_record_holds_track_a_fields`

In Track A the episode's file in the episode record, `record.json` in a fork and `episode.json` at fork 0, holds `track_a` with the keys `alarm`, `K`, `k_n`, `R_n`, `outcome` and `end_cause`. The key `end_cause` of `track_a` holds one of `horizon`, `alarm`, `last_stage` and `simulator_stop` (`episode-runner-impl.md` §11, Track A's fields in the record). The episode's outcome, beside `track_a`, is `alarm` or `last_stage` when the alarm or the start of the last stage ended the episode, and never `terminated`. With the end cause `simulator_stop`, the outcome is `terminated` or `discarded`.

### `no_node_can_reach_the_episode_record`

The episode's Incus host lies beyond every network path from a RavensPort node, and the episode record lies outside every mount of a node.

### `the_episode_record_leaves_the_copy_before_the_reset`

After the export of an episode in a fork, the episode record lies on the machine that runs the Incus hosts, and the reset then deletes the fork's Incus host.

### `the_episode_record_states_the_training_volumes_versions`

In training mode, the episode record states the training volume's version at the episode's start and at its end. A lineage that starts from a given volume starts its first episode from that volume's version.

### `image_hashes_match_the_deployed_images`

The hash of each node image in the episode record matches the image that Incus deployed.

### `the_episode_type_appears_only_in_the_episode_record`

Of the episode's files, only those in the episode record state whether the episode is benign or adversarial.

### `the_export_omits_an_incomplete_line`

The export starts 2 seconds after the end notice. With a line of `archives.json` incomplete at the export, the exported dated file ends with its last complete line.

### `the_export_keeps_each_dated_file_whole`

For an episode that spans midnight on the clock of `siem-01`, the episode record holds both days' dated files of `alerts.json` and of `archives.json`. Each file is whole from its first byte, under its path relative to `/var/ossec/logs/`, and a dated file that the Wazuh manager compressed before the export stays compressed.

---

## 12  Reset and the competition image

Targets `episodes.md` §3.2 (modes), §3.3 (reset) and §3.4 (competition image), and `episode-runner-impl.md` §1 (competition images) and §5 (forks).

### `a_competition_deployment_takes_no_snapshot_set`

On a deployment of the competition profile, `make snapshot` fails and changes nothing. `make restore` refuses a set taken from such a deployment, and changes nothing. A living port's `sim-host` holds the start-up input and no checkpoint, at paths that share nothing (`deployment.md` §10, snapshot sets).

### `reset_returns_every_host_to_its_state_at_deployment`

After the reset at fork 0, the next living port's FMS holds no transaction, and the outputs of the security monitoring begin at the Wazuh manager's own start. The next living port's tank holds 35,000 litres, the simulator's default (`sim.md` §2.3, start state).

### `the_reset_leaves_no_node`

After the reset at fork 0 of an adversarial episode, whose attack chain has run, no instance of the deployment remains, the adversary host among them (`episodes.md` §3.3, reset). The episode record names no instance that remains after the reset (`episodes.md` §13, episode record).

### `the_reset_deletes_the_copy_and_leaves_the_living_port_running`

After the reset of a fork, the fork's Incus host and its instances are gone. The living port's simulator still runs, and the living port's readings of the tank gauge keep their spacing.

### `the_training_volume_passes_to_each_copy`

In training mode a file that the defender writes to the training volume in one fork is present in the `siem-01` of the next fork of the same living port. The next episode starts from the version of the training volume that the previous episode left.

### `the_training_volume_is_attached_only_for_an_episode`

In training mode, the `siem-01` of a living port that makes forks holds no training volume at any save. Each fork's `siem-01` holds the training volume read-write from the episode's start to the reset.

### `a_living_port_runs_its_training_episodes_one_at_a_time`

In training mode, no fork of a living port begins step 0 before the reset of the fork before it has kept the training volume's version.

### `every_evaluation_copy_keeps_the_submitted_volume_read_only`

In evaluation mode, the living port's `siem-01` holds the entry's submitted storage volume read-only from the living port's creation, and every fork's `siem-01` holds the same volume read-only. A write to the volume fails in every fork, and the volume's contents equal the submitted contents after every episode.

### `a_fork_0_episode_holds_the_volume_read_only`

With `--volume`, the command `run` attaches the volume read-only at `/var/lib/entry` before step 0. A write to the volume from `siem-01` fails, and the volume's contents equal the submitted contents after the episode.

### `the_competition_override_file_changes_only_its_nodes_and_the_scaled_values`

Rendering with the competition's additional override file changes the build script of `siem-01` and the flow timeouts in the build scripts of the three firewall nodes. The render also changes the names of the images of those four nodes and of `sim-host`, in every rendered file that holds them, and every value that the compression factor scales (`episodes.md` §5.3, timings). Every other rendered line stays the same, including every line of the adversary host's rendered files.

### `the_override_file_leaves_the_adversary_host_unchanged`

The competition's additional override file neither adds nor patches the node `adversary`. A render with the file and a render without it hold the same nodes, and the same rendered files of the adversary host.

### `the_competition_image_adds_only_the_competition_software`

The competition image of `siem-01` holds the installed packages and the configuration files of the operational image, together with the launcher, the idle defender's program and the query interface. The configuration files of the two images differ only in the values that the compression factor scales, the schedules of the security monitoring's agent (`episodes.md` §5.3, timings). The launcher, the idle defender's program and the query interface exist in that competition image alone. The competition image of `sim-host` holds the files of its operational image with the competition's entry point as the simulator's binary, together with the empty directory `/var/lib/sim/startup`, and nothing else.

### `a_living_port_creates_its_nodes_from_the_competition_images`

In a living port, `siem-01` runs from `ravensport/siem-01-competition:latest`, and `sim-host` from `ravensport/sim-competition:latest`. Each firewall node runs from its competition firewall image, such as `ravensport/fw-ot-dmz-competition:latest`. Every other node, the adversary host among them, runs from the image that it runs from in any other deployment. A deployment with a deployment profile alone creates those five nodes from their operational images, such as `ravensport/siem-01:latest`.

### `an_entrys_tooling_is_built_into_its_hosts_images`

A test manifest for a firewall node builds into that node's competition firewall image, and a test manifest for `fms-01` into its operational image. The entry's living ports run both hosts from the built images.

### `a_build_rebuilds_only_the_images_whose_inputs_change`

The test starts once every image of both renders exists. A build repeated with the same render skips every image as unchanged. A build after a switch between a render with the competition's additional override file and a render without it rebuilds only the images whose inputs differ between the two renders. A change of the compression factor alone rebuilds every image that holds a scaled value, the competition image of `sim-host` among them.

---

## 13  Reproducibility

Targets `episodes.md` §14 (reproducibility).

### `two_runs_of_one_seed_share_their_draws`

Two episodes with one episode seed hold the same draws in their episode records. The draws set the onset step and the waits. At fork 0 the draws also set the tug streams and the first occurrences of the file-integrity scans.

### `a_replay_reaches_the_fork_instant_with_the_same_tugs`

A replay of fork k, from the living port's latest snapshot before fork k, reaches the fork instant with the same tug arrivals, requested volumes, refusals and audits as the living port had. The replay then makes the fork with the same episode seed.

### `event_times_of_one_seed_agree_in_distribution`

Repeated runs of one seed, in measurement episodes, give event times whose distributions agree across runs. A two-sample Kolmogorov-Smirnov test at the 1% level decides for the times of tug arrivals and of stage beginnings (§0.5, statistical tests).

---

## 14  Episode mix and measurement episodes

Targets `episodes.md` §15 (episode mix) and §16 (measurement episodes and episode cost).

### `evaluation_runs_the_fixed_numbers_of_each_type`

An evaluation run holds exactly the number of benign and adversarial episodes that the published ratio and the episode count give.

### `measurement_episodes_record_phases_and_resources`

Each measurement episode records the time of the deployment or the fork, settle, the steps, export and reset, and the processor and memory use of each Incus host and of each node.

### `the_settle_timeout_is_three_times_the_longest_settle`

The settle timeout that the measurement episodes set equals three times the longest time they recorded for the settle conditions to hold.

### `the_settle_delay_exceeds_the_longest_flow_record_lag`

The settle delay that the measurement episodes set exceeds the longest flow-record lag that they measured, and the allowance is at least that lag.

### `admitted_chains_complete_on_fresh_seeds`

Track B or suppressed measurement episodes on fresh seeds run each chain that needs a port activity on seeds that admit it. The measurement episodes also run each such chain on seeds that the rule excludes and that the midpoints of the ranges, with no margin, admit. Every admitted chain begins its last stage, unless a terminated episode ends first. Each measured time of step 0, of a refuel's first connection and of a poll, and each stage's run time, is within its range of `episodes.md` §10.2 (choosing the chain).
