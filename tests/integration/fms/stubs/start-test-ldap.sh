#!/usr/bin/env bash
# tests/integration/fms/stubs/start-test-ldap.sh
#
# Starts (or stops) a standalone OpenLDAP (slapd) instance for the FMS
# integration test suite.
#
# Usage:
#   start-test-ldap.sh          — start slapd (idempotent; exits 0 if already running)
#   start-test-ldap.sh --stop   — stop the running instance and remove its data directory
#
# Prerequisites (Debian/Ubuntu):
#   apt install slapd ldap-utils
#
# The instance listens on ${LDAP_STUB_PORT} (default 389).  Port 389 requires
# root; set LDAP_STUB_PORT=1389 and update the conftest default to run as a
# non-root user.
#
# After this script starts slapd, the pytest conftest.py seeds operator
# accounts for each test session automatically via ldap3.  No manual account
# creation is required.
#
# Environment variables (match those consumed by the pytest conftest):
#   LDAP_STUB_PORT      — listen port           (default: 389)
#   LDAP_BASE_DN        — directory base DN      (default: dc=ravensport,dc=local)
#   LDAP_BIND_DN        — admin bind DN          (default: cn=admin,${LDAP_BASE_DN})
#   LDAP_BIND_PASSWORD  — admin password         (default: LdapAdmin1!)
#   RUNNER_HOST         — interface to listen on (default: 0.0.0.0)

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

LDAP_PORT="${LDAP_STUB_PORT:-389}"
BASE_DN="${LDAP_BASE_DN:-dc=ravensport,dc=local}"
ADMIN_DN="${LDAP_BIND_DN:-cn=admin,${BASE_DN}}"
ADMIN_PASSWORD="${LDAP_BIND_PASSWORD:-LdapAdmin1!}"
BIND_ADDRESS="${RUNNER_HOST:-0.0.0.0}"

SLAPD_RUNDIR="${TMPDIR:-/tmp}/ravensport-test-slapd"
SLAPD_DATADIR="${SLAPD_RUNDIR}/data"
SLAPD_CONF="${SLAPD_RUNDIR}/slapd.conf"
SLAPD_PIDFILE="${SLAPD_RUNDIR}/slapd.pid"
SLAPD_LOGFILE="${SLAPD_RUNDIR}/slapd.log"
SEED_LDIF="${SLAPD_RUNDIR}/seed.ldif"

# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

die() { echo "ERROR: $*" >&2; exit 1; }
info() { echo "[start-test-ldap] $*"; }

require_binary() {
    command -v "$1" >/dev/null 2>&1 || die "'$1' not found. Install with: apt install ${2:-$1}"
}

slapd_is_running() {
    [[ -f "${SLAPD_PIDFILE}" ]] && kill -0 "$(cat "${SLAPD_PIDFILE}")" 2>/dev/null
}

find_schema_dir() {
    local candidates=(
        /etc/ldap/schema
        /etc/openldap/schema
        /usr/local/etc/openldap/schema
        /usr/share/doc/slapd/examples/schema  # Debian alternative
    )
    for d in "${candidates[@]}"; do
        if [[ -f "${d}/core.schema" ]]; then
            echo "${d}"
            return 0
        fi
    done
    die "Cannot locate OpenLDAP schema directory. Install with: apt install slapd"
}

find_module_dir() {
    local candidates=(
        /usr/lib/ldap
        /usr/lib/openldap
        /usr/local/lib/openldap
        /usr/lib/x86_64-linux-gnu/openldap
        /usr/lib/aarch64-linux-gnu/openldap
    )
    for d in "${candidates[@]}"; do
        if [[ -f "${d}/back_mdb.so" || -f "${d}/back_mdb.la" ]]; then
            echo "${d}"
            return 0
        fi
    done
    # back_mdb may be compiled in; return empty string to skip moduleload
    echo ""
}

wait_for_ldap() {
    local host="${BIND_ADDRESS}"
    [[ "${host}" == "0.0.0.0" ]] && host="127.0.0.1"
    local max_wait=15
    local elapsed=0
    info "Waiting for slapd to accept connections on ${host}:${LDAP_PORT}..."
    while ! ldapsearch -x -H "ldap://${host}:${LDAP_PORT}" \
            -D "${ADMIN_DN}" -w "${ADMIN_PASSWORD}" \
            -b "${BASE_DN}" "(objectClass=*)" dn >/dev/null 2>&1; do
        sleep 0.5
        elapsed=$(( elapsed + 1 ))
        if (( elapsed >= max_wait * 2 )); then
            die "slapd did not become ready within ${max_wait} s. Check ${SLAPD_LOGFILE}"
        fi
    done
    info "slapd is ready."
}

# ---------------------------------------------------------------------------
# Stop
# ---------------------------------------------------------------------------

cmd_stop() {
    if ! slapd_is_running; then
        info "slapd is not running (no PID file at ${SLAPD_PIDFILE})."
        return 0
    fi
    local pid
    pid="$(cat "${SLAPD_PIDFILE}")"
    info "Stopping slapd (PID ${pid})..."
    kill "${pid}"
    local waited=0
    while kill -0 "${pid}" 2>/dev/null; do
        sleep 0.2
        waited=$(( waited + 1 ))
        (( waited < 50 )) || { kill -9 "${pid}" 2>/dev/null || true; break; }
    done
    rm -rf "${SLAPD_RUNDIR}"
    info "slapd stopped and run directory removed."
}

