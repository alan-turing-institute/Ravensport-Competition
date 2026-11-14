#!/usr/bin/env bash
# Snapshot sets against a live deployment (deployment.md §8.1, §8.2, §10.2,
# §10.3): take a set, export it, restore it twice (the second time with a seed)
# and stop each restored run. tests/unit/test_snapshot_scripts.py covers the
# order of the steps and every refusal against stub commands; this test covers
# what the stubs cannot show:
#   - the sim comes to rest on SIGTERM and writes a checkpoint the restore's
#     sim accepts, with and without a reseed file;
#   - a disk device whose source is in this checkout mounts read-only in the
#     unprivileged sim-host container;
#   - every instance's export, and so its recorded disk usage, is non-empty;
#   - every instance's export carries the MAC addresses of its snapshot, not
#     those Incus gives the temporary copy, and an instance imported from an
#     export keeps them;
#   - the host clock is held near the stop time during a restored run and
#     returned to time synchronisation afterwards;
#   - Incus lists an instance's snapshots in the form make down reads them.
#
# The test stops the running environment, steps the host clock (through sudo),
# and can take up to an hour while the sim comes to rest. It deletes its
# snapshots, set directory, snapshot file and imported instance, and leaves the instances running
# from the state of the set, with the sim cold-started. Requires a deployment
# (make up). Run from deploy/:
#   bash tests/integration/test_snapshot.sh

set -uo pipefail

cd "$(dirname "$0")/../.." || exit 1
# shellcheck source=scripts/snapshot-lib.sh
source scripts/snapshot-lib.sh

LABEL="integration-test-$(date -u +%Y%m%dT%H%M%SZ)"
SET_DIR="$SNAPSHOTS_DIR/$LABEL"
# An instance imported from the snapshot file under a name of its own, to show
# that an import keeps the MAC addresses the export carries.
IMPORTED="snapshot-import-sim-host"
WORK=$(mktemp -d)
FILE="$SNAPSHOTS_DIR/ravensport-snapshot-$LABEL-$(jq -r .profile "$(rendered_dir)/manifest.json")-$(uname -m).tar"
INSTANCES=$(ravensport_instances)
ORDER=$(stop_order <<<"$INSTANCES")
ORIGINAL_SET=$(cat "$DEPLOYED_SET" 2>/dev/null || true)
# The checkpoint format version the sim writes, read from the sim's source,
# from which the sim-host image is built, so that the check follows the sim.
CHECKPOINT_FORMAT_VERSION=$(sed -nE 's/^pub const FORMAT_VERSION: u32 = ([0-9]+);$/\1/p' \
    ../infra/sim/src/checkpoint.rs)
PASS=0
FAIL=0

pass()    { printf "  PASS  %s\n" "$1"; PASS=$((PASS + 1)); }
fail()    { printf "  FAIL  %s\n" "$1"; FAIL=$((FAIL + 1)); }
check()   { if eval "$2"; then pass "$1"; else fail "$1"; fi; }
section() { echo; echo "--- $1 ---"; }

all_instances_are() {
    local instance
    for instance in $INSTANCES; do
        [ "$("$INCUS" query "/1.0/instances/$instance" | jq -r .status)" = "$1" ] || return 1
    done
}

# The MAC addresses Incus records for a network interface, as sorted
# volatile.<interface>.hwaddr=<address> lines.
snapshot_hwaddrs() {
    "$INCUS" query "/1.0/instances/$1/snapshots/$LABEL" |
        jq -r '.config | to_entries[] | select(.key | test("^volatile\\..+\\.hwaddr$"))
            | "\(.key)=\(.value)"' | sort
}
instance_hwaddrs() {
    "$INCUS" query "/1.0/instances/$1" |
        jq -r '.config | to_entries[] | select(.key | test("^volatile\\..+\\.hwaddr$"))
            | "\(.key)=\(.value)"' | sort
}
# Those an instance's export carries, from the instance configuration in its
# backup/index.yaml.
exported_hwaddrs() {
    tar -xOf "$FILE" "instances/$1.tar.zst" | zstd -dc |
        tar -xO --occurrence=1 backup/index.yaml 2>/dev/null |
        sed -nE 's/^ +(volatile\.[^.:]+\.hwaddr): (.+)$/\1=\2/p' | sort -u
}

