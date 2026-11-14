# Snapshot, export, import and restore across two virtual machines

This directory holds local snapshot sets and exported snapshot files
(`specs/deployment/deployment.md` §10). Git ignores everything in it except
this file.

The procedure below takes a snapshot set on one Lima virtual machine (VM), the
producing machine, and exports it as a snapshot file. A second Lima VM, the restoring machine, imports and restores the file. The checks in
step 8 confirm that the restored environment carries the producer's history and
no artefact other than those of a planned maintenance window (`narrative.md`
§3).

This file is intended as developer documentation, for the purpose of testing and validating the snapshot procedure. **Users should instead follow the snapshot instructions given in the README at the RavensPort repository root.**

## Setup

Each VM has its own clone of the repository. Both clones should be on the
same commit (assumed for some of the checks below, although not an absolute requirement for snapshot sharing). The two VMs can run on different hosts.

Set the same `LABEL` variable on both machines:

```sh
LABEL=sample-snapshot
```

## On the producing machine

### 1. Deploy

```sh
cd <repo root on the producing machine>
git status --short                      # empty
make -C deploy build                    # if the node images predate the commit
make -C deploy up
```

Deploy without `EXTRA_OVERRIDE`. A deployment of the competition profile takes
no snapshot set, and `make -C deploy snapshot` refuses it (`deployment.md` §10).

### 2. Let the environment run

Leave it running in real time, and run no command inside an instance: every
command run inside an instance becomes part of the history. Let it run for at
least two hours, so that the history includes completed transfers.

### 3. Take the snapshot set

```sh
make -C deploy snapshot LABEL=$LABEL     # the sim's stop alone can take up to an hour
ls deploy/snapshots/$LABEL               # checkpoint.json  inputs/  rendered/  stopped-at
```

### 4. Export the snapshot file

```sh
make -C deploy export-snapshot LABEL=$LABEL
F=$(ls deploy/snapshots/ravensport-snapshot-$LABEL-*.tar)
ls -lh $F
tar -xOf $F manifest.json | python3 -m json.tool | head -20
```

If the restoring machine runs on a different host, copy the snapshot file from the
producing machine's host to a directory accessible from the restoring machine.

At this point, the environment can be restored on the producing machine with `make -C deploy restore SNAPSHOT=$LABEL`. To remove it, run
`make -C deploy down FORCE=1`. The `FORCE` flag is required because `make -C deploy down` alone will refuse (with an error) while the
instances hold a snapshot set.
This guard mechanism is intended to prevent accidental deletion of snapshots before they are exported.

## On the restoring machine

The restoring machine needs no render and no node images: the import creates
every instance, including `sim-host`, from the file.

Create a `DIR` variable containing the directory path, accessible from the restoring machine, containing the copied snapshot file.
```sh
DIR=<directory path containing the snapshot file on the restoring machine>
```

### 5. Preconditions

```sh
cd <repo root on the restoring machine>
F=$DIR/$(basename $(ls $DIR/ravensport-snapshot-$LABEL-*.tar))
tar -xOf $F manifest.json | jq -r '"commit \(.commit)\narch \(.arch)\nincus \(.incus_version)"'
git rev-parse HEAD; uname -m; incus version   # the same commit and architecture
git status --short                            # empty
incus list -f csv -c n                        # no RavensPort instances
ls deploy/snapshots/clock-held 2>&1           # does not exist
incus storage info default | grep -E 'space'
```

The import refuses a file produced on another architecture, and refuses to
start while RavensPort instances exist or the storage pool has too little free
space. A different commit produces a warning, not a refusal; resolve it before
continuing, since the checks below assume the producer's build steps. If the
pool has too little space, delete any node images: `incus image list`, then
`incus image delete <alias>`.

Two helper scripts for step 8, written to the restoring machine. The first lists
the time range of a metric and any gap longer than 75 seconds. The FMS polls
the tank gauge and pushes a sample to the log server every 30 seconds
(`FMS_ATG_POLL_INTERVAL`), so a gap longer than two and a half poll intervals
is more than a delayed poll:

