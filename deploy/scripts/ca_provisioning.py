"""Deterministic key material and X.509 parameters for the RavensPort
internal certificate authority (digital.md §2.7, §8 decision 7).

RSA key generation is deliberately non-seedable in most crypto libraries —
OpenSSL, Python's `cryptography` — precisely to prevent predictable keys
(the class of bug behind the 2008 Debian OpenSSL PRNG disaster). This module
does it anyway, on purpose: an attacker with `world.yaml` can already
regenerate every other credential in the deployment (deployment-config.md
§6), so the CA root key must be no different, and the render pipeline's
"identical inputs -> byte-identical artefacts" guarantee (deployment.md
§9.1) depends on it too. These are simulation-only credentials with no
real-world asset behind them — the same reasoning that already justifies
plaintext secrets in `world.yaml`.

This module owns only the *deterministic randomness* and the resulting raw
key material — `render.py` calls `write_key_material` unconditionally on
every render, cheaply, since it is pure computation. It does not touch
X.509 structure (extensions, self-signing, CSR signing): that remains
`scripts/build-ca.sh`'s job, using ordinary `openssl` tooling, consuming the
key material and the `params.json` this module writes alongside it.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from Crypto.PublicKey import RSA

RSA_BITS = 2048

CA_LABEL = "ca-root"
LEAF_LABELS = {
    "fms-01": "leaf:fms-01",
    "hmi-01": "leaf:hmi-01",
    "hist-01": "leaf:hist-01",
    "hist-dmz-01": "leaf:hist-dmz-01",
    "corp-it": "leaf:corp-it",
    "siem-01": "leaf:siem-01",
    "recon-01": "leaf:recon-01",
}

ROOT_VALIDITY_DAYS = 3650
LEAF_VALIDITY_DAYS = 365

_OPENSSL_TIME_FORMAT = "%Y%m%d%H%M%SZ"


class _DeterministicStream:
    """A counter-mode SHA-256 byte stream, deterministic from `seed`.

    Used as PyCryptodome's `randfunc` in place of the OS CSPRNG: each call
    returns the next `n` pseudorandom bytes from an unbounded, reproducible
    stream, so the same seed always drives RSA.generate's prime search down
    the same path to the same key.
    """

    def __init__(self, seed: bytes) -> None:
        self._seed = seed
        self._counter = 0
        self._buffer = b""

    def __call__(self, n: int) -> bytes:
        while len(self._buffer) < n:
            block = hashlib.sha256(
                self._seed + self._counter.to_bytes(8, "big")
            ).digest()
            self._buffer += block
            self._counter += 1
        result, self._buffer = self._buffer[:n], self._buffer[n:]
        return result


def derive_seed(seed_hex: str, label: str) -> bytes:
    """A deterministic 32-byte seed for `label`, derived from world.yaml's
    `crypto.password_salt_seed` — the same HMAC-SHA256 construction render.py
    already uses for every other seeded secret (`_derive_salt`), but the full
    digest rather than a truncated salt, since this seeds a key rather than
    salting a hash."""
    return hmac.new(bytes.fromhex(seed_hex), label.encode(), hashlib.sha256).digest()


def generate_rsa_key_pem(seed_hex: str, label: str, bits: int = RSA_BITS) -> bytes:
    """A deterministic RSA private key, PEM-encoded. The same (seed_hex,
    label) always yields the same key; a different label (e.g. "ca-root" vs.
    "leaf:fms-01") always yields a different one under the same seed, so
    compromising one leaf's key does not compromise the root or its sibling."""
    seed = derive_seed(seed_hex, label)
    key = RSA.generate(bits, randfunc=_DeterministicStream(seed))
    return key.export_key("PEM")


def deterministic_serial(seed_hex: str, label: str) -> int:
    """A positive X.509 serial number, deterministic from (seed_hex, label).
    Capped at 159 bits (not 160): a serial whose top bit is set is read as
    negative under DER's two's-complement INTEGER encoding unless padded,
    which some strict validators reject outright."""
    digest = hmac.new(
        bytes.fromhex(seed_hex), f"serial:{label}".encode(), hashlib.sha256
    ).digest()
    value = int.from_bytes(digest[:20], "big") & ((1 << 159) - 1)
    return value or 1  # zero is not a valid serial number


def _validity_window(not_before: datetime, days: int) -> dict[str, str]:
    not_after = not_before + timedelta(days=days)
    return {
        "not_before": not_before.strftime(_OPENSSL_TIME_FORMAT),
        "not_after": not_after.strftime(_OPENSSL_TIME_FORMAT),
    }


def _check_not_stale(not_before: datetime) -> None:
    """Refuses to build a CA whose leaf certificates would already be expired
    (digital.md §8 decision 7's 1-year leaf validity, checked against the
    shorter of the two windows since it is the binding constraint). This is
    the exact failure mode a hardcoded `notBefore` constant was silently
    exposed to; making the field required in world.yaml documents that it is
    load-bearing, and this check makes forgetting to move it forward a
    render-time failure instead of a runtime TLS one."""
    leaf_not_after = not_before + timedelta(days=LEAF_VALIDITY_DAYS)
    now = datetime.now(timezone.utc)
    if leaf_not_after <= now:
        raise SystemExit(
            f"ERROR: crypto.ca_not_before ({not_before.date()}) is stale — leaf "
            f"certificates valid for {LEAF_VALIDITY_DAYS} days from that date "
            f"expired on {leaf_not_after.date()}, before today ({now.date()}). "
            "Move ca_not_before forward in world.yaml and re-render."
        )


def write_key_material(seed_hex: str, not_before: date, build_dir: Path) -> None:
    """Writes the deterministic private keys and the X.509 parameters
    (`params.json`) `build-ca.sh` needs to sign real certificates from them —
    serial numbers and a validity window, both deterministic and pinned, so
    that repeated (re)signing from unchanged inputs reproduces byte-identical
    certificates, not just byte-identical keys. `not_before` comes from
    world.yaml's `crypto.ca_not_before` (required, not a source constant —
    see that field's comment for why).

    Idempotent and side-effect-free to re-run: called unconditionally on
    every render (main() in render.py), unlike build-ca.sh's own X.509
    construction step, which is the one that is actually worth caching.
    """
    not_before_dt = datetime.combine(
        not_before, datetime.min.time(), tzinfo=timezone.utc
    )
    _check_not_stale(not_before_dt)

    ca_dir = build_dir / "ca"
    ca_dir.mkdir(parents=True, exist_ok=True)
    (ca_dir / "ca.key").write_bytes(generate_rsa_key_pem(seed_hex, CA_LABEL))

    params = {
        "root": {
            "serial": deterministic_serial(seed_hex, CA_LABEL),
            **_validity_window(not_before_dt, ROOT_VALIDITY_DAYS),
        },
        "leaves": {},
    }
    for node, label in LEAF_LABELS.items():
        leaf_dir = build_dir / "tls" / node
        leaf_dir.mkdir(parents=True, exist_ok=True)
        (leaf_dir / "key.pem").write_bytes(generate_rsa_key_pem(seed_hex, label))
        params["leaves"][node] = {
            "serial": deterministic_serial(seed_hex, label),
            **_validity_window(not_before_dt, LEAF_VALIDITY_DAYS),
        }

    (ca_dir / "params.json").write_text(
        json.dumps(params, indent=2, sort_keys=True) + "\n"
    )
