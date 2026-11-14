"""
Security monitoring management-interface credential hash tests (siem-tests.md
§10), targeting deployment-config.md §6 and siem.md §8.5.

siem-01's management interface credential (identities.siem_admin) is seeded
into the Wazuh manager's own role-based access control database
(/var/ossec/api/configuration/security/rbac.db) at image-build time, hashed by
render.py rather than left for the manager to hash at first start — the same
reasoning as the FMS argon2id and Grafana PBKDF2 hashes. The scheme is the
manager's own: Wazuh 4.14.7 hashes management-interface users through its
bundled Werkzeug 3.1.6 generate_password_hash, whose default is scrypt with
cost parameters n=32768, r=8, p=1 and a 16-character salt drawn from
[a-zA-Z0-9], stored as scrypt:32768:8:1$<salt>$<hex-digest>.

render.py must reproduce this deterministically: the salt is derived from
crypto.password_salt_seed combined with the username (the same _derive_salt
mechanism used for the argon2id, SSHA, and Grafana hashes), mapped into that
16-character alphabet, and the digest computed with the standard-library
hashlib.scrypt under the same parameters.

The function under test is assumed to be named `compute_wazuh_scrypt_hash` and
to return the complete stored string; if the implementation names it
differently, edit `_wazuh_hash` below.

Run with: pytest tests/unit -q
"""

from __future__ import annotations

import hashlib
import re
import string
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]  # deploy/
sys.path.insert(0, str(ROOT / "scripts"))
import render  # noqa: E402

# Wazuh 4.14.7's bundled Werkzeug 3.1.6 generate_password_hash default
# (deployment-config.md §6).
_N = 32768
_R = 8
_P = 1
_SALT_LENGTH = 16
_SALT_ALPHABET = string.ascii_letters + string.digits
_STORED_RE = re.compile(r"\Ascrypt:(\d+):(\d+):(\d+)\$([a-zA-Z0-9]{16})\$([0-9a-f]+)\Z")


def load_world() -> dict:
    return yaml.safe_load((ROOT / "world.yaml").read_text())


def _wazuh_hash(password: str, seed_hex: str, username: str) -> str:
    """Call render.py's Wazuh scrypt hashing function. Returns the complete
    scrypt:n:r:p$salt$hex string.

    The indirection exists so that a rename in render.py is fixed in one place
    rather than four; a missing function raises AttributeError naming it.
    """
    return render.compute_wazuh_scrypt_hash(password, seed_hex, username)


_WERKZEUG_DKLEN = 64  # Werkzeug's scrypt method fixes a 64-byte derived key.


def _werkzeug_scrypt(password: str, salt: str) -> str:
    """Reproduce Werkzeug's own scrypt digest for the pinned parameters,
    independently of render.py's implementation."""
    derived = hashlib.scrypt(
        password.encode(),
        salt=salt.encode(),
        n=_N,
        r=_R,
        p=_P,
        dklen=_WERKZEUG_DKLEN,
        # OpenSSL's scrypt requires roughly 128*r*n bytes of working memory
        # (~32 MB for these parameters); hashlib's own default maxmem is
        # smaller than that and raises rather than raising the limit itself.
        maxmem=256 * 1024 * 1024,
    )
    return derived.hex()


def test_wazuh_hash_is_reproducible_across_renders():
    """The same (password, seed, username) must hash identically every render,
    or a rebuild silently changes the deployed management credential
    (deployment.md §9.1). This holds only if the salt is derived
    deterministically, not generated randomly by the manager at first start."""
    world = load_world()
    seed = world["crypto"]["password_salt_seed"]

    first = _wazuh_hash("a-management-password", seed, "siem_admin")
    second = _wazuh_hash("a-management-password", seed, "siem_admin")
    assert first == second, (
        "identical inputs gave different results: the salt is random, not "
        "derived from crypto.password_salt_seed, so every rebuild bakes a "
        "different management credential into the image (deployment.md §9.1)"
    )


def test_wazuh_hash_salt_varies_per_username():
    """Two identities with the same plaintext password must hash differently:
    the salt varies per identity, not one fixed salt reused for every
    credential in the pipeline."""
    world = load_world()
    seed = world["crypto"]["password_salt_seed"]
    shared_password = "same-plaintext-password"

    first = _wazuh_hash(shared_password, seed, "siem_admin")
    second = _wazuh_hash(shared_password, seed, "another_user")
    assert first != second, (
        "two usernames sharing a plaintext password produced the same hash: "
        "the salt derives from the seed alone, so cracking one instance's "
        "credential yields the other's"
    )


def test_wazuh_hash_matches_stored_format():
    """The stored form must be scrypt:32768:8:1$<16-char-salt>$<hex-digest>,
    the exact shape Wazuh's rbac.db column expects — anything else is rejected
    at load or silently never matches at login."""
    world = load_world()
    seed = world["crypto"]["password_salt_seed"]

    stored = _wazuh_hash("a-management-password", seed, "siem_admin")
    m = _STORED_RE.match(stored)
    assert m is not None, f"{stored!r} does not match scrypt:n:r:p$salt$hex"
    assert (int(m.group(1)), int(m.group(2)), int(m.group(3))) == (_N, _R, _P), (
        f"cost parameters {m.groups()[:3]} do not match n={_N}, r={_R}, p={_P}"
    )
    salt = m.group(4)
    assert len(salt) == _SALT_LENGTH
    assert all(c in _SALT_ALPHABET for c in salt), (
        f"salt {salt!r} contains a character outside [a-zA-Z0-9]"
    )


def test_wazuh_hash_reproduces_werkzeug_scrypt():
    """The produced digest must be byte-for-byte what Werkzeug's own scrypt
    invocation would produce for the same (password, salt) — confirming the
    n=32768/r=8/p=1 parameters, not merely a plausible-looking value."""
    world = load_world()
    seed = world["crypto"]["password_salt_seed"]

    stored = _wazuh_hash("a-management-password", seed, "siem_admin")
    m = _STORED_RE.match(stored)
    assert m is not None, f"{stored!r} does not match scrypt:n:r:p$salt$hex"
    salt, digest_hex = m.group(4), m.group(5)
    expected = _werkzeug_scrypt("a-management-password", salt)
    assert digest_hex == expected, (
        "the seeded digest is not what Werkzeug's scrypt produces for this "
        "salt: a cost parameter or key length diverges, so the manager will "
        "reject the seeded credential at login"
    )
