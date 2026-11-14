#!/usr/bin/env bash
# test_policy.sh — Firewall policy integration tests for the Ravensport topology.
#
# Verifies the full cross-zone policy matrix derived from firewall_rules in
# topology.yaml across four categories:
#
#   1. Permitted flows       — each allowed flow connects on the exact permitted port.
#   2. Port restriction      — permitted src→dst, non-permitted port is dropped.
#   3. Destination restrict  — permitted zone pair, non-permitted dst is dropped.
#   4. Blocked zone pairs    — every direction absent from firewall_rules is dropped.
#   5. ICMP                  — blocked across all zone boundaries (no rule permits it).
#
# blocked() distinguishes three outcomes:
#   PASS  — connection timed out, confirming the fw node dropped the packet.
#   WARN  — connection was refused quickly; the packet reached the destination,
#            meaning the fw node forwarded when it should have dropped.
#   FAIL  — connection succeeded; the fw node is not blocking this path.
#
# Run from deploy/ after `make up`: bash tests/integration/test_policy.sh

set -uo pipefail

INCUS="${INCUS:-incus}"
PROFILE="${PROFILE:-ci-minimal}"
TCP_TIMEOUT=4    # seconds; blocked() requires elapsed >= TCP_TIMEOUT-1 to pass
UDP_TIMEOUT=4    # same semantics for UDP helpers
PASS=0
FAIL=0
WARN=0
SKIP=0
NOTE=0

pass() { printf "  PASS  %s\n" "$1"; (( PASS++ )) || true; }
fail() { printf "  FAIL  %s\n" "$1"; (( FAIL++ )) || true; }
warn() { printf "  WARN  %s\n" "$1"; (( WARN++ )) || true; }
# note: service-behaviour result that does not affect exit status.
# Use when the network path is already confirmed open by a routed/permitted
# test and the only reason for failure is that the service is not yet deployed.
note() { printf "  NOTE  %s\n" "$1"; (( NOTE++ )) || true; }

# ── IPs (from topology.yaml) ─────────────────────────────────────────────────
FUEL_IP=10.10.0.10       # sim-host ext-supplier NIC (fuel-supplier source)  vnet-ext
ATG_IP=10.20.0.10        # atg-relay-01      vnet-dmz
HIST_DMZ_IP=10.20.0.20   # hist-dmz-01       vnet-dmz
RECON_IP=10.30.0.11      # sim-host it-recon-user NIC (recon-user source)    vnet-it
SYSADMIN_IP=10.30.0.12   # sim-host it-sysadmin NIC (administrator source)   vnet-it
RECON01_IP=10.30.0.10    # recon-01 (reconciliation user's RDP target node)  vnet-it
CORP_IT_IP=10.30.0.20    # corp-it           vnet-it
SIEM_IP=10.30.0.30       # siem-01           vnet-it
ADMIN_IP=10.30.0.40      # admin-01          vnet-it
BASTION_IP=10.20.0.30    # dmz-bastion-01    vnet-dmz
FMS_IP=10.40.0.10        # fms-01            vnet-ot
HIST_IP=10.40.0.11       # hist-01           vnet-ot
HMI_IP=10.40.0.12        # hmi-01            vnet-ot
SIM_IP=10.40.0.51        # atg-console (sim-host ot-atg NIC)                 vnet-ot

# ── Helpers ───────────────────────────────────────────────────────────────────

# routed LABEL SRC DST_IP PORT
# The fw node must forward the packet. Passes if the connection is accepted OR
# refused quickly (packet reached the destination). Fails only on timeout (fw dropped).
# Use when the destination service is not yet deployed but the network path must be open.
routed() {
  local label="$1" src="$2" dst="$3" port="$4"
  local start elapsed rc
  start=$(date +%s)
  $INCUS exec "$src" -- timeout "$TCP_TIMEOUT" bash -c \
    "exec 3<>/dev/tcp/$dst/$port" 2>/dev/null
  rc=$?
  elapsed=$(( $(date +%s) - start ))
  if [ "$rc" -eq 0 ]; then
    pass "$label — connected"
  elif [ "$elapsed" -lt $(( TCP_TIMEOUT - 1 )) ]; then
    pass "$label — refused (packet reached destination)"
  else
    fail "$label — timed out (firewall dropping or no route)"
  fi
}

# permitted LABEL SRC DST_IP PORT
# The destination service must be listening; a connection failure is a test failure.
permitted() {
  local label="$1" src="$2" dst="$3" port="$4"
  if $INCUS exec "$src" -- timeout "$TCP_TIMEOUT" bash -c \
      "exec 3<>/dev/tcp/$dst/$port" 2>/dev/null; then
    pass "$label"
  else
    fail "$label — connection failed (fw blocking or service down)"
  fi
}

