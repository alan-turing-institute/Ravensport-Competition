#!/usr/bin/env bash
# Firewall policy integration tests.
#
# Verifies that fw nodes enforce the flows listed in firewall_rules and drop
# everything else.  Three helpers distinguish the three possible outcomes:
#
#   allowed() — expects the service to accept the connection (exit 0).
#               Use only when a service is known to be listening.
#
#   routed()  — expects the packet to reach the destination, whether the
#               service accepts or refuses.  A quick refusal (< TCP_TIMEOUT-1 s)
#               counts as pass; a timeout means the fw node dropped it.
#               Use for permitted flows where the service may not be running.
#
#   blocked() — expects the connection to time out (fw node drops the packet),
#               or to fail immediately with "Network is unreachable" (no route
#               exists on the source node, so the packet never enters the
#               network).  A quick TCP refusal is a failure: the packet reached
#               the destination.  A timeout is ambiguous: it may indicate a
#               firewall DROP or an unreachable destination.  Callers must
#               ensure the destination is live (via a preceding allowed() call
#               on the same source) before relying on a PASS.
#
# Run after `make up` with: bash tests/integration/test_firewall.sh

set -uo pipefail

INCUS="${INCUS:-incus}"
TCP_TIMEOUT=3
PASS=0
FAIL=0

pass() { printf "  PASS  %s\n" "$1"; (( PASS++ )) || true; }
fail() { printf "  FAIL  %s\n" "$1"; (( FAIL++ )) || true; }

# Standard helpers: exec into ubuntu containers that have bash.
allowed() {
  local label="$1" src="$2" dst="$3" port="$4"
  if $INCUS exec "$src" -- timeout "$TCP_TIMEOUT" bash -c \
      "exec 3<>/dev/tcp/$dst/$port" 2>/dev/null; then
    pass "$label"
  else
    fail "$label — connection failed (firewall blocking or service down)"
  fi
}

blocked() {
  local label="$1" src="$2" dst="$3" port="${4:-9999}"
  local rc stderr_out
  stderr_out=$($INCUS exec "$src" -- timeout "$TCP_TIMEOUT" bash -c \
      "exec 3<>/dev/tcp/$dst/$port" 2>&1 1>/dev/null)
  rc=$?
  if [ "$rc" -eq 0 ]; then
    fail "$label — connected (firewall not blocking)"
    return
  fi
  if [ "$rc" -eq 124 ]; then
    pass "$label"
    return
  fi
  if echo "$stderr_out" | grep -qi "unreachable\|no route"; then
    pass "$label"
    return
  fi
  fail "$label — refused quickly; packet reached $dst:$port (firewall not blocking)"
}

# nc-based helpers: for the minimal Alpine sim-host container, which has
# busybox nc but not bash.  The _via variants accept an explicit source IP so
# that source policy routing on sim-host routes via the correct fw node.
allowed_via() {
  local label="$1" src="$2" srcip="$3" dst="$4" port="$5"
  if $INCUS exec "$src" -- nc -z -s "$srcip" -w "$TCP_TIMEOUT" "$dst" "$port" 2>/dev/null; then
    pass "$label"
  else
    fail "$label — connection failed (firewall blocking or service down)"
  fi
}

blocked_via() {
  local label="$1" src="$2" srcip="$3" dst="$4" port="$5"
  local rc stderr_out
  stderr_out=$(timeout "$TCP_TIMEOUT" $INCUS exec -T -n "$src" -- \
      nc -z -s "$srcip" -w $(( TCP_TIMEOUT + 1 )) "$dst" "$port" 2>&1 1>/dev/null)
  rc=$?
  if [ "$rc" -eq 0 ]; then
    fail "$label — connected (firewall not blocking)"
  elif [ "$rc" -eq 124 ]; then
    pass "$label"
  elif echo "$stderr_out" | grep -qi "unreachable\|no route"; then
    pass "$label"
  else
    fail "$label — refused quickly; packet reached $dst:$port (firewall not blocking)"
  fi
}

