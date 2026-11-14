"""
Host monitoring configuration tests for security monitoring (siem-tests.md §10),
targeting siem.md §2.1.

Every agent runs file integrity monitoring on the product's default configured
paths, and inventory of installed packages, listening ports, running processes
and network interfaces, and nothing else: neither rootcheck nor configuration
assessment. siem-01 monitors its own host with exactly the same configuration.
Both come from one partial, _host-monitoring.xml.j2; these assert what the
rendered build scripts install.

Run with: pytest tests/unit -q
"""

from __future__ import annotations

import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # deploy/
sys.path.insert(0, str(ROOT / "scripts"))
from render import load_world, siem_schedules  # noqa: E402

# The agents' schedules at render.py's compression factor (siem.md §2.1,
# §8.5): the 12-hour file integrity scan and the hourly inventory, divided by
# it.
_SCHEDULES = siem_schedules(load_world(ROOT / "world.yaml"))

# siem.md §1: the twelve hosts that carry an agent.
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

# The product's default configured paths (siem.md §2.1).
_DEFAULT_FIM_PATHS = {"/etc", "/usr/bin", "/usr/sbin", "/bin", "/sbin", "/boot"}

# The four inventory categories siem.md §2.1 names, as syscollector elements,
# and every other category the module offers, which must be off.
_INVENTORY_ON = {"packages", "ports", "processes", "network"}
_INVENTORY_OFF = {"hardware", "os", "users", "groups", "services", "browser_extensions"}


def _render() -> Path:
    out = subprocess.run(
        [sys.executable, "scripts/render.py", "--profile", "ci-minimal"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode == 0, out.stderr
    return ROOT / "build" / "node-build"


def _heredoc(script: str, marker: str) -> str:
    found = re.search(rf"<< ?'{marker}'\n(.*?)\n{marker}", script, re.DOTALL)
    assert found, f"no {marker} heredoc in the build script"
    return found.group(1)


def _agent_config(build: Path, node: str) -> ET.Element:
    return ET.fromstring(_heredoc((build / f"{node}.sh").read_text(), "WAZUHAGENTCONF"))


def _assert_monitoring(config: ET.Element, where: str) -> None:
    syscheck = config.findall("syscheck")
    assert len(syscheck) == 1, (
        f"{where}: expected one <syscheck>, found {len(syscheck)}"
    )
    fim = syscheck[0]
    assert fim.findtext("disabled") == "no", f"{where}: file integrity monitoring off"
    assert fim.findtext("scan_on_start") == "yes", f"{where}: no scan at start"
    assert fim.findtext("frequency") == str(_SCHEDULES["file_integrity_s"]), (
        f"{where}: not the 12-hour schedule divided by the compression factor"
    )
    paths = {
        path.strip()
        for entry in fim.findall("directories")
        for path in (entry.text or "").split(",")
    }
    assert paths == _DEFAULT_FIM_PATHS, f"{where}: monitored paths {sorted(paths)}"

    wodles = [w for w in config.findall("wodle") if w.get("name") == "syscollector"]
    assert len(wodles) == 1, f"{where}: expected one syscollector, found {len(wodles)}"
    inventory = wodles[0]
    assert inventory.findtext("disabled") == "no", f"{where}: inventory off"
    assert inventory.findtext("interval") == f"{_SCHEDULES['inventory_s']}s", (
        f"{where}: not the hourly schedule divided by the compression factor"
    )
    for category in _INVENTORY_ON:
        assert inventory.findtext(category) == "yes", (
            f"{where}: {category} not reported"
        )
    for category in _INVENTORY_OFF:
        assert inventory.findtext(category) == "no", f"{where}: {category} reported"
    assert inventory.find("ports").get("all") == "no", (
        f"{where}: reports every port, not only listening ones"
    )

    rootcheck = config.findall("rootcheck")
    assert [r.findtext("disabled") for r in rootcheck] == ["yes"], (
        f"{where}: rootcheck is not explicitly disabled"
    )
    sca = config.findall("sca")
    assert [s.findtext("enabled") for s in sca] == ["no"], (
        f"{where}: configuration assessment is not explicitly disabled"
    )


def test_every_agent_runs_file_integrity_and_inventory_only():
    """siem.md §2.1: every agent's ossec.conf monitors the default paths and
    reports the four inventory categories, with rootcheck and configuration
    assessment explicitly off."""
    build = _render()
    for node in _MONITORED_NODES:
        _assert_monitoring(_agent_config(build, node), node)


def test_siem_01_monitors_its_own_host_as_the_agents_do():
    """siem-01's own monitoring is the agents' block for block: the partial it
    installs in place of the packaged blocks is the one every agent carries."""
    build = _render()
    script = (build / "siem-01.sh").read_text()
    monitoring = _heredoc(script, "XMLEOF")
    _assert_monitoring(
        ET.fromstring(f"<ossec_config>{monitoring}</ossec_config>"), "siem-01"
    )
    agent = _heredoc((build / "fms-01.sh").read_text(), "WAZUHAGENTCONF")
    assert monitoring.strip() in agent, "siem-01's monitoring differs from the agents'"


def test_siem_01_removes_every_packaged_monitoring_block():
    """The manager's packaged ossec.conf carries its own syscheck, rootcheck,
    configuration-assessment and syscollector blocks; the build removes each,
    and fails rather than leave one active."""
    script = (_render() / "siem-01.sh").read_text()
    for block in ("<syscheck>", "<rootcheck>", "<sca>", '<wodle name="syscollector">'):
        assert block in script.split("/tmp/patch_ossec.py > /dev/null")[1], (
            f"siem-01's patch does not remove the packaged {block} block"
        )
    assert "expected one block matching" in script, (
        "siem-01's patch does not fail when a packaged block is missing or repeated"
    )