# ---------------------------------------------------------------------------
# Start
# ---------------------------------------------------------------------------

cmd_start() {
    require_binary slapd   "slapd"
    require_binary slapadd "ldap-utils"
    require_binary slappasswd "ldap-utils"
    require_binary ldapsearch "ldap-utils"

    # Ubuntu ships an AppArmor profile for slapd that restricts it to
    # /etc/ldap/, /var/lib/ldap/, and /var/run/slapd/.  Running slapd with a
    # config under /tmp/ produces a silent "Permission denied" failure even
    # when file permissions are correct.  Disable the profile before starting.
    #
    # Checking `aa-status | grep slapd` is unreliable without root (the output
    # is truncated).  Instead, test for the profile file directly and verify it
    # has not been disabled via aa-disable (which leaves a symlink in disable/).
    local _apparmor_profile=/etc/apparmor.d/usr.sbin.slapd
    local _apparmor_disable=/etc/apparmor.d/disable/usr.sbin.slapd
    if command -v aa-status >/dev/null 2>&1 && aa-status --enabled 2>/dev/null; then
        if [[ -f "${_apparmor_profile}" ]] && [[ ! -e "${_apparmor_disable}" ]]; then
            die "AppArmor has an enforcing profile for slapd which will block" \
                "access to ${SLAPD_RUNDIR} (symptoms: empty log file)." \
                "Disable it first: sudo aa-disable /etc/apparmor.d/usr.sbin.slapd"
        fi
    fi

    if slapd_is_running; then
        info "slapd is already running (PID $(cat "${SLAPD_PIDFILE}"))."
        return 0
    fi

    # Remove any leftover run directory from a previous run.  The data
    # directory can exist without a PID file if slapd exited before writing
    # it (e.g. a failed start or SIGKILL).  Always wipe so slapadd begins
    # with an empty database.
    rm -rf "${SLAPD_RUNDIR}"

    mkdir -p "${SLAPD_DATADIR}"
    chmod 700 "${SLAPD_DATADIR}"

    local schema_dir
    schema_dir="$(find_schema_dir)"
    info "Using schema directory: ${schema_dir}"

    local module_dir
    module_dir="$(find_module_dir)"

    local hashed_pw
    hashed_pw="$(slappasswd -s "${ADMIN_PASSWORD}")"

    # -----------------------------------------------------------------------
    # slapd.conf
    # -----------------------------------------------------------------------

    cat > "${SLAPD_CONF}" <<EOF
# Generated by scripts/start-test-ldap.sh — do not edit.

include ${schema_dir}/core.schema
include ${schema_dir}/cosine.schema
include ${schema_dir}/inetorgperson.schema
include ${schema_dir}/nis.schema
EOF

    if [[ -n "${module_dir}" ]]; then
        cat >> "${SLAPD_CONF}" <<EOF

modulepath ${module_dir}
moduleload back_mdb.so
EOF
    fi

    cat >> "${SLAPD_CONF}" <<EOF

pidfile  ${SLAPD_PIDFILE}
argsfile ${SLAPD_RUNDIR}/slapd.args
loglevel 0

database  mdb
suffix    "${BASE_DN}"
rootdn    "${ADMIN_DN}"
rootpw    ${hashed_pw}
directory ${SLAPD_DATADIR}
maxsize   104857600

# All non-root users get read access (which includes auth, needed for bind).
# The rootdn has unlimited access implicitly and must not appear in ACLs.
access to *
    by * read
EOF

    # -----------------------------------------------------------------------
    # Seed LDIF — base DN and people OU only.
    # Operator user accounts are added by the pytest conftest at session start.
    # -----------------------------------------------------------------------

    # Derive the top-level DC value from the base DN (first RDN component).
    local dc_value
    dc_value="$(echo "${BASE_DN}" | sed 's/dc=\([^,]*\).*/\1/')"

    cat > "${SEED_LDIF}" <<EOF
dn: ${BASE_DN}
objectClass: top
objectClass: dcObject
objectClass: organization
o: Ravensport
dc: ${dc_value}

dn: ou=people,${BASE_DN}
objectClass: top
objectClass: organizationalUnit
ou: people
EOF

    # -----------------------------------------------------------------------
    # Initialise the database (must run before slapd starts).
    # -----------------------------------------------------------------------
    info "Seeding directory with base DN and people OU..."
    slapadd -f "${SLAPD_CONF}" -l "${SEED_LDIF}" 2>>"${SLAPD_LOGFILE}" \
        || die "slapadd failed. Check ${SLAPD_LOGFILE}"

    # -----------------------------------------------------------------------
    # Start slapd.
    # -----------------------------------------------------------------------
    local listen_uri="ldap://${BIND_ADDRESS}:${LDAP_PORT}"
    info "Starting slapd on ${listen_uri}..."
    slapd -f "${SLAPD_CONF}" -h "${listen_uri}" 2>>"${SLAPD_LOGFILE}" \
        || die "slapd failed to start. Check ${SLAPD_LOGFILE}"

    wait_for_ldap
    info "slapd PID: $(cat "${SLAPD_PIDFILE}")"
    info "Run directory: ${SLAPD_RUNDIR}"
    info "Log: ${SLAPD_LOGFILE}"
    info "To stop: $0 --stop"
}

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

case "${1:-}" in
    --stop) cmd_stop ;;
    "")     cmd_start ;;
    *)      die "Unknown argument: $1. Usage: $0 [--stop]" ;;
esac