```sh
WORK=~/ravensport-checks; mkdir -p $WORK
cat > $WORK/gaps.py <<'EOF'
import sys, json, datetime as dt
ts = sorted(t for line in sys.stdin if line.strip() for t in json.loads(line)["timestamps"])
iso = lambda ms: dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).isoformat(timespec="seconds")
print(f"{len(ts)} samples, {iso(ts[0])} to {iso(ts[-1])}")
for a, b in zip(ts, ts[1:]):
    if b - a > 75_000:
        print(f"gap {iso(a)} -> {iso(b)} ({(b - a) // 1000} s)")
EOF
```

The second groups the security monitoring alerts from a given time onwards into
those before and after the stop:

```sh
cat > $WORK/siem_window.py <<'EOF'
import sys, json, collections, datetime as dt
parse = lambda s: dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
stop, since = parse(sys.argv[1]), parse(sys.argv[2])
groups = {"before stop": collections.Counter(), "after stop": collections.Counter()}
for line in sys.stdin:
    a = json.loads(line)
    t = dt.datetime.strptime(a["timestamp"], "%Y-%m-%dT%H:%M:%S.%f%z")
    if t >= since:
        key = "after stop" if t >= stop else "before stop"
        groups[key][(a["rule"]["id"], a["agent"]["name"], a["rule"]["description"])] += 1
for k, c in groups.items():
    print(f"== {k}")
    for (rid, agent, desc), n in sorted(c.items()):
        print(f"{n:6} {rid:>6} {agent:14} {desc}")
EOF
```

### 6. Import and restore

```sh
make -C deploy restore SNAPSHOT=$F
cat deploy/snapshots/$LABEL/runs.log       # one line: <time> seed=none
```

Do not interrupt the restore: the sim refuses its checkpoint if it starts more
than five minutes after the snapshot time.

### 7. Set the variables for the checks

```sh
STOP=$(cat deploy/snapshots/$LABEL/stopped-at)
NODES=$(incus list -f csv -c n | grep -v '^sim-host$')
```

### 8. Check the restored history

The checks below run commands inside the instances, so they become part of
this restored run. They do not change the snapshot set.

**a. Clock.** The host clock reads the snapshot time plus the time elapsed
since the restore, not real time:

```sh
echo "stopped at $STOP, now $(date -u +%FT%TZ)"; cat deploy/snapshots/clock-held
```

**b. Boots and shutdowns.**

```sh
for n in $NODES; do echo "== $n"; incus exec $n -- journalctl --list-boots --no-pager; done
for n in $NODES; do echo "== $n"; incus exec $n -- journalctl -b -1 -n 4 --no-pager -o short-iso; done
for n in $NODES; do incus exec $n -- bash -c 'echo "$(hostname): last before $(journalctl -b -1 -n 1 -o short-iso --no-pager | cut -c1-25) | first after $(journalctl -b 0 -o short-iso --no-pager | head -1 | cut -c1-25)"'; done
```

- Each node lists exactly two boots: the producer's boot (boot `-1`) and the
  restored boot (boot `0`). No boot of the build container precedes them.
- Boot `-1` began when the producer deployed, and the boots of the nodes began
  within a few minutes of each other. Its last entry is earlier than `$STOP`,
  and its last lines show systemd shutting down cleanly, not a crash.
- Boot `0` begins after `$STOP`.

**c. No clock-step entries.** Every count is `0`:

```sh
for n in $NODES; do printf '%s: ' $n; incus exec $n -- bash -c "{ journalctl -b -1 -o cat; journalctl -b 0 -o cat; } | grep -c -E 'time jump|Time has been changed|Can.t synchronise|System clock wrong'"; done
```

**d. FMS records.**

```sh
incus exec fms-01 -- sudo -u postgres psql -d fms \
  -c "SELECT min(recorded_at), max(recorded_at), count(*) FROM atg_readings;" \
  -c "SELECT count(*) AS jobs FROM jobs;" -c "SELECT count(*) AS transactions FROM transactions;" \
  -c "SELECT recorded_at AS last_before, next_at AS first_after, next_at - recorded_at AS gap
      FROM (SELECT recorded_at, lead(recorded_at) OVER (ORDER BY recorded_at) AS next_at FROM atg_readings) s
      WHERE next_at - recorded_at > interval '75 seconds';"
```

- The readings begin shortly after the start of boot `-1` on `fms-01` and
  continue after `$STOP`.