# routed_via LABEL SRC SRCIP DST PORT
# Source-bound variant of a reachability check: the path must be open, whether
# the service accepts (exit 0) or refuses quickly (packet reached the dst). A
# timeout is a failure (dropped). Used for permitted flows whose destination
# service may not be running (e.g. the dock-operator egress to hmi-01, whose
# binary requires a separate push step and may not be present).
routed_via() {
  local label="$1" src="$2" srcip="$3" dst="$4" port="$5"
  local rc
  timeout "$TCP_TIMEOUT" $INCUS exec -T -n "$src" -- \
    nc -z -s "$srcip" -w $(( TCP_TIMEOUT + 1 )) "$dst" "$port" 2>/dev/null
  rc=$?
  if [ "$rc" -eq 0 ]; then
    pass "$label — connected"
  elif [ "$rc" -eq 124 ]; then
    fail "$label — timed out (firewall dropping or no route)"
  else
    pass "$label — refused (packet reached destination)"
  fi
}

echo "==> Firewall policy tests"
echo

echo "--- Permitted flows (firewall_rules) ---"

# ext -> dmz: sim-host (fuel supplier actor) connects to ATG relay.
# HAProxy is running in both profiles so allowed_via() is appropriate here.
# sim-host has no bash; source-bind to 10.10.0.10 to activate policy routing
# table 100 so the packet enters vnet-ext and traverses fw-ext-dmz correctly.
allowed_via "sim-host/ext-supplier -> atg-relay-01:10001  [ext->dmz]" \
  sim-host 10.10.0.10 10.20.0.10 10001

# dmz -> ot: ATG relay forwards to the ATG console NIC on sim-host (10.40.0.51:10001).
allowed "atg-relay-01 -> sim-host/atg-console:10001  [dmz->ot]" \
  atg-relay-01 atg-console 10001

# it -> dmz: IT zone read-only HTTPS access to the DMZ log-server replica.
# recon-01 (the reconciliation user's workstation) originates this flow.
allowed "recon-01 -> hist-dmz-01:443  [it->dmz]" \
  recon-01 hist-dmz-01 443

# Systems administrator: the actor reaches its workstation within IT, then
# OpenSSH ProxyJump creates the two separately authenticated cross-zone hops.
allowed_via "sim-host/it-sysadmin -> admin-01:22  [it, workstation login]" \
  sim-host 10.30.0.12 10.30.0.40 22
allowed "admin-01 -> dmz-bastion-01:22  [it->dmz, administration]" \
  admin-01 dmz-bastion-01 22
for _managed in fms-01 hist-01 hmi-01; do
  allowed "dmz-bastion-01 -> $_managed:22  [dmz->ot, administration]" \
    dmz-bastion-01 "$_managed" 22
done

# ot -> dmz: log-server replication push. hist-01's vmagent writes metrics to
# the replica's VictoriaMetrics (8428) and its vlagent writes events to the
# replica's VictoriaLogs (9428) — the two rules of deployment.md §4.2. The
# source is hist-01, the node that actually originates the flow.
#
# Addressed by IP: hist-01 carries no hosts entry for the replica, because every
# remote-write target in hist.md §6 is an IP literal. The recon-01 probe above
# keeps the name, which is the form the reconciliation user's browser uses.
allowed "hist-01 -> hist-dmz-01:8428  [ot->dmz, metric replication]" \
  hist-01 10.20.0.20 8428
allowed "hist-01 -> hist-dmz-01:9428  [ot->dmz, event replication]" \
  hist-01 10.20.0.20 9428

echo

echo "--- Appliance reachability and port enforcement ---"

# Each appliance address on sim-host is served by HAProxy on exactly one port;
# any other port simply has nothing listening. Cross-zone access to the ATG
# console is additionally gated by fw-ot-dmz (dmz->ot permits 10.40.0.51:10001
# from atg-relay-01 only), so a wrong port or a different DMZ source is
# dropped at the firewall.
allowed  "atg-relay-01 -> sim-host/atg-console:10001 (permitted)" \
  atg-relay-01 atg-console 10001
blocked  "atg-relay-01 -> sim-host/atg-console:502   (blocked by fw-ot-dmz — 10001 only)" \
  atg-relay-01 atg-console 502
