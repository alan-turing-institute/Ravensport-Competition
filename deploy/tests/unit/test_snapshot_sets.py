"""
Snapshot sets (deployment.md §10): the stop order of a planned maintenance
window, and the checks an import makes on a snapshot file before it changes
anything.

The stop order is stated as rules in §10.2, so every node of the topology must
have a place in it, and a node with none is refused rather than stopped in an
arbitrary position. An import refuses a file whose format version,
architecture or digests do not match, whose entries are malformed, or which
lacks the rendered files its instances came from, when RavensPort instances
already exist, or when the storage pool is too small (§10.4); each refusal is
asserted here against a stub `incus` that records every command it is given,
so that a refused file is shown to have changed nothing.

Run with: pytest tests/unit -q
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import tarfile
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]  # deploy/
LIB = ROOT / "scripts" / "snapshot-lib.sh"
IMPORT = ROOT / "scripts" / "import-snapshot.sh"


def _stop_order(instances: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", f'source "{LIB}"; stop_order'],
        cwd=ROOT,
        input="\n".join(instances) + "\n",
        capture_output=True,
        text=True,
        check=False,
    )


def test_stop_order_follows_the_maintenance_window():
    instances = [
        "fw-ext-dmz",
        "fw-dmz-it",
        "fw-ot-dmz",
        "atg-relay-01",
        "hist-dmz-01",
        "dmz-bastion-01",
        "siem-01",
        "corp-it",
        "recon-01",
        "admin-01",
        "fms-01",
        "hmi-01",
        "hist-01",
        "adversary",
        "sim-host",
    ]
    result = _stop_order(instances)
    assert result.returncode == 0, result.stderr
    # The adversary host, and then the systems-administration path, workstation
    # then bastion, stop before the hosts they reach (deployment.md §10.2).
    assert result.stdout.split() == [
        "adversary",
        "admin-01",
        "dmz-bastion-01",
        "hmi-01",
        "recon-01",
        "fms-01",
        "atg-relay-01",
        "hist-01",
        "hist-dmz-01",
        "corp-it",
        "siem-01",
        "fw-ext-dmz",
        "fw-dmz-it",
        "fw-ot-dmz",
    ]


def test_every_topology_node_has_a_stop_position():
    """An instance added to topology.yaml must be placed by the rules of §10.2
    before a snapshot set can be taken."""
    nodes = list(yaml.safe_load((ROOT / "topology.yaml").read_text())["nodes"])
    result = _stop_order(nodes)
    assert result.returncode == 0, result.stderr
    assert sorted(result.stdout.split()) == sorted(n for n in nodes if n != "sim-host")


def test_a_node_without_a_stop_position_is_refused():
    result = _stop_order(["fms-01", "plc-01"])
    assert result.returncode != 0
    assert "plc-01" in result.stderr


# ── import refusals ───────────────────────────────────────────────────────────

STUB_INCUS = """#!/usr/bin/env bash
echo "$*" >>"$STUB_LOG"
case "$1 $2" in
  "query /1.0/storage-pools/default/resources")
    echo "{\\"space\\": {\\"total\\": $STUB_POOL_TOTAL, \\"used\\": 0}}" ;;
  "query /1.0/instances/"*)
    name="${2#/1.0/instances/}"
    [[ " $STUB_EXISTING " == *" $name "* ]] || exit 1
    echo '{"status": "Stopped"}' ;;
