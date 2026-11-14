#!/usr/bin/env bash
# Take a local snapshot set (deployment.md §10.2): stop the environment in the
# order a planned maintenance window shuts the port down, saving the sim's
# checkpoint (§8.1) and the stop time (§8.2), then snapshot every instance. The
# set keeps the rendered files the instances come from, and the inputs they
# were rendered from, so that it restores without the local render.
#
# Once the sim's checkpoint is confirmed on sim-host, it is the only record of
# the sim's state, so a failure from then on keeps the set, marked incomplete.
# Run on an incomplete set, the script completes it, omitting the steps
# already done. A set whose fuel supplier poll falls due at the start of a
# restored run is taken with a warning, since such a set stops the sim.
#
# Usage: snapshot.sh <label>
set -euo pipefail

cd "$(dirname "$0")/.."
# shellcheck source=scripts/snapshot-lib.sh
source scripts/snapshot-lib.sh

LABEL="${1:-}"
[[ $LABEL =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] ||
    die "LABEL must be letters, digits, '.', '_' and '-', starting with a letter or digit"
SET_DIR="$SNAPSHOTS_DIR/$LABEL"
CHECKPOINT="$SET_DIR/checkpoint.json"
STOPPED_AT_FILE="$SET_DIR/stopped-at"

# On failure the set is removed until the checkpoint is confirmed, and kept
# from then on. A kept set is completed by running the script again, unless
# the failure shows that it cannot be.
complete=false
keep=false
completable=true
on_exit() {
    $complete && return
    if ! $keep; then
        rm -rf "$SET_DIR"
    elif $completable; then
        echo "The snapshot set '$LABEL' is incomplete and is kept in $SET_DIR." >&2
        echo "Once the cause is fixed, complete it with: make -C deploy snapshot LABEL=$LABEL" >&2
    fi
}

cannot_complete() {
    completable=false
    die "$*; the set cannot be completed. Discard it by deleting $SET_DIR" \
        "and the Incus snapshots '$LABEL' already created"
}

has_snapshot() {
    "$INCUS" query "/1.0/instances/$1/snapshots/$LABEL" >/dev/null 2>&1
}

if set_incomplete "$LABEL"; then
    # ── completing an incomplete set ──────────────────────────────────────────
    # The instances are the set's, not those of a later render of build/.
    keep=true
    trap on_exit EXIT
    echo "==> Completing the incomplete snapshot set '$LABEL'"
    MANIFEST="$SET_DIR/rendered/manifest.json"
    test -f "$MANIFEST" || cannot_complete "$MANIFEST not found"
    INSTANCES=$(jq -r '.nodes[]' "$MANIFEST")
    ORDER=$(stop_order <<<"$INSTANCES")
    for instance in $INSTANCES; do
        instance_exists "$instance" || cannot_complete "$instance does not exist"
    done
    if instance_running sim-host; then
        status=$("$INCUS" exec sim-host -- rc-service sim status 2>&1 || true)
        [[ $status == *stopped* ]] ||
            cannot_complete "the sim service on sim-host is '$status', not stopped: the sim has run since its checkpoint"
    fi
    if [ -f "$STOPPED_AT_FILE" ]; then
        for instance in $INSTANCES; do
            if instance_running "$instance"; then
                cannot_complete "$instance has been started since the set's stop time, $(cat "$STOPPED_AT_FILE")"
            fi
        done
    fi
    if [ ! -f "$CHECKPOINT" ]; then
        if ! instance_running sim-host ||
            ! "$INCUS" exec sim-host -- test -f /run/sim/checkpoint.json; then
            cannot_complete "the set lacks the sim's checkpoint and sim-host no longer holds it"
        fi
    fi
    # An instance started since the set was begun, even if stopped again, no
    # longer holds the state the set is completed from. Incus updates an
    # instance's last_used_at on every start, so a change shows a start; the
    # comparison does not depend on the host clock, which a restore sets back.
    for instance in $INSTANCES; do
        recorded=$(awk -v i="$instance" '$1 == i { print $2 }' "$SET_DIR/$INCOMPLETE_MARKER")
        [ -n "$recorded" ] ||
            cannot_complete "the set does not record when $instance was last started," \
                "so it cannot be shown that the environment has not run since the set was begun"
        current=$(last_used_at "$instance") ||
            die "the last start of $instance cannot be read from Incus"
        [ "$current" = "$recorded" ] ||
            cannot_complete "$instance has been started since the set was begun (last started at $current)"
    done
else
    # ── taking a new set ──────────────────────────────────────────────────────
    MANIFEST="$(rendered_dir)/manifest.json"
    if [ -f "$MANIFEST" ] && competition_manifest <"$MANIFEST"; then
        die "a deployment of the competition profile takes no snapshot set: its sim-host runs" \
            "the competition's entry point ($COMPETITION_SIM_IMAGE), which starts only from its" \
            "start-up input and reads no checkpoint (deployment.md §10). Nothing was changed"
    fi
    [ -e "$SET_DIR" ] && die "$SET_DIR already exists; choose another label"
    INSTANCES=$(ravensport_instances)
    ORDER=$(stop_order <<<"$INSTANCES")
    RENDERED=$(rendered_dir)
    INPUTS=$(inputs_dir)
    for file in "${RENDERED_FILES[@]}"; do
        test -f "$RENDERED/$file" || die "$RENDERED/$file not found — run make render first"
    done
    test -d "$INPUTS" ||
        die "$INPUTS not found: the deployment was rendered before render.py recorded its inputs; run make render"
    STARTS=""
    for instance in $INSTANCES; do
        instance_running "$instance" || die "$instance is not running; a set is taken from a running environment"
        if has_snapshot "$instance"; then
            die "$instance already holds an Incus snapshot '$LABEL'; choose another label"
        fi
        used=$(last_used_at "$instance") ||
            die "the last start of $instance cannot be read from Incus"
        STARTS+="$instance $used"$'\n'
    done
    # A sim that has exited cannot come to rest and write a checkpoint.
    status=$("$INCUS" exec sim-host -- rc-service sim status 2>&1 || true)
    [[ $status == *started* ]] ||
        die "the sim service on sim-host is not running but '$status'; /var/log/sim.log on sim-host records why"

    # 1. The set, marked incomplete until its last step. The marker records
    # each instance's last start, against which a completion is checked.
    trap on_exit EXIT
    mkdir -p "$SET_DIR/rendered"
    printf '%s' "$STARTS" >"$SET_DIR/$INCOMPLETE_MARKER"
    for file in "${RENDERED_FILES[@]}"; do
        cp "$RENDERED/$file" "$SET_DIR/rendered/"
    done
    cp -R "$INPUTS" "$SET_DIR/inputs"

    # 2. The sim comes to rest and writes its checkpoint.
    echo "==> Stopping the sim and saving its checkpoint (could take up to an hour)"
    "$INCUS" exec sim-host -- rc-service sim stop ||
        die "the sim service did not stop; see /var/log/sim.log on sim-host"
    status=$("$INCUS" exec sim-host -- rc-service sim status 2>&1 || true)
    [[ $status == *stopped* ]] ||
        die "the sim service is not stopped but '$status': a sim that crashed or was killed wrote no checkpoint"
    "$INCUS" exec sim-host -- test -f /run/sim/checkpoint.json ||
        die "the sim stopped but wrote no checkpoint"
    # From here the checkpoint is the only record of the sim's state.
    keep=true
fi

# ── 2. the checkpoint, copied out before sim-host stops ────────────────────────

if [ ! -f "$CHECKPOINT" ]; then
    # Copied under another name, so that an interrupted copy is not taken for
    # the checkpoint.
    "$INCUS" file pull sim-host/run/sim/checkpoint.json "$CHECKPOINT.tmp"
    mv "$CHECKPOINT.tmp" "$CHECKPOINT"
fi
if instance_running sim-host; then
    "$INCUS" stop sim-host
fi

# ── 3–4. the remaining instances, in the stop order ────────────────────────────

remaining=()
for instance in $ORDER; do
    if instance_running "$instance"; then
        remaining+=("$instance")
    fi
done
if [ ${#remaining[@]} -gt 0 ]; then
    echo "==> Waiting 60s for the FMS to forward its last records"
    sleep 60
    for instance in "${remaining[@]}"; do
        echo "==> Stopping $instance"
        "$INCUS" stop "$instance"
    done
fi

# ── 5. the stop time, kept once recorded ───────────────────────────────────────

if [ ! -f "$STOPPED_AT_FILE" ]; then
    date -u +%Y-%m-%dT%H:%M:%SZ >"$STOPPED_AT_FILE.tmp"
    mv "$STOPPED_AT_FILE.tmp" "$STOPPED_AT_FILE"
fi

# ── 6. the set can be restored ─────────────────────────────────────────────────

WRITTEN_AT=$(jq -r .written_at "$CHECKPOINT")
STOPPED_AT=$(cat "$STOPPED_AT_FILE")
earliest_start=$(($(date -u -d "$STOPPED_AT" +%s) + RESTORE_CLOCK_OFFSET_S))
start_gap=$((earliest_start - $(date -u -d "$WRITTEN_AT" +%s)))
if [ "$start_gap" -gt "$MAX_START_GAP_S" ]; then
    cannot_complete "the sim's checkpoint was written at $WRITTEN_AT and the set stopped at" \
        "$STOPPED_AT; a restore starts the sim at least ${start_gap}s after its checkpoint," \
        "more than the ${MAX_START_GAP_S}s the sim accepts, so no restore of the set can start the sim"
fi

# A fuel supplier poll due less than this after a restore's clock time is made
# as the restored sim starts, while atg-relay-01 still refuses connections, and
# stops the sim (deployment.md §10.2). The set is kept, with a warning: the
# history it holds cannot be taken again.
SUPPLIER_POLL_MARGIN_S=60
POLL_AT=$(jq -r '.fuel_supplier.next_poll_at // empty' "$CHECKPOINT")
poll_warning=false
if [ -n "$POLL_AT" ]; then
    poll_lead=$(($(date -u -d "$POLL_AT" +%s) - earliest_start))
    if [ "$poll_lead" -lt "$SUPPLIER_POLL_MARGIN_S" ]; then
        poll_warning=true
        if [ "$poll_lead" -lt 0 ]; then
            poll_when="$((-poll_lead))s before"
        else
            poll_when="${poll_lead}s after"
        fi
        clock_at=$(date -u -d "@$earliest_start" +%Y-%m-%dT%H:%M:%SZ)
    fi
fi

# ── 7. the Incus snapshots, those already taken kept ───────────────────────────

for instance in $INSTANCES; do
    has_snapshot "$instance" || "$INCUS" snapshot create "$instance" "$LABEL"
done

# ── 8. complete ────────────────────────────────────────────────────────────────

rm -f "$SET_DIR/$INCOMPLETE_MARKER"
complete=true

echo
echo "Snapshot set '$LABEL' taken; stopped at $STOPPED_AT."
echo "Restore it with: make -C deploy restore SNAPSHOT=$LABEL"
if $poll_warning; then
    {
        echo
        echo "WARNING: snapshot set '$LABEL' will probably stop the sim within seconds of every restore,"
        echo "WARNING: so do not export it. The fuel supplier's next poll, due at $POLL_AT, falls"
        echo "WARNING: $poll_when the time a restore sets the clock to ($clock_at). A poll due less than"
        echo "WARNING: ${SUPPLIER_POLL_MARGIN_S}s after that time is made as the restored sim starts, while atg-relay-01"
        echo "WARNING: still refuses connections (deployment.md §10.2)."
    } >&2
fi
