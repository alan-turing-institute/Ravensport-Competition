"""
FMS environment-contract tests.

The rendered ``/etc/fms/fms.env`` and the FMS binary's configuration loader
(``fms/src/config.rs``) are two independently maintained sides of one contract:
the loader reads a fixed set of ``FMS_*`` variables, and the renderer must supply
them under exactly those names. Nothing else in the suite couples the two, so a
rename on either side produces an image that builds, hashes, and boots — then
crash-loops on first start.

These tests derive the contract directly from ``config.rs`` so it cannot
drift silently.

Run with: pytest tests/unit -q
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import yaml

# tests/unit/ -> tests/ -> deploy/. render.py runs from here; the FMS source
# tree is the sibling of deploy/ (the Makefile default FMS_SRC=../fms).
ROOT = Path(__file__).resolve().parents[2]
CONFIG_RS = ROOT.parent / "fms" / "src" / "config.rs"

# Every FMS_* key the loader reads at all, and the subset it treats as mandatory
# (a bare require() with no default — its absence aborts startup).
_KEY_RE = re.compile(r'"(FMS_[A-Z0-9_]+)"')
_REQUIRE_RE = re.compile(r'require\("(FMS_[A-Z0-9_]+)"\)')


def run_render(profile: str) -> Path:
    out = subprocess.run(
        [sys.executable, "scripts/render.py", "--profile", profile],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert out.returncode == 0, out.stderr
    return ROOT / "build"


def config_keys() -> tuple[set[str], set[str]]:
    """Return (all keys the loader reads, the mandatory subset) from config.rs."""
    src = CONFIG_RS.read_text()
    known = set(_KEY_RE.findall(src))
    required = set(_REQUIRE_RE.findall(src))
    assert known, f"parsed no FMS_* keys from {CONFIG_RS} — parser or path is wrong"
    assert required <= known
    return known, required


def rendered_fms_env_keys() -> set[str]:
    """FMS_* keys present in the rendered fms-01.env for the ci-minimal profile."""
    build = run_render("ci-minimal")
    env_file = build / "env" / "fms-01.env"
    assert env_file.exists(), f"{env_file} was not rendered"
    keys: set[str] = set()
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key = line.split("=", 1)[0].strip()
        if key.startswith("FMS_"):
            keys.add(key)
    return keys


def test_rendered_env_supplies_every_required_key():
    """
    Forward contract: every mandatory require() key must be emitted

    The loader aborts with "required env var <key> is not set"
    """
    _, required = config_keys()
    emitted = rendered_fms_env_keys()
    missing = required - emitted
    assert not missing, (
        f"fms-01.env omits required FMS keys the loader will abort on: "
        f"{sorted(missing)}"
    )


def test_rendered_env_emits_no_unknown_keys():
    """
    Reverse contract: every emitted FMS_* key must be one the loader reads

    Ensures a key isn't emitted under a name the loader ignores
    """
    known, _ = config_keys()
    emitted = rendered_fms_env_keys()
    unknown = emitted - known
    assert not unknown, (
        f"fms-01.env emits FMS keys config.rs never reads (likely a rename/typo): "
        f"{sorted(unknown)}"
    )


def rendered_fms_env_value(key: str) -> str:
    build = run_render("ci-minimal")
    env = (build / "env" / "fms-01.env").read_text()
    line = next((row for row in env.splitlines() if row.startswith(key + "=")), None)
    assert line is not None, f"{key} not found in fms-01.env"
    return line.split("=", 1)[1]


def test_forced_float_process_constants_reach_env():
    """The schema forces the process constants to float; that type must survive
    into the artefact. config.rs reads all three via env_f64, so casting them
    back to int (as the old emitter did) diverges from the sim, which emits
    float. Assert the rendered value is float-formatted.
    """
    for key in ("FMS_TANK_CAPACITY_L", "FMS_CAVITATION_THRESHOLD_PCT", "FMS_K_FACTOR"):
        value = rendered_fms_env_value(key)
        assert "." in value, f"{key}={value} is not float-formatted (int cast leaked)"
        float(value)  # must parse


def test_grade_reaches_env():
    """The FMS accepts a job only for the configured grade (fms.md §7.4), which
    must be world.yaml's: the one the sim writes on both paper artefacts."""
    world = yaml.safe_load((ROOT / "world.yaml").read_text())
    assert rendered_fms_env_value("FMS_GRADE") == world["process"]["grade"]