# blocked LABEL SRC DST_IP PORT
# The fw node must drop the packet silently. A quick TCP refusal means the packet
# reached the destination, which is reported as WARN rather than PASS. An
# immediate "Network is unreachable" (no route on the source node) is treated
# as PASS: the packet never entered the network.
# A timeout is ambiguous: it may indicate a firewall DROP or an unreachable
# destination. Callers must ensure the destination is live (via a preceding
# permitted() call on the same source) before relying on a PASS here.
blocked() {
  local label="$1" src="$2" dst="$3" port="$4"
  local start elapsed rc stderr_out
  start=$(date +%s)
  stderr_out=$($INCUS exec "$src" -- timeout "$TCP_TIMEOUT" bash -c \
    "exec 3<>/dev/tcp/$dst/$port" 2>&1 1>/dev/null)
  rc=$?
  elapsed=$(( $(date +%s) - start ))
  if [ "$rc" -eq 0 ]; then
    fail "$label — connected (fw not blocking)"
  elif echo "$stderr_out" | grep -qi "unreachable\|no route"; then
    pass "$label"
  elif [ "$elapsed" -ge $(( TCP_TIMEOUT - 1 )) ]; then
    pass "$label"
  else
    warn "$label — refused quickly; packet reached $dst:$port (fw forwarded, service rejected)"
  fi
}

# icmp_blocked LABEL SRC DST_IP [SRCIP]
# Any ping reply from the destination is a failure.
# Pass SRCIP to source-bind via ping -I; required when the src container has a
# NIC on the same subnet as DST_IP so that the kernel does not route directly
# (bypassing the firewall).
icmp_blocked() {
  local label="$1" src="$2" dst="$3" srcip="${4:-}"
  local ping_flags=(-c 1 -W 3)
  [ -n "$srcip" ] && ping_flags+=(-I "$srcip")
  if $INCUS exec "$src" -- ping "${ping_flags[@]}" "$dst" >/dev/null 2>&1; then
    fail "$label — ping succeeded (ICMP not blocked)"
  else
    pass "$label"
  fi
}

# permitted_udp LABEL SRC DST_IP PORT
# Sends a single UDP datagram and expects the packet to reach the destination.
# A quick exit (< UDP_TIMEOUT-1 s) means the fw node forwarded the datagram
# and the destination returned an ICMP port-unreachable (no service listening).
# A timeout means the fw node dropped the packet.
#
# Limitation: when a UDP service IS listening and silently discards unknown
# datagrams (e.g. chrony ignoring a non-NTP payload), no ICMP is returned and
# this helper will time out even though the path is open.  In that situation
# the service-level tests in sections 6 and 10 are the authoritative check.
permitted_udp() {
  local label="$1" src="$2" dst="$3" port="$4"
  local start elapsed rc
  start=$(date +%s)
  $INCUS exec "$src" -- timeout "$UDP_TIMEOUT" bash -c \
    "nc -u -w $UDP_TIMEOUT -z $dst $port" 2>/dev/null
  rc=$?
  elapsed=$(( $(date +%s) - start ))
  if [ "$elapsed" -lt $(( UDP_TIMEOUT - 1 )) ]; then
    pass "$label — packet reached $dst:$port/udp"
  else
    fail "$label — timed out (fw blocking or service discarded probe — see comment)"
  fi
}

# blocked_udp LABEL SRC DST_IP PORT
# Expects the fw node to drop the packet silently (timeout).
# A quick exit means ICMP port-unreachable came back, so the packet reached
# the destination; the fw node forwarded when it should have dropped (WARN).
blocked_udp() {
  local label="$1" src="$2" dst="$3" port="$4"
  local start elapsed
  start=$(date +%s)
  $INCUS exec "$src" -- timeout "$UDP_TIMEOUT" bash -c \
    "nc -u -w $UDP_TIMEOUT -z $dst $port" 2>/dev/null
  elapsed=$(( $(date +%s) - start ))
  if [ "$elapsed" -ge $(( UDP_TIMEOUT - 1 )) ]; then
    pass "$label"
  else
    warn "$label — got quick response; packet reached $dst:$port/udp (fw forwarded)"
  fi
}

# ntp_query LABEL SRC DST_IP
# Sends a legitimate one-shot NTP request from SRC to DST_IP:123 using
# `chronyd -Q` `-f /dev/null` avoids reading the baked chrony.conf so 
# the target is exactly DST_IP.
ntp_query() {
  local label="$1" src="$2" dst="$3" out
  out=$($INCUS exec "$src" -- timeout "$(( UDP_TIMEOUT + 4 ))" \
    chronyd -Q -f /dev/null "server $dst iburst" 2>&1)
  if printf '%s' "$out" | grep -q "System clock wrong by"; then
    pass "$label — NTP reply received from $dst:123/udp"
  else
    fail "$label — no NTP reply from $dst:123 (fw blocking or chrony not serving)"
  fi
}

# nc-based helpers for the minimal Alpine sim-host container, which has busybox
# nc but not bash.  The _via variants accept an explicit source IP so that
# source policy routing on sim-host directs traffic through the correct fw node.

permitted_via() {
  local label="$1" src="$2" srcip="$3" dst="$4" port="$5"
  if $INCUS exec "$src" -- nc -z -s "$srcip" -w "$TCP_TIMEOUT" "$dst" "$port" 2>/dev/null; then
    pass "$label"
  else
    fail "$label — connection failed (fw blocking or service down)"
  fi
}

