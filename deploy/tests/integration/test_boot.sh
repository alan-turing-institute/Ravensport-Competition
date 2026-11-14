#!/usr/bin/env bash
# Post-deployment boot checks.
#
# Verifies that nodes have started in the correct state after `make up`:
#
#   1. Each fw node has IP forwarding enabled and nftables running with a
#      drop-default forward chain.
#   2. Every zone bridge carries no host IP (Incus host cannot route between
#      zones without traversing a fw node).
#   3. hmi-01 has HAProxy active and listening on port 443.
#   4. sim-host has the sim and HAProxy processes running, and sim-net.sh
#      is re-run safe.
#
# Run from deploy/ after `make up`:  bash tests/integration/test_boot.sh

set -uo pipefail

INCUS="${INCUS:-incus}"
PASS=0
FAIL=0

pass() { printf "  PASS  %s\n" "$1"; (( PASS++ )) || true; }
fail() { printf "  FAIL  %s\n" "$1"; (( FAIL++ )) || true; }

FW_NODES=(fw-ext-dmz fw-dmz-it fw-ot-dmz)

ZONE_BRIDGES=(vnet-ext vnet-dmz vnet-it vnet-ot)

echo "==> Firewall node configuration"
echo
for fw in "${FW_NODES[@]}"; do
  # IP forwarding is enabled via /etc/sysctl.d/ applied by systemd-sysctl at boot.
  fwd=$($INCUS exec "$fw" -- sysctl -n net.ipv4.ip_forward 2>/dev/null || echo "error")
  if [ "$fwd" = "1" ]; then
    pass "$fw: ip_forward = 1"
  else
    fail "$fw: ip_forward = '${fwd}' (expected 1)"
  fi

  # nftables must be active (enabled + running).
  active=$($INCUS exec "$fw" -- systemctl is-active nftables 2>/dev/null || echo "error")
  if [ "$active" = "active" ]; then
    pass "$fw: nftables active"
  else
    fail "$fw: nftables '${active}' (expected active)"
  fi

  # The forward chain must have a drop-default policy.  A misconfigured node
  # with no forward chain or an accept policy would pass traffic unchecked.
  # Capture to a variable first — piping incus exec output directly can lose
  # data due to buffering differences between container runtimes.
  fw_chain=$($INCUS exec "$fw" -- nft list chain inet filter forward 2>/dev/null)
  if echo "$fw_chain" | grep -q 'policy drop'; then
    pass "$fw: forward chain policy drop"
  else
    fail "$fw: forward chain policy not drop (nft list chain inet filter forward output unexpected)"
  fi
done

echo
echo "==> Zone bridge isolation"
echo
# Each isolated zone bridge must have ipv4.address=none, confirming the Incus
# host holds no IP on the subnet and therefore cannot route between zones.
for bridge in "${ZONE_BRIDGES[@]}"; do
  addr=$($INCUS network show "$bridge" 2>/dev/null \
    | awk '/^ +ipv4\.address:/ {print $2}')
  if [ "$addr" = "none" ]; then
    pass "$bridge: ipv4.address=none (host isolated)"
  else
    fail "$bridge: ipv4.address='${addr:-missing}' (host can route between zones)"
  fi
done

echo
echo "==> hmi-01 HAProxy and HMI service checks"
echo
# HAProxy terminates TLS on port 443 and proxies to the HMI binary on
# 127.0.0.1:8080 (hmi.md §9.3).

haproxy_active=$($INCUS exec hmi-01 -- systemctl is-active haproxy 2>/dev/null || echo "error")
if [ "$haproxy_active" = "active" ]; then
  pass "hmi-01: haproxy active"
else
  fail "hmi-01: haproxy '$haproxy_active' (expected active)"
fi

if $INCUS exec hmi-01 -- ss -tlnp 2>/dev/null | grep -q '0.0.0.0:443'; then
  pass "hmi-01: haproxy listening on 0.0.0.0:443"
else
  fail "hmi-01: nothing listening on 0.0.0.0:443"
fi

if $INCUS exec hmi-01 -- test -f /etc/haproxy/tls/hmi-01.pem 2>/dev/null; then
  pass "hmi-01: TLS certificate present at /etc/haproxy/tls/hmi-01.pem"
else
  fail "hmi-01: TLS certificate missing at /etc/haproxy/tls/hmi-01.pem"
fi

# hmi-01.pem is a leaf+key bundle (HAProxy's `crt` directive wants both in one
# file); openssl x509 reads only the first PEM block, the certificate.
if $INCUS exec hmi-01 -- bash -c \
   "openssl x509 -in /etc/haproxy/tls/hmi-01.pem -noout -ext subjectAltName 2>/dev/null | grep -q 'DNS:hmi-01'"; then
  pass "hmi-01: certificate carries DNS SAN hmi-01"
else
  fail "hmi-01: certificate missing DNS:hmi-01 SAN"
fi

