#!/usr/bin/env bash
# Restore a snapshot set and start a run from it (deployment.md §8.1):
# restore every instance to the set, hold the host clock at the set's stop time
# (§8.2), start sim-host from the set's checkpoint, and confirm that the sim
# keeps running. SNAPSHOT is the label of
# a local set, or the path of a snapshot file, which is imported first (§10.4).
# The instance list comes from the set's own rendered files, never from build/.
#
# Usage: restore.sh <label | snapshot file> [seed]
set -euo pipefail

cd "$(dirname "$0")/.."
# shellcheck source=scripts/snapshot-lib.sh
source scripts/snapshot-lib.sh

SNAPSHOT="${1:-}"
SEED="${2:-}"
[ -n "$SNAPSHOT" ] || die "usage: make -C deploy restore SNAPSHOT=<label | snapshot file> [SEED=<n>]"
[ -z "$SEED" ] || [[ $SEED =~ ^[0-9]{1,20}$ ]] || die "SEED must be a non-negative integer"

# The run is identified in the set's run log by the real time of the
# restore, read before the clock is set back to the snapshot time.
RUN="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

# A restore, or an import, replaces the instance state another, incomplete set
# is completed from. The set being restored is refused below if incomplete.
for other in $(incomplete_sets); do
    [ "$other" = "$SNAPSHOT" ] && continue
    die "the snapshot set '$other' is incomplete, and a restore would replace the instance state" \
        "it is completed from; complete it first with make -C deploy snapshot LABEL=$other," \
        "or discard it by deleting $SNAPSHOTS_DIR/$other and the Incus snapshots '$other'"
done

# A set taken from a deployment of the competition profile is never restored
# (deployment.md §10). It is refused before anything is changed: a snapshot
# file before it is imported.
if [ -f "$SNAPSHOT" ]; then
    SET_MANIFEST=$(tar -xOf "$SNAPSHOT" rendered/manifest.json 2>/dev/null || true)
else
    SET_MANIFEST=$(cat "$SNAPSHOTS_DIR/$SNAPSHOT/rendered/manifest.json" 2>/dev/null || true)
fi
if competition_manifest <<<"$SET_MANIFEST"; then
    die "'$SNAPSHOT' was taken from a deployment of the competition profile, whose sim-host runs" \
        "the competition's entry point ($COMPETITION_SIM_IMAGE): such a set is never restored" \
        "(deployment.md §10). Nothing was changed"
fi

if [ -f "$SNAPSHOT" ]; then
    bash scripts/import-snapshot.sh "$SNAPSHOT"
    LABEL=$(tar -xOf "$SNAPSHOT" manifest.json | jq -r .label)
else
    LABEL="$SNAPSHOT"
fi
SET_DIR="$SNAPSHOTS_DIR/$LABEL"

# ── preconditions ──────────────────────────────────────────────────────────────

require_complete_set "$LABEL"
for file in checkpoint.json stopped-at "${RENDERED_FILES[@]/#/rendered/}"; do
    test -f "$SET_DIR/$file" || die "$SET_DIR/$file not found: '$LABEL' is not a snapshot set"
done
INSTANCES=$(jq -r '.nodes[]' "$SET_DIR/rendered/manifest.json")
ORDER=$(stop_order <<<"$INSTANCES")
for instance in $INSTANCES; do
    "$INCUS" query "/1.0/instances/$instance/snapshots/$LABEL" >/dev/null 2>&1 ||
        die "$instance has no snapshot '$LABEL' (make down deletes snapshots; restore the set from its exported file)"
    if instance_running "$instance"; then
        die "$instance is running; stop the current restored run first with make -C deploy stop"
    fi
done

# From here the deployed instances are the set's: make stop, a later
# snapshot and make down use its rendered files.
echo "$LABEL" >"$DEPLOYED_SET"

# ── 1. restore the instances ───────────────────────────────────────────────────

for instance in $INSTANCES; do
    echo "==> Restoring $instance to '$LABEL'"
    "$INCUS" snapshot restore "$instance" "$LABEL"
done

# ── 2. hold and set the host clock (§8.2) ──────────────────────────────────────

STOPPED_AT=$(cat "$SET_DIR/stopped-at")
START_AT=$(date -u -d "$STOPPED_AT + $RESTORE_CLOCK_OFFSET_S seconds" +%Y-%m-%dT%H:%M:%SZ)
echo "==> Holding the host clock and setting it to $START_AT"
hold_clock
sudo date -u -s "$START_AT" >/dev/null

