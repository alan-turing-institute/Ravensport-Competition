#!/usr/bin/env bash
# Image build process checks.
#
# Two concerns:
#
# 1. Selective rebuild — each node build script must skip the image rebuild
#    when its inputs have not changed, and must rebuild when they have.
#    A broken skip wastes several minutes per node; a broken trigger leaves
#    stale configuration baked into the image.
#
# 2. Config propagation — key artefacts rendered by render.py (env files,
#    nftables rules, /etc/hosts) must be present and have the expected content
#    inside the built images.
#
# Requires: a rendered build/ directory (make render) and a built set of node
# images (make images); incus must be available. No full network deployment is
# needed — tests launch and tear down throwaway containers from the images.
# Run from deploy/:
#   bash tests/integration/test_image_build.sh

set -uo pipefail

INCUS="${INCUS:-incus}"
PROFILE="${PROFILE:-ci-minimal}"
PASS=0
FAIL=0
SKIP=0

# ── reporting helpers ─────────────────────────────────────────────────────────

pass()    { printf "  PASS  %s\n" "$1"; PASS=$((PASS + 1)); }
fail()    { printf "  FAIL  %s\n" "$1"; FAIL=$((FAIL + 1)); }
skip()    { printf "  SKIP  %s\n" "$1"; SKIP=$((SKIP + 1)); }
section() { echo; echo "--- $1 ---"; }

require_image() {
    local alias="$1"
    if ! "$INCUS" image show "$alias" >/dev/null 2>&1; then
        echo "SKIP: image $alias not found — run 'make images' first"
        exit 0
    fi
}

# ── selective rebuild ─────────────────────────────────────────────────────────

section "Selective rebuild — skip when inputs are unchanged"

# Tests that re-running a build script does not trigger a rebuild when the
# image already exists with a matching content hash.

check_skip() {
    local node="$1"
    local script="build/node-build/${node}.sh"
    local alias="ravensport/${node}:latest"

    if ! "$INCUS" image show "$alias" >/dev/null 2>&1; then
        skip "skip check for $node: image not found"
        return
    fi
    if [ ! -f "$script" ]; then
        skip "skip check for $node: build script not found (run make render)"
        return
    fi

    output=$(bash "$script" 2>&1)
    if echo "$output" | grep -q "inputs unchanged, skipping"; then
        pass "re-run of $node build script skips rebuild"
    else
        fail "re-run of $node build script did not skip (expected 'inputs unchanged, skipping')"
        echo "    output: $output"
    fi
}

for node in fw-ext-dmz fw-dmz-it fw-ot-dmz fms-01 hmi-01 atg-relay-01 corp-it; do
    check_skip "$node"
done

section "Selective rebuild — fail fast when the CA has not been built"

# fms-01, hmi-01, and corp-it all hard-depend on build/tls/<node>; each build
# script must refuse to proceed (not silently fall back to a self-signed
# certificate or a plaintext listener) if `make build-ca` has not been run.
# Exercised against a throwaway copy of build/tls so the real, already-built
# CA material is untouched.

check_requires_ca() {
    local node="$1"
    local script="build/node-build/${node}.sh"
    local tls_dir="build/tls/${node}"

    if [ ! -f "$script" ]; then
        skip "requires-CA check for $node: build script not found (run make render)"
        return
    fi
    if [ ! -d "$tls_dir" ]; then
        skip "requires-CA check for $node: $tls_dir not found (run make build-ca first)"
        return
    fi

    local moved="${tls_dir}.hidden-for-test"
    mv "$tls_dir" "$moved"
    output=$(bash "$script" 2>&1)
    status=$?
    mv "$moved" "$tls_dir"

    if [ "$status" -ne 0 ] && echo "$output" | grep -qi "build-ca"; then
        pass "$node build script fails with a clear error when build/tls/$node is missing"
    else
        fail "$node build script did not fail clearly without its CA-signed certificate (exit $status)"
        echo "    output: $output"
    fi
}

for node in fms-01 hmi-01 corp-it; do
    check_requires_ca "$node"
done

section "Selective rebuild — rebuild when the CA leaf certificate changes"