if $INCUS exec hmi-01 -- bash -c \
   "openssl verify -x509_strict -purpose sslserver -CAfile /etc/ssl/certs/ca-certificates.crt /etc/haproxy/tls/hmi-01.pem >/dev/null 2>&1"; then
  pass "hmi-01: certificate validates against the system trust store (RavensPort CA installed there)"
else
  fail "hmi-01: certificate does not validate against the system trust store"
fi

echo
echo "==> Log-server service, listener and certificate checks"
echo
# hist-01 runs both stores behind both ingestion agents; hist-dmz-01 runs the
# stores and Grafana alone (hist.md §9). The listener addresses are the property
# most worth checking here: hist-01's stores answer on loopback only, reachable
# solely through its local agent, whereas the replica's answer every address —
# hist-01's agents write to them across the zone boundary and its own Grafana
# queries them over loopback, so binding only the zone address would serve one
# and fail the other.

check_units() {
  local node="$1"; shift
  local unit active
  for unit in "$@"; do
    active=$($INCUS exec "$node" -- systemctl is-active "$unit" 2>/dev/null || echo "error")
    if [ "$active" = "active" ]; then
      pass "$node: $unit active"
    else
      fail "$node: $unit '$active' (expected active)"
    fi
  done
}

check_listeners() {
  local node="$1"; shift
  local endpoint listening address port
  # Captured once: ss is cheap but incus exec is not, and piping its output
  # directly can lose data to buffering.
  listening=$($INCUS exec "$node" -- ss -tln 2>/dev/null)
  for endpoint in "$@"; do
    address="${endpoint%:*}"
    port="${endpoint##*:}"
    # Surrounding spaces so that 8428 does not match 18428, and -F because the
    # address is full of regex metacharacters.
    if echo "$listening" | grep -qF " ${endpoint} "; then
      pass "$node: listening on $endpoint"
    elif [ "$address" = "0.0.0.0" ] \
         && echo "$listening" | grep -qE " (\*|\[::\]):${port} "; then
      # A socket bound to every address may be dual-stack, which ss prints as
      # *:port or [::]:port rather than 0.0.0.0:port. Grafana binds this way.
      # The property asked for is reachability on every address, and all three
      # forms provide it. This alternative is deliberately not accepted for a
      # specific address: a store expected on 127.0.0.1 that appears as *:port
      # is reachable from the whole zone and must still fail.
      pass "$node: listening on ${endpoint} (as $(echo "$listening" \
        | grep -oE " (\*|\[::\]):${port} " | tr -d ' ' | head -1))"
    else
      fail "$node: nothing listening on $endpoint"
    fi
  done
}

check_hist_tls_material() {
  local node="$1" file
  for file in cert.pem key.pem; do
    if $INCUS exec "$node" -- test -f "/etc/hist-tls/$file" 2>/dev/null; then
      pass "$node: TLS material present at /etc/hist-tls/$file"
    else
      fail "$node: TLS material missing at /etc/hist-tls/$file"
    fi
  done

  # CA-signed, not self-signed (digital.md §2.7, hist.md §9/§10.2/§10.3): one
  # shared keypair per host, carrying a DNS SAN of the node's own hostname, and
  # validating against the system trust store every node installs the CA root
  # into. Checked once against the shared file rather than per listening
  # service — every TLS-terminating service on this host presents these same
  # bytes, so one check covers them all; test_01..04 confirm what each
  # individual listener actually presents at runtime.
  if $INCUS exec "$node" -- bash -c \
     "openssl x509 -in /etc/hist-tls/cert.pem -noout -ext subjectAltName 2>/dev/null | grep -q 'DNS:${node}'"; then
    pass "$node: certificate carries DNS SAN ${node}"
  else
    fail "$node: certificate missing DNS:${node} SAN"
  fi

  if $INCUS exec "$node" -- bash -c \
     "openssl verify -x509_strict -purpose sslserver -CAfile /etc/ssl/certs/ca-certificates.crt /etc/hist-tls/cert.pem >/dev/null 2>&1"; then
    pass "$node: certificate validates against the system trust store (RavensPort CA installed there)"
  else
    fail "$node: certificate does not validate against the system trust store"
  fi
}

check_units hist-01 victoria-metrics vmagent victoria-logs vlagent grafana-server
check_listeners hist-01 127.0.0.1:8428 127.0.0.1:9428 0.0.0.0:8429 0.0.0.0:9429 \
  0.0.0.0:6514 0.0.0.0:443
check_hist_tls_material hist-01

check_units hist-dmz-01 victoria-metrics victoria-logs grafana-server
# Each store binds two addresses on two ports (hist.md §9.2): the zone address
# on the standard port, TLS, for hist-01's replication traffic, and loopback on
# a dedicated port, plain HTTP, for Grafana's own local datasource queries
# (§7) — a request Grafana's certificate could not validate at 127.0.0.1 in any
# case, since it carries a hostname-only SAN, and 0.0.0.0/127.0.0.1 cannot
# share one port regardless.
check_listeners hist-dmz-01 127.0.0.1:18428 127.0.0.1:19428 0.0.0.0:8428 0.0.0.0:9428 0.0.0.0:443
check_hist_tls_material hist-dmz-01