# ── 3–5. the mount directory, mounted read-only on sim-host ────────────────────

# Holds only what the sim reads at start, so that nothing else in the set
# reaches sim-host. Emptied and refilled by every restore, so that a reseed
# file from an earlier run is never read again; the stored checkpoint
# beside it is never modified.
MOUNT_DIR="$SET_DIR/mount"
rm -rf "$MOUNT_DIR"
mkdir "$MOUNT_DIR"
cp "$SET_DIR/checkpoint.json" "$MOUNT_DIR/checkpoint.json"
if [ -n "$SEED" ]; then
    printf '{"tug_arrival_seed": %s}\n' "$SEED" >"$MOUNT_DIR/reseed.json"
fi
# World-readable, so the sim's unprivileged user reads them through the
# container's user namespace without an identity shift.
chmod 0755 "$MOUNT_DIR"
chmod 0644 "$MOUNT_DIR"/*

"$INCUS" config device remove sim-host sim-state >/dev/null 2>&1 || true
"$INCUS" config device add sim-host sim-state disk \
    source="$(cd "$MOUNT_DIR" && pwd)" path=/var/lib/sim/state readonly=true

# ── 6. start the digital world ─────────────────────────────────────────────────

# The reverse of the stop order: the firewalls first, the applications last.
for instance in $(tac <<<"$ORDER"); do
    echo "==> Starting $instance"
    "$INCUS" start "$instance"
done
wait_for_fms
wait_for_hmi
# The dock operator logs in as soon as the sim starts, and the FMS checks its
# password with a bind against the directory on corp-it (deployment.md §8).
wait_for_directory

# ── 7. the checkpoint's age ────────────────────────────────────────────────────

WRITTEN_AT=$(jq -r .written_at "$SET_DIR/checkpoint.json")
NOW=$(date -u +%Y-%m-%dT%H:%M:%SZ)
gap=$(($(date -u -d "$NOW" +%s) - $(date -u -d "$WRITTEN_AT" +%s)))
if [ "$gap" -gt "$MAX_START_GAP_S" ]; then
    die "the restore took too long: it is $NOW and the checkpoint was written at" \
        "$WRITTEN_AT, more than ${MAX_START_GAP_S}s earlier, so the sim would refuse it." \
        "sim-host has not been started; stop the restored run with make -C deploy stop and restore again"
fi

# ── 8. start the sim ───────────────────────────────────────────────────────────

echo "==> Starting sim-host from the checkpoint"
"$INCUS" start sim-host
loaded=false
for ((i = 0; i < 60; i++)); do
    if "$INCUS" exec sim-host -- test -e /run/sim/checkpoint-loaded 2>/dev/null; then
        loaded=true
        break
    fi
    sleep 1
done
if ! $loaded; then
    "$INCUS" exec sim-host -- tail -n 20 /var/log/sim.log >&2 2>/dev/null || true
    die "the sim did not accept the checkpoint within 60s; /var/log/sim.log on sim-host records why"
fi
# Removed so that a later restart of sim-host does not reuse the checkpoint.
"$INCUS" config device remove sim-host sim-state >/dev/null

# ── 9. the sim keeps running ───────────────────────────────────────────────────

# The sim acts at once on every due time that has passed, and retries a failed
# connection for seven seconds before it stops, so a fault in its first
# connections, such as a fuel supplier poll refused at atg-relay-01, has
# stopped it within 30 seconds (deployment.md §8.1).
echo "==> Confirming that the sim is still running after 30s"
sleep 30
status=$("$INCUS" exec sim-host -- rc-service sim status 2>&1 || true)
if [[ $status != *started* ]]; then
    "$INCUS" exec sim-host -- tail -n 20 /var/log/sim.log >&2 2>/dev/null || true
    die "the sim stopped within 30s of accepting the checkpoint: its service is '$status';" \
        "/var/log/sim.log on sim-host records why. Stop the restored run with make -C deploy stop"
fi

# The record of every run started from the set: the set and the seed
# reproduce it.
echo "$RUN seed=${SEED:-none}" >>"$SET_DIR/runs.log"

echo
echo "Restored run $RUN started from '$LABEL'${SEED:+ with SEED=$SEED}; recorded in $SET_DIR/runs.log."
echo "The host clock is held; stop the restored run with: make -C deploy stop"
