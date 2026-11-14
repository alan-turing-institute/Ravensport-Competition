#!/usr/bin/env bash
# Apply a pre-built set of Incus images to the environment.
# Expects build/incus-commands.sh to exist (run `make render` then
# `make images`, or `make build`, before calling this script).
#
# sim-host is started last, after the FMS, the HMI and the directory on
# corp-it are confirmed ready, so the sim's dock_operator actor finds the full
# authentication chain available on its first attempt (deployment.md §8).
#
# In a competition deployment, the episode runner sets SIM_STATE_DIR to the
# directory holding the simulator's start-up input, startup.json, and the
# directory is attached to the stopped sim-host as a read-only disk device at
# /var/lib/sim/startup before it starts (deployment.md §8). Every other
# deployment leaves SIM_STATE_DIR unset, and attaches nothing.
set -euo pipefail

INCUS="${INCUS:-incus}"

cd "$(dirname "$0")/.."

SIM_STATE_DIR="${SIM_STATE_DIR:-}"

# Readiness probes (wait_for_fms, wait_for_hmi, wait_for_directory), shared
# with restore.sh.
# shellcheck source=scripts/snapshot-lib.sh
source scripts/snapshot-lib.sh

# ── deploy ────────────────────────────────────────────────────────────────────

# New instances started under a held clock would record the snapshot time.
require_clock_released

# The instances now come from build/, not from a snapshot set.
rm -f "$DEPLOYED_SET"

echo "==> Applying build/incus-commands.sh (sim-host deferred)"
# Process substitution (not a pipe) so that bash reads the script from a
# separate fd rather than stdin.  With `grep | bash`, every child process
# (including `incus init`) inherits the pipe as fd 0; incus detects a
# non-terminal stdin and attempts to read YAML instance config from it,
# consuming the remainder of the script and failing to unmarshal.
bash <(grep -v '^incus start sim-host$' build/incus-commands.sh)

echo "==> Waiting for digital world to be ready"
wait_for_fms
wait_for_hmi
wait_for_directory

if [[ -n "$SIM_STATE_DIR" ]]; then
    echo "==> Attaching the simulator's start-up input to sim-host"
    if [[ ! -r "$SIM_STATE_DIR/startup.json" ]]; then
        echo "ERROR: no readable start-up input at $SIM_STATE_DIR/startup.json" >&2
        exit 1
    fi
    # A redeploy over an existing sim-host replaces the device rather than
    # failing on its name. The start-up input has a mount point of its own,
    # apart from the checkpoint's mount directory, /var/lib/sim/state, which
    # only a restore attaches (deployment.md §10).
    "$INCUS" config device remove sim-host startup-input >/dev/null 2>&1 || true
    "$INCUS" config device add sim-host startup-input disk \
        source="$(cd "$SIM_STATE_DIR" && pwd)" path=/var/lib/sim/startup readonly=true
fi

echo "==> Starting sim-host"
"$INCUS" start sim-host

echo
echo "Done."
