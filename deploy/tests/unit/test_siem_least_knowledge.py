"""
Least-knowledge tests for security monitoring (siem-tests.md §10), targeting
deployment-config.md §5: "For each deployed node, assert that its baked
configuration set — every projection written into the node's image — contains
only the facts that node would realistically possess."

siem-01 is the declared exception to that rule (deployment-config.md §5):
the manager must know every agent's key, so it legitimately holds the complete
set, while each monitored node holds only its own. This module asserts both
halves of that exception, in the manner of test_hist_least_knowledge.py, and
that siem-01's management-interface credential reaches no other node.

Run with: pytest tests/unit -q
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]  # deploy/
sys.path.insert(0, str(ROOT / "scripts"))
import render  # noqa: E402

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


def _render(profile: str = "ci-minimal") -> Path:
    out = subprocess.run(
        [sys.executable, "scripts/render.py", "--profile", profile],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode == 0, out.stderr
    return ROOT / "build"


def _world() -> dict:
    return yaml.safe_load((ROOT / "world.yaml").read_text())


def _artefacts_for(build: Path, node: str) -> dict[str, str]:
    """Every rendered file baked into `node`'s image, keyed by path relative to
    build/ — files whose name stems with the node's own name, the convention
    every per-node build artefact in this pipeline follows (e.g.
    `build/tls/<node>/cert.pem`, `build/grafana/<node>-user-seed.sql`).

    `build/siem/client.keys` is the one exception: Wazuh's own client.keys
    format is a fixed filename, not a per-node one, so it is attributed to
    `siem-01` explicitly rather than by the naming convention — it is the
    complete agent-key set siem-01 legitimately holds (siem.md §8.5)."""
    owned: dict[str, str] = {}
    for path in sorted(build.rglob("*")):
        if not path.is_file():
            continue
        stem = path.name.split(".", 1)[0]
        is_siem_client_keys = node == "siem-01" and path.relative_to(build) == Path(
            "siem/client.keys"
        )
        if stem == node or stem.startswith(f"{node}-") or is_siem_client_keys:
            owned[str(path.relative_to(build))] = path.read_text(errors="replace")
    return owned


def _build_script_nodes(build: Path) -> list[str]:
    manifest = json.loads((build / "manifest.json").read_text())
    return sorted(manifest["build_script_nodes"])


def _agent_key(seed_hex: str, node_name: str) -> str:
    return render.derive_agent_key(seed_hex, node_name)


def test_siem_01_carries_every_monitored_node_s_key():
    """The positive half of the declared exception: siem-01 must actually hold
    every agent's key, or the manager cannot authenticate any of them."""
    build = _render()
    seed = _world()["crypto"]["password_salt_seed"]

    combined = "\n".join(_artefacts_for(build, "siem-01").values())
    for node in _MONITORED_NODES:
        assert _agent_key(seed, node) in combined, (
            f"siem-01 does not carry {node}'s agent key; the manager would "
            "reject that agent's connection"
        )


@pytest.mark.parametrize("node", _MONITORED_NODES)
def test_monitored_node_carries_only_its_own_key(node: str):
    """The negative half: a monitored node holding another node's key, or
    siem-01's complete set, would let a compromise of that one host forge
    traffic as every other agent (deployment-config.md §5)."""
    build = _render()
    seed = _world()["crypto"]["password_salt_seed"]

    combined = "\n".join(_artefacts_for(build, node).values())
    for other in _MONITORED_NODES:
        if other == node:
            continue
        assert _agent_key(seed, other) not in combined, (
            f"{node} carries {other}'s agent key"
        )


@pytest.mark.parametrize("node", _MONITORED_NODES)
def test_monitored_node_carries_no_management_credential(node: str):
    """The management-interface credential (identities.siem_admin) authenticates
    only to siem-01's own TCP 55000 interface; no monitored node has any reason
    to hold its hash."""
    build = _render()
    world = _world()
    seed = world["crypto"]["password_salt_seed"]
    admin = world["identities"]["siem_admin"]

    stored_hash = render.compute_wazuh_scrypt_hash(
        admin["password"], seed, admin["username"]
    )
    combined = "\n".join(_artefacts_for(build, node).values())
    assert stored_hash not in combined, (
        f"{node} carries siem-01's management-interface credential hash"
    )


def test_no_monitored_node_carries_another_s_agent_key_via_siem_01_leak():
    """siem-01 legitimately holds every key; this asserts every other node's
    isolation holds even when checked against the full build script node set,
    not only the ten explicitly monitored hosts — a regression guard against a
    future host gaining an unwarranted copy of the complete set."""
    build = _render()
    seed = _world()["crypto"]["password_salt_seed"]

    for node in _build_script_nodes(build):
        if node == "siem-01":
            continue
        combined = "\n".join(_artefacts_for(build, node).values())
        held = [
            other
            for other in _MONITORED_NODES
            if other != node and _agent_key(seed, other) in combined
        ]
        assert not held, f"{node} carries agent keys belonging to: {held}"