seconds_from_stop() {
    echo $(($(date -u +%s) - $(date -u -d "$(cat "$SET_DIR/stopped-at")" +%s)))
}

# Checks made while a restored run is in progress.
check_restored_run() {
    check "every instance is running" "all_instances_are Running"
    check "the sim accepted the checkpoint" \
        "$INCUS exec sim-host -- test -e /run/sim/checkpoint-loaded"
    check "the sim service is started" \
        "$INCUS exec sim-host -- rc-service sim status 2>/dev/null | grep -q started"
    check "the checkpoint mount is removed from sim-host" \
        "! $INCUS config device show sim-host | grep -q '^sim-state:'"
    check "time synchronisation is held off" \
        "[ \"\$(timedatectl show -p NTP --value)\" = no ]"
    check "the clock is recorded as held" "clock_held"
    if on_lima; then
        check "the Lima guest agent cannot set the time" "test -f $GUESTAGENT_DROPIN"
    fi
    local elapsed
    elapsed=$(seconds_from_stop)
    check "the host clock is within the start gap of the stop time (${elapsed}s)" \
        "[ $elapsed -ge 0 ] && [ $elapsed -le $MAX_START_GAP_S ]"
}

stop_restored_run() {
    bash scripts/stop.sh >/dev/null || fail "make stop failed"
    check "every instance is stopped" "all_instances_are Stopped"
    check "time synchronisation is restored" \
        "[ \"\$(timedatectl show -p NTP --value)\" = yes ]"
    check "the clock is no longer recorded as held" "! clock_held"
    if on_lima; then
        check "the Lima guest agent may set the time again" "! test -f $GUESTAGENT_DROPIN"
    fi
}

cleanup() {
    local instance
    if instance_running sim-host; then
        bash scripts/stop.sh >/dev/null 2>&1 || true
    fi
    for instance in $INSTANCES; do
        "$INCUS" snapshot delete "$instance" "$LABEL" >/dev/null 2>&1 || true
    done
    "$INCUS" delete --force "$IMPORTED" >/dev/null 2>&1 || true
    rm -rf "$SET_DIR" "$FILE" "$WORK"
    # The instances keep the state of the deleted set, whose rendered files
    # were those the deployment had before the test.
    if [ -n "$ORIGINAL_SET" ]; then
        echo "$ORIGINAL_SET" >"$DEPLOYED_SET"
    else
        rm -f "$DEPLOYED_SET"
    fi
    for instance in $(tac <<<"$ORDER"); do
        instance_running "$instance" || "$INCUS" start "$instance" || true
    done
    # The sim's dock operator logs in through the HMI as soon as it starts, so
    # sim-host starts only once the FMS and HMI are ready, as in deploy.sh.
    if ! instance_running sim-host; then
        (wait_for_fms && wait_for_hmi) || true
        "$INCUS" start sim-host || true
    fi
}
trap cleanup EXIT

if ! all_instances_are Running; then
    echo "ERROR: every instance must be running; deploy with make up first" >&2
    exit 1
fi

section "Taking the set '$LABEL'"
if bash scripts/snapshot.sh "$LABEL"; then
    pass "make snapshot succeeded"
else
    fail "make snapshot failed"
    exit 1
fi
check "every instance is stopped" "all_instances_are Stopped"
for instance in $INSTANCES; do
    check "$instance has the snapshot" \
        "$INCUS query /1.0/instances/$instance/snapshots/$LABEL >/dev/null 2>&1"
done
check "Incus lists the snapshot as make down reads it" \
    "$INCUS query /1.0/instances/sim-host/snapshots | jq -r '.[] | sub(\".*/\"; \"\")' | grep -qx $LABEL"