# The replica must hold no binary that can originate a remote-write, which is
# the runtime counterpart to the build-time assertion that neither agent tarball
# is fetched for it. Checking for the binary rather than for an inactive unit
# catches an agent that was installed but simply not enabled.
for agent in vmagent vlagent; do
  if $INCUS exec hist-dmz-01 -- test -e "/usr/local/bin/$agent" 2>/dev/null; then
    fail "hist-dmz-01: $agent binary present; the replica must originate no remote-write"
  else
    pass "hist-dmz-01: no $agent binary"
  fi
done

echo
echo "==> corp-it directory service checks"
echo

check_units corp-it slapd chrony
check_listeners corp-it 0.0.0.0:636

# corp-it.md §3: slapd's service list carries only ldaps:/// and the local
# ldapi:/// socket — no plaintext ldap:/// listener should ever be reachable.
if $INCUS exec corp-it -- ss -tln 2>/dev/null | grep -qE ' (0\.0\.0\.0|\*|\[::\]):389 '; then
  fail "corp-it: plaintext LDAP listening on port 389"
else
  pass "corp-it: no plaintext LDAP listener on port 389"
fi

for file in cert.pem key.pem; do
  if $INCUS exec corp-it -- test -f "/etc/ldap/tls/$file" 2>/dev/null; then
    pass "corp-it: TLS material present at /etc/ldap/tls/$file"
  else
    fail "corp-it: TLS material missing at /etc/ldap/tls/$file"
  fi
done

# CA-signed, not self-signed (digital.md §2.7, corp-it.md §5/§6), carrying a
# DNS SAN of corp-it's own hostname, and validating against the system trust
# store every node installs the CA root into. test_01_server.py confirms what
# the live LDAPS listener actually presents at runtime; this checks the
# installed material directly.
if $INCUS exec corp-it -- bash -c \
   "openssl x509 -in /etc/ldap/tls/cert.pem -noout -ext subjectAltName 2>/dev/null | grep -q 'DNS:corp-it'"; then
  pass "corp-it: certificate carries DNS SAN corp-it"
else
  fail "corp-it: certificate missing DNS:corp-it SAN"
fi

if $INCUS exec corp-it -- bash -c \
   "openssl verify -x509_strict -purpose sslserver -CAfile /etc/ssl/certs/ca-certificates.crt /etc/ldap/tls/cert.pem >/dev/null 2>&1"; then
  pass "corp-it: certificate validates against the system trust store (RavensPort CA installed there)"
else
  fail "corp-it: certificate does not validate against the system trust store"
fi

echo
echo "==> sim-host process checks"
echo
# sim-host uses a custom Alpine/OpenRC image without cloud-init. Verify the two
# supervised processes are running: the sim binary (loopback only) and the HAProxy
# reverse proxy (zone interfaces).

# The image ships busybox pgrep, whose -x matches the full argv[0]
# (e.g. /usr/sbin/haproxy), so `pgrep -x haproxy` never matches. Anchor the
# process name instead — busybox pgrep treats the pattern as a regex over the
# process name, so "^name$" is an exact match that also works under procps.
for proc in sim haproxy; do
  if $INCUS exec sim-host -- pgrep "^${proc}\$" >/dev/null 2>&1; then
    pass "sim-host: $proc running"
  else
    fail "sim-host: $proc not found"
  fi
done

# The networking step must be re-run safe: a service restart or a manual re-run
# must reconfigure cleanly, not abort on already-present addresses/routes/rules
# (sim-network.md §2). Re-run it and confirm it still exits 0 with exactly one
# source-policy rule per interface (no duplicates).
if $INCUS exec sim-host -- /usr/local/bin/sim-net.sh >/dev/null 2>&1; then
  pass "sim-host: sim-net.sh re-run is idempotent (exit 0)"
else
  fail "sim-host: sim-net.sh re-run failed (not re-run safe)"
fi
nrules=$($INCUS exec sim-host -- sh -c "ip rule show | grep -cE 'from [0-9.]+ lookup'" 2>/dev/null || echo 0)
nnics=$($INCUS exec sim-host -- sh -c "ip -4 -br addr show | grep -cE '10\\.[0-9]'" 2>/dev/null || echo 0)
if [ "$nrules" = "$nnics" ] && [ "$nrules" -gt 0 ]; then
  pass "sim-host: one source-policy rule per interface, no duplicates ($nrules)"
else
  fail "sim-host: policy-rule/interface mismatch (rules=$nrules, nics=$nnics)"
fi

echo
echo "==> Results: ${PASS} passed, ${FAIL} failed"
[ "$FAIL" -eq 0 ]
