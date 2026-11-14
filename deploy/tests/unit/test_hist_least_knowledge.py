"""
Least-knowledge tests for the log-server nodes (hist-tests.md §8), targeting
deployment-config.md §5: "For each deployed node, assert that its baked
configuration set — every projection written into the node's image — contains
only the facts that node would realistically possess."

That rule is why these tests read the *whole* artefact set for a node rather than
one file. A credential that never appears in an env file can still reach an image
through a build script, a seed, or a provisioning file, and the check is worthless
if it only looks where the secret was expected to be.

Password hashes deliberately bypass PROJECTION_MAP — world_schema.py records that
the map carries only cross-consumer scalar facts — so no existing projection test
covers them. This module is that coverage.

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

_HIST_IDENTITY = {
    "hist-01": "hist_admin",
    "hist-dmz-01": "reconciliation_user",
}


def _render(profile: str = "ci-minimal") -> Path:
    out = subprocess.run(
        [sys.executable, "scripts/render.py", "--profile", profile],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert out.returncode == 0, out.stderr
    return ROOT / "build"


def _world() -> dict:
    return yaml.safe_load((ROOT / "world.yaml").read_text())


def _artefacts_for(build: Path, node: str) -> dict[str, str]:
    """Every rendered file baked into `node`'s image, keyed by path relative to
    build/.

    Ownership is by filename: render.py writes per-node artefacts as either
    `<dir>/<node>.<ext>` or `<dir>/<node>-<something>`. Walking the tree rather
    than listing known directories means an artefact added later is covered
    automatically, which is the point — the leak this guards against is a *new*
    projection carrying something it should not.
    """
    owned: dict[str, str] = {}
    for path in sorted(build.rglob("*")):
        if not path.is_file():
            continue
        stem = path.name.split(".", 1)[0]
        if stem == node or stem.startswith(f"{node}-"):
            owned[str(path.relative_to(build))] = path.read_text(errors="replace")
    return owned


def _build_script_nodes(build: Path) -> list[str]:
    manifest = json.loads((build / "manifest.json").read_text())
    return sorted(manifest["build_script_nodes"])


# ── Secrets that must reach no node at all ────────────────────────────────────


def test_no_plaintext_password_reaches_any_node():
    """world.yaml holds plaintext so render.py can derive hashes from it. The
    plaintext itself is a build-host fact: every consumer receives a hash or an
    encoded form, so a plaintext in any baked artefact means a projection skipped
    its hashing step."""
    build = _render()
    world = _world()
    plaintexts = {
        f"identities.{key}": world["identities"][key]["password"]
        for key in ("admin", "hist_admin", "reconciliation_user")
    }
    for i, op in enumerate(world["identities"]["operators"]):
        plaintexts[f"identities.operators[{i}]"] = op["password"]

    for node in _build_script_nodes(build):
        for rel, content in _artefacts_for(build, node).items():
            for source, secret in plaintexts.items():
                assert secret not in content, (
                    f"{rel} carries the plaintext of {source}; only a derived "
                    "hash may reach a node (deployment-config.md §6)"
                )


def test_salt_seed_is_never_projected():
    """crypto.password_salt_seed is the reproducibility surface for every derived
    credential in the deployment, and world.yaml states it reaches no node.
    Possession of the seed and a username reproduces that identity's salt for all
    three hashing schemes at once."""
    build = _render()
    seed = _world()["crypto"]["password_salt_seed"]

    for node in _build_script_nodes(build):
        for rel, content in _artefacts_for(build, node).items():
            assert seed not in content, (
                f"{rel} carries crypto.password_salt_seed, which derives the salt "
                "for every identity's credential and belongs only to the build host"
            )


# ── Grafana credentials stay on their own instance ────────────────────────────


@pytest.mark.parametrize("node,identity", _HIST_IDENTITY.items())
def test_log_server_carries_its_own_grafana_credential(node: str, identity: str):
    """The positive half, so that a later refactor cannot satisfy the isolation
    tests below by projecting nothing at all."""
    build = _render()
    world = _world()
    entry = world["identities"][identity]
    pw_hash, salt = render.compute_pbkdf2_grafana_hash(
        entry["password"], world["crypto"]["password_salt_seed"], entry["username"]
    )

    combined = "\n".join(_artefacts_for(build, node).values())
    assert pw_hash in combined, (
        f"{node} carries no Grafana password hash for {entry['username']!r}; its "
        "instance would have no usable account"
    )
    assert salt in combined, (
        f"{node} carries the hash for {entry['username']!r} but not its salt; "
        "Grafana recomputes the digest from the stored salt, so login fails"
    )


@pytest.mark.parametrize("node,identity", _HIST_IDENTITY.items())
def test_grafana_credential_reaches_no_other_node(node: str, identity: str):
    """Each Grafana credential belongs to exactly one instance. hist-01's admin
    reaching the DMZ would put an operational-technology credential on the host
    the reconciliation user can log into; the reverse would place the DMZ
    credential inside the OT zone. hist-tests.md §6 asserts the instances reject
    each other's credentials at run time — this asserts they never had the chance
    to accept them."""
    build = _render()
    world = _world()
    entry = world["identities"][identity]
    pw_hash, salt = render.compute_pbkdf2_grafana_hash(
        entry["password"], world["crypto"]["password_salt_seed"], entry["username"]
    )

    for other in _build_script_nodes(build):
        if other == node:
            continue
        for rel, content in _artefacts_for(build, other).items():
            assert pw_hash not in content, (
                f"{rel} carries {node}'s Grafana password hash for "
                f"{entry['username']!r}"
            )
            assert salt not in content, (
                f"{rel} carries {node}'s Grafana salt for {entry['username']!r}"
            )
            # The verifier must not travel; the name may, where the account
            # genuinely exists. `recon-user` is one person holding accounts on
            # two systems with two separate passwords (recon.md §7.1,
            # digital.md §5.2): Grafana on hist-dmz-01, and the workstation
            # login in the directory on corp-it, whose LDIF therefore names them
            # legitimately. The hash and salt assertions above still apply, and
            # they are the ones that matter — knowing the name grants nothing,
            # holding the verifier does.
            #
            # `recon-01` itself is deliberately not exempted. It authenticates
            # against the directory and holds no account of its own, so its
            # artefacts naming the reconciliation user would be a regression
            # towards the local account this replaced.
            if (node, other) == ("hist-dmz-01", "corp-it"):
                continue
            assert entry["username"] not in content, (
                f"{rel} names {entry['username']!r}, an account that exists only "
                f"on {node}"
            )


# ── The log servers hold nothing belonging to other components ────────────────


@pytest.mark.parametrize("node", _HIST_IDENTITY)
def test_log_server_carries_no_other_components_credentials(node: str):
    """A log server stores what it is sent. It has no reason to hold the FMS
    administrator's hash, an operator's directory password, or the LDAP base
    distinguished name: none authenticate anything on this host, where Grafana's
    built-in store is the only authority (hist.md §7)."""
    build = _render()
    world = _world()
    seed = world["crypto"]["password_salt_seed"]

    forbidden = {
        "the FMS administrator's argon2id hash": render.compute_argon2id_hash(
            world["identities"]["admin"]["password"],
            seed,
            world["identities"]["admin"]["username"],
        ),
        "the LDAP base distinguished name": world["ldap"]["base_dn"],
    }
    for op in world["identities"]["operators"]:
        forbidden[f"operator {op['username']}'s SSHA password"] = (
            render.compute_ssha_password(op["password"], seed, op["username"])
        )
        forbidden[f"operator {op['username']}'s username"] = op["username"]

    for rel, content in _artefacts_for(build, node).items():
        for description, secret in forbidden.items():
            assert secret not in content, (
                f"{rel} carries {description}; the log server authenticates "
                "nobody against it and has no reason to hold it"
            )


@pytest.mark.parametrize("node", _HIST_IDENTITY)
def test_log_server_carries_no_rfid_card_numbers(node: str):
    """Card numbers belong to the simulated-physical world and to the FMS that
    validates them. A log server receives events *about* card presentations at run
    time over the ingestion path; baking the roster into its image would let an
    attacker who reaches the historian enumerate valid cards without ever
    observing one in use."""
    build = _render()
    world = _world()

    cards = {
        f"operator {op['username']}": op["rfid_card"]
        for op in world["identities"]["operators"]
    }
    cards.update(
        {f"vessel {v['name']}": v["rfid_card"] for v in world["roster"]["vessels"]}
    )

    for rel, content in _artefacts_for(build, node).items():
        for owner, card in cards.items():
            assert str(card) not in content, (
                f"{rel} contains RFID card number {card} ({owner}); the roster is "
                "not a log-server fact"
            )
