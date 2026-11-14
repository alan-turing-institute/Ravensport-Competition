#!/usr/bin/env bash
# Build the offline RavensPort internal certificate authority (CA) and sign
# leaf certificates for fms-01, hmi-01, hist-01, hist-dmz-01, corp-it, siem-01
# and recon-01 (digital.md §2.7, §8 decision 7).
#
# The CA is not a running service: this script runs once per build, entirely
# on the build host. The root private key (build/ca/ca.key) never leaves this
# host and is never pushed into any image; only ca.pem (the public root) and
# each leaf node's signed certificate are pushed into images by the per-node
# build scripts.
#
# The private key material this script signs is deterministic — written by
# render.py's ca_provisioning module from world.yaml's
# crypto.password_salt_seed, not generated here — and params.json (also
# render.py's output) pins the serial number and validity window each
# certificate gets, so that re-running this script from unchanged inputs
# reproduces byte-identical certificates, not just byte-identical keys
# (deployment.md §9.1). This script's own job is purely the X.509 structure:
# self-signing the root and signing each leaf CSR against it, using
# `openssl ca` (rather than the `req -x509`/`x509 -req` shortcuts) because it
# is the one openssl subcommand that accepts an explicit notBefore/notAfter
# instead of computing them from the build host's current time.
set -euo pipefail
cd "$(dirname "$0")/.."   # deploy/

CA_DIR="build/ca"
TLS_DIR="build/tls"
SCRATCH="$CA_DIR/.openssl-ca"
LEAVES="fms-01 hmi-01 hist-01 hist-dmz-01 corp-it siem-01 recon-01"

# The sim's reconciliation user verifies recon-01's remote-desktop certificate
# against the root, which the sim's build.rs compiles in from a committed copy
# in the crate, so that the crate builds without a prior render
# (deployment-config.md §3). Written on both paths below, so that the copy
# follows the root whether or not this run re-signs it.
SIM_CA_ROOT="../infra/sim/ca.pem"

if [ ! -f "$CA_DIR/ca.key" ] || [ ! -f "$CA_DIR/params.json" ]; then
    echo "ERROR: $CA_DIR/ca.key or params.json not found — run 'make render' first." >&2
    exit 1
fi
for node in $LEAVES; do
    if [ ! -f "$TLS_DIR/$node/key.pem" ]; then
        echo "ERROR: $TLS_DIR/$node/key.pem not found — run 'make render' first." >&2
        exit 1
    fi
done

# Extracts one field from params.json. Shells out to python3 rather than
# adding a `jq` dependency; python3 is already required elsewhere in this
# pipeline (render.py itself, and the netplan-to-networkd conversion the
# per-node build scripts run).
params_field() {
    python3 -c "import json; print(json.load(open('$CA_DIR/params.json'))$1)"
}

# Serial numbers are up to 159 bits — far beyond bash's `printf '%x'`, which
# silently clamps to 64 bits (and so collided two different serials on the
# same clamped value the one time this was tried with plain printf). The hex
# conversion has to happen in Python, on the same arbitrary-precision integer
# params_field already read out.
params_serial_hex() {
    python3 -c "import json; print('%040x' % json.load(open('$CA_DIR/params.json'))$1)"
}

# ── skip-if-unchanged (the input-hash caching pattern used for host images,
# deployment.md §6.1): the deterministic key material and pinned parameters
# are the only real inputs to this script's output, alongside the script's
# own logic — so a changed world.yaml seed, a changed NOT_BEFORE pin, or a
# changed script all correctly invalidate the cache; an unrelated re-render
# with the same seed reproduces the same key bytes and so does not.
LEAF_KEY_FILES=""
for node in $LEAVES; do
    LEAF_KEY_FILES="$LEAF_KEY_FILES $TLS_DIR/$node/key.pem"
done
# shellcheck disable=SC2086
CURRENT_HASH=$(sha256sum "$0" "$CA_DIR/ca.key" "$CA_DIR/params.json" $LEAF_KEY_FILES \
    | sha256sum | cut -d' ' -f1)
STORED_HASH=$(cat "$CA_DIR/.input-hash" 2>/dev/null || echo "")
ALL_CERTS_PRESENT=true
for node in $LEAVES; do
    [ -f "$TLS_DIR/$node/cert.pem" ] || ALL_CERTS_PRESENT=false
done
if [ "$CURRENT_HASH" = "$STORED_HASH" ] \
   && [ -f "$CA_DIR/ca.pem" ] \
   && [ "$ALL_CERTS_PRESENT" = true ] \
   && [ -f "$TLS_DIR/hmi-01/hmi-01.pem" ]; then
    echo "  CA: inputs unchanged, skipping"
    cp "$CA_DIR/ca.pem" "$SIM_CA_ROOT"
    exit 0
