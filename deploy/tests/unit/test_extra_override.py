"""
An additional override file (deployment.md §5.2, render.py; §5.3, invocation;
§7.3, additional override file).

render.py's --extra-override applies an additional override file after the
profile's override file. The file may patch node definitions, add a directory of
build-script templates that render.py searches after deploy/build-scripts/, and
set the name of the image that a node is built as and created from. The file may
also set the compression factor and the flow timeouts of packet inspection,
which a profile's override file cannot set. A file that sets flow timeouts
gives each firewall node an image name of its own. The merge keeps the
profile's name.

Run with: pytest tests/unit -q
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]  # deploy/
BUILD = ROOT / "build"
sys.path.insert(0, str(ROOT / "scripts"))
from render import (  # noqa: E402
    flow_timeouts_from_override,
    load_world,
    world_at_override_factor,
)
from world_schema import COMPRESSION_FACTOR  # noqa: E402


def _render(*extra_args: str) -> None:
    out = subprocess.run(
        [sys.executable, "scripts/render.py", "--profile", "ci-minimal", *extra_args],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert out.returncode == 0, f"render failed:\n{out.stderr}"


@pytest.fixture(scope="module", autouse=True)
def _leave_build_as_a_plain_render():
    yield
    # The shared build/ directory is left as a render without an additional
    # override file leaves it, for the rest of the suite.
    _render()


@pytest.fixture
def override(tmp_path: Path) -> Path:
    """An additional override file that sets siem-01's image name and gives
    hmi-01 a template found only in the override's template directory."""
    templates = tmp_path / "templates"
    templates.mkdir()
    (templates / "hmi-01-test.sh.j2").write_text(
        "#!/usr/bin/env bash\n"
        "# test template for {{ node_name }}, image {{ image_name }}\n"
    )
    # A template of the same name as one in deploy/build-scripts/, which the
    # search order leaves unused.
    (templates / "siem-01.sh.j2").write_text("#!/usr/bin/env bash\n# shadow\n")
    path = tmp_path / "override.yaml"
    path.write_text(
        "template_dir: templates\n"
        "nodes:\n"
        "  siem-01:\n"
        "    image_name: ravensport/siem-01-test:latest\n"
        "  hmi-01:\n"
        "    build_script: hmi-01-test\n"
    )
    return path


def test_the_merge_keeps_the_profile_name(override: Path) -> None:
    _render("--extra-override", str(override))
    manifest = json.loads((BUILD / "manifest.json").read_text())
    assert manifest["profile"] == "ci-minimal"
    assert 'PROFILE="ci-minimal"' in (BUILD / "incus-commands.sh").read_text()


def test_an_image_name_reaches_the_build_script_and_the_deployment(
    override: Path,
) -> None:
    _render("--extra-override", str(override))
    script = (BUILD / "node-build" / "siem-01.sh").read_text()
    assert 'IMAGE_ALIAS="ravensport/siem-01-test:latest"' in script
    commands = (BUILD / "incus-commands.sh").read_text()
    assert "incus init ravensport/siem-01-test:latest siem-01" in commands
    manifest = json.loads((BUILD / "manifest.json").read_text())
    assert manifest["images"]["siem-01"] == "ravensport/siem-01-test:latest"
    assert manifest["images"]["fms-01"] == "ravensport/fms-01:latest"


def test_templates_are_searched_after_deploy_build_scripts(override: Path) -> None:
    _render("--extra-override", str(override))
    # Found only in the override's template directory.
    hmi = (BUILD / "node-build" / "hmi-01.sh").read_text()
    assert hmi.startswith("#!/usr/bin/env bash\n# test template for hmi-01")
    # deploy/build-scripts/siem-01.sh.j2 comes first.
    siem = (BUILD / "node-build" / "siem-01.sh").read_text()
    assert "# shadow" not in siem
    assert "current_hash=$(sha256sum" in siem


def test_without_an_additional_override_each_node_keeps_its_image() -> None:
    _render()
    manifest = json.loads((BUILD / "manifest.json").read_text())
    for name in manifest["build_script_nodes"]:
        assert manifest["images"][name] == f"ravensport/{name}:latest"
    script = (BUILD / "node-build" / "siem-01.sh").read_text()
    assert 'IMAGE_ALIAS="ravensport/siem-01:latest"' in script
    commands = (BUILD / "incus-commands.sh").read_text()
    assert "incus init ravensport/siem-01:latest siem-01" in commands


def test_the_empty_blocks_leave_the_hash_list_unbroken() -> None:
    """siem-01.sh.j2's blocks render to nothing in the operational image, so
    every line of its hash list still continues the command."""
    _render()
    script = (BUILD / "node-build" / "siem-01.sh").read_text()
    after_open = script.split("current_hash=$(sha256sum \\\n", 1)[1]
    hash_list = after_open.split("  | sha256sum", 1)[0]
    lines = hash_list.splitlines()
    assert lines, "siem-01.sh has an empty hash list"
    for line in lines:
        assert line.startswith("    ") and line.endswith(" \\"), repr(line)
    assert "{%" not in script and "{#" not in script


# ── The compression factor (deployment.md §7.3) ──────────────────────────────


def _world():
    return load_world(ROOT / "world.yaml")


def test_an_additional_override_file_sets_the_compression_factor() -> None:
    world = world_at_override_factor(_world(), {}, {"compression_factor": 5})
    assert world.compression_factor == 5


