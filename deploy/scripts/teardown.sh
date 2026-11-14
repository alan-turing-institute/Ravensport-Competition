#!/usr/bin/env bash
# Remove every RavensPort instance and network (deployment.md §8).
#
# Deleting an instance deletes its snapshots, so a snapshot set that has not
# been exported could no longer be restored, and deleting the instances of a
# restored run leaves the host clock held. Without FORCE=1 the teardown
# therefore changes nothing while a restored run is in progress (§8.2), while
# any instance holds a snapshot (§10.2), or when Incus cannot be queried to
# find out. With
# FORCE=1 it deletes them and then releases a held clock.
#
# Once past those checks the teardown never errors out and always tries
# everything.
#
# Usage: teardown.sh [1]
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1
# shellcheck source=scripts/snapshot-lib.sh
source scripts/snapshot-lib.sh

FORCE="${1:-}"
[ -z "$FORCE" ] || [ "$FORCE" = 1 ] || die "FORCE must be 1"

commands="$(rendered_dir)/teardown-commands.sh"
manifest="$(rendered_dir)/manifest.json"

# ── checks ─────────────────────────────────────────────────────────────────────

if [ "$FORCE" != 1 ]; then
    if clock_held; then
        die "a restored run is in progress (the host clock has been held since $(cat "$CLOCK_HELD"))." \
            "Stop it with make -C deploy stop, which keeps the snapshot sets," \
            "or run make -C deploy down FORCE=1 to delete the instances and their snapshots"
    fi
    if [ -f "$manifest" ]; then
        existing=$("$INCUS" query /1.0/instances 2>/dev/null | jq -r '.[] | sub(".*/"; "")') ||
            die "Incus cannot be queried, so make down cannot check for snapshot sets;" \
                "run make -C deploy down FORCE=1 to tear down without the check"
        declare -A holders=()
        any_instance=false
        for instance in $(jq -r '.nodes[]' "$manifest"); do
            grep -qx "$instance" <<<"$existing" || continue
            any_instance=true
            snapshots=$("$INCUS" query "/1.0/instances/$instance/snapshots" 2>/dev/null |
                jq -r '.[] | sub(".*/"; "")') ||
                die "the snapshots of $instance cannot be queried, so make down cannot check for" \
                    "snapshot sets; run make -C deploy down FORCE=1 to tear down without the check"
            for label in $snapshots; do
                holders[$label]+=" $instance"
            done
        done
        # An incomplete set is completed from the instances, and its checkpoint
        # may be held only on sim-host, so it is named even when no instance
        # holds one of its Incus snapshots yet.
        if $any_instance; then
            for label in $(incomplete_sets); do
                holders[$label]+=""
            done
        fi
        if [ ${#holders[@]} -gt 0 ]; then
            {
                echo "make down deletes the RavensPort instances and their snapshots, after which these"
                echo "snapshot sets can be restored only from an exported snapshot file, and an"
                echo "incomplete set can no longer be completed:"
                for label in $(printf '%s\n' "${!holders[@]}" | sort); do
                    if set_incomplete "$label"; then
                        # An incomplete set is not exported (§10.2).
                        echo "  $label: incomplete (make -C deploy snapshot LABEL=$label)"
                    elif [ -f "$SNAPSHOTS_DIR/$label/rendered/manifest.json" ] &&
                        [ -f "$(snapshot_file "$label")" ]; then
                        echo "  $label: exported to $(snapshot_file "$label")"
                    else
                        echo "  $label: not exported (make -C deploy export-snapshot LABEL=$label)"
                    fi
                done
            } >&2
            die "nothing was changed; run make -C deploy down FORCE=1 to delete them"
        fi
    fi
fi

# ── teardown ───────────────────────────────────────────────────────────────────

# Instances restored from a snapshot set are removed by the set's own commands
# (deployment.md §10.2), which list them as they were rendered.
if [ -f "$commands" ]; then
    bash "$commands"
    rm -f "$DEPLOYED_SET"
else
    echo "No $commands — run render.py first, or delete instances by hand."
fi

# The sim boundary packet filter lives inside the sim-host container and is
# torn down with it; no host-side ruleset to remove.

# With every instance deleted, no host records the step back to real time.
if clock_held; then
    echo "==> Returning the host clock to real time"
    release_clock || echo "WARNING: the host clock could not be released; run make -C deploy stop" >&2
fi

echo "Teardown complete."