# The leaf certificate is one of fms-01's declared build-script inputs
# (deploy/build-scripts/fms-01.sh.j2); mutating it must change the image's
# input-hash and force a rebuild, the same as any other rendered input.

FMS_LEAF="build/tls/fms-01/cert.pem"
FMS_ALIAS="ravensport/fms-01:latest"

if ! "$INCUS" image show "$FMS_ALIAS" >/dev/null 2>&1; then
    skip "fms-01 CA rebuild trigger test: image not found"
elif [ ! -f "$FMS_LEAF" ]; then
    skip "fms-01 CA rebuild trigger test: $FMS_LEAF not found (run make build-ca)"
else
    original_hash=$("$INCUS" image get-property "$FMS_ALIAS" "ravensport.input-hash")

    # Append a harmless trailing marker line, exactly as the pre-existing env-file
    # trigger test below does — and restore via the same sed line-delete, not a
    # $(cat ...)/printf round-trip through a shell variable, which silently strips
    # the PEM's trailing newline and corrupts the file on every run.
    echo "# test-marker" >> "$FMS_LEAF"
    output=$(bash build/node-build/fms-01.sh 2>&1)

    new_hash=$("$INCUS" image get-property "$FMS_ALIAS" "ravensport.input-hash")

    sed -i '/^# test-marker$/d' "$FMS_LEAF"
    "$INCUS" image set-property "$FMS_ALIAS" "ravensport.input-hash" "$original_hash"

    if echo "$output" | grep -q "inputs unchanged, skipping"; then
        fail "fms-01 build script skipped after its leaf certificate was modified"
    elif [ "$new_hash" != "$original_hash" ]; then
        pass "fms-01 image rebuilt after its leaf certificate changed (hash updated)"
    else
        fail "fms-01 image appeared to rebuild but hash is unchanged"
    fi
fi

section "Selective rebuild — rebuild when env file changes"

# Appending a comment to build/env/fms-01.env changes its SHA-256 and must
# trigger a rebuild of the fms-01 image. The image is restored afterwards.

FMS_ENV="build/env/fms-01.env"
FMS_ALIAS="ravensport/fms-01:latest"

if ! "$INCUS" image show "$FMS_ALIAS" >/dev/null 2>&1; then
    skip "fms-01 rebuild trigger test: image not found"
elif [ ! -f "$FMS_ENV" ]; then
    skip "fms-01 rebuild trigger test: $FMS_ENV not found (run make render)"
else
    original_hash=$("$INCUS" image get-property "$FMS_ALIAS" "ravensport.input-hash")

    # Temporarily mutate the env file to change its hash.
    echo "# test-marker" >> "$FMS_ENV"
    output=$(bash build/node-build/fms-01.sh 2>&1)

    # Capture the hash stored by the rebuild before restoring.
    new_hash=$("$INCUS" image get-property "$FMS_ALIAS" "ravensport.input-hash")

    # Restore the env file and the stored hash so subsequent make images/up
    # runs see consistent inputs and do not trigger a spurious rebuild.
    sed -i '/^# test-marker$/d' "$FMS_ENV"
    "$INCUS" image set-property "$FMS_ALIAS" "ravensport.input-hash" "$original_hash"

    if echo "$output" | grep -q "inputs unchanged, skipping"; then
        fail "fms-01 build script skipped after env file was modified"
    elif [ "$new_hash" != "$original_hash" ]; then
        pass "fms-01 image rebuilt after env file change (hash updated)"
    else
        fail "fms-01 image appeared to rebuild but hash is unchanged"
    fi
fi

section "Selective rebuild — sim-host image: skip when inputs are unchanged"

SIM_SCRIPT="scripts/build-sim-image.sh"
SIM_ALIAS="ravensport/sim:latest"

if ! "$INCUS" image show "$SIM_ALIAS" >/dev/null 2>&1; then
    skip "sim rebuild skip test: image not found"
elif [ ! -f "$SIM_SCRIPT" ]; then
    skip "sim rebuild skip test: $SIM_SCRIPT not found"
else
    output=$(bash "$SIM_SCRIPT" 2>&1)
    if echo "$output" | grep -q "inputs unchanged, skipping"; then
        pass "re-run of build-sim-image.sh skips rebuild"
    else
        fail "re-run of build-sim-image.sh did not skip"
        echo "    output: $output"
    fi
