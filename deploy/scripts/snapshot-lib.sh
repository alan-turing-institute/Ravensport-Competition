# shellcheck shell=bash
# Shared by the snapshot-set scripts (deployment.md §8.1, §8.2, §10): the
# rendered files the deployed instances come from, the instance list, the stop
# order of a planned maintenance window, the readiness probes, and the hold on
# the host clock. Sourced, not run; the caller has already changed to deploy/.

INCUS="${INCUS:-incus}"
# Outside build/, so that neither a render nor make clean removes the sets.
SNAPSHOTS_DIR="${SNAPSHOTS_DIR:-snapshots}"
# The snapshot file format version (deployment.md §10.3).
# shellcheck disable=SC2034
SNAPSHOT_FORMAT_VERSION=2
# The sim refuses a checkpoint written longer ago than this (sim.md §9.3).
# shellcheck disable=SC2034
MAX_START_GAP_S=300
# A restore sets the host clock to a set's stop time plus this (§8.2).
# shellcheck disable=SC2034
RESTORE_CLOCK_OFFSET_S=5

# The file that marks a set make snapshot has begun and not completed (§10.2).
# It records each instance's Incus last_used_at when the set was begun, which
# changes whenever the instance is started.
INCOMPLETE_MARKER=incomplete

set_incomplete() {
    [ -e "$SNAPSHOTS_DIR/$1/$INCOMPLETE_MARKER" ]
}

