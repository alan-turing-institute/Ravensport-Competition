"""
hist-01's own hostname resolution of hist-dmz-01 (hist-tests.md §8), targeting
hist.md §3.1 and §6.

hist-01's rendered `/etc/hosts` must resolve `hist-dmz-01` to its DMZ address —
the `resolve: [hist-dmz-01]` topology.yaml entry hist.md §3.1 requires so that
the replication targets in §6 can be addressed, and their certificates
validated, by hostname. Without it, the `-remoteWrite.url=https://hist-dmz-01:…`
targets in §9.1 fail to resolve at all, which is a different failure from the
certificate mismatch `replication_targets_present_ca_signed_certificate` (§4)
guards against, and one none of the functional replication tests would
otherwise distinguish from hist-dmz-01 simply being down.

Run with: pytest tests/unit -q
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # deploy/


def _render(profile: str = "ci-minimal") -> Path:
    out = subprocess.run(
        [sys.executable, "scripts/render.py", "--profile", profile],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert out.returncode == 0, out.stderr
    return ROOT / "build"


def test_hist01_resolves_hist_dmz_01_by_hostname():
    build = _render()
    hosts = (build / "hosts" / "hist-01").read_text()
    assert "hist-dmz-01" in hosts, (
        "hist-01 has no /etc/hosts entry to resolve hist-dmz-01"
    )