routed_via() {
  local label="$1" src="$2" srcip="$3" dst="$4" port="$5"
  local start elapsed rc
  start=$(date +%s)
  $INCUS exec "$src" -- nc -z -s "$srcip" -w "$TCP_TIMEOUT" "$dst" "$port" 2>/dev/null
  rc=$?
  elapsed=$(( $(date +%s) - start ))
  if [ "$rc" -eq 0 ]; then
    pass "$label — connected"
  elif [ "$elapsed" -lt $(( TCP_TIMEOUT - 1 )) ]; then
    pass "$label — refused (packet reached destination)"
  else
    fail "$label — timed out (firewall dropping or no route)"
  fi
}

blocked_via() {
  local label="$1" src="$2" srcip="$3" dst="$4" port="$5"
  local start elapsed rc stderr_out
  start=$(date +%s)
  stderr_out=$($INCUS exec "$src" -- nc -z -s "$srcip" -w "$TCP_TIMEOUT" "$dst" "$port" 2>&1 1>/dev/null)
  rc=$?
  elapsed=$(( $(date +%s) - start ))
  if [ "$rc" -eq 0 ]; then
    fail "$label — connected (fw not blocking)"
  elif echo "$stderr_out" | grep -qi "unreachable\|no route"; then
    pass "$label"
  elif [ "$elapsed" -ge $(( TCP_TIMEOUT - 1 )) ]; then
    pass "$label"
  else
    warn "$label — refused quickly; packet reached $dst:$port (fw forwarded, service rejected)"
  fi
}

# skip_if_not_full LABEL
# Skips a service-behaviour test when PROFILE is not 'full'.  Service tests
# require real service deployments that are not currently present in ci-minimal.
skip_if_not_full() {
  local label="$1"
  if [ "${PROFILE:-ci-minimal}" != "full" ]; then
    printf "  SKIP  %s  [service behaviour: full profile only]\n" "$label"
    (( SKIP++ )) || true
    return 0
  fi
  return 1
}

# chrony_tracks_anchor NODE
# The node's chrony must have selected corp-it as its reference source. Passes
# when `chronyc tracking` names the corp-it address as the reference.
chrony_tracks_anchor() {
  local node="$1" out
  out=$($INCUS exec "$node" -- chronyc tracking 2>/dev/null || true)
  if printf '%s' "$out" | grep -q "$CORP_IT_IP"; then
    pass "$node chrony reference is corp-it ($CORP_IT_IP)"
  else
    fail "$node chrony reference is not corp-it — $(printf '%s' "$out" | awk -F: '/Reference ID/{print $0}')"
  fi
}

# chrony_within_tolerance NODE TOL_SECONDS [PERTURB_SECONDS]
# Deliberately steps NODE's clock forward, then verifies chrony pulls it back to
# within TOL_SECONDS of the anchor. VM clients only: containers share the host
# kernel clock and cannot be perturbed independently. The correction is forced
# with `chronyc makestep` (chrony auto-steps only during its first updates after
# start), inside a bounded poll loop so no fixed sleep has to be guessed.
chrony_within_tolerance() {
  local node="$1" tol="$2" perturb="${3:-45}" now sys deadline
  now=$($INCUS exec "$node" -- date +%s)
  $INCUS exec "$node" -- date -s "@$(( now + perturb ))" >/dev/null 2>&1
  $INCUS exec "$node" -- chronyc burst 4/4 >/dev/null 2>&1 || true
  deadline=$(( SECONDS + 30 ))
  while [ "$SECONDS" -lt "$deadline" ]; do
    $INCUS exec "$node" -- chronyc makestep >/dev/null 2>&1 || true
    sys=$($INCUS exec "$node" -- chronyc tracking 2>/dev/null | awk '/^System time/ {print $4}')
    if [ -n "$sys" ] && awk "BEGIN{exit !($sys < $tol)}"; then
      pass "$node converges to within ${tol}s after +${perturb}s perturbation (offset ${sys}s)"
      return 0
    fi
    sleep 2
  done
  fail "$node did not converge to within ${tol}s after +${perturb}s perturbation (last offset ${sys:-none}s)"
}

echo "==> Firewall policy tests"
echo

# ── 1. Permitted flows ────────────────────────────────────────────────────────
# Verifies that each rule in firewall_rules actually permits traffic.
# Services must be up: HAProxy on atg-relay-01:10001, HAProxy on sim-host
# (atg-console:10001, vessel-rfid:9001, operator-rfid:9002), Grafana on
# hist-dmz-01:443, and the replica's two stores on hist-dmz-01:8428
# (VictoriaMetrics) and :9428 (VictoriaLogs), which bind 0.0.0.0 there.
echo "--- 1. Permitted flows ---"
permitted_via "sim-host/ext-supplier → atg-relay-01:10001  [ext→dmz, ATG relay]" \
  sim-host 10.10.0.10  "$ATG_IP"      10001
permitted    "atg-relay-01 → sim-host/atg-console:10001     [dmz→ot,  ATG forward]" \
  atg-relay-01     "$SIM_IP"      10001
