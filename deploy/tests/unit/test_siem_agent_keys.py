"""
Security monitoring agent pre-shared key tests (siem-tests.md §10), targeting
siem.md §2.1/§8.5.

Each monitored node's Wazuh agent authenticates to the manager on TCP 1514
with a pre-shared key, unique per agent, derived from crypto.password_salt_seed
combined with the node name — the same deterministic idiom as every other
credential in this pipeline, but unhashed: an agent key is a live symmetric
secret, not a verifier, so render.py derives it and projects it directly.
siem-01 receives the complete set (build/siem/client.keys); each monitored
node receives only its own key.

The function under test is assumed to be named `derive_agent_key` and to
return the full 64-character lowercase hex key; if the implementation names
it differently, edit `_agent_key` below.

Run with: pytest tests/unit -q
"""

from __future__ import annotations

import hashlib
import hmac
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]  # deploy/
sys.path.insert(0, str(ROOT / "scripts"))
import render  # noqa: E402

_HEX_KEY_RE = re.compile(r"\A[0-9a-f]{64}\Z")

# The twelve monitored hosts (siem.md §1): every deployed host except sim-host.
_MONITORED_NODES = (
    "fms-01",
    "hmi-01",
    "hist-01",
    "hist-dmz-01",
    "atg-relay-01",
    "dmz-bastion-01",
    "corp-it",
    "recon-01",
    "admin-01",
    "fw-ext-dmz",
    "fw-dmz-it",
    "fw-ot-dmz",
)


def load_world() -> dict:
    return yaml.safe_load((ROOT / "world.yaml").read_text())


def _agent_key(seed_hex: str, node_name: str) -> str:
    """Call render.py's agent-key derivation function.

    The indirection exists so that a rename in render.py is fixed in one place
    rather than many; a missing function raises AttributeError naming it.
    """
    return render.derive_agent_key(seed_hex, node_name)


def _independent_key(seed_hex: str, node_name: str) -> str:
    """Reproduce the derivation independently of render.py's implementation:
    the full HMAC-SHA256 digest of the node name under the seed, hex-encoded —
    the same construction _derive_salt uses, but untruncated, since a
    pre-shared key needs 32 bytes rather than the 16 a salt needs."""
    seed = bytes.fromhex(seed_hex)
    return hmac.new(seed, node_name.encode(), hashlib.sha256).hexdigest()


def test_agent_key_is_reproducible_across_renders():
    """The same (seed, node name) must derive the same key every render, or a
    rebuild silently changes a live authentication secret and the manager and
    agent images fall out of sync (deployment.md §9.1)."""
    world = load_world()
    seed = world["crypto"]["password_salt_seed"]

    first = _agent_key(seed, "fms-01")
    second = _agent_key(seed, "fms-01")
    assert first == second


def test_agent_key_is_64_lowercase_hex_characters():
    world = load_world()
    seed = world["crypto"]["password_salt_seed"]

    key = _agent_key(seed, "fms-01")
    assert _HEX_KEY_RE.match(key), f"{key!r} is not 64 lowercase hex characters"


def test_agent_keys_are_reproduced_independently():
    """The derivation must match a from-scratch computation of the same
    construction, not merely be internally consistent with itself."""
    world = load_world()
    seed = world["crypto"]["password_salt_seed"]

    for node in _MONITORED_NODES:
        assert _agent_key(seed, node) == _independent_key(seed, node), (
            f"{node}'s derived key does not match the expected HMAC-SHA256 "
            "construction over crypto.password_salt_seed and the node name"
        )


def test_every_monitored_node_gets_a_distinct_key():
    """No two monitored nodes may share a key: a collision would let one
    agent's traffic decrypt or be mistaken for another's."""
    world = load_world()
    seed = world["crypto"]["password_salt_seed"]

    keys = {node: _agent_key(seed, node) for node in _MONITORED_NODES}
    assert len(set(keys.values())) == len(keys), (
        f"duplicate agent keys among monitored nodes: {keys}"
    )


def test_siem_01_client_keys_carries_the_complete_set():
    """siem-01's projected client.keys (build/siem/client.keys) must carry one
    line per monitored node, each in Wazuh's <id> <name> <ip> <key> format,
    with the source-address field left unrestricted (siem.md §8.5): the
    two-hop OT→DMZ→IT path and the multiple firewall-node interfaces make a
    single source address unreliable, so authentication rests on the key."""
    world = load_world()
    seed = world["crypto"]["password_salt_seed"]

    client_keys_path = ROOT / "build" / "siem" / "client.keys"
    assert client_keys_path.exists(), (
        "build/siem/client.keys was not rendered — render.py must project the "
        "complete agent-key set onto siem-01 (siem.md §8.5)"
    )
    content = client_keys_path.read_text()
    lines = [line for line in content.splitlines() if line.strip()]
    assert len(lines) == len(_MONITORED_NODES), (
        f"client.keys carries {len(lines)} lines, expected one per monitored "
        f"node ({len(_MONITORED_NODES)})"
    )

    by_name = {}
    for line in lines:
        _agent_id, name, ip, key = line.split()
        assert ip == "any", (
            f"{name}'s client.keys entry restricts the source address to "
            f"{ip!r}; siem.md §8.5 requires 'any'"
        )
        by_name[name] = key

    for node in _MONITORED_NODES:
        assert node in by_name, f"client.keys is missing an entry for {node}"
        assert by_name[node] == _agent_key(seed, node), (
            f"client.keys carries the wrong key for {node}"
        )


def test_each_monitored_node_projects_only_its_own_key():
    """Each monitored node's own key file must carry exactly one key — its
    own — never another node's or the complete set siem-01 holds."""
    world = load_world()
    seed = world["crypto"]["password_salt_seed"]

    for node in _MONITORED_NODES:
        key_path = ROOT / "build" / "siem" / f"{node}.key"
        assert key_path.exists(), f"build/siem/{node}.key was not rendered"
        content = key_path.read_text()

        assert _agent_key(seed, node) in content, (
            f"{node}.key does not carry {node}'s own derived key"
        )
        for other in _MONITORED_NODES:
            if other == node:
                continue
            assert _agent_key(seed, other) not in content, (
                f"{node}.key carries {other}'s agent key"
            )