- The transaction count is greater than zero.
- There is exactly one gap, with `last_before` shortly before `$STOP` and
  `first_after` shortly after it. It runs from the sim's stop to the restored
  FMS's first poll, and lasts about two to three minutes: the restore sets the
  clock to `$STOP` plus five seconds, so the time between the snapshot and the
  restore does not appear in the history. The gap covers only the stop after
  the sim's checkpoint, the restored boot, the readiness probes and the sim's
  start.

**e. Log server and its replica.**

```sh
incus exec hist-01     -- curl -s 'http://127.0.0.1:8428/api/v1/export?match[]=atg_tank_volume_litres'  | python3 $WORK/gaps.py
incus exec hist-dmz-01 -- curl -s 'http://127.0.0.1:18428/api/v1/export?match[]=atg_tank_volume_litres' | python3 $WORK/gaps.py
incus exec hist-01     -- curl -s http://127.0.0.1:9428/select/logsql/query  --data-urlencode 'query=* | stats by (event_type) count() n, min(_time) first, max(_time) last'
incus exec hist-dmz-01 -- curl -s http://127.0.0.1:19428/select/logsql/query --data-urlencode 'query=* | stats by (event_type) count() n, min(_time) first, max(_time) last'
```

- Each metric series shows the same single gap as in check d.
- The event counts on `hist-01` and `hist-dmz-01` agree.

**f. Security monitoring.** Take `SINCE` from the `last_before` time printed in
check d:

```sh
SINCE=<last_before from check d, as 2026-10-30T15:00:00Z>
incus exec siem-01 -- head -c 200 /var/ossec/logs/archives/archives.json   # earliest record near the start of boot -1
incus exec siem-01 -- cat /var/ossec/logs/alerts/alerts.json | python3 $WORK/siem_window.py "$STOP" "$SINCE"
```

- **Before the stop:** each host's agent stops or disconnects. The firewalls
  are the exception, since they stop after the security monitoring manager.
- **After the stop:** the manager starts, and each agent starts and reconnects.
  Nothing else appears, in particular no file-integrity alert caused by the
  restore.

**g. Sim.**

```sh
incus exec sim-host -- ls -l /run/sim/checkpoint-loaded   # the checkpoint was accepted
incus exec sim-host -- tail -n 20 /var/log/sim.log        # a warm start from the checkpoint
incus exec sim-host -- ls -A /var/lib/sim/state           # empty
incus config device list sim-host                         # no sim-state device
```

The media access control (MAC) addresses of the restored instances cannot be
compared with the producer's from the restoring machine. That the export
carries the snapshot's MAC addresses is checked by
`deploy/tests/unit/test_snapshot_scripts.py`.

### 9. Stop the restored run

```sh
date -u                          # time in the restored run
make -C deploy stop              # stops the instances; returns the clock to real time
date -u                          # real time again
```

The instances are stopped but kept, with the snapshot set, so the run can be
started again from the same snapshot.

### 10. Optional: replay the run with a new seed

```sh
make -C deploy restore SNAPSHOT=$LABEL [SEED=<n>]
```

Without `SEED`, the restored run repeats the snapshot's own continuation exactly
(`specs/sim.md` §9.3). With it, the sim draws its tug arrivals from the new
seed. Each restore appends a line to `deploy/snapshots/$LABEL/runs.log`.

### 11. Delete the snapshot set

```sh
make -C deploy down FORCE=1      # only if the restored set is no longer needed
rm -rf deploy/snapshots/$LABEL
```

`make -C deploy down FORCE=1` deletes the instances and their Incus snapshots,
but not the set's directory, which a restore cannot use once the instances are
gone. The snapshot file in the restoring machine's `$DIR` can be imported
and restored again.

## Expected and unexpected findings

`narrative.md` §3 accounts for what a planned maintenance window leaves in the
history:

- one clean shutdown and one start of each port system;
- a single gap in the tank readings, the metrics and the events, covering the
  window;
- no transfer begun during the window;
- the security monitoring recording the systems stopping and starting.

The deployment's readiness probes after the restore are also expected
(`deployment.md` §10.1).

Anything else is an artefact of the snapshot process:

- clock-step entries;
- records out of order across the window;
- more than one gap;
- a node that does not list exactly the producer's boot and the restored
  boot, or a shutdown that is not clean;
- a sim that cold-started;
- the checkpoint device or files left on `sim-host`.