blocked  "hist-dmz-01 -> sim-host/atg-console:10001  (blocked by fw-ot-dmz — atg-relay-01 only)" \
  hist-dmz-01 10.40.0.51 10001

# RFID readers: vessel-rfid:9001 and operator-rfid:9002 are reached intra-zone
# (vnet-ot) and served directly by HAProxy on sim-host.
allowed  "fms-01 -> sim-host/vessel-rfid:9001   (permitted)" \
  fms-01 10.40.0.53 9001
allowed  "hmi-01 -> sim-host/operator-rfid:9002 (permitted)" \
  hmi-01 10.40.0.54 9002

# Dock operator egress: sim-host sources from its ot-operator NIC (10.40.0.55)
# to the HMI. Intra-zone (vnet-ot) and unfirewalled, so the path must be open.
# The HMI binary requires a separate push step and may not be present at test
# time, so a refusal still proves the packet arrived.
routed_via "sim-host/dock-operator -> hmi-01:443  [ot->ot, dock operator egress]" \
  sim-host 10.40.0.55 10.40.0.12 443

echo

# Pre-flight: confirm all nodes involved in blocked-path tests are running.
# blocked() cannot distinguish a firewall DROP from an unreachable destination;
# verify each container is up before attributing any timeout to firewall policy.
for _node in atg-relay-01 sim-host fms-01 hist-01 hmi-01 corp-it \
    admin-01 dmz-bastion-01 recon-01 hist-dmz-01; do
  if $INCUS exec "$_node" -- true 2>/dev/null; then
    pass "pre-flight: $_node is running"
  else
    fail "pre-flight: $_node is not running — blocked-path results unreliable"
  fi
done
echo

echo "--- Blocked paths (no firewall_rule) ---"

# Reverse of the permitted ext->dmz flow.
blocked "atg-relay-01 -> sim-host/ext-supplier  [dmz->ext, no rule]" \
  atg-relay-01 10.10.0.10

# ext <-> ot / ext <-> it
# sim-host is the only node on vnet-ext and also has direct macvlan NICs on
# vnet-ot and a bridged NIC on vnet-it. When sending from the ext-supplier IP
# (10.10.0.10), Linux routes to 10.40.0.0/24 and 10.30.0.0/24 via those
# directly-connected interfaces, bypassing fw-ext-dmz. The ext→ot and ext→it
# blocked directions therefore cannot be tested from sim-host; only the reverse
# directions (below) are testable.
blocked "fms-01 -> sim-host/ext-supplier  [ot->ext, no rule]" \
  fms-01 10.10.0.10
blocked "corp-it -> sim-host/ext-supplier  [it->ext, no rule]" \
  corp-it 10.10.0.10

# it <-> ot
blocked "corp-it -> fms-01  [it->ot, no rule]" \
  corp-it fms-01
blocked "admin-01 -> fms-01  [it->ot, ProxyJump required]" \
  admin-01 fms-01 22
routed_via "liveness: fms-01 -> corp-it:636  [LDAPS port permitted]" \
  fms-01 10.40.0.10 corp-it 636
blocked "fms-01 -> corp-it  [ot->it — LDAPS and NTP only, no general rule]" \
  fms-01 corp-it 9999

# The administration rules are source constrained, not zone-wide SSH holes.
blocked "recon-01 -> dmz-bastion-01:22  [it->dmz, admin-01 only]" \
  recon-01 10.20.0.30 22
blocked "hist-dmz-01 -> fms-01:22  [dmz->ot, bastion only]" \
  hist-dmz-01 10.40.0.10 22
blocked "dmz-bastion-01 -> sim-host/atg-console:22  [dmz->ot, managed hosts only]" \
  dmz-bastion-01 10.40.0.51 22

# The systems-administrator actor address is outbound-only, even to its
# same-zone workstation.
blocked "admin-01 -> sim-host/it-sysadmin:22  [it, egress-only NIC]" \
  admin-01 10.30.0.12 22

echo
echo "==> Results: ${PASS} passed, ${FAIL} failed"
[ "$FAIL" -eq 0 ]