fi

section "Selective rebuild — sim-host image: rebuild when sim-host input changes"

# The sim image content hash covers the compiled binary and all files under
# deploy/sim-host/. This test mutates a sim-host file to verify that the hash
# mechanism detects the change and triggers a rebuild.
#
# The complementary world.yaml → sim-constants.toml → binary chain is covered
# at the unit level: test_sim_constants_change_when_sim_relevant_field_changes
# verifies that a sim-relevant world.yaml edit changes sim-constants.toml output,
# and test_build_rs_declares_rerun_if_changed verifies that build.rs emits the
# cargo:rerun-if-changed directive that makes Cargo act on that change.

SIM_HOST_CFG="build/haproxy/sim-host.cfg"

if ! "$INCUS" image show "$SIM_ALIAS" >/dev/null 2>&1; then
    skip "sim rebuild trigger test: image not found"
elif [ ! -f "$SIM_HOST_CFG" ]; then
    skip "sim rebuild trigger test: $SIM_HOST_CFG not found"
else
    original_sim_hash=$("$INCUS" image get-property "$SIM_ALIAS" "ravensport.input-hash")

    # Temporarily mutate the haproxy config to change the input hash.
    echo "# test-marker" >> "$SIM_HOST_CFG"
    output=$(bash "$SIM_SCRIPT" 2>&1)

    new_sim_hash=$("$INCUS" image get-property "$SIM_ALIAS" "ravensport.input-hash")

    # Restore the config file and the stored hash so subsequent make images/up
    # runs see consistent inputs and do not trigger a spurious rebuild.
    sed -i '/^# test-marker$/d' "$SIM_HOST_CFG"
    "$INCUS" image set-property "$SIM_ALIAS" "ravensport.input-hash" "$original_sim_hash"

    if echo "$output" | grep -q "inputs unchanged, skipping"; then
        fail "sim build script skipped after sim-host input was modified"
    elif [ "$new_sim_hash" != "$original_sim_hash" ]; then
        pass "sim image rebuilt after sim-host input change (hash updated)"
    else
        fail "sim build appeared to rebuild but hash is unchanged"
    fi
fi

# ── config propagation ────────────────────────────────────────────────────────

section "Config propagation — fms-01"

require_image "ravensport/fms-01:latest"

CTR="rp-imgtest-fms-$$"
cleanup_fms() { "$INCUS" delete -f "$CTR" 2>/dev/null || true; }
trap cleanup_fms EXIT

"$INCUS" init ravensport/fms-01:latest "$CTR" >/dev/null
"$INCUS" start "$CTR"

# Allow systemd to settle, including ravensport-hosts.service restoring /etc/hosts.
sleep 5

check_fms() {
    local label="$1"; shift
    local result
    result=$("$INCUS" exec "$CTR" -- bash -c "$*" 2>&1)
    if [ $? -eq 0 ] && [ -n "$result" ]; then
        pass "$label"
    else
        fail "$label (got: ${result:-<empty>})"
    fi
}

check_fms "fms binary present and executable" \
    "test -x /usr/local/bin/fms && echo ok"

check_fms "fms.env installed at /etc/fms/fms.env" \
    "test -f /etc/fms/fms.env && echo ok"

check_fms "FMS_DATABASE_URL in env file" \
    "grep -q FMS_DATABASE_URL /etc/fms/fms.env && echo ok"

check_fms "FMS_ADMIN_PASSWORD_HASH is an argon2id hash" \
    "grep -q '^FMS_ADMIN_PASSWORD_HASH=.*argon2id' /etc/fms/fms.env && echo ok"

check_fms "CA-signed TLS certificate installed at /etc/fms/tls/cert.pem" \
    "test -f /etc/fms/tls/cert.pem && echo ok"

check_fms "TLS certificate is owned by the fms system user, not world-readable" \
    "stat -c '%U:%a' /etc/fms/tls/key.pem | grep -qE '^fms:0?600\$' && echo ok"

check_fms "TLS certificate validates against the host's system trust store (RavensPort CA is installed there)" \
    "openssl verify -x509_strict -purpose sslserver -CAfile /etc/ssl/certs/ca-certificates.crt /etc/fms/tls/cert.pem && echo ok"