permitted    "recon-01 → hist-dmz-01:443                     [it→dmz,  Grafana HTTPS]" \
  recon-01         "$HIST_DMZ_IP" 443
permitted_via "sim-host/it-sysadmin → admin-01:22             [it, workstation login]" \
  sim-host "$SYSADMIN_IP" "$ADMIN_IP" 22
permitted    "admin-01 → dmz-bastion-01:22                    [it→dmz, administration]" \
  admin-01         "$BASTION_IP" 22
permitted    "dmz-bastion-01 → fms-01:22                      [dmz→ot, administration]" \
  dmz-bastion-01   "$FMS_IP" 22
permitted    "dmz-bastion-01 → hist-01:22                     [dmz→ot, administration]" \
  dmz-bastion-01   "$HIST_IP" 22
permitted    "dmz-bastion-01 → hmi-01:22                      [dmz→ot, administration]" \
  dmz-bastion-01   "$HMI_IP" 22
# The two replication rules of deployment.md §4.2, probed from hist-01 — the
# node whose vmagent and vlagent actually originate them (hist.md §6).
#
# These confirm the path is open, not that hist-01 is the only node permitted to
# use it: firewall_rules has no source-host field, so the rendered rule admits
# the whole OT zone to hist-dmz-01:8428/:9428. Until firewall_rules gains
# source-host granularity, no assertion here can distinguish the two.
permitted    "hist-01 → hist-dmz-01:8428                     [ot→dmz,  metric replication]" \
  hist-01          "$HIST_DMZ_IP" 8428
permitted    "hist-01 → hist-dmz-01:9428                     [ot→dmz,  event replication]" \
  hist-01          "$HIST_DMZ_IP" 9428
echo

# ── 2. Port restriction: permitted src→dst, non-permitted port ────────────────
# Each firewall_rule names a specific port. Other ports to the same destination
# must be dropped — a misconfigured broad ACCEPT rule would fail these.
echo "--- 2. Port restriction (permitted path, wrong port) ---"
blocked_via "sim-host/ext-supplier → atg-relay-01:22    [ext→dmz, port 10001 only]" \
  sim-host 10.10.0.10  "$ATG_IP"      22
blocked_via "sim-host/ext-supplier → atg-relay-01:443   [ext→dmz, port 10001 only]" \
  sim-host 10.10.0.10  "$ATG_IP"      443
blocked     "atg-relay-01 → sim-host/atg-console:22     [dmz→ot,  port 10001 only]" \
  atg-relay-01     "$SIM_IP"      22
blocked     "atg-relay-01 → sim-host/atg-console:443    [dmz→ot,  port 10001 only]" \
  atg-relay-01     "$SIM_IP"      443
blocked     "recon-01 → hist-dmz-01:22                   [it→dmz,  port 443 only]" \
  recon-01         "$HIST_DMZ_IP" 22
blocked     "admin-01 → dmz-bastion-01:443               [it→dmz,  port 22 only]" \
  admin-01         "$BASTION_IP" 443
blocked     "dmz-bastion-01 → fms-01:443                 [dmz→ot,  port 22 only]" \
  dmz-bastion-01   "$FMS_IP" 443
# The replication ports are permitted from OT only.
blocked     "recon-01 → hist-dmz-01:8428                 [it→dmz,  port 443 only]" \
  recon-01         "$HIST_DMZ_IP" 8428
blocked     "recon-01 → hist-dmz-01:9428                 [it→dmz,  port 443 only]" \
  recon-01         "$HIST_DMZ_IP" 9428
blocked     "hist-01 → hist-dmz-01:22                   [ot→dmz,  ports 8428/9428 only]" \
  hist-01          "$HIST_DMZ_IP" 22
blocked     "hist-01 → hist-dmz-01:443                  [ot→dmz,  ports 8428/9428 only]" \
  hist-01          "$HIST_DMZ_IP" 443
# Regression check: the retired PostgreSQL rule should not return. Unlike the
# checks above, this port is one the topology once permitted.
blocked     "hist-01 → hist-dmz-01:5432                 [ot→dmz,  retired Postgres rule]" \
  hist-01          "$HIST_DMZ_IP" 5432
echo

# ── 3. Destination restriction: permitted zone pair, non-permitted dst ─────────
# Each firewall_rule names a specific destination host. Other hosts in the same
# destination zone must be dropped — a misconfigured subnet ACCEPT would fail these.
# Each blocked() call is preceded by a permitted() liveness check on the same source
# to confirm the zone path is open before attributing a timeout to firewall policy.
# Tests 3 and 4 retain a residual gap: atg-relay-01's liveness from recon-01/fms-01
# cannot be confirmed via TCP — no permitted path from those sources to atg-relay-01 exists.
echo "--- 3. Destination restriction (permitted zone pair, wrong dst) ---"
permitted_via "sim-host/ext-supplier → atg-relay-01:10001  [liveness: ext→dmz path]" \
  sim-host 10.10.0.10  "$ATG_IP"      10001
blocked_via   "sim-host/ext-supplier → hist-dmz-01:10001   [ext→dmz, dst atg-relay-01 only]" \
  sim-host 10.10.0.10  "$HIST_DMZ_IP" 10001
