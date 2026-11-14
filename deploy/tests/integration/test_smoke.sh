#!/usr/bin/env bash
# End-to-end smoke test. Assumes `make up` has just finished.
# Validates the ATG relay chain (sim-host/ext-supplier → atg-relay-01 →
# sim-host/ot-atg, crossing fw-ext-dmz then fw-ot-dmz) and direct RFID
# connectivity to the networked readers on sim-host.
set -euo pipefail

FAILURES=0
pass() { printf "  ✓ %s\n" "$1"; }
fail() { printf "  ✗ %s\n" "$1"; FAILURES=$((FAILURES + 1)); }
warn() { printf "  ~ %s\n" "$1"; }

INCUS="${INCUS:-incus}"

ATG_RELAY_IP=10.20.0.10    # atg-relay-01
ATG_CONSOLE_IP=10.40.0.51  # sim-host ot-atg NIC
VESSEL_RFID_IP=10.40.0.53  # sim-host ot-vessel-rfid NIC
OP_RFID_IP=10.40.0.54      # sim-host ot-op-rfid NIC

echo "→ ATG relay: vnet-ext hop (sim-host/ext-supplier → atg-relay-01:10001)"
# sim-host has no bash; use busybox nc.  Source bind to 10.10.0.10 activates
# the ext-supplier policy-routing table so the packet enters vnet-ext and
# traverses fw-ext-dmz under the correct firewall rule.
if $INCUS exec sim-host -- nc -z -s 10.10.0.10 -w 5 "$ATG_RELAY_IP" 10001 2>/dev/null; then
  pass "sim-host reaches atg-relay-01:10001 (source IP 10.10.0.10)"
else
  fail "sim-host cannot reach atg-relay-01:10001"
fi

echo "→ ATG relay: vnet-dmz→vnet-ot hop (atg-relay-01 → sim-host/atg-console:10001)"
if $INCUS exec atg-relay-01 -- timeout 5 bash -c \
   "exec 3<>/dev/tcp/${ATG_CONSOLE_IP}/10001" 2>/dev/null; then
  pass "atg-relay-01 reaches sim-host/atg-console:10001 (10.40.0.51)"
else
  fail "atg-relay-01 cannot reach sim-host/atg-console:10001"
fi

echo "→ RFID reader: fms-01 connects to vessel-rfid (10.40.0.53:9001) on sim-host"
if $INCUS exec fms-01 -- timeout 5 bash -c \
   "exec 3<>/dev/tcp/${VESSEL_RFID_IP}/9001" 2>/dev/null; then
  pass "fms-01 reaches vessel-rfid:9001 (HAProxy → sim loopback)"
else
  fail "fms-01 cannot reach vessel-rfid:9001"
fi

echo "→ RFID reader: hmi-01 connects to operator-rfid (10.40.0.54:9002) on sim-host"
if $INCUS exec hmi-01 -- timeout 5 bash -c \
   "exec 3<>/dev/tcp/${OP_RFID_IP}/9002" 2>/dev/null; then
  pass "hmi-01 reaches operator-rfid:9002 (HAProxy → sim loopback)"
else
  fail "hmi-01 cannot reach operator-rfid:9002"
fi

echo "→ FMS API: health endpoint responds, presenting a CA-validated certificate"
# --cacert (not -k): the FMS's certificate must actually validate against the
# RavensPort internal CA root, not merely respond over any TLS. --resolve pins
# the connection to loopback while still presenting "fms-01" for SNI/hostname
# verification — the certificate's only SAN (digital.md §2.7).
if $INCUS exec fms-01 -- curl -fs --cacert /etc/ssl/certs/ca-certificates.crt \
   --resolve fms-01:443:127.0.0.1 https://fms-01/api/v1/health 2>/dev/null \
   | grep -q '"status":"ok"'; then
  pass "fms-01 FMS API is up and its certificate validates against the CA root"
