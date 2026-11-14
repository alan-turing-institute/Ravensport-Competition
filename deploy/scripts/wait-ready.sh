#!/usr/bin/env bash
# Poll each instance until it reports ready.
# Cloud-init nodes: wait for cloud-init status = done.
# Custom-image nodes (no cloud-init): wait for container status = Running.
set -euo pipefail

TIMEOUT="${TIMEOUT:-600}"
INTERVAL=5
INCUS="${INCUS:-incus}"
cd "$(dirname "$0")/.."

if ! command -v jq >/dev/null; then
  echo "jq not found; install with: apt install -y jq" >&2
  exit 2
fi

if [ "$#" -gt 0 ]; then
  NODES=("$@")
else
  mapfile -t NODES < <(jq -r '.nodes[]' build/manifest.json)
fi

has_cloud_init() {
  jq -r '.cloud_init_nodes[]' build/manifest.json 2>/dev/null | grep -qx "$1"
}

for node in "${NODES[@]}"; do
  printf "  %-22s " "$node"
  elapsed=0
  while :; do
    if has_cloud_init "$node"; then
      status=$($INCUS exec "$node" -- cloud-init status 2>/dev/null \
                | awk '/status:/ {print $2}' || true)
      if [ "$status" = "done" ]; then echo "ready"; break; fi
      if [ "$status" = "error" ]; then
        echo "FAILED"
        $INCUS exec "$node" -- cloud-init status --long || true
        exit 1
      fi
    else
      state=$($INCUS info "$node" 2>/dev/null | awk '/^Status:/{print toupper($2)}' || true)
      if [ "$state" = "RUNNING" ]; then echo "ready"; break; fi
    fi
    if [ "$elapsed" -ge "$TIMEOUT" ]; then
      echo "TIMEOUT (last status: ${status:-${state:-unknown}})"
      exit 1
    fi
    sleep "$INTERVAL"
    elapsed=$((elapsed + INTERVAL))
  done
done