fi

echo "==> Building RavensPort internal CA and leaf certificates"
rm -rf "$SCRATCH"
mkdir -p "$SCRATCH/newcerts"
touch "$SCRATCH/index.txt"

# Minimal `openssl ca` configuration. The subject is always supplied via
# `-subj` on each invocation below, so [req_dn] is deliberately empty and
# shared by the root and every leaf rather than templated per-CN.
cat > "$SCRATCH/ca.cnf" <<CNF
[ca]
default_ca = CA_default

[CA_default]
dir             = $SCRATCH
database        = $SCRATCH/index.txt
new_certs_dir   = $SCRATCH/newcerts
serial          = $SCRATCH/serial
default_md      = sha256
policy          = policy_anything
email_in_dn     = no
unique_subject  = no

[policy_anything]
commonName = supplied

[req]
distinguished_name = req_dn
prompt = no

[req_dn]
CNF

# ── root: self-signed, from render.py's deterministic ca.key ─────────────────
ROOT_SERIAL_HEX=$(params_serial_hex "['root']['serial']")
ROOT_NOT_BEFORE=$(params_field "['root']['not_before']")
ROOT_NOT_AFTER=$(params_field "['root']['not_after']")

# basicConstraints/keyUsage are explicit, not left to openssl.cnf defaults
# (which vary by distribution): a strict TLS handshake verifier — unlike the
# lenient default of `openssl verify` — requires a CA certificate to assert
# CA:TRUE and keyCertSign explicitly, or it rejects the chain.
cat > "$SCRATCH/root_ext.cnf" <<'EXT'
basicConstraints=critical,CA:TRUE
keyUsage=critical,keyCertSign,cRLSign
EXT

openssl req -new -key "$CA_DIR/ca.key" -out "$SCRATCH/ca.csr" \
    -config "$SCRATCH/ca.cnf" -subj "/CN=RavensPort Internal CA"
echo "$ROOT_SERIAL_HEX" > "$SCRATCH/serial"
openssl ca -config "$SCRATCH/ca.cnf" -selfsign \
    -keyfile "$CA_DIR/ca.key" -in "$SCRATCH/ca.csr" -out "$CA_DIR/ca.pem" \
    -startdate "$ROOT_NOT_BEFORE" -enddate "$ROOT_NOT_AFTER" \
    -extfile "$SCRATCH/root_ext.cnf" -batch -notext >/dev/null

# ── leaves: signed by the root above ──────────────────────────────────────────
sign_leaf() {
    local hostname="$1"
    local out_dir="$TLS_DIR/$hostname"
    local serial_hex not_before not_after

    serial_hex=$(params_serial_hex "['leaves']['$hostname']['serial']")
    not_before=$(params_field "['leaves']['$hostname']['not_before']")
    not_after=$(params_field "['leaves']['$hostname']['not_after']")

    # Hostname-only DNS SAN, no IP SAN (digital.md §8 decision 7): trust
    # depends on hostname resolution, not on network topology.
    cat > "$SCRATCH/${hostname}_ext.cnf" <<EXT
basicConstraints=CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectAltName=DNS:${hostname}
EXT

    openssl req -new -key "$out_dir/key.pem" -out "$SCRATCH/${hostname}.csr" \
        -config "$SCRATCH/ca.cnf" -subj "/CN=${hostname}"
    echo "$serial_hex" > "$SCRATCH/serial"
    openssl ca -config "$SCRATCH/ca.cnf" \
        -keyfile "$CA_DIR/ca.key" -cert "$CA_DIR/ca.pem" \
        -in "$SCRATCH/${hostname}.csr" -out "$out_dir/cert.pem" \
        -startdate "$not_before" -enddate "$not_after" \
        -extfile "$SCRATCH/${hostname}_ext.cnf" -batch -notext >/dev/null
}

for node in $LEAVES; do
    echo "==> Signing leaf certificate for $node"
    sign_leaf "$node"
done

# HAProxy's `crt` directive wants certificate and key concatenated in one
# file; rebuilt unconditionally, since concatenation is cheap and this keeps
# hmi-01.pem in step with cert.pem/key.pem regardless of what changed above.
cat "$TLS_DIR/hmi-01/cert.pem" "$TLS_DIR/hmi-01/key.pem" > "$TLS_DIR/hmi-01/hmi-01.pem"
chmod 600 "$TLS_DIR/hmi-01/hmi-01.pem"

cp "$CA_DIR/ca.pem" "$SIM_CA_ROOT"

rm -rf "$SCRATCH"
echo "$CURRENT_HASH" > "$CA_DIR/.input-hash"
echo "==> CA and leaf certificates ready"
