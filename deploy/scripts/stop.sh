#!/usr/bin/env bash
# Stop a run restored from a snapshot set (deployment.md §8.2): stop the
# instances, then return the host clock to real time. The clock is stepped only
# with every instance stopped, so that no host records the step.
#
# Acts only while the clock is held, that is, while a restored run is in
# progress. A deployment created by make deploy is never stopped here, and its
# sim is never force-stopped.
#
# The run's state is not kept: the next restore replaces every instance's
# filesystem with the set's. An instance that does not stop cleanly within
# STOP_TIMEOUT_S is therefore force-stopped, with a warning that names it. A
# snapshot set is never taken this way; snapshot.sh waits for every clean stop.
set -euo pipefail

cd "$(dirname "$0")/.."
# shellcheck source=scripts/snapshot-lib.sh
source scripts/snapshot-lib.sh

clock_held || die "no restored run is in progress (the host clock is not held):" \
    "make -C deploy stop applies only to an environment restored from a snapshot set"

INSTANCES=$(ravensport_instances)
ORDER=$(stop_order <<<"$INSTANCES")
STOP_TIMEOUT_S=120

# The run's state is not kept, so the sim is not brought to rest.
if instance_running sim-host; then
    echo "==> Stopping sim-host"
    "$INCUS" stop --force sim-host
fi
for instance in $ORDER; do
    if instance_running "$instance"; then
        echo "==> Stopping $instance"
        if ! "$INCUS" stop --timeout "$STOP_TIMEOUT_S" "$instance"; then
            echo "WARNING: $instance did not stop within ${STOP_TIMEOUT_S}s; forcing it to stop" >&2
            "$INCUS" stop --force "$instance"
        fi
    fi
done

echo "==> Returning the host clock to real time"
release_clock
echo "Restored run stopped."
