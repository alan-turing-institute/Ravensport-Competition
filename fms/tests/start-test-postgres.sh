#!/usr/bin/env bash
# fms/tests/start-test-postgres.sh
#
# Starts (or stops) an ephemeral local PostgreSQL cluster for the FMS Rust API
# integration tests (fms/tests/api.rs). Mirrors start-test-ldap.sh.
#
# Usage:
#   start-test-postgres.sh         — start (idempotent; exits 0 if already running)
#   start-test-postgres.sh --stop  — stop the cluster and remove its data directory
#
# Prerequisites:
#   PostgreSQL (initdb / pg_ctl / postgres) must be available. In the intended
#   workflow this is provided by the Lima test VM, which provisions PostgreSQL;
#   these tests are meant to run there, not on a dev machine. On a dev machine,
#   run the database-free suites instead (make test-fms-unit / make test-local).
#   A host that already has PostgreSQL (e.g. CI, via $FMS_TEST_DATABASE_URL) can
#   also run them — installing it locally just to run these is not the workflow.
#
# Listens on 127.0.0.1:<port>, where <port> is taken from $FMS_TEST_DATABASE_URL
# (default postgres://postgres:postgres@127.0.0.1:5432) with a `postgres`
# superuser and trust authentication — so the cluster binds exactly where the
# api.rs tests connect. Each test then creates its own database on this cluster.
# The cluster lives entirely under a throwaway directory and is removed on --stop.

set -euo pipefail

# Derive the listen port from the URL the tests connect to, so the bind port and
# the test's connect port cannot drift apart (one source of truth).
PG_URL="${FMS_TEST_DATABASE_URL:-postgres://postgres:postgres@127.0.0.1:5432}"
PG_PORT="$(printf '%s' "$PG_URL" | sed -E 's#^.*:([0-9]+).*#\1#')"
[[ "$PG_PORT" =~ ^[0-9]+$ ]] || PG_PORT=5432
PG_RUNDIR="${TMPDIR:-/tmp}/ravensport-test-postgres"
PGDATA="${PG_RUNDIR}/data"
PG_LOGFILE="${PG_RUNDIR}/postgres.log"

die()  { echo "ERROR: $*" >&2; exit 1; }
info() { echo "[start-test-postgres] $*"; }

# Debian/Ubuntu install initdb/pg_ctl under /usr/lib/postgresql/<ver>/bin (not on
# PATH); Homebrew under /opt/homebrew. Resolve them, preferring the newest
# installed version so a host with several majors does not pick an old one.
find_pg_bin() {
    local b d
    b="$(command -v "$1" 2>/dev/null || true)"
    [[ -n "$b" ]] && { echo "$b"; return 0; }
    while IFS= read -r d; do
        [[ -x "${d}/$1" ]] && { echo "${d}/$1"; return 0; }
    done < <(printf '%s\n' /usr/lib/postgresql/*/bin /usr/local/pgsql/bin /opt/homebrew/opt/postgresql*/bin | sort -V -r)
    die "PostgreSQL not found (no '$1' on PATH, in /usr/lib/postgresql/*/bin, or under Homebrew).
The Postgres-backed FMS integration tests are meant to run inside the Lima test
VM, which provisions PostgreSQL for them: run them there with
  limactl shell ravensport   # then, from the repo, make test-fms
On a dev machine, run 'make test-local' for the database-free suites (it uses
'make test-fms-unit' for the FMS). Installing PostgreSQL on a dev machine is not
the intended workflow."
}

pg_is_running() {
    [[ -f "${PGDATA}/postmaster.pid" ]] \
        && kill -0 "$(head -1 "${PGDATA}/postmaster.pid")" 2>/dev/null
}

cmd_stop() {
    if pg_is_running; then
        local pid
        pid="$(head -1 "${PGDATA}/postmaster.pid")"
        info "Stopping postgres (PID ${pid})..."
        if ! "$(find_pg_bin pg_ctl)" -D "${PGDATA}" -m fast stop >/dev/null 2>&1; then
            info "fast stop failed; force-killing PID ${pid}."
            kill -9 "${pid}" 2>/dev/null || true
            for _ in $(seq 1 25); do kill -0 "${pid}" 2>/dev/null || break; sleep 0.2; done
        fi
    else
        info "postgres is not running."
    fi
    rm -rf "${PG_RUNDIR}"
    info "postgres stopped and run directory removed."
}

cmd_start() {
    local initdb pgctl
    initdb="$(find_pg_bin initdb)"
    pgctl="$(find_pg_bin pg_ctl)"

    if pg_is_running; then
        info "postgres is already running on 127.0.0.1:${PG_PORT}."
        return 0
    fi

    # Wipe any leftover cluster from a previous run that exited without --stop.
    rm -rf "${PG_RUNDIR}"
    mkdir -p "${PG_RUNDIR}"

    info "Initialising cluster in ${PGDATA}..."
    "${initdb}" -D "${PGDATA}" --username=postgres --auth=trust \
        >>"${PG_LOGFILE}" 2>&1 || die "initdb failed. Check ${PG_LOGFILE}"

    # -w waits for the server to accept connections; fsync=off speeds up the
    # throwaway cluster; the socket dir is kept inside the run directory.
    info "Starting postgres on 127.0.0.1:${PG_PORT}..."
    "${pgctl}" -D "${PGDATA}" -l "${PG_LOGFILE}" -w \
        -o "-p ${PG_PORT} -c listen_addresses=127.0.0.1 -c unix_socket_directories=${PG_RUNDIR} -c fsync=off" \
        start >/dev/null 2>&1 || die "postgres failed to start. Check ${PG_LOGFILE}"

    info "postgres ready on 127.0.0.1:${PG_PORT} (data: ${PGDATA})."
    info "To stop: $0 --stop"
}

case "${1:-}" in
    --stop) cmd_stop ;;
    "")     cmd_start ;;
    *)      die "Unknown argument: $1. Usage: $0 [--stop]" ;;
esac