else
  fail "fms-01 FMS API health check failed (unreachable, or certificate did not validate)"
fi

echo "→ HMI: hmi binary listening on 127.0.0.1:8080 (loopback only)"
# The HMI binary must bind loopback only; HAProxy owns the external interface.
# Requires make push-hmi to have been run.
if $INCUS exec hmi-01 -- ss -tlnp 2>/dev/null | grep -q '127.0.0.1:8080'; then
  pass "hmi-01: hmi binary listening on 127.0.0.1:8080 (loopback only)"
else
  fail "hmi-01: hmi binary not listening on 127.0.0.1:8080"
fi

echo "→ HMI: health endpoint via HAProxy (hmi-01 internal), presenting a CA-validated certificate"
# Hits HAProxy on 443 from within the container; proxy terminates TLS and
# forwards to the HMI binary on 127.0.0.1:8080. Tests the full local chain.
# --resolve pins the connection to loopback while still presenting "hmi-01" for
# SNI/hostname verification — the certificate's only SAN (digital.md §2.7).
if $INCUS exec hmi-01 -- curl -fs --cacert /etc/ssl/certs/ca-certificates.crt \
   --resolve hmi-01:443:127.0.0.1 https://hmi-01/health 2>/dev/null | grep -q '"status":"ok"'; then
  pass "hmi-01: HMI health via HAProxy → 200 {\"status\":\"ok\"}, certificate validates"
else
  fail "hmi-01: HMI health via HAProxy failed"
fi

echo "→ HMI: fms-01 reaches hmi-01 HTTPS on OT network (10.40.0.12:443), certificate validates"
# Cross-node test: fms-01 and hmi-01 share vnet-ot. Confirms HAProxy is
# reachable from the OT network, not just loopback, and that the certificate
# presented there validates the same way it does over loopback — the network
# path does not change what HAProxy serves.
if $INCUS exec fms-01 -- curl -fs --cacert /etc/ssl/certs/ca-certificates.crt \
   --resolve hmi-01:443:10.40.0.12 https://hmi-01/health 2>/dev/null | grep -q '"status":"ok"'; then
  pass "fms-01 reaches hmi-01:443 over OT network, certificate validates"
else
  fail "fms-01 cannot reach hmi-01:443 over OT network, or its certificate did not validate"
fi

echo "→ HMI: port 8080 not reachable from OT network (loopback-only enforcement)"
# The HMI binary binds 127.0.0.1:8080 only. A connection from fms-01 must be
# refused, confirming plain HTTP is not exposed on the OT interface.
if ! $INCUS exec fms-01 -- curl -fs --max-time 3 http://10.40.0.12:8080/health \
   >/dev/null 2>&1; then
  pass "hmi-01 port 8080 refused from OT network (loopback-only as expected)"
else
  fail "hmi-01 port 8080 reachable from OT network (should be loopback-only)"
fi

