"""
Grafana seed-hash unit tests (hist-tests.md §8), targeting deployment-config.md
§6 and hist.md §7.

hist-01's Grafana admin (identities.hist_admin) and hist-dmz-01's reconciliation
user (identities.reconciliation_user) are seeded into Grafana's internal user
table at image-build time. render.py must hash their passwords with Grafana's own
scheme — PBKDF2-HMAC-SHA256, 10,000 iterations, a 50-byte derived key,
hex-encoded (`EncodePassword(password, salt)` in Grafana's pkg/util/encoding.go)
— with the salt derived deterministically from crypto.password_salt_seed and the
username (the same _derive_salt mechanism used for the argon2id and SSHA hashes),
so the seeded row is byte-for-byte what Grafana itself would have written and is
reproducible across rebuilds.

These tests target the dedicated hashing function directly, mirroring how the FMS
argon2id and LDAP SSHA hashes are consumed by bespoke render.py code rather than
PROJECTION_MAP (password hashes never flow through that map).

The function is assumed to return a `(hash_hex, salt)` pair; if the grafana
implementation returns a different shape or name, edit `_grafana_hash` below.

Run with: pytest tests/unit -q
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]  # deploy/
sys.path.insert(0, str(ROOT / "scripts"))
import render  # noqa: E402

# Grafana's EncodePassword parameters (pkg/util/encoding.go).
_ITERATIONS = 10_000
_KEY_LENGTH = 50


def load_world() -> dict:
    return yaml.safe_load((ROOT / "world.yaml").read_text())


def _grafana_hash(password: str, seed_hex: str, username: str) -> tuple[str, object]:
    """Call render.py's Grafana hashing function. Returns a (hash_hex, salt) pair.

    The indirection exists so that a rename in render.py is fixed in one place
    rather than four; a missing function raises AttributeError naming it.
    """
    return render.compute_pbkdf2_grafana_hash(password, seed_hex, username)


def _grafana_encode_password(password: str, salt: object) -> str:
    """Reproduce Grafana's EncodePassword(password, salt): PBKDF2-HMAC-SHA256,
    10,000 iterations, 50-byte derived key, hex-encoded. Grafana treats the salt
    column as a UTF-8 string; accept either a str (encoded) or raw bytes."""
    salt_bytes = salt.encode() if isinstance(salt, str) else salt
    derived = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), salt_bytes, _ITERATIONS, _KEY_LENGTH
    )
    return derived.hex()


def test_grafana_hash_is_reproducible_across_renders():
    """The same (password, seed, username) must hash identically every render, or
    a rebuild silently changes the deployed credential (deployment.md §9.1). This
    holds only if the salt is derived deterministically, not generated randomly."""
    world = load_world()
    seed = world["crypto"]["password_salt_seed"]
    admin = world["identities"]["hist_admin"]

    first = _grafana_hash(admin["password"], seed, admin["username"])
    second = _grafana_hash(admin["password"], seed, admin["username"])
    assert first == second, (
        "identical inputs gave different results: the salt is random, not derived "
        "from crypto.password_salt_seed, so every rebuild bakes a different "
        "credential into the image (deployment.md §9.1)"
    )


def test_grafana_hash_salt_varies_per_username():
    """Two identities with the same plaintext password must hash differently: the
    salt varies per identity (derived from the username), not one fixed salt."""
    world = load_world()
    seed = world["crypto"]["password_salt_seed"]
    shared_password = "same-plaintext-password"

    admin_hash = _grafana_hash(shared_password, seed, "hist-admin")
    recon_hash = _grafana_hash(shared_password, seed, "recon-user")
    assert admin_hash != recon_hash, (
        "two usernames sharing a plaintext password produced the same hash and "
        "salt: the salt derives from the seed alone, so cracking one instance's "
        "credential yields the other's"
    )


@pytest.mark.parametrize("identity", ["hist_admin", "reconciliation_user"])
def test_grafana_hash_reproduces_grafana_encode_password(identity: str):
    """The produced hash must be byte-for-byte what Grafana's own EncodePassword
    would write for the same (password, salt) — confirming the 10,000-iteration,
    50-byte, SHA-256, hex scheme, not merely a plausible-looking value."""
    world = load_world()
    seed = world["crypto"]["password_salt_seed"]
    entry = world["identities"][identity]

    hash_hex, salt = _grafana_hash(entry["password"], seed, entry["username"])
    assert hash_hex == _grafana_encode_password(entry["password"], salt), (
        f"the seeded hash for {identity} is not what Grafana's EncodePassword "
        f"produces for salt {salt!r}: one of the PBKDF2 parameters diverges from "
        f"{_ITERATIONS} iterations / {_KEY_LENGTH}-byte key / SHA-256 / hex, so "
        "Grafana will reject the seeded credential at login"
    )