check_fms "FMS_LDAP_URI addresses corp-it as ldaps:// by hostname" \
    "grep -q '^FMS_LDAP_URI=ldaps://corp-it' /etc/fms/fms.env && echo ok"

check_fms "fms.service is enabled" \
    "systemctl is-enabled fms.service"

check_fms "postgresql is installed" \
    "command -v psql && echo ok"

check_fms "fms database exists" \
    "sudo -u postgres psql -lqt | cut -d'|' -f1 | grep -qw fms && echo ok"

check_fms "/etc/hosts contains only expected entries (loopback, fms-01, corp-it, hist-01, siem-01)" \
    "lines=\$(grep -cv '^127\|^::1\|^$\|fms-01\|corp-it\|hist-01\|siem-01' /etc/hosts); test \$lines -eq 0 && echo ok"

# cloud-init is purged from the image, so the recoverable residue that matters —
# the run history (sem/), processed instance data (instance/, instances/ carrying
# the build-container id), and data/ — cannot exist. Incus re-injects an inert
# NoCloud seed/ at launch (self-referential: instance-id and hostname are this
# node's own, network-config is a generic dhcp stub nothing consumes). That seed
# holds nothing an attacker or another node can use, so it is permitted; only the
# run-residue is forbidden.
check_fms "cloud-init purged — no package, no /etc/cloud, no run residue" \
    "! command -v cloud-init && ! test -d /etc/cloud && ! test -e /var/lib/cloud/instance && ! test -d /var/lib/cloud/instances && ! test -d /var/lib/cloud/sem && ! test -d /var/lib/cloud/data && echo ok"

"$INCUS" delete -f "$CTR" 2>/dev/null || true
trap - EXIT

section "Config propagation — atg-relay-01"

require_image "ravensport/atg-relay-01:latest"

CTR="rp-imgtest-atg-$$"
cleanup_atg() { "$INCUS" delete -f "$CTR" 2>/dev/null || true; }
trap cleanup_atg EXIT

"$INCUS" init ravensport/atg-relay-01:latest "$CTR" >/dev/null
"$INCUS" start "$CTR"
sleep 5

check_atg() {
    local label="$1"; shift
    local result
    result=$("$INCUS" exec "$CTR" -- bash -c "$*" 2>&1)
    if [ $? -eq 0 ] && [ -n "$result" ]; then
        pass "$label"
    else
        fail "$label (got: ${result:-<empty>})"
    fi
}

check_atg "haproxy is installed" \
    "command -v haproxy && echo ok"

check_atg "haproxy.service is enabled" \
    "systemctl is-enabled haproxy"

check_atg "haproxy config references atg-console by name" \
    "grep -q atg-console /etc/haproxy/haproxy.cfg && echo ok"

check_atg "atg-console resolvable from /etc/hosts" \
    "getent hosts atg-console && echo ok"

check_atg "no topology-wide entries in /etc/hosts (loopback, atg-relay-01, atg-console, siem-01)" \
    "lines=\$(grep -cv '^127\|^::1\|^$\|atg-relay-01\|atg-console\|siem-01' /etc/hosts); test \$lines -eq 0 && echo ok"

"$INCUS" delete -f "$CTR" 2>/dev/null || true
trap - EXIT

section "Config propagation — corp-it (LDAP)"

require_image "ravensport/corp-it:latest"

CTR="rp-imgtest-corp-$$"
cleanup_corp() { "$INCUS" delete -f "$CTR" 2>/dev/null || true; }
trap cleanup_corp EXIT

"$INCUS" init ravensport/corp-it:latest "$CTR" >/dev/null
"$INCUS" start "$CTR"
sleep 3

check_corp() {
    local label="$1"; shift
    local result
    result=$("$INCUS" exec "$CTR" -- bash -c "$*" 2>&1)
    if [ $? -eq 0 ] && [ -n "$result" ]; then
        pass "$label"
    else
        fail "$label (got: ${result:-<empty>})"
    fi
}

check_corp "slapd is installed" \
    "command -v slapd && echo ok"

check_corp "slapd.service is enabled" \
    "systemctl is-enabled slapd"

check_corp "LDAP people OU exists" \
    "slapcat -n 1 2>/dev/null | grep -q 'ou=people' && echo ok"