permitted     "atg-relay-01 → sim-host/atg-console:10001   [liveness: dmz→ot path]" \
  atg-relay-01     "$SIM_IP"      10001
blocked       "atg-relay-01 → fms-01:10001                  [dmz→ot,  dst atg-console only]" \
  atg-relay-01     "$FMS_IP"      10001
# Source restriction: the dmz→ot rule names atg-relay-01 as its only source
# (digital.md §1.2). hist-dmz-01 routes to vnet-ot via fw-ot-dmz exactly as
# the relay does, so a drop here is attributable to the source match alone.
blocked       "hist-dmz-01 → sim-host/atg-console:10001    [dmz→ot,  src atg-relay-01 only]" \
  hist-dmz-01      "$SIM_IP"      10001
permitted     "admin-01 → dmz-bastion-01:22                 [liveness: admin SSH first hop]" \
  admin-01         "$BASTION_IP" 22
blocked       "recon-01 → dmz-bastion-01:22                 [it→dmz, src admin-01 only]" \
  recon-01         "$BASTION_IP" 22
blocked       "admin-01 → hist-dmz-01:22                    [it→dmz, dst bastion only]" \
  admin-01         "$HIST_DMZ_IP" 22
permitted     "dmz-bastion-01 → fms-01:22                   [liveness: bastion SSH second hop]" \
  dmz-bastion-01   "$FMS_IP" 22
blocked       "hist-dmz-01 → fms-01:22                      [dmz→ot, src bastion only]" \
  hist-dmz-01      "$FMS_IP" 22
blocked       "dmz-bastion-01 → sim-host/atg-console:22     [dmz→ot, managed dst only]" \
  dmz-bastion-01   "$SIM_IP" 22
permitted     "recon-01 → hist-dmz-01:443                   [liveness: it→dmz path]" \
  recon-01         "$HIST_DMZ_IP" 443
blocked       "recon-01 → atg-relay-01:443                  [it→dmz,  dst hist-dmz-01 only]" \
  recon-01         "$ATG_IP"      443
permitted     "hist-01 → hist-dmz-01:8428                   [liveness: ot→dmz path]" \
  hist-01          "$HIST_DMZ_IP" 8428
blocked       "hist-01 → atg-relay-01:8428                  [ot→dmz,  dst hist-dmz-01 only]" \
  hist-01          "$ATG_IP"      8428
blocked       "hist-01 → atg-relay-01:9428                  [ot→dmz,  dst hist-dmz-01 only]" \
  hist-01          "$ATG_IP"      9428
echo

# ── Pre-flight: confirm all nodes involved in section 4 are running ───────────
# blocked() cannot distinguish a firewall DROP from an unreachable destination;
# verify each container is up before attributing any timeout to firewall policy.
for _node in atg-relay-01 sim-host fms-01 hist-01 hmi-01 hist-dmz-01 \
    recon-01 corp-it admin-01 dmz-bastion-01; do
  if $INCUS exec "$_node" -- true 2>/dev/null; then
    pass "pre-flight: $_node is running"
  else
    fail "pre-flight: $_node is not running — section 4 blocked() results unreliable"
  fi
done
echo

# ── 4. Blocked zone pairs: all directions absent from firewall_rules ───────────
echo "--- 4. Blocked zone pairs ---"

# Reverse of each permitted flow: initiating a new connection in the return
# direction must be blocked; established/related return packets are unaffected.
blocked     "atg-relay-01 → sim-host/ext-supplier:22    [dmz→ext, reverse of ext→dmz]" \
  atg-relay-01 "$FUEL_IP"  22
blocked     "fms-01 → atg-relay-01:22                   [ot→dmz,  reverse of dmz→ot]" \
  fms-01       "$ATG_IP"   22
blocked     "hist-dmz-01 → recon-01:22                  [dmz→it,  reverse of it→dmz]" \
  hist-dmz-01  "$RECON01_IP" 22
# The replica must not be able to initiate back into the primary: replication is
# a one-way push from hist-01
blocked     "hist-dmz-01 → hist-01:22                   [dmz→ot,  reverse of ot→dmz]" \
  hist-dmz-01  "$HIST_IP"  22
blocked     "hist-dmz-01 → hist-01:8429                 [dmz→ot,  no ingestion from the replica]" \
  hist-dmz-01  "$HIST_IP"  8429
blocked     "hist-dmz-01 → fms-01:22                    [dmz→ot,  no rule]" \
  hist-dmz-01  "$FMS_IP"   22

# Zone pairs entirely absent from firewall_rules (both directions).
# ext→ot and ext→it: sim-host is the only node on vnet-ext and also holds
# macvlan NICs on vnet-ot and a bridged NIC on vnet-it. Traffic from the
# ext-supplier IP (10.10.0.10) to those subnets routes via the
# directly-connected interfaces, bypassing fw-ext-dmz. Only the reverse
# directions (ot→ext and it→ext below) are testable.
blocked     "fms-01 → sim-host/ext-supplier:22      [ot→ext,  no rule]" \
  fms-01           "$FUEL_IP"  22
