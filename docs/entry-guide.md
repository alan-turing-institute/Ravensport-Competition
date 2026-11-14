# Writing an entry for Track A

This guide gives the parts of an entry, the messages of each step, the query interface, the tooling that an entry may add, the scoring of Track A and the episode records. The specifications [`episodes.md`](../specs/competition/episodes.md) and [`episode-runner-impl.md`](../specs/competition/episode-runner-impl.md) are the authority for each.

## Episodes

In Track A, an entry raises the Track A alarm as early as possible in an adversarial episode, and never in a benign one.

An episode is one scored run of the port. A benign episode holds the port's operation alone, and an adversarial episode adds one attack chain, the ordered stages of one attack, run as one program on the adversary host. Each episode runs in a fork of a living port. A living port is a deployment of RavensPort that runs continuously, and a fork copies the living port's whole state at one instant into a virtual machine of its own. Every host of a fork therefore holds the records of the port's operation since the living port's deployment.

An episode runs at most 144 steps of 5 seconds, 12 minutes in all. The port runs at a compression factor of 5, so each step covers 25 seconds of the port's operation. In an adversarial episode, the episode runner starts the attack chain at a step from 6 to 24, and releases each stage after a wait of 5 to 20 seconds. The compression factor, the steps at which the attack chain starts and its waits are provisional values, which measurement episodes may change ([`episodes.md` §17, parameters](../specs/competition/episodes.md#parameters)).

## The entry

An entry comprises four parts.

- **A modified `siem-01`.** The entry's storage volume, and the entry's own image of `siem-01` where it gives one.
- **Added host tooling.** Any software that the entry adds to other hosts of the digital world, built into each host's image.
- **A telemetry specification.** The declaration of the data that the entry reads.
- **Self-reported performance.** The scores that the entrant measured in its own episodes in evaluation mode.

The example entries show one format of the telemetry specification. The entry requirements, which fix the format and the scope of added host tooling, await specification.

`siem-01` mounts the entry's storage volume at `/var/lib/entry`. At the start of each episode, the launcher on `siem-01` runs the defender's program, `/var/lib/entry/defender`, as root in the working directory `/root`. The program runs once for each episode. The program may use the interpreters of the image of `siem-01`, built on Ubuntu 24.04, which holds `sh`, `bash` and `python3`. A volume with no `defender` runs the idle defender, which answers no action at every step. A `defender` that is not a regular file that `siem-01` can read and execute discards the episode.

In evaluation mode, `siem-01` mounts the entry's storage volume read-only, and the defender's policy stays fixed. In training mode, the training volume, a writable version of the entry's storage volume, passes from each episode of a living port to the next. A defender can therefore keep what it learns, and replace its own program from the next episode on.

## Each step

A step session into `siem-01` runs the defender's program and carries the step messages, each one JavaScript Object Notation (JSON) object on one line. At the start of each step, the episode runner writes a decision request to the program's standard input.

```json
{"track": "A", "step": 7, "previous_reward": 0, "ended": false}
```

The program answers each decision request with one decision message on its standard output, no action or the Track A alarm. The program writes each decision message as one line, and flushes its standard output after it.

```json
{"decision": "no action"}
{"decision": "alarm"}
```

The first valid decision message to arrive during a step is that step's decision, and a step that ends without one takes no action. A decision message that arrives late counts for the step during which it arrives. The episode runner drops and counts each invalid line. After the last step, the episode runner sends the end notice, a step message whose `ended` is `true`.

The Track A alarm ends the episode. The episode runner releases no further stage, and sends the end notice within 1 second. In training mode, each decision request after step 0 carries the previous step's reward, `0` in Track A, and the end notice carries the episode's reward, `R_n`. In evaluation mode, `previous_reward` is `null` in every step message.

## Reading the security monitoring

The defender reads the outputs of the security monitoring through the query interface, the program `siem-read` on `siem-01`, which the defender runs once for each read.