for file in "${RENDERED_FILES[@]}"; do
    check "the set holds the rendered $file" "test -f $SET_DIR/rendered/$file"
done
check "the set holds the world.yaml it was rendered from" "test -f $SET_DIR/inputs/world.yaml"
check "the checkpoint has the sim's format version (${CHECKPOINT_FORMAT_VERSION:-not found})" \
    "[ -n \"$CHECKPOINT_FORMAT_VERSION\" ] && [ \"\$(jq -r .format_version $SET_DIR/checkpoint.json)\" = \"$CHECKPOINT_FORMAT_VERSION\" ]"
check "the checkpoint was written before the stop time" \
    "[ \$(date -u -d \"\$(jq -r .written_at $SET_DIR/checkpoint.json)\" +%s) -le \$(date -u -d \"\$(cat $SET_DIR/stopped-at)\" +%s) ]"

section "Exporting the set"
if bash scripts/export-snapshot.sh "$LABEL" >/dev/null; then
    pass "make export-snapshot succeeded"
    # shellcheck disable=SC2034  # read by the checks, through eval
    manifest=$(tar -xOf "$FILE" manifest.json)
    check "the file lists every instance" \
        "[ \"\$(jq -r '.instances[].name' <<<\"\$manifest\" | sort)\" = \"\$(sort <<<\"\$INSTANCES\")\" ]"
    check "every instance has a non-zero disk usage" \
        "jq -e '[.instances[].disk_usage_bytes] | all(. > 0)' <<<\"\$manifest\" >/dev/null"
    check "the file carries the rendered files and inputs" \
        "jq -e '[.files[].path] | index(\"rendered/incus-commands.sh\") and index(\"inputs/world.yaml\")' <<<\"\$manifest\" >/dev/null"
    check "no temporary copy remains" \
        "! $INCUS list -f csv -c n | grep -q '^snapshot-export-'"
    for instance in $INSTANCES; do
        expected=$(snapshot_hwaddrs "$instance")
        exported=$(exported_hwaddrs "$instance")
        if [ -n "$expected" ] && [ "$exported" = "$expected" ]; then
            pass "$instance's export carries its snapshot's MAC addresses"
        else
            fail "$instance's export carries its snapshot's MAC addresses"
        fi
    done
    tar -xf "$FILE" -C "$WORK" instances/sim-host.tar.zst
    if "$INCUS" import "$WORK/instances/sim-host.tar.zst" "$IMPORTED" >/dev/null 2>&1; then
        pass "sim-host's export imports"
        expected=$(snapshot_hwaddrs sim-host)
        imported=$(instance_hwaddrs "$IMPORTED")
        if [ -n "$expected" ] && [ "$imported" = "$expected" ]; then
            pass "the imported instance keeps sim-host's MAC addresses"
        else
            fail "the imported instance keeps sim-host's MAC addresses"
        fi
    else
        fail "sim-host's export imports"
    fi
    "$INCUS" delete --force "$IMPORTED" >/dev/null 2>&1 || true
    rm -rf "${WORK:?}/instances"
else
    fail "make export-snapshot failed"
fi

section "Restoring the set"
if bash scripts/restore.sh "$LABEL"; then
    pass "make restore succeeded"
    check_restored_run
else
    fail "make restore failed"
fi

section "Stopping the restored run"
stop_restored_run

section "Restoring the set with SEED=7"
if bash scripts/restore.sh "$LABEL" 7; then
    pass "make restore SEED=7 succeeded"
    check "the mount directory has a reseed file" "test -f $SET_DIR/mount/reseed.json"
    check "the run log records both runs" \
        "[ \"\$(cut -d' ' -f2 $SET_DIR/runs.log | tr '\\n' ' ')\" = 'seed=none seed=7 ' ]"
    check_restored_run
else
    fail "make restore SEED=7 failed"
fi

section "Stopping the restored run"
stop_restored_run

echo
echo "Snapshot tests: $PASS passed, $FAIL failed."
[ "$FAIL" -eq 0 ]