esac
"""

INSTANCES = ["fms-01", "sim-host"]


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _snapshot_file(
    tmp_path: Path,
    corrupt: str | None = None,
    rendered_nodes: list[str] = INSTANCES,
    omit: str | None = None,
    **overrides,
) -> Path:
    """A snapshot file holding `INSTANCES`, whose rendered manifest names
    `rendered_nodes`, with manifest fields replaced by `overrides`. If
    `corrupt` names a member, that member is altered after its digest was
    taken; if `omit` names a rendered file or input, the file does not carry
    it."""
    members = {
        "checkpoint.json": b'{"format_version": 1}',
        "stopped-at": b"2025-03-01T12:04:00Z\n",
        "rendered/manifest.json": json.dumps(
            {"profile": "ci-minimal", "nodes": rendered_nodes}
        ).encode(),
        "rendered/incus-commands.sh": b"# --- Instances ---\n",
        "rendered/teardown-commands.sh": b"",
        "inputs/world.yaml": b"# world\n",
    }
    if omit:
        del members[omit]
    files = [
        {"path": name, "sha256": _sha256(data)}
        for name, data in members.items()
        if name.startswith(("rendered/", "inputs/"))
    ]
    for instance in INSTANCES:
        members[f"instances/{instance}.tar.zst"] = f"export of {instance}".encode()
    manifest = {
        "format_version": 2,
        "label": "week-1",
        "profile": "ci-minimal",
        "arch": platform.machine(),
        "commit": "0" * 40,
        "incus_version": "6.0.0",
        "stopped_at": "2025-03-01T12:04:00Z",
        "instances": [
            {
                "name": instance,
                "archive": f"instances/{instance}.tar.zst",
                "sha256": _sha256(members[f"instances/{instance}.tar.zst"]),
                "disk_usage_bytes": 1000,
            }
            for instance in INSTANCES
        ],
        "files": files,
        "checkpoint_sha256": _sha256(members["checkpoint.json"]),
        "stopped_at_sha256": _sha256(members["stopped-at"]),
        **overrides,
    }
    if corrupt:
        members[corrupt] += b"x"
    members["manifest.json"] = json.dumps(manifest).encode()

    staging = tmp_path / "staging"
    for name, data in members.items():
        (staging / name).parent.mkdir(parents=True, exist_ok=True)
        (staging / name).write_bytes(data)
    path = tmp_path / "ravensport-snapshot-week-1.tar"
    with tarfile.open(path, "w") as archive:
        for name in members:
            archive.add(staging / name, arcname=name)
    return path


def _import(
    tmp_path: Path, file: Path, existing: str = "", pool_total: int = 10**12
) -> tuple[subprocess.CompletedProcess, str]:
    stub = tmp_path / "incus"
    stub.write_text(STUB_INCUS)
    stub.chmod(0o755)
    log = tmp_path / "incus.log"
    log.touch()
    env = {
        **os.environ,
        "INCUS": str(stub),
        "STUB_LOG": str(log),
        "STUB_EXISTING": existing,
        "STUB_POOL_TOTAL": str(pool_total),
        "SNAPSHOTS_DIR": str(tmp_path / "snapshots"),
    }
    result = subprocess.run(
        ["bash", str(IMPORT), str(file)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    snapshots = tmp_path / "snapshots"
    left = sorted(p.name for p in snapshots.iterdir()) if snapshots.exists() else []
    return result, log.read_text(), left


def _assert_refused_without_change(result, log: str, left: list[str], reason: str):
    assert result.returncode != 0, result.stdout
    assert reason in result.stderr, result.stderr
    changes = [line for line in log.splitlines() if not line.startswith("query ")]
    assert not changes, f"a refused file changed the deployment: {changes}"
    assert not left, f"a refused file left {left} in the snapshots directory"


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"format_version": 1}, "format version 1"),
        ({"arch": "riscv64"}, "riscv64"),
    ],
)
def test_an_incompatible_file_is_refused(tmp_path: Path, overrides: dict, reason: str):
    _assert_refused_without_change(
        *_import(tmp_path, _snapshot_file(tmp_path, **overrides)), reason
    )


@pytest.mark.parametrize(
    "member",
    [
        "checkpoint.json",
        "stopped-at",
        "instances/fms-01.tar.zst",
        "rendered/incus-commands.sh",
        "inputs/world.yaml",
    ],
)
def test_a_corrupted_member_is_refused(tmp_path: Path, member: str):
    _assert_refused_without_change(
        *_import(tmp_path, _snapshot_file(tmp_path, corrupt=member)),
        "does not match its digest",
    )


def test_an_existing_instance_is_refused(tmp_path: Path):
    _assert_refused_without_change(
        *_import(tmp_path, _snapshot_file(tmp_path), existing="fms-01"),
        "fms-01 already exists",
    )


def test_a_pool_too_small_is_refused(tmp_path: Path):
    _assert_refused_without_change(
        *_import(tmp_path, _snapshot_file(tmp_path), pool_total=1999),
        "the instances use 2000",
    )


@pytest.mark.parametrize("path", ["../outside", "rendered/../../outside", "build/x"])
def test_a_file_entry_outside_the_rendered_files_and_inputs_is_refused(
    tmp_path: Path, path: str
):
    files = [{"path": path, "sha256": ""}]
    _assert_refused_without_change(
        *_import(tmp_path, _snapshot_file(tmp_path, files=files)),
        "invalid file entry",
    )


def test_a_file_without_its_rendered_files_is_refused(tmp_path: Path):
    _assert_refused_without_change(
        *_import(
            tmp_path, _snapshot_file(tmp_path, omit="rendered/teardown-commands.sh")
        ),
        "lacks rendered/teardown-commands.sh",
    )


def test_a_file_whose_instances_differ_from_its_rendered_manifest_is_refused(
    tmp_path: Path,
):
    _assert_refused_without_change(
        *_import(tmp_path, _snapshot_file(tmp_path, rendered_nodes=["fms-01"])),
        "lists instances other than those its rendered manifest names",
    )


def test_an_archive_outside_its_instance_entry_is_refused(tmp_path: Path):
    instances = [
        {
            "name": "fms-01",
            "archive": "../../outside.tar.zst",
            "sha256": "",
            "disk_usage_bytes": 1,
        }
    ]
    _assert_refused_without_change(
        *_import(tmp_path, _snapshot_file(tmp_path, instances=instances)),
        "invalid instance entry",
    )