def test_without_a_factor_the_render_uses_the_default() -> None:
    assert (
        world_at_override_factor(_world(), {}, {}).compression_factor
        == COMPRESSION_FACTOR
    )


def test_a_profile_override_file_cannot_set_the_compression_factor() -> None:
    with pytest.raises(SystemExit, match="profile's override file cannot set"):
        world_at_override_factor(
            _world(), {"compression_factor": COMPRESSION_FACTOR}, {}
        )


@pytest.mark.parametrize("factor", [2.5, "5", True])
def test_a_compression_factor_that_is_not_a_whole_number_is_refused(factor) -> None:
    with pytest.raises(SystemExit, match="whole number"):
        world_at_override_factor(_world(), {}, {"compression_factor": factor})


def test_render_rejects_a_factor_that_breaks_whole_seconds() -> None:
    # The 5-second poll of the dispenser divides by 5 but not by 7.
    with pytest.raises(SystemExit, match="whole seconds"):
        world_at_override_factor(_world(), {}, {"compression_factor": 7})


def test_a_render_takes_the_additional_override_files_factor(tmp_path: Path) -> None:
    # The fuel management system (FMS) polls the tank gauge every 30 seconds in
    # operation, so every 6 seconds at a compression factor of 5 and every 30
    # at the default of 1.
    path = tmp_path / "override.yaml"
    path.write_text("compression_factor: 5\n")
    _render("--extra-override", str(path))
    assert "FMS_ATG_POLL_INTERVAL=6\n" in (BUILD / "env" / "fms-01.env").read_text()
    _render()
    assert "FMS_ATG_POLL_INTERVAL=30\n" in (BUILD / "env" / "fms-01.env").read_text()


# ── The flow timeouts of packet inspection (deployment.md §7.3) ──────────────

FIREWALLS = ("fw-ext-dmz", "fw-dmz-it", "fw-ot-dmz")
TIMEOUTS = {"tcp": {"closed": 20, "new": 30}, "udp": {"established": 20}}


def _firewall_nodes(named: bool) -> dict:
    """The firewall nodes as the merged topology holds them, with or without
    an image name of their own."""
    return {
        name: {"build_script": "fw-zone"}
        | ({"image_name": f"ravensport/{name}-test:latest"} if named else {})
        for name in FIREWALLS
    }


def test_without_flow_timeouts_the_render_sets_none() -> None:
    assert flow_timeouts_from_override({}, {}, _firewall_nodes(named=False)) is None


def test_an_additional_override_file_sets_the_flow_timeouts() -> None:
    extra = {"flow_timeouts": TIMEOUTS}
    assert (
        flow_timeouts_from_override({}, extra, _firewall_nodes(named=True)) == TIMEOUTS
    )


def test_a_profile_override_file_cannot_set_flow_timeouts() -> None:
    with pytest.raises(
        SystemExit, match="profile's override file cannot set flow_timeouts"
    ):
        flow_timeouts_from_override(
            {"flow_timeouts": TIMEOUTS}, {}, _firewall_nodes(named=True)
        )


def test_flow_timeouts_need_an_image_name_for_each_firewall_node() -> None:
    nodes = _firewall_nodes(named=True)
    del nodes["fw-dmz-it"]["image_name"]
    with pytest.raises(SystemExit, match="image name of its own.*none for fw-dmz-it"):
        flow_timeouts_from_override({}, {"flow_timeouts": TIMEOUTS}, nodes)


@pytest.mark.parametrize(
    "timeouts",
    [
        {},
        {"sctp": {"new": 30}},
        {"udp": {"closed": 20}},
        {"icmp": {}},
        {"tcp": {"closed": 2.5}},
        {"tcp": {"closed": "20"}},
        {"tcp": {"closed": True}},
        {"tcp": {"closed": 0}},
    ],
)
def test_a_flow_timeout_outside_suricatas_names_or_whole_seconds_is_refused(
    timeouts,
) -> None:
    with pytest.raises(SystemExit, match="flow_timeouts"):
        flow_timeouts_from_override(
            {}, {"flow_timeouts": timeouts}, _firewall_nodes(named=True)
        )


def test_a_render_writes_the_flow_timeouts_into_each_firewall_script(
    tmp_path: Path,
) -> None:
    path = tmp_path / "override.yaml"
    path.write_text(
        "flow_timeouts:\n"
        "  tcp:\n"
        "    closed: 20\n"
        "    new: 30\n"
        "  udp:\n"
        "    established: 20\n"
        "nodes:\n"
        + "".join(
            f"  {name}:\n    image_name: ravensport/{name}-test:latest\n"
            for name in FIREWALLS
        )
    )
    _render("--extra-override", str(path))
    for name in FIREWALLS:
        script = (BUILD / "node-build" / f"{name}.sh").read_text()
        assert f'IMAGE_ALIAS="ravensport/{name}-test:latest"' in script
        assert 'cfg["flow-timeouts"]["tcp"]["closed"] = 20\n' in script
        assert 'cfg["flow-timeouts"]["tcp"]["new"] = 30\n' in script
        assert 'cfg["flow-timeouts"]["udp"]["established"] = 20\n' in script
    _render()
    for name in FIREWALLS:
        script = (BUILD / "node-build" / f"{name}.sh").read_text()
        assert f'IMAGE_ALIAS="ravensport/{name}:latest"' in script
        assert 'cfg["flow-timeouts"]' not in script
