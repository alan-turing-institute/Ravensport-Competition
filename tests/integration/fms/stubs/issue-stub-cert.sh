#!/usr/bin/env bash
# tests/integration/fms/stubs/issue-stub-cert.sh
#
# Issues the syslog stub's TLS certificate from the deployment's internal CA.
#
# Usage:
#   issue-stub-cert.sh <ca-dir> <out-dir> <name>
#
# Writes <out-dir>/<name>.pem and <out-dir>/<name>.key. The FMS verifies the
# syslog listener against the system trust store and the name it dials
# (fms.md §3), so the stub needs a leaf the deployment's CA signed, for a DNS
# name rather than an address (digital.md §2.7). Signing with <ca-dir>/ca.key
# (written by `make -C deploy build-ca`) means fms-01 accepts the stub through
# the trust path it uses for hist-01, and nothing is added to its trust store.
#
# The extensions match build-ca.sh's leaves. The validity window starts an hour
# in the past, so a small clock difference between the runner and fms-01 does
# not make the leaf not-yet-valid, and ends a day ahead; the Makefile re-issues
# the leaf on every run.

set -euo pipefail

CA_DIR="$1"
OUT_DIR="$2"
NAME="$3"

if [ ! -f "$CA_DIR/ca.key" ] || [ ! -f "$CA_DIR/ca.pem" ]; then
    echo "ERROR: $CA_DIR/ca.key or ca.pem not found — run 'make -C deploy build-ca' first." >&2
    exit 1
fi

mkdir -p "$OUT_DIR"
SCRATCH="$(mktemp -d)"
trap 'rm -rf "$SCRATCH"' EXIT
mkdir -p "$SCRATCH/newcerts"
touch "$SCRATCH/index.txt"
openssl rand -hex 16 > "$SCRATCH/serial"

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

cat > "$SCRATCH/ext.cnf" <<EXT
basicConstraints=CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectAltName=DNS:${NAME}
EXT

openssl req -new -newkey rsa:2048 -nodes -keyout "$OUT_DIR/$NAME.key" \
    -out "$SCRATCH/leaf.csr" -config "$SCRATCH/ca.cnf" -subj "/CN=${NAME}" 2>/dev/null
chmod 600 "$OUT_DIR/$NAME.key"
openssl ca -config "$SCRATCH/ca.cnf" \
    -keyfile "$CA_DIR/ca.key" -cert "$CA_DIR/ca.pem" \
    -in "$SCRATCH/leaf.csr" -out "$OUT_DIR/$NAME.pem" \
    -startdate "$(date -u -d '-1 hour' +%Y%m%d%H%M%SZ)" \
    -enddate "$(date -u -d '+1 day' +%Y%m%d%H%M%SZ)" \
    -extfile "$SCRATCH/ext.cnf" -batch -notext 2>/dev/null

echo "Issued $OUT_DIR/$NAME.pem for DNS:$NAME, signed by $CA_DIR/ca.pem."
