#!/usr/bin/env bash
# Preflight: fail with an actionable message when Incus is unavailable, rather
# than letting a deployment or integration target die partway through on a raw
# `incus` error. Called as the first prerequisite of the deployment-dependent
# make targets (deploy `up`/`test-integration`, integration `setup`).
set -euo pipefail

INCUS="${INCUS:-incus}"

if ! command -v "$INCUS" >/dev/null 2>&1; then
  cat >&2 <<EOF
Incus not found ($INCUS).
The deploy and integration test suites need a working Incus host. On macOS this
is the Lima virtual machine: start it with
  limactl start --name=ravensport deploy/lima.yaml
and run this from inside it (limactl shell ravensport). To run only the suites
that need neither Incus nor Postgres, use 'make test-local' from the repo root.
EOF
  exit 1
fi

if ! "$INCUS" info >/dev/null 2>&1; then
  cat >&2 <<EOF
Incus is installed ($INCUS) but its daemon is not reachable.
Check the daemon is running and your user is in the 'incus-admin' group. A fresh
login or reboot activates the group; the Lima provisioning does this
automatically, so this usually means the deployment is not running.
EOF
  exit 1
fi