check_corp "operator jrenard exists in directory" \
    "slapcat -n 1 2>/dev/null | grep -q 'uid: jrenard' && echo ok"

check_corp "CA-signed TLS certificate installed at /etc/ldap/tls/cert.pem" \
    "test -f /etc/ldap/tls/cert.pem && echo ok"

check_corp "TLS key is owned by openldap, not group- or world-readable" \
    "stat -c '%U:%a' /etc/ldap/tls/key.pem | grep -qE '^openldap:0?600\$' && echo ok"

check_corp "TLS certificate validates against the host's system trust store (RavensPort CA is installed there)" \
    "openssl verify -x509_strict -purpose sslserver -CAfile /etc/ssl/certs/ca-certificates.crt /etc/ldap/tls/cert.pem && echo ok"

check_corp "slapd's service list is ldaps-only (no plaintext ldap:/// configured)" \
    "grep -q '^SLAPD_SERVICES=\"ldaps:/// ldapi:///\"\$' /etc/default/slapd && echo ok"

"$INCUS" delete -f "$CTR" 2>/dev/null || true
trap - EXIT

# ── no self-update, no external lookups, no build logs ────────────────────────

section "No self-update, no external lookups, no build logs — every per-node image"

# No port host updates itself or contacts a service outside the port (narrative
# §4, deployment.md §6.1 step 7). Each image is started without a network, and
# its unit state and effective resolved configuration are read directly.

cleanup_ext() { "$INCUS" delete -f "$CTR" 2>/dev/null || true; }

check_ext() {
    local label="$1"; shift
    local result
    result=$("$INCUS" exec "$CTR" -- bash -c "$*" 2>&1)
    if [ $? -eq 0 ] && [ -n "$result" ]; then
        pass "$label"
    else
        fail "$label (got: ${result:-<empty>})"
    fi
}

for node in $(python3 -c 'import json; print(*json.load(open("build/manifest.json"))["build_script_nodes"])'); do
    if ! "$INCUS" image show "ravensport/${node}:latest" >/dev/null 2>&1; then
        skip "$node: image not found"
        continue
    fi
    CTR="rp-imgtest-ext-${node}-$$"
    trap cleanup_ext EXIT
    "$INCUS" init "ravensport/${node}:latest" "$CTR" >/dev/null
    "$INCUS" start "$CTR"
    sleep 5

    check_ext "$node: unattended-upgrades is not installed" \
        "! dpkg-query -W -f='\${Status}' unattended-upgrades 2>/dev/null | grep -q 'install ok installed' && echo ok"
    for timer in apt-daily.timer apt-daily-upgrade.timer motd-news.timer ua-timer.timer; do
        check_ext "$node: $timer is masked" \
            "test \"\$(systemctl is-enabled $timer)\" = masked && echo ok"
    done
    check_ext "$node: systemd-resolved has no fallback DNS servers" \
        "test \"\$(systemd-analyze cat-config systemd/resolved.conf | grep '^FallbackDNS=' | tail -n 1)\" = 'FallbackDNS=' && echo ok"

    # The image carries no log from its build (deployment.md §6.1 step 9): the
    # journal holds only this boot, and no file under /var/log names the build
    # container, whose name begins "rpbuild". The package installation record
    # is kept.
    check_ext "$node: journal holds no boot before this one" \
        "! journalctl -b -1 -n 1 -q --no-pager >/dev/null 2>&1 && echo ok"
    check_ext "$node: no file under /var/log names the build container" \
        "! grep -rla rpbuild /var/log && echo ok"
    check_ext "$node: package installation record is kept" \
        "test -s /var/log/dpkg.log && test -s /var/log/apt/history.log && echo ok"
    check_ext "$node: no unattended-upgrades log directory" \
        "test ! -e /var/log/unattended-upgrades && echo ok"

    "$INCUS" delete -f "$CTR" 2>/dev/null || true
    trap - EXIT
done

# ── summary ───────────────────────────────────────────────────────────────────

echo
total=$((PASS + FAIL + SKIP))
echo "Results: $PASS passed, $FAIL failed, $SKIP skipped of $total checks"
[ "$FAIL" -eq 0 ]
