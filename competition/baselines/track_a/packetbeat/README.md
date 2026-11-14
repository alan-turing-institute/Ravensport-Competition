# Packetbeat defender for Track A

This example entry for Track A shows how an entry adds tooling of its own to `siem-01`, the security monitoring host. The entry builds Packetbeat, a packet analyser, into its own image of `siem-01`. The defender raises the Track A alarm when Packetbeat reports a connection to the Wazuh manager that the port's operation never makes.

## The entry's files

- **Storage volume.** [`defender`](defender), the defender's program, with `telemetry.json` and `protocol.py`, the shared step protocol, copied from [`../common/protocol.py`](../common/protocol.py). `siem-01` mounts the volume at `/var/lib/entry`.
- **The entry's own image of `siem-01`.** `ravensport/siem-01-packetbeat:latest`, the competition image of `siem-01` with Packetbeat, its configuration [`packetbeat.yml`](packetbeat.yml), its systemd unit [`packetbeat.service`](packetbeat.service) and the Wazuh manager's log input [`localfile.xml`](localfile.xml), built by [`build-scripts/siem-01-packetbeat.sh.j2`](build-scripts/siem-01-packetbeat.sh.j2).
- **Override file.** `dist/override.yaml`, which names the entry's image of `siem-01`.
- **Telemetry specification.** [`telemetry.json`](telemetry.json), which declares the defender's one read.

The entry adds no tooling to any other host.

## What the defender reads

At each step, the defender reads the Packetbeat events archived since its previous read:

```
siem-read archives --host siem-01 --input /var/log/packetbeat/events.ndjson
```

`telemetry.json` declares that read by output file, host and input, and `protocol.py` refuses any undeclared read (Competition Rule 3). The Wazuh manager archives each event's decoded fields under `data`, and writes each number as a string. The defender keeps no state between episodes and writes nothing to its volume, so the entry behaves the same in training and evaluation modes.

## Method

No process of the port uses the Wazuh manager's management interface, Transmission Control Protocol (TCP) port 55000, or its enrolment service, TCP port 1515, which this deployment leaves disabled. In operation, `siem-01` accepts connections only from the twelve monitored hosts' Wazuh agents, on TCP port 1514. The defender raises the alarm at the first event that reports a connection to port 55000 or 1515, or to port 1514 from an address of no monitored host. One event suffices, because the port's operation produces none.

Every attack chain with a foothold on the information technology (IT) segment begins with the stage `scan-it-segment`, which scans every TCP port of every host on the segment, `siem-01` included. The scan's first connection to port 55000, 1515 or 1514 of `siem-01` raises the alarm, since the adversary host, which runs the scan, is no monitored host. Chains from the external segment never reach `siem-01`.

## Adding Packetbeat to siem-01

**Build the binary.** `build-packetbeat.sh` builds Packetbeat's open-licence edition, the `packetbeat` directory of Beats v9.5.5, with Go 1.26.8 and libpcap 1.10.7, for amd64 and arm64. The build runs with Docker in Ubuntu 24.04, the release of `siem-01`'s image, so the binary needs no newer C library than `siem-01` holds. The binaries' digests must match `packetbeat.sha256`.

**Send the events to the Wazuh manager.** Packetbeat writes each event as one JavaScript Object Notation (JSON) object per line to its standard output, which the systemd unit appends to `/var/log/packetbeat/events.ndjson`. The log input in `localfile.xml` makes the Wazuh manager read that file with its packaged JSON decoder, so each event reaches `archives.json`. The capture filter in `packetbeat.yml` passes the packets to and from ports 55000 and 1515 of `siem-01`, and the opening packet of each connection to port 1514. Each clause names `siem-01`'s address and the port's direction, so a scan from any source port passes the filter at ports 55000, 1515 and 1514 alone.

**Build the image.** The entry's template extends `siem-01`'s competition template, `siem-01-competition.sh.j2`, through two blocks that each call `super()`, so the competition's own steps still run. `extra_hash_inputs` lists the binary, configuration, unit, log input and digests, so a change to any of them rebuilds the image. `extra_files` checks the binary's digest and installs the binary, configuration and unit as root's files, since Packetbeat refuses a configuration file that root does not own. The block then creates the empty events file, so that the Wazuh manager finds it at its first start, appends the log input to `ossec.conf`, and enables the unit.

**Name the image.** `override-patch.yaml` sets the build script and image name of `siem-01`, and the template directory. `override.py` merges the patch into the competition's additional override file, [`competition/deploy/override.yaml`](../../../deploy/override.yaml), and writes `dist/override.yaml`, which keeps every other value, such as the compression factor. The entry's template directory replaces the competition's, so it also holds a link to `siem-01-competition.sh.j2`.

## Limitations

- The entry detects no chain that stays on the external segment.
- The capture of `scan-it-segment` on `siem-01`'s own interface, the delay of the alarm on a living port and the self-reported performance await live episodes.
- The defender holds the addresses from which the monitored hosts' Wazuh agents connect, as `deploy/topology.yaml` gives them. A change of address there needs the same change in the defender.

## Running it

On a machine with Docker, build the binaries into `dist/`.

```sh
competition/baselines/track_a/packetbeat/build-packetbeat.sh
```

Inside the Incus host, from the repository's root, deploy the entry, run episodes and test it.

```sh
E=competition/baselines/track_a/packetbeat
$E/deploy-entry.sh check
$E/deploy-entry.sh override
$E/deploy-entry.sh images
$E/deploy-entry.sh volume ~/volumes/packetbeat
PY=deploy/.venv-$(uname -s)-$(uname -m)/bin/python
$PY -m competition.episode_runner run --track A --volume ~/volumes/packetbeat --extra-override $E/dist/override.yaml
$PY -m pytest competition/tests/test_baseline_packetbeat.py -q
```

On the machine itself, the living port manager takes the override file as a path in the tree, which must also hold Packetbeat's binary in `dist/`.

```sh
python3 -m competition.living create lp1 --base-disk DISK --mac MAC --tree TREE \
    --extra-override competition/baselines/track_a/packetbeat/dist/override.yaml --volume VOLUME
```
