"""
Cloud-init residue guard

Ensures no recoverable cloud-init residue — ``user.user-data`` or the
``/var/lib/cloud/`` tree — survives in a deployed instance. The build scripts
launch from the Ubuntu cloud image and run ``cloud-init status --wait`` during
provisioning, which populates ``/var/lib/cloud/``; the published image must then
remove cloud-init rather than disabled. A disabled-but-present cloud-init also
re-creates the tree on the deployed instance's next boot, so the package is
purged, not flagged off.

This unit test checks the *rendered* build scripts statically (no Incus needed).
The runtime counterpart — that a built image actually carries no residue — lives
in tests/integration/test_image_build.sh.

Run with: pytest tests/unit -q
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def run_render(profile: str) -> Path:
    out = subprocess.run(
        [sys.executable, "scripts/render.py", "--profile", profile],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert out.returncode == 0, out.stderr
    return ROOT / "build"


def node_build_scripts() -> list[Path]:
    build = run_render("ci-minimal")
    scripts = sorted((build / "node-build").glob("*.sh"))
    assert scripts, "no per-node build scripts were rendered"
    return scripts


def test_build_scripts_purge_cloud_init():
    """Every build script must purge the package and remove the runtime tree."""
    for script in node_build_scripts():
        text = script.read_text()
        assert "apt-get purge -y cloud-init" in text, (
            f"{script.name} does not purge the cloud-init package"
        )
        assert "rm -rf /var/lib/cloud" in text, (
            f"{script.name} does not remove the /var/lib/cloud residue tree"
        )


def test_build_scripts_do_not_merely_disable_cloud_init():
    """The disabled-flag approach must be gone — it leaves residue and the
    package regenerates the tree on the next boot."""
    for script in node_build_scripts():
        text = script.read_text()
        assert "cloud-init.disabled" not in text, (
            f"{script.name} still disables cloud-init instead of purging it"
        )