# The labels of every incomplete set, one per line.
incomplete_sets() {
    local marker
    for marker in "$SNAPSHOTS_DIR"/*/"$INCOMPLETE_MARKER"; do
        [ -e "$marker" ] || continue
        basename "$(dirname "$marker")"
    done
}

# The time Incus records for the instance's last start.
last_used_at() {
    "$INCUS" query "/1.0/instances/$1" | jq -er .last_used_at
}

# Refuse an incomplete set, which neither a restore nor an export reads.
require_complete_set() {
    if set_incomplete "$1"; then
        die "'$1' is an incomplete snapshot set; complete it with make -C deploy snapshot LABEL=$1"
    fi
}

die() {
    echo "ERROR: $*" >&2
    exit 1
}

# The rendered files a snapshot set carries (deployment.md §10.2): the
# instance and network list, the commands that create the networks, and the
# commands that remove the instances and networks.
# shellcheck disable=SC2034
RENDERED_FILES=(manifest.json incus-commands.sh teardown-commands.sh)

# The label of the set the deployed instances were restored or imported from.
# Absent after make deploy, whose instances come from build/.
DEPLOYED_SET="$SNAPSHOTS_DIR/deployed-set"

# The directory holding the rendered files the deployed instances come from:
# the set they were restored from, or build/.
rendered_dir() {
    if [ -f "$DEPLOYED_SET" ]; then
        echo "$SNAPSHOTS_DIR/$(cat "$DEPLOYED_SET")/rendered"
    else
        echo build
    fi
}

# The directory holding the inputs those files were rendered from.
inputs_dir() {
    if [ -f "$DEPLOYED_SET" ]; then
        echo "$SNAPSHOTS_DIR/$(cat "$DEPLOYED_SET")/inputs"
    else
        echo build/inputs
    fi
}

# The snapshot file a local set is exported to (deployment.md §10.3).
snapshot_file() {
    local profile
    profile=$(jq -r .profile "$SNAPSHOTS_DIR/$1/rendered/manifest.json")
    echo "$SNAPSHOTS_DIR/ravensport-snapshot-$1-$profile-$(uname -m).tar"
}

# Every deployed RavensPort instance, from the rendered manifest.
ravensport_instances() {
    local manifest
    manifest="$(rendered_dir)/manifest.json"
    test -f "$manifest" || die "$manifest not found — run make render first"
    jq -r '.nodes[]' "$manifest"
}

# The stop order of a planned maintenance window (deployment.md §10.2), as glob
# patterns: the adversary host, and then the systems-administration path,
# workstation then bastion, before the hosts they reach; the applications
# before the stores they write to; the security monitoring manager after every
# agent that reports to it; and the firewalls, which carry agent traffic between
# zones, last. sim-host is stopped first, on its own (§8.1), and is not listed.
STOP_ORDER=(
    'adversary'
    'admin-*' 'dmz-bastion-*'
    'hmi-*' 'recon-*'
    'fms-*'
    'atg-relay-*'
    'hist-0*'
    'hist-dmz-*'
    'corp-it'
    'siem-*'
    'fw-*'
)

# Read instance names on stdin and print every one but sim-host in stop order.
# An instance no pattern places is refused, so that an instance added to the
# topology is placed by the rules above rather than stopped in an arbitrary
# position.
stop_order() {
    local -a instances=() ordered=()
    local instance pattern
    mapfile -t instances
    for pattern in "${STOP_ORDER[@]}"; do
        for instance in "${instances[@]}"; do
            # shellcheck disable=SC2053  # the pattern is matched as a glob
            [[ $instance == $pattern ]] && ordered+=("$instance")
        done
    done
    for instance in "${instances[@]}"; do
        [ "$instance" = sim-host ] && continue
        printf '%s\n' "${ordered[@]}" | grep -qx "$instance" ||
            die "$instance has no place in the stop order; add it to STOP_ORDER in" \
                "scripts/snapshot-lib.sh by the rules of deployment.md §10.2"
    done
    printf '%s\n' "${ordered[@]}"
}

instance_exists() {
    "$INCUS" query "/1.0/instances/$1" >/dev/null 2>&1
}

instance_running() {
    [ "$("$INCUS" query "/1.0/instances/$1" 2>/dev/null | jq -r .status)" = Running ]
}

# ── deployments of the competition profile (deployment.md §10) ─────────────────

# The image sim-host is created from in a deployment of the competition profile,
# made with the competition's additional override file: the competition's entry
# point in place of the simulator's own binary (build-sim-image.sh
# --competition). The entry point starts the simulator only from its start-up
# input and reads no checkpoint, so such a deployment takes no snapshot set and
# is never restored from one.
COMPETITION_SIM_IMAGE=ravensport/sim-competition:latest

# Whether the rendered manifest read on standard input is that of a deployment
# of the competition profile. A manifest that names no image for sim-host,
# rendered before render.py recorded the images, is not.
competition_manifest() {
    [ "$(jq -r '.images["sim-host"] // empty' 2>/dev/null)" = "$COMPETITION_SIM_IMAGE" ]
}

# ── readiness probes (deployment.md §8) ────────────────────────────────────────

# Poll the FMS health endpoint from within fms-01 (loopback).
wait_for_fms() {
    local i
    echo "  Waiting for FMS on fms-01 (up to 120s)..."
    for ((i = 0; i < 24; i++)); do
        if "$INCUS" exec fms-01 -- curl -fsk --max-time 3 \
               https://127.0.0.1/api/v1/health 2>/dev/null | grep -q '"status":"ok"'; then
            echo "  FMS ready"
            return 0
        fi
        sleep 5
    done
    die "FMS did not become ready within 120s"
}

# Poll the HMI health endpoint from fms-01 (both are on vnet-ot).
wait_for_hmi() {
    local i
    echo "  Waiting for HMI on hmi-01 (up to 120s)..."
    for ((i = 0; i < 24; i++)); do
        if "$INCUS" exec fms-01 -- curl -fsk --max-time 3 \
               https://10.40.0.12/health 2>/dev/null | grep -q '"status":"ok"'; then
            echo "  HMI ready"
            return 0
        fi
        sleep 5
    done
    die "HMI did not become ready within 120s"
}

# Open a TLS connection from fms-01 to the directory on corp-it (LDAP over TLS,
# port 636), along the two-hop path of the FMS's operator binds. A completed
# handshake means slapd is listening. Standard input is /dev/null because
# s_client keeps the connection open until its input ends: with the terminal
# as input, every attempt would last until timeout stopped it.
wait_for_directory() {
    local i
    echo "  Waiting for directory on corp-it (up to 120s)..."
    for ((i = 0; i < 24; i++)); do
        if "$INCUS" exec fms-01 -- timeout 3 openssl s_client \
               -connect corp-it:636 -brief </dev/null >/dev/null 2>&1; then
            echo "  Directory ready"
            return 0
        fi
        sleep 5
    done
    die "directory on corp-it did not become ready within 120s"
}

# ── host clock (deployment.md §8.2) ────────────────────────────────────────────

# Present from the moment a restore starts to hold the clock until the clock is
# released, holding the real time the hold began. A restored run is in
# progress while it exists.
CLOCK_HELD="$SNAPSHOTS_DIR/clock-held"

clock_held() {
    [ -f "$CLOCK_HELD" ]
}

require_clock_released() {
    if clock_held; then
        die "a restored run holds the host clock (since $(cat "$CLOCK_HELD")); stop it first with make -C deploy stop"
    fi
}

# The systemd drop-in that denies the Lima guest agent the capability to set
# the time.
GUESTAGENT_DROPIN=/etc/systemd/system/lima-guestagent.service.d/no-set-time.conf

on_lima() {
    systemctl cat lima-guestagent.service >/dev/null 2>&1
}

# Stop everything on the host that sets the clock: the NTP client and, on a
# Lima virtual machine, the guest agent, which can set the time as often as
# every 10 seconds.
hold_clock() {
    if ! clock_held; then
        mkdir -p "$SNAPSHOTS_DIR"
        date -u +%Y-%m-%dT%H:%M:%SZ >"$CLOCK_HELD"
    fi
    sudo timedatectl set-ntp false
    if on_lima; then
        sudo mkdir -p "$(dirname "$GUESTAGENT_DROPIN")"
        printf '[Service]\nCapabilityBoundingSet=~CAP_SYS_TIME\n' |
            sudo tee "$GUESTAGENT_DROPIN" >/dev/null
        sudo systemctl daemon-reload
        sudo systemctl restart lima-guestagent
    fi
}

# Return the clock to real time: the reverse of hold_clock. The marker is
# removed only once every step has succeeded.
release_clock() {
    if on_lima; then
        sudo rm -f "$GUESTAGENT_DROPIN" &&
            sudo systemctl daemon-reload &&
            sudo systemctl restart lima-guestagent || return 1
    fi
    sudo timedatectl set-ntp true || return 1
    rm -f "$CLOCK_HELD"
}
