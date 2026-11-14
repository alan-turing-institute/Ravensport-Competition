"""
HMI environment-contract tests.

The rendered ``/etc/hmi/hmi.env`` and the HMI binary's configuration loader
(``hmi/src/config.rs``) are two independently maintained sides of one contract.
Unlike the FMS loader, which aborts on a missing ``require()`` key, every HMI
variable is read with ``unwrap_or_else(default)`` — so a renamed or mistyped key
does **not** crash the HMI. It silently falls back to a hardcoded default (for
example ``HMI_OPERATOR_RFID_ADDR`` defaults to a baked-in IP address), producing
a misconfigured but running node with no boot-time signal. That quiet failure is
exactly what this contract guards against.

The tests derive the key set directly from ``config.rs`` so the contract cannot
drift silently:

  * forward — every key the loader reads must be emitted, so the topology- and
    world-derived values actually take effect rather than the baked defaults;
  * reverse — every emitted ``HMI_*`` key must be one the loader reads, so a
    render-side rename that would be silently ignored fails here instead.

Run with: pytest tests/unit -q
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # deploy/
# The HMI source tree is a sibling of deploy/ (RavensPort/hmi).
CONFIG_RS = ROOT.parent / "hmi" / "src" / "config.rs"

# Every HMI_* key the loader reads via std::env::var("HMI_...").
_KEY_RE = re.compile(r'env::var\("(HMI_[A-Z0-9_]+)"\)')


def run_render(profile: str) -> Path:
    out = subprocess.run(
        [sys.executable, "scripts/render.py", "--profile", profile],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert out.returncode == 0, out.stderr
    return ROOT / "build"


def config_keys() -> set[str]:
    """The HMI_* keys config.rs reads."""
    keys = set(_KEY_RE.findall(CONFIG_RS.read_text()))
    assert keys, f"parsed no HMI_* keys from {CONFIG_RS} — parser or path is wrong"
    return keys


def rendered_hmi_env_keys() -> set[str]:
    """HMI_* keys present in the rendered hmi-01.env for the ci-minimal profile."""
    build = run_render("ci-minimal")
    env_file = build / "env" / "hmi-01.env"
    assert env_file.exists(), f"{env_file} was not rendered"
    keys: set[str] = set()
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key = line.split("=", 1)[0].strip()
        if key.startswith("HMI_"):
            keys.add(key)
    return keys


def test_rendered_env_supplies_every_key_config_reads():
    """
    Forward contract: every HMI_* key the loader reads must be emitted.

    A missing key does not crash the HMI — it silently uses a baked default — so
    a topology- or world-derived value (e.g. HMI_OPERATOR_RFID_ADDR) would be
    replaced by the hardcoded fallback without any signal.
    """
    missing = config_keys() - rendered_hmi_env_keys()
    assert not missing, (
        f"hmi-01.env omits HMI keys the loader reads (they will silently default): "
        f"{sorted(missing)}"
    )


def test_rendered_env_emits_no_unknown_keys():
    """
    Reverse contract: every emitted HMI_* key must be one the loader reads.

    Catches a render-side rename/typo the loader would silently ignore.
    """
    unknown = rendered_hmi_env_keys() - config_keys()
    assert not unknown, (
        f"hmi-01.env emits HMI keys config.rs never reads (likely a rename/typo): "
        f"{sorted(unknown)}"
    )