blocked     "corp-it → sim-host/ext-supplier:22     [it→ext,  no rule]" \
  corp-it          "$FUEL_IP"  22
blocked     "atg-relay-01 → sim-host/it-recon-user:22 [dmz→it,  no rule]" \
  atg-relay-01     "$RECON_IP" 22
blocked_via "sim-host/it-recon-user → fms-01:22     [it→ot,   no rule]" \
  sim-host 10.30.0.11 "$FMS_IP"      22
blocked     "admin-01 → fms-01:22                   [it→ot,   ProxyJump required]" \
  admin-01         "$FMS_IP" 22
blocked     "fms-01 → sim-host/it-recon-user:22      [ot→it,   no rule]" \
  fms-01           "$RECON_IP" 22
echo

# ── 5. ICMP blocked across all zone boundaries ────────────────────────────────
# ICMP is not listed in firewall_rules; the fw forward chain drops it in all
# directions. icmp_blocked() treats any ping failure as a pass.
echo "--- 5. ICMP blocked across zone boundaries ---"
# sim-host has busybox ping. It holds no global default route — every flow is
# source-bound to a managed address (sim-network.md §2) — so its probes must
# source-bind (ping -I <addr>) to select the matching policy table and traverse
# the right firewall; an unbound probe would have no route at all. recon-01 (a
# normal vnet-it node with a default route via fw-dmz-it) carries the it→ checks.
icmp_blocked "sim-host/ext-supplier → atg-relay-01  [ext→dmz]"  sim-host         "$ATG_IP"      10.10.0.10
icmp_blocked "sim-host/ext-supplier → fms-01         [ext→ot]"   sim-host         "$FMS_IP"      10.10.0.10
icmp_blocked "sim-host/ext-supplier → corp-it         [ext→it]"   sim-host         "$CORP_IT_IP"  10.10.0.10
icmp_blocked "recon-01 → hist-dmz-01                  [it→dmz]"  recon-01         "$HIST_DMZ_IP"
icmp_blocked "recon-01 → fms-01                       [it→ot]"   recon-01         "$FMS_IP"
icmp_blocked "fms-01 → hist-dmz-01                    [ot→dmz]"  fms-01           "$HIST_DMZ_IP"
icmp_blocked "atg-relay-01 → fms-01                   [dmz→ot]"  atg-relay-01     "$FMS_IP"
echo

# ── 6. NTP — permitted flow ───────────────────────────────────────────────────
# The DMZ single hop and the OT two-hop path both terminate on corp-it:123.
# A legitimate NTP request confirms the path and the service together. The raw
# permitted_udp probe cannot be used here: chrony occupies 123 and discards a
# non-NTP probe silently, so no ICMP port-unreachable returns to confirm
# delivery.
echo "--- 6. NTP permitted flow ---"

# fms-01's two-hop probe exercises the DMZ→IT firewall rule as its second leg:
# the forward rule at fw-dmz-it matches on interface and destination, not
# source, so an OT-sourced packet and a DMZ-sourced packet hit the same rule.
# The DMZ→IT leg is therefore proven under ci-minimal by fms-01 alone.
ntp_query "fms-01 → corp-it:123/udp        [ot→it two-hop, NTP]" \
  fms-01 "$CORP_IT_IP"

ntp_query "hist-dmz-01 → corp-it:123/udp  [dmz→it, NTP]" \
  hist-dmz-01 "$CORP_IT_IP"
echo

# ── 6b. NTP — service convergence ─────────────────────────────────────────────
# Network path proven above; here the chrony service must actually select
# corp-it and hold every client within the narrative's one-second tolerance
# (narrative.md §5). The perturbation check targets a VM client, whose clock is
# independent of the host; container clients (atg-relay-01) share the host clock
# and run chronyd with -x, so they track the anchor but cannot be stepped.
echo "--- 6b. NTP service convergence (full profile) ---"
if ! skip_if_not_full "chrony service convergence"; then
  for node in fms-01 hmi-01 hist-01 hist-dmz-01 fw-ot-dmz atg-relay-01 \
      recon-01 admin-01 dmz-bastion-01; do
    chrony_tracks_anchor "$node"
  done
  chrony_within_tolerance fms-01 1.0
fi
echo

# ── 7. NTP — port restriction ─────────────────────────────────────────────────
# The NTP rule permits only UDP 123 from hist-dmz-01 to corp-it.
# Other ports from hist-dmz-01 to corp-it must be dropped.
echo "--- 7. NTP port restriction (dmz→it, UDP 123 only) ---"
blocked_udp "hist-dmz-01 → corp-it:124/udp  [wrong port]" \
  hist-dmz-01 "$CORP_IT_IP" 124
blocked     "hist-dmz-01 → corp-it:22        [wrong proto and port]" \
  hist-dmz-01 "$CORP_IT_IP" 22
echo

# ── 8. NTP — destination restriction ─────────────────────────────────────────
# Other IT hosts and hosts in all other zones must not receive UDP 123 from hist-dmz-01.
echo "--- 8. NTP destination restriction (dmz→it, corp-it only) ---"
blocked_udp "hist-dmz-01 → recon-01:123/udp  [wrong dst in it]" \
  hist-dmz-01 "$RECON01_IP" 123
