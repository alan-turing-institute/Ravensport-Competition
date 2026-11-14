#!/usr/bin/env bash
# Post-provisioning internet hardening checks.
#
# Asserts that zone nodes cannot reach the internet after provisioning
# completes.  All checks are expected to FAIL before the hardening fix is
# applied (outbound egress currently permitted) and to PASS once it lands.
#
# Fw nodes are excluded: they hold the uplink NIC and retain direct internet
# access by design.
#
# Limitation: checks depend on the VM host having outbound internet access.
# On an air-gapped host, connections time out regardless of firewall state;
# results are unreliable and should be disregarded.
#
# Run from deploy/ after `make up`:  bash tests/integration/test_internet_egress.sh

set -uo pipefail

INCUS="${INCUS:-incus}"
TCP_TIMEOUT=5     # WAN round-trip margin
PUBLIC_IP=8.8.8.8 # Google DNS — TCP/53 is always listening; a fast refusal
PUBLIC_PORT=53    # proves the packet reached the public host (egress open)
PASS=0
FAIL=0

pass() { printf "  PASS  %s\n" "$1"; (( PASS++ )) || true; }
fail() { printf "  FAIL  %s\n" "$1"; (( FAIL++ )) || true; }

# internet_blocked: expect the connection to time out (fw node dropping the
# packet).  A successful connect or a fast refusal both prove egress is open.
internet_blocked() {
  local label="$1" src="$2"
  local start elapsed
  start=$(date +%s)
  if $INCUS exec "$src" -- timeout "$TCP_TIMEOUT" bash -c \
      "exec 3<>/dev/tcp/$PUBLIC_IP/$PUBLIC_PORT" 2>/dev/null; then
    fail "$label — connected (internet egress open)"
    return
  fi
  elapsed=$(( $(date +%s) - start ))
  if [ "$elapsed" -ge $(( TCP_TIMEOUT - 1 )) ]; then
    pass "$label"
  else
    fail "$label — refused quickly; packet reached $PUBLIC_IP (internet egress open)"
  fi
}

# internet_blocked_via: nc-based variant for sim-host (Alpine/busybox, no bash).
# Source-binds to SRCIP so sim-host's policy routing selects the correct
# egress interface and fw node.
internet_blocked_via() {
  local label="$1" src="$2" srcip="$3"
  local start elapsed rc
  start=$(date +%s)
  $INCUS exec "$src" -- nc -z -s "$srcip" -w "$TCP_TIMEOUT" \
    "$PUBLIC_IP" "$PUBLIC_PORT" 2>/dev/null
  rc=$?
  elapsed=$(( $(date +%s) - start ))
  if [ "$rc" -eq 0 ]; then
    fail "$label — connected (internet egress open)"
  elif [ "$elapsed" -ge $(( TCP_TIMEOUT - 1 )) ]; then
    pass "$label"
  else
    fail "$label — refused quickly; packet reached $PUBLIC_IP (internet egress open)"
  fi
}

echo "==> Post-provisioning internet hardening"
echo

echo "--- DMZ zone nodes (via fw-ext-dmz) ---"
internet_blocked "atg-relay-01: no internet egress" atg-relay-01
internet_blocked "hist-dmz-01:  no internet egress" hist-dmz-01

echo
echo "--- IT zone nodes (via fw-dmz-it) ---"
internet_blocked "corp-it:  no internet egress" corp-it
internet_blocked "recon-01: no internet egress" recon-01

echo
echo "--- OT zone nodes (via fw-ot-dmz) ---"
internet_blocked "fms-01:  no internet egress" fms-01
internet_blocked "hmi-01:  no internet egress" hmi-01
internet_blocked "hist-01: no internet egress" hist-01

echo
echo "--- sim-host (via fw-ext-dmz, ext-supplier egress 10.10.0.10) ---"
internet_blocked_via "sim-host/ext-supplier: no internet egress" sim-host 10.10.0.10

echo
echo "==> Results: ${PASS} passed, ${FAIL} failed"
[ "$FAIL" -eq 0 ]