# The log-server push targets, from hist.md §3.1 (ports) and §5 (paths). fms.md
# §6 gives the same two as the loader's defaults. The host is hist-01 by name
# for both (digital.md §2.7), not looked up from topology.yaml.
_LOG_TARGETS = {
    "FMS_VMAGENT_URL": (8429, "/api/v1/import"),
    "FMS_VLAGENT_JSONLINE_URL": (9429, "/insert/jsonline"),
}


def test_log_server_targets_address_hist_01_not_a_stub():
    """The push targets must be hist-01's ingestion endpoints, by value.

    The two contract tests above compare key *names* on each side, which is why
    they never caught `FMS_LOG_SERVER_URL` pointing at an Ignition stub port for
    as long as it did: the name was present in both config.rs and render.py, so
    both agreed, and only the value was wrong. Assert the address, the port and
    the path, so a target that resolves but is not vmagent or vlagent fails here
    rather than as dropped events at runtime.

    By hostname, not IP: hist-01's leaf certificate carries a DNS SAN of
    "hist-01" only (digital.md §2.7), and fms-01 resolves it via /etc/hosts
    (topology.yaml fms-01.resolve, hist.md §3.1).
    """
    from urllib.parse import urlparse

    for key, (port, path) in _LOG_TARGETS.items():
        url = urlparse(rendered_fms_env_value(key))
        assert url.hostname == "hist-01", f"{key} points at {url.hostname}, not hist-01"
        assert url.port == port, f"{key} uses port {url.port}, hist.md §3.1 says {port}"
        assert url.path == path, f"{key} path is {url.path!r}, hist.md §5 says {path!r}"


def test_syslog_target_addresses_hist_01():
    """Path 3's target is hist-01's syslog listener (hist.md §3.1, fms.md §6),
    by hostname (digital.md §2.7) and on the TLS port (6514, RFC 5425) rather
    than the plain-TCP 514 this listener used before the internal CA was
    available.

    Host and port are separate variables rather than a URL, so the URL test above
    does not cover them; without this, a syslog target left pointing at a stub
    would survive exactly as `FMS_LOG_SERVER_URL` did.
    """
    assert rendered_fms_env_value("FMS_VLAGENT_SYSLOG_HOST") == "hist-01"
    assert rendered_fms_env_value("FMS_VLAGENT_SYSLOG_PORT") == "6514"


def test_no_stub_log_server_variables_remain():
    """`FMS_LOG_SERVER_URL`/`FMS_LOG_SERVER_MODE` are replaced, not supplemented.

    Leaving either in place would let the FMS keep a second, unspecified target:
    neither appears in fms.md §6, and `log_server_mode` was parsed but never read.
    """
    emitted = rendered_fms_env_keys()
    known, _ = config_keys()
    for stale in ("FMS_LOG_SERVER_URL", "FMS_LOG_SERVER_MODE"):
        assert stale not in emitted, f"render.py still emits {stale}"
        assert stale not in known, f"config.rs still reads {stale}"


def test_intervals_reach_env_as_int():
    """Intervals are int in the schema and read via env_u64; they must not gain
    a decimal point."""
    for key in (
        "FMS_ATG_POLL_INTERVAL",
        "FMS_MODBUS_POLL_INTERVAL",
        "FMS_SESSION_TIMEOUT",
    ):
        value = rendered_fms_env_value(key)
        assert "." not in value, f"{key}={value} should be an integer"
        int(value)  # must parse