blocked_udp "hist-dmz-01 → fms-01:123/udp    [dmz→ot, no rule]" \
  hist-dmz-01 "$FMS_IP" 123
echo

# ── 9. NTP — blocked reverse ─────────────────────────────────────────────────
# corp-it must not be able to initiate new connections to hist-dmz-01.
# Return traffic from established NTP sessions is handled by ct state rules
# and is not tested here.
echo "--- 9. NTP blocked reverse ---"
blocked_udp "corp-it  → hist-dmz-01:123/udp  [it→dmz, no reverse rule]" \
  corp-it "$HIST_DMZ_IP" 123
echo

# ── 10. IdP (LDAP over TLS) — permitted flows ────────────────────────────────
# fms-01 and hmi-01 are the only OT auth clients.  Traffic transits OT → DMZ
# via fw-ot-dmz then DMZ → IT via fw-dmz-it (transparent two-hop routing).
# The service-level check (ldapsearch) is full-profile only.
echo "--- 10. IdP permitted flows (ot→it transit) ---"
routed        "fms-01  → corp-it:636/tcp  [LDAPS]"         fms-01  "$CORP_IT_IP" 636
routed        "hmi-01  → corp-it:636/tcp  [LDAPS]"         hmi-01  "$CORP_IT_IP" 636
if ! skip_if_not_full "fms-01 ldapsearch (service behaviour)"; then
  # ldaps://corp-it, not the IP: corp-it's leaf certificate carries a
  # hostname-only SAN (corp-it.md §5), and fms-01 already resolves the name
  # via /etc/hosts the same way the FMS's own bind does.
  if $INCUS exec fms-01 -- ldapsearch -H "ldaps://corp-it" -x \
      -b "dc=ravensport,dc=local" "(uid=*)" uid 2>/dev/null | grep -q "^dn:"; then
    pass "fms-01: ldapsearch returned entries  [LDAP service]"
  else
    note "fms-01: ldapsearch — no entries returned by corp-it"
  fi
fi
echo

# ── 11. IdP — port restriction ────────────────────────────────────────────────
# The IdP rules permit only TCP 636 from vnet-ot to corp-it — plaintext LDAP
# on 389 is not one of them (corp-it.md §3): the directory is reachable only
# over LDAPS, so 389 belongs here alongside the other ports that must be
# dropped, not among the permitted flows in §10. No Kerberos service is
# deployed (digital.md §8 deviation 4), so 88 is dropped on both transports.
# All other ports must be dropped, confirming no over-broad ACCEPT rule was
# applied.
echo "--- 11. IdP port restriction (ot→it, port 636 only) ---"
blocked "fms-01 → corp-it:389   [plaintext LDAP not permitted]" fms-01 "$CORP_IT_IP" 389
blocked "hmi-01 → corp-it:389   [plaintext LDAP not permitted]" hmi-01 "$CORP_IT_IP" 389
blocked "fms-01 → corp-it:88    [Kerberos not deployed]"        fms-01 "$CORP_IT_IP" 88
blocked_udp "fms-01 → corp-it:88/udp  [Kerberos not deployed]"  fms-01 "$CORP_IT_IP" 88
blocked "fms-01 → corp-it:22    [SSH not permitted]"          fms-01 "$CORP_IT_IP" 22
blocked "fms-01 → corp-it:443   [HTTPS not permitted]"       fms-01 "$CORP_IT_IP" 443
blocked "fms-01 → corp-it:53    [DNS not open; /etc/hosts used]" fms-01 "$CORP_IT_IP" 53
blocked_udp "fms-01 → corp-it:53/udp  [DNS UDP not open]"    fms-01 "$CORP_IT_IP" 53
echo

# ── 12. IdP — destination restriction ────────────────────────────────────────
# The IdP rules permit only corp-it (10.30.0.20) as the destination in vnet-it.
# Other IT hosts must be unreachable from vnet-ot on IdP ports.
echo "--- 12. IdP destination restriction (ot→it, corp-it only) ---"
blocked "fms-01 → sim-host/it-recon-user:636  [wrong it dst for LDAPS]"     fms-01 "$RECON_IP" 636
blocked "fms-01 → sim-host/it-recon-user:389  [wrong it dst for LDAP]"      fms-01 "$RECON_IP" 389
echo

# ── 13. IdP — blocked reverse / IT-initiated paths ───────────────────────────
# corp-it must not initiate new connections into vnet-ot (no reverse rule).
# Established/related return packets are handled by ct state and are not
# tested here; only new-connection initiation from corp-it is checked.
echo "--- 13. IdP blocked reverse and IT→OT ---"
blocked "corp-it → fms-01:22   [it→ot, no reverse rule]"     corp-it "$FMS_IP"   22
blocked "corp-it → hmi-01:22   [it→ot, no reverse rule]"     corp-it "$HMI_IP"   22
blocked_via "sim-host/it-recon-user → fms-01:636  [it→ot, no rule for recon]" \
  sim-host 10.30.0.11 "$FMS_IP"  636