echo "→ HMI: POST /api/auth/login returns 202"
# Login stores credentials in memory and returns a challenge ID immediately;
# FMS is not called at this stage so this works without a live FMS connection.
login_code=$($INCUS exec hmi-01 -- curl -s --cacert /etc/ssl/certs/ca-certificates.crt \
  --resolve hmi-01:443:127.0.0.1 -o /dev/null -w "%{http_code}" \
  -X POST https://hmi-01/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"dock-operator","password":"operator-password"}' 2>/dev/null)
if [ "$login_code" = "202" ]; then
  pass "HMI POST /api/auth/login → 202"
else
  fail "HMI POST /api/auth/login → $login_code (expected 202)"
fi

echo "→ HMI: authenticated endpoint without token returns 401"
# GET /api/tanks/01/level requires a bearer token; absence returns
# 401 {"error":"invalid_credentials"}.
unauth_code=$($INCUS exec hmi-01 -- curl -s --cacert /etc/ssl/certs/ca-certificates.crt \
  --resolve hmi-01:443:127.0.0.1 -o /dev/null -w "%{http_code}" \
  https://hmi-01/api/tanks/01/level 2>/dev/null)
if [ "$unauth_code" = "401" ]; then
  pass "HMI GET /api/tanks/01/level (no token) → 401"
else
  fail "HMI GET /api/tanks/01/level (no token) → $unauth_code (expected 401)"
fi

echo "→ Dock operator: sim-host's HAProxy backend completes its TLS handshake with hmi-01"
# The direct reproduction of this feature's original bug report: sim-host's
# HAProxy dock-operator backend rejected hmi-01's certificate in a live
# deployment despite the certificate passing static `openssl verify`. Hitting
# sim-host's own loopback frontend (127.0.0.1:20002) exactly as the sim's
# dock-operator actor does exercises the real chain — sim-host HAProxy's `ssl
# verify required ca-file ... verifyhost hmi-01` backend, over the real OT
# network, against hmi-01's real HAProxy frontend — with no way to fake a pass
# by skipping certificate validation, since the mode here is plain HTTP on
# sim-host's own loopback: the TLS handshake happens on the far side of this
# request, entirely inside HAProxy, where no `-k`/`--cacert` flag of this
# test's own could paper over a failure.
# sim-host is a minimal Alpine image with no bash and no curl; busybox wget is
# the client available (sim-image.expected.packages).
if $INCUS exec sim-host -- wget -q -O - --timeout=5 http://127.0.0.1:20002/health 2>/dev/null \
   | grep -q '"status":"ok"'; then
  pass "sim-host → hmi-01 dock-operator path: TLS handshake and request succeeded end to end"
else
  fail "sim-host → hmi-01 dock-operator path failed (TLS handshake or proxying broken)"
fi

echo "→ Grafana: an admin at hmi-01 reaches hist-01 by hostname, certificate validates"
# No --resolve override here, deliberately: this is the actual capability
# topology.yaml's hmi-01 resolve: [hist-01] entry exists for (hist.md §3.1,
# digital.md §2.7) — hmi-01's own /etc/hosts must resolve the name unaided, the
# same way a human's browser would.
if $INCUS exec hmi-01 -- curl -fs --cacert /etc/ssl/certs/ca-certificates.crt \
   https://hist-01/api/health >/dev/null 2>&1; then
  pass "hmi-01 reaches hist-01's Grafana by hostname, certificate validates"
else
  fail "hmi-01 cannot reach hist-01's Grafana by hostname, or its certificate did not validate"
fi

echo "→ Grafana: the reconciliation user at recon-01 reaches hist-dmz-01 by hostname, certificate validates"
# Likewise: recon-01's pre-existing resolve: [hist-dmz-01] entry is now
# load-bearing for certificate validation, not just a convenience.
if $INCUS exec recon-01 -- curl -fs --cacert /etc/ssl/certs/ca-certificates.crt \
   https://hist-dmz-01/api/health >/dev/null 2>&1; then
  pass "recon-01 reaches hist-dmz-01's Grafana by hostname, certificate validates"
else
  fail "recon-01 cannot reach hist-dmz-01's Grafana by hostname, or its certificate did not validate"
fi

echo "→ LDAPS: fms-01 reaches corp-it's directory by hostname, certificate validates"
# No --resolve override here, deliberately: this is the actual capability
# topology.yaml's fms-01 resolve: [corp-it, hist-01] entry exists for
# (corp-it.md §5, digital.md §2.7) — fms-01's own /etc/hosts must resolve the
# name unaided, the same way its operator bind does. openssl s_client rather
# than ldapsearch: fms-01 does its own LDAP bind from the Rust binary, not via
# ldap-utils, so no LDAP client tooling is assumed present on this host — a
# validated TLS handshake against the right name is what the FMS's bind
# itself depends on.
if $INCUS exec fms-01 -- bash -c \
   'echo | openssl s_client -connect corp-it:636 -CAfile /etc/ssl/certs/ca-certificates.crt -verify_return_error' \
   >/dev/null 2>&1; then
  pass "fms-01 reaches corp-it's directory by hostname over LDAPS, certificate validates"
else
  fail "fms-01 cannot reach corp-it's directory over LDAPS, or its certificate did not validate"
fi

echo "→ LDAPS: an operator login through the FMS binds against corp-it's directory"
# The escalation from the handshake above. This is the FMS's own ldap3 client
# performing a real bind, which covers what a handshake cannot: that rustls
# accepts corp-it's certificate against fms-01's system trust store, that the
# hostname SAN matches the name the FMS dials, and that the {SSHA} verifier in
# the directory matches the password world.yaml holds. A 200 also proves the
# card table was seeded from FMS_SEED_CARDS, the operator login being
# two-factor (fms.md §4).
#
# The credentials come from world.yaml because no rendered artefact carries the
# operator password: it reaches the directory only as an {SSHA} verifier
# (deployment-config.md §6), so there is nothing to read it back from.
# Addressed through the renderer's virtual environment, named as
# deploy/Makefile names it, since PyYAML lives there rather than in the system
# interpreter; the system interpreter is the fallback for a standalone run.
#
# This asserts the deployed configuration. `configure-fms` in the FMS
# integration Makefile writes /etc/fms/test-overrides.env, which repoints
# FMS_LDAP_URI at the runner's plaintext stub — while that file is present this
# check says nothing about LDAPS.
DEPLOY_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
VENV_PY="$DEPLOY_DIR/.venv-$(uname -s)-$(uname -m)/bin/python"
[ -x "$VENV_PY" ] || VENV_PY=python3
operator_login_body=$("$VENV_PY" - "$DEPLOY_DIR/world.yaml" <<'PY' || true
import json, sys, yaml

operator = yaml.safe_load(open(sys.argv[1]))["identities"]["operators"][0]
print(
    json.dumps(
        {
            "username": operator["username"],
            "password": operator["password"],
            "rfid_card_number": operator["rfid_card"],
        }
    )
)
PY
)
if [ -z "$operator_login_body" ]; then
  fail "could not read the operator credentials from $DEPLOY_DIR/world.yaml"
else
  # The body is a single argv element and incus exec runs curl directly with no
  # shell on the far side, so the password needs no quoting and never reaches a
  # command line that a shell would re-parse.
  login_code=$($INCUS exec fms-01 -- curl -s \
    --cacert /etc/ssl/certs/ca-certificates.crt \
    --resolve fms-01:443:127.0.0.1 -o /dev/null -w "%{http_code}" \
    -X POST https://fms-01/api/v1/auth/login \
    -H 'Content-Type: application/json' \
    -d "$operator_login_body" 2>/dev/null)
  if [ "$login_code" = "200" ]; then
    pass "operator login through the FMS bound against corp-it over LDAPS"
  else
    fail "operator login through the FMS returned $login_code (expected 200); a 401 is a failed directory bind rather than a wrong password, and fms-01's journal carries the bind error"
  fi
fi

echo "→ Dock operator: sim-host's HAProxy is not in a crashed state"
# The dock-operator backend carries no health check (no-check — sim-network.md
# §6), so a broken TLS handshake would not otherwise be visible in HAProxy's
# own service state; this only confirms HAProxy itself is still running, as a
# secondary signal alongside the direct check above.
if $INCUS exec sim-host -- rc-service haproxy status 2>/dev/null | grep -qi started; then
  pass "sim-host: haproxy service is started (not crashed)"
else
  fail "sim-host: haproxy service is not in a started state"
fi

echo
if [ "$FAILURES" -eq 0 ]; then
  echo "Smoke tests complete: all passed."
else
  echo "Smoke tests complete: $FAILURES failure(s)."
  exit 1
fi
