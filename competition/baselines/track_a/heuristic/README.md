# Heuristic defender for Track A

This example entry for Track A shows a defender built from two fixed rules, with no training, no image of `siem-01` of its own and no added host tooling. At each step the defender reads the security monitoring's new alerts and packet inspection records. The defender raises the Track A alarm on a Wazuh security alert of severity 8 or above, or on network traffic to or from an address that no host of the port holds. Otherwise the defender answers no action.

## The entry's files

The entry's storage volume, which `siem-01` mounts at `/var/lib/entry`, holds three files. [`make_volume.py`](../make_volume.py) assembles the volume.

- [`defender`](defender): the defender's program, in Python 3 with the standard library alone, which the launcher on `siem-01` runs as root in `/root`.
- [`telemetry.json`](telemetry.json): the telemetry specification, which declares the data that the defender reads.
- `protocol.py`: the step protocol and the reads through the query interface, copied from [`../common/protocol.py`](../common/protocol.py), which every example entry shares.

## What the defender reads

At each decision request, the defender makes two reads through the query interface, `siem-read`:

- `siem-read alerts`: every alert of the Wazuh manager, the security monitoring's detection software;
- `siem-read archives --input /var/log/suricata/eve.json`: the records of packet inspection, Suricata's flow records and alerts from the three firewalls between the port's network zones.

Each read returns the records written since the previous read with the same request. Competition Rule 3 allows an entry only the reads that its telemetry specification declares, and `telemetry.json` declares these two:

```json
{
  "reads": [
    {"output": "alerts"},
    {"output": "archives", "input": "/var/log/suricata/eve.json"}
  ]
}
```

`protocol.py` refuses any read that the specification does not declare. The format is this example's choice, since the entry requirements remain an open item ([`episodes.md` §19, open items](../../../../specs/competition/episodes.md#open-items)).

## Method

Either rule raises the alarm when it holds for the records of the step's reads.

1. **Alert severity.** Any alert of severity 8 or above, on Wazuh's scale of 0 to 15, which the alert carries in `rule.level`. Severity 8 marks an event seen for the first time, and higher severities mark errors from invalid sources, integrity warnings and events of high importance. The threshold, `MIN_LEVEL` in `defender`, is 8, one above the highest severity of any alert measured in benign operation, the port's start-up included.
2. **Address plan.** A packet inspection record whose source or destination is an Internet Protocol version 4 (IPv4) address that no host of the port holds. The address plan, `ADDRESS_PLAN` in `defender`, lists the 24 addresses that [`deploy/topology.yaml`](../../../../deploy/topology.yaml) gives the port's hosts, and leaves out the adversary host. Multicast, broadcast and unspecified addresses name no host and never raise the alarm. The rule ignores Internet Protocol version 6 (IPv6) addresses, which derive from hardware addresses that differ between deployments.

The rules keep no state, and the defender reads no reward.

## Limitations

- The threshold was chosen from benign operation without a road tanker delivery or an audit, either of which would raise a false alarm if it produced an alert of severity 8 or above.
- An attack that uses only the plan's addresses and raises no alert of severity 8 or above, such as one from a compromised host of the port, raises no alarm.
- Suricata writes a flow record only once the flow's timeout expires, so the address rule raises the alarm at least 20 to 30 seconds after the first packet from an unknown address.

## Running it

Inside the Incus host, at the repository's root, write the storage volume and run episodes with it.

```sh
python3 competition/baselines/track_a/make_volume.py heuristic ~/volumes/heuristic
PY=deploy/.venv-$(uname -s)-$(uname -m)/bin/python
$PY -m competition.episode_runner run --track A --episodes 4 --volume ~/volumes/heuristic         # four benign episodes
$PY -m competition.episode_runner run --track A --type adversarial --volume ~/volumes/heuristic   # one adversarial episode
```

The replay takes the episode record directories that the episode runner prints.

```sh
python3 competition/baselines/track_a/common/replay.py --entry competition/baselines/track_a/heuristic --jobs 8 EPISODE_RECORD...
```

The replay serves development alone. Scoring runs every entry live in its episodes ([`episodes.md` §3.2, modes](../../../../specs/competition/episodes.md#32-modes)).