| Request or option | Selects |
|---|---|
| `siem-read alerts` | The Wazuh manager's alerts, from `alerts.json` |
| `siem-read archives` | Every record that the Wazuh manager received, from `archives.json`, the records of packet inspection among them |
| `--host NAME` | Records whose `agent.name` is `NAME`, the originating host |
| `--input LOCATION` | Records whose `location` is `LOCATION`, the log file or command that the Wazuh agent collected them from, such as `/var/log/suricata/eve.json` |
| `--since TIME` | Records from the first whose `timestamp` is at or after `TIME`, an instant in Coordinated Universal Time (UTC) such as `2026-11-12T20:15:05.250000Z` |

Each read returns the matching records that the Wazuh manager wrote since the previous read with the same request, one JSON object per line. A request without `--since` first reads from step 0. A request with `--since` first reads from its `TIME`, which can lie before the episode. A defender can therefore read the port's history back to the living port's deployment. A request that `siem-read` cannot serve exits with status 2. Every read enters the record of reads, which the episode record keeps, so that the organisers can replay each read and check it against the entry's telemetry specification.

## Added tooling and the entry's own images

An entry may add tooling that collects data on the hosts of the digital world, and transmits the data only along flows that already carry each host's data to `siem-01`. Until the entry requirements say otherwise, the tooling on each host is a manifest of software and the entry's configuration of the host's Wazuh agent. Data from added tooling reaches the defender only once the Wazuh manager writes it to `alerts.json` or `archives.json`. An entry may also give its own image of any host except the adversary host, built from the image that the host would otherwise run. The entry's own version of the competition's additional override file, [`competition/deploy/override.yaml`](../competition/deploy/override.yaml), names each such image.

## Scoring in Track A

When an episode ends, the episode runner computes the episode's reward, `R_n`.

| Episode | Track A alarm | `k_n` | `R_n` |
|---|---|---|---|
| Benign | None | | `0` |
| Benign | A false alarm | | `-1` |
| Adversarial | Before the release of the first stage, a false alarm | `0` | `-1` |
| Adversarial | After `k_n` stages are released, for `k_n` from 1 to `K - 1` | `k_n` | `-(k_n - 1)/(K - 1)` |
| Adversarial | None, a missed attack | `K` | `-1` |

`K` is the number of stages of the episode's attack chain. `k_n` is the number of stages that the episode runner released before the Track A alarm arrived, and `K` when no Track A alarm arrived. A Track A alarm after the first release and before the second scores `0`, the best score of an adversarial episode. Each further stage released before the Track A alarm lowers `R_n` by `1/(K - 1)`. With `K = 5`, a Track A alarm after the third release scores `-(3 - 1)/(5 - 1) = -0.5`.

The episode ends at the arrival of the Track A alarm, after 144 steps, or at the instant at which the episode runner would release the attack chain's last stage. The last stage therefore never runs in Track A, and an attack with no Track A alarm by then counts as missed. A simulator stop also ends an episode early, with its score applied ([`episodes.md` §12, terminated and discarded episodes](../specs/competition/episodes.md#discarded)).

An entry's score is the mean of `R_n` over its benign episodes and the mean over its adversarial episodes, weighted by the published proportions of the two types. The organisers publish the ratio of benign to adversarial episodes and the number of episodes before final scoring. Every entry runs on living ports with the same seeds, so that every entry meets the same operation of the port. Final scoring also uses attack methods beyond those in this release.

## Episode records

Each episode leaves an episode record, a directory with the episode's inputs, each step's decision and reward, and the times at which each stage of the attack chain began and ended. The episode record also holds the record of reads, the simulator's log, and the exported `alerts.json` and `archives.json` files. The key `track_a` of the episode's file holds `K`, `k_n`, `R_n`, the Track A alarm's step and arrival, the outcome and the end cause.

| Command | Episode records |
|---|---|
| `run`, at fork 0 | `competition/records/` in the repository, one directory for each episode, with the episode's file `episode.json` |
| `loop`, on a living port | `~/ravensport-living/lp1/episodes/fork-K/` on the machine, with the episode's file `record.json` and the export in `export/` |

The living port manager keeps each version of the training volume in `~/ravensport-living/lp1/training/`. Exported episode records serve development and offline training.