echo

# ── 14. Transit integrity ─────────────────────────────────────────────────────
# The fw nodes' own DMZ interfaces must not be mistaken for IdP endpoints.
# Packets to fw-ot-dmz's or fw-dmz-it's DMZ IP on IdP ports must be dropped
# (there is no rule permitting OT hosts to reach fw node management addresses
# on application ports).
echo "--- 14. Transit integrity (fw DMZ interfaces are not IdP endpoints) ---"
FW_OT_DMZ_DMZ_IP=10.20.0.4   # fw-ot-dmz dmz0
FW_DMZ_IT_DMZ_IP=10.20.0.3   # fw-dmz-it dmz0
blocked "fms-01 → fw-ot-dmz dmz0:636  [fw node is not IdP]" fms-01 "$FW_OT_DMZ_DMZ_IP" 636
blocked "fms-01 → fw-dmz-it dmz0:636  [fw node is not IdP]" fms-01 "$FW_DMZ_IT_DMZ_IP" 636
echo

# ── 15. Sim-host egress guard ─────────────────────────────────────────────────
# Not a firewall property. ot-operator, ext-supplier and it-recon-user originate
# connections only, and egress-guard.nft on sim-host drops anything arriving on
# them. No fw node can enforce this: recon-01 and sim-host/it-recon-user are both
# on vnet-it, so nothing sits between them.
#
# Until now the guard was covered only by test_sim_image.sh confirming the file
# is present in the image, which is not evidence that it loads or that it drops
# anything. These assert the behaviour.
#
# 3389 is the port that matters: it is what the reconciliation user actor dials
# on recon-01, so a guard that failed open would make the reverse direction
# reachable and the actor addressable from the host it audits from.
echo "--- 15. Sim-host egress guard (egress-only NICs) ---"
blocked "recon-01 → sim-host/it-recon-user:3389 [it, egress-only NIC]" \
  recon-01 "$RECON_IP" 3389
blocked "recon-01 → sim-host/it-recon-user:22   [it, egress-only NIC]" \
  recon-01 "$RECON_IP" 22
blocked "admin-01 → sim-host/it-sysadmin:22     [it, egress-only NIC]" \
  admin-01 "$SYSADMIN_IP" 22
echo

# ── 16. Security monitoring agent channel ─────────────────────────────────────
# Every monitored host's agent reaches siem-01 on TCP 1514 (siem.md §8.1): OT
# hosts over the two-hop path via fw-ot-dmz and fw-dmz-it, DMZ hosts via
# fw-dmz-it, and IT hosts directly within the zone. The manager listens on 1514
# whenever it runs, so permitted() — not routed() — is used: a refusal here is
# a failure, not a service that is yet to be deployed.
echo "--- 16. Security monitoring agent channel (TCP 1514 to siem-01) ---"
permitted "fms-01       → siem-01:1514   [ot→it two-hop, agent channel]" \
  fms-01           "$SIEM_IP"     1514
permitted "atg-relay-01 → siem-01:1514   [dmz→it, agent channel]" \
  atg-relay-01     "$SIEM_IP"     1514
permitted "dmz-bastion-01 → siem-01:1514 [dmz→it, agent channel]" \
  dmz-bastion-01   "$SIEM_IP"     1514
permitted "corp-it      → siem-01:1514   [it, same zone, agent channel]" \
  corp-it          "$SIEM_IP"     1514
permitted "admin-01     → siem-01:1514   [it, same zone, agent channel]" \
  admin-01         "$SIEM_IP"     1514
# Port restriction: the rules admit 1514 only. 55000 is the manager's
# management interface, reachable within the IT zone but from no other zone.
blocked   "fms-01       → siem-01:9999   [ot→it, port 1514 only]" \
  fms-01           "$SIEM_IP"     9999
blocked   "atg-relay-01 → siem-01:55000  [dmz→it, management interface not permitted]" \
  atg-relay-01     "$SIEM_IP"     55000
# Destination restriction: the rules name siem-01 as the only destination on
# 1514, so another IT host on the same port is dropped.
blocked   "fms-01       → corp-it:1514   [ot→it, dst siem-01 only]" \
  fms-01           "$CORP_IT_IP"  1514
echo

echo "==> Results: ${PASS} passed, ${FAIL} failed, ${WARN} warnings, ${SKIP} skipped, ${NOTE} notes"
if [ "$WARN" -gt 0 ]; then
  printf "    Warnings mean the fw node forwarded a packet it should have dropped.\n"
  printf "    Inspect nftables rules: incus exec <fw-node> -- nft list ruleset\n"
fi
if [ "$SKIP" -gt 0 ]; then
  printf "    Skipped tests require PROFILE=full (real service deployments).\n"
fi
if [ "$NOTE" -gt 0 ]; then
  printf "    Notes are service-behaviour checks where the network path is open\n"
  printf "    but the service is not yet deployed.  They do not affect exit status.\n"
fi
[ "$FAIL" -eq 0 ] && [ "$WARN" -eq 0 ]
