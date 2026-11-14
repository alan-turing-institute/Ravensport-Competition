"""
Boundary capture size tests for security monitoring (siem-tests.md §10),
targeting siem.md §5 and §8.4.

A sending host hands its virtual interface segments larger than the MTU, and
the zone bridge delivers them to a firewall unsplit. Suricata on the
interface they arrive on must capture such a frame whole, or it inspects
none of its payload. These assert what the rendered firewall build scripts
write into suricata.yaml.

Run with: pytest tests/unit -q
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # deploy/

_FW_NODES = ("fw-ext-dmz", "fw-dmz-it", "fw-ot-dmz")

# The largest IP packet with its Ethernet header.
_LARGEST_FRAME = 65535 + 14


def _patch(node: str) -> str:
    out = subprocess.run(
        [sys.executable, "scripts/render.py", "--profile", "ci-minimal"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode == 0, out.stderr
    script = (ROOT / "build" / "node-build" / f"{node}.sh").read_text()
    found = re.search(
        r"tee /tmp/patch_suricata.py > /dev/null << 'PYEOF'\n(.*?)\nPYEOF",
        script,
        re.DOTALL,
    )
    assert found, f"{node}: no suricata.yaml patch in the build script"
    return found.group(1)


def _setting(patch: str, pattern: str) -> int:
    found = re.search(pattern, patch)
    assert found, f"no setting matching {pattern!r}"
    return int(found.group(1))


def test_capture_accepts_the_largest_ip_packet():
    """siem.md §5: every firewall captures frames up to the largest IP packet,
    in a capture ring whose blocks each hold one such frame whole."""
    for node in _FW_NODES:
        patch = _patch(node)
        size = _setting(patch, r'cfg\["default-packet-size"\] = (\d+)')
        assert size >= _LARGEST_FRAME, f"{node}: capture size {size}"
        block = _setting(patch, r'capture\["block-size"\] = (\d+)')
        assert block >= size and block & (block - 1) == 0, (
            f"{node}: block size {block} does not hold a {size}-byte frame "
            "or is not a power of two"
        )
        assert 'capture["tpacket-v3"] = True' in patch, f"{node}: not tpacket-v3"


def test_capture_settings_reach_the_interfaces_in_use():
    """The interfaces are named on Suricata's command line, so they take the
    af-packet `default` entry; the capture settings must be written there,
    and the file written without reordering that entry's keys."""
    for node in _FW_NODES:
        patch = _patch(node)
        assert (
            'capture = next(e for e in cfg["af-packet"] '
            'if e.get("interface") == "default")'
        ) in patch, f"{node}: capture settings not written to the default entry"
        assert "sort_keys=False" in patch, f"{node}: suricata.yaml keys reordered"


def test_capture_ring_stays_small():
    """Suricata keeps at least half of max-pending-packets frames in the
    capture ring; with whole-frame blocks that sets the ring's memory, so the
    packet pool is reduced to keep each ring to a few megabytes."""
    for node in _FW_NODES:
        patch = _patch(node)
        pending = _setting(patch, r'cfg\["max-pending-packets"\] = (\d+)')
        ring = _setting(patch, r'capture\["ring-size"\] = (\d+)')
        assert ring >= pending // 2, f"{node}: Suricata would raise ring {ring}"
        ring_bytes = (ring + 1) * _setting(patch, r'capture\["block-size"\] = (\d+)')
        assert ring_bytes <= 16 * 2**20, f"{node}: ring of {ring_bytes} bytes"
