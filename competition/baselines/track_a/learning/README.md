# Learning defender for Track A

This example entry for Track A shows a defender that learns which properties of the security monitoring's records are rare in benign operation. The defender raises the Track A alarm at a step whose new records show enough rare properties to reach a threshold. The model is trained offline, and the defender continues to learn in training mode.

## The entry's files

The entry's storage volume, which `siem-01` mounts at `/var/lib/entry`, holds four files.

- [`defender`](defender): the defender's program, in Python 3 with the standard library alone.
- [`model.json`](model.json): the model, which the defender updates in training mode.
- [`telemetry.json`](telemetry.json): the telemetry specification, which declares the defender's reads.
- `protocol.py`: the step protocol and the reads, copied from [`../common/protocol.py`](../common/protocol.py).

[`train.py`](train.py) trains the model offline and stays out of the volume. The entry adds no host tooling.

## What the defender reads

At each decision request the defender makes two reads through the query interface: `siem-read alerts`, the alerts of the Wazuh manager, the security monitoring's detection software, and `siem-read archives`, every archived record. The model decides which records matter, so the defender reads them all. Each read returns the records written since the previous read with the same request. `telemetry.json` declares both reads, and `protocol.py` checks each read against it.

## Method

**Tokens.** The defender describes each record by tokens, short strings that each state one property of a flow, connection, journal line, listening port or alert, among others. The token `flow|fw-ext-dmz|ext0|ICMP|10.10.0.20|10.74.0.20|-`, for example, states a flow that packet inspection on the firewall `fw-ext-dmz` saw. Values that vary between runs, such as ephemeral ports and the numbers in log messages, become classes or placeholders.

**Rarity.** The model counts, for each token, the benign steps that held it. A token's weight is 1 if no benign step held it, and decreases on a logarithmic scale to 0 once its smoothed frequency reaches one in 2,000 benign steps (`Model.weight` in `defender`).

**Alarm.** The step's score sums the highest weight in each group, the tokens of one kind from one host, with one more group for the addresses seen in flows and connections. The defender raises the alarm when the score reaches the model's threshold, 1.682, and writes the score and the weightiest tokens to its standard error. Since a group adds at most 1, the alarm needs rare tokens in two groups or more, such as the unseen flow and the unseen address of a probe from an unknown host.

**Offline training.** `train.py` reads exported episode records as the defender's reads would have returned them at each decision request. The model counts as benign every step of a benign episode, the steps of an adversarial episode before the first release, and the steps of each living port's history after its start-up. With each living port scored under the counts of the others, the threshold lies midway between the highest benign score and the lowest score with which an attack first exceeded every benign score.

**Learning in training mode.** In training mode the volume is writable, and the end notice carries the episode's reward, `R_n`. The defender then adds the episode's steps to the model if it scored `R_n` of 0 without an alarm, a benign episode, or `R_n` of -1 after its own alarm, a false alarm. Learning a false alarm's steps stops it from recurring. In evaluation mode the volume is read-only and no reward arrives, so the defender learns nothing.

## Limitations

- The shipped model has seen no road tanker delivery, audit or daily timer of the operating systems after start-up, and the first such event can raise a false alarm.
- Against a chain with an external foothold, the alarm waits for the first unseen tokens, which come from packet inspection's flow records and arrive only once Suricata's flow timeouts expire.
- An alarm that arrives after the attack chain's last stage fell due finds the episode ended as a missed attack, with `R_n` of -1, and the defender learns the attack's steps as benign.

## Running it

From the repository's root, [`make_volume.py`](../make_volume.py) writes the storage volume with this directory's `model.json`, and the episode runner runs episodes with it in evaluation mode, both inside the Incus host. The replay, [`../common/replay.py`](../common/replay.py), runs the defender's program over exported episode records, for development alone, and its second form runs them in turn on one writable volume, as training mode does.

```sh
python3 competition/baselines/track_a/make_volume.py learning ~/volumes/learning
python3 competition/baselines/track_a/learning/train.py --out model.json --jobs 8 EPISODE_RECORD...
PY=deploy/.venv-$(uname -s)-$(uname -m)/bin/python
$PY -m competition.episode_runner run --track A --episodes 4 --volume ~/volumes/learning
python3 competition/baselines/track_a/common/replay.py --entry competition/baselines/track_a/learning --jobs 8 EPISODE_RECORD...
python3 competition/baselines/track_a/common/replay.py --entry competition/baselines/track_a/learning --training --volume VOLUME EPISODE_RECORD...
```

Training mode runs in forks of a living port ([the training guide](../../../../docs/training.md)).
