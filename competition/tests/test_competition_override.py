"""The competition's additional override file (episodes.md §3.4, competition
image; §5.3, timings; episodes-tests.md §2, time and the compression factor;
§12, reset and the competition image).

The competition's additional override file defines the compression factor, at
5, where a render without the file uses 1, and the flow timeouts of packet
inspection on the firewall nodes. Against a render at the competition's
compression factor, rendering with competition/deploy/override.yaml therefore
changes only the build scripts of the nodes that the file gives a competition
image, and the names of those nodes' images. The competition image of siem-01
adds the idle defender's program, the launcher and the query interface to the
operational image's build, and each competition firewall image adds the flow
timeouts. sim-host has no build script:
build-sim-image.sh builds its competition image, with the competition's entry
point. The file neither adds nor patches the adversary host, which has no
competition image (episodes.md §6.5, adversary host).

Run with: pytest competition/tests -q
"""

from __future__ import annotations

import difflib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
DEPLOY = REPO / "deploy"
OVERRIDE = REPO / "competition" / "deploy" / "override.yaml"
OPERATIONAL_IMAGE = "ravensport/siem-01:latest"
COMPETITION_IMAGE = "ravensport/siem-01-competition:latest"
SIEM_SCRIPT = "node-build/siem-01.sh"
IDLE_DEFENDER = "../competition/idle_defender/idle-defender"
# The competition's programs on siem-01, by their source below deploy/ and
# their installed path (episode-runner-impl.md §1, competition images; §12,
# query interface).
SIEM_PROGRAMS = {
    IDLE_DEFENDER: "/usr/local/bin/idle-defender",
    "../competition/launcher/defender": "/usr/local/bin/defender",
    "../competition/siem_read/siem-read": "/usr/local/bin/siem-read",
}
FIREWALLS = ("fw-ext-dmz", "fw-dmz-it", "fw-ot-dmz")
ADVERSARY = "adversary"

# build-ca.sh owns build/ca/ and build/tls/, which a render preserves.
_NOT_RENDERED = ("ca", "tls")


def _render(override: Path | None) -> dict[str, bytes]:
    """Render ci-minimal, with the additional override file `override` where
    one is given, and return every rendered file under build/ by its path
    relative to build/, read before the next render rewrites it."""
    cmd = [sys.executable, "scripts/render.py", "--profile", "ci-minimal"]
    if override is not None:
        cmd += ["--extra-override", str(override)]
    out = subprocess.run(cmd, cwd=DEPLOY, capture_output=True, text=True)
    assert out.returncode == 0, f"render failed:\n{out.stderr}"
    build = DEPLOY / "build"
    return {
        str(path.relative_to(build)): path.read_bytes()
        for path in sorted(build.rglob("*"))
        if path.is_file() and path.relative_to(build).parts[0] not in _NOT_RENDERED
    }


@pytest.fixture(scope="module")
def renders(tmp_path_factory) -> tuple[dict[str, bytes], dict[str, bytes]]:
    """The operational render at the competition's compression factor, and the
    competition's render. A render without an additional override file uses a
    compression factor of 1, so an override file that sets the competition's
    factor alone leaves only the competition images between the two
    renders."""
    factor_only = tmp_path_factory.mktemp("factor") / "override.yaml"
    factor = yaml.safe_load(OVERRIDE.read_text())["compression_factor"]
    factor_only.write_text(f"compression_factor: {factor}\n")
    competition = _render(OVERRIDE)
    operational = _render(factor_only)
    # Rendered last, so build/ is left as a render without an override file
    # leaves it.
    _render(None)
    return operational, competition


def _images() -> dict[str, tuple[str, str]]:
    """Each node that the competition's file renames, with its operational and
    its competition image name. A node built by a build script is built as
    ravensport/<node>:latest, and a node without one, sim-host, is created from
    the image that the topology names."""
    nodes = yaml.safe_load(OVERRIDE.read_text())["nodes"]
    topology = yaml.safe_load((DEPLOY / "topology.yaml").read_text())["nodes"]
    built = {
        name: (f"ravensport/{name}:latest", node["image_name"])
        for name, node in nodes.items()
        if "image_name" in node
    }
    named = {
        name: (topology[name]["image"], node["image"])
        for name, node in nodes.items()
        if "image" in node
    }
    return {**built, **named}


def _with_operational_names(text: bytes) -> bytes:
    for operational, competition in _images().values():
        text = text.replace(competition.encode(), operational.encode())
    return text


def _added_and_removed(before: bytes, after: bytes) -> tuple[list[str], list[str]]:
    diff = list(
        difflib.unified_diff(
            before.decode().splitlines(),
            _with_operational_names(after).decode().splitlines(),
            lineterm="",
            n=0,
        )
    )
    added = [line[1:] for line in diff[2:] if line.startswith("+")]
    removed = [line[1:] for line in diff[2:] if line.startswith("-")]
    return added, removed


def _override_nodes() -> dict:
    return yaml.safe_load(OVERRIDE.read_text())["nodes"]


def _rendered_nodes(rendered: dict[str, bytes]) -> set[str]:
    """The nodes that a render deploys, from its manifest."""
    return set(json.loads(rendered["manifest.json"])["nodes"])


def _is_adversarys(path: str) -> bool:
    """Whether a rendered file is one of the adversary host's own: its build
    script, or a file under a directory that the node names."""
    parts = Path(path).parts
    return Path(path).stem == ADVERSARY or ADVERSARY in parts[:-1]


def test_the_competition_override_file_changes_only_its_nodes_and_the_scaled_values(
    renders,
) -> None:
    operational, competition = renders
    assert operational.keys() == competition.keys()
    changed = {path for path in operational if operational[path] != competition[path]}
    scripts = {
        f"node-build/{name}.sh"
        for name, node in _override_nodes().items()
        if "image" not in node
    }
    assert {SIEM_SCRIPT} | {f"node-build/{name}.sh" for name in FIREWALLS} <= scripts
    assert scripts <= changed
    for path in sorted(changed - scripts):
        assert _with_operational_names(competition[path]) == operational[path], (
            f"{path} differs in more than the names of the renamed images"
        )


def test_the_competition_build_script_adds_only_the_competition_programs(
    renders,
) -> None:
    """The idle defender's program, the launcher and the query interface, with
    the comment that names them."""
    operational, competition = renders
    added, removed = _added_and_removed(
        operational[SIEM_SCRIPT], competition[SIEM_SCRIPT]
    )
    assert not removed, f"the competition build script drops lines: {removed}"
    assert added, "the competition build script adds nothing"
    named = [*SIEM_PROGRAMS, *SIEM_PROGRAMS.values()]
    for line in added:
        assert line.lstrip().startswith("#") or any(name in line for name in named), (
            f"the competition build script adds {line!r}"
        )


@pytest.mark.parametrize("name", FIREWALLS)
def test_a_competition_firewall_script_adds_only_the_flow_timeouts(
    renders, name
) -> None:
    operational, competition = renders
    path = f"node-build/{name}.sh"
    added, removed = _added_and_removed(operational[path], competition[path])
    assert not removed, f"{path} drops lines: {removed}"
    timeouts = yaml.safe_load(OVERRIDE.read_text())["flow_timeouts"]
    expected = {
        f'cfg["flow-timeouts"]["{proto}"]["{state}"] = {seconds}'
        for proto, states in timeouts.items()
        for state, seconds in states.items()
    }
    settings = {line for line in added if not line.startswith("#")}
    assert settings == expected
    assert all(line.startswith("#") for line in set(added) - settings)


@pytest.mark.parametrize("name", FIREWALLS)
def test_each_firewall_node_is_built_as_its_competition_firewall_image(
    renders, name
) -> None:
    operational, competition = renders
    script = competition[f"node-build/{name}.sh"].decode()
    assert f'IMAGE_ALIAS="ravensport/{name}-competition:latest"' in script
    assert (
        f'IMAGE_ALIAS="ravensport/{name}:latest"'
        in operational[f"node-build/{name}.sh"].decode()
    )
    assert not re.search(
        r'cfg\["flow-timeouts"\]', operational[f"node-build/{name}.sh"].decode()
    )


def test_the_competition_image_hashes_its_programs(renders) -> None:
    """A change to the idle defender, the launcher or the query interface
    rebuilds the competition image: each program is both an input of the
    image's hash and a file the build pushes to its installed path."""
    operational, competition = renders
    script = competition[SIEM_SCRIPT].decode()
    after_open = script.split("current_hash=$(sha256sum", 1)[1]
    hash_list = after_open.split("| sha256sum", 1)[0]
    for source, installed in SIEM_PROGRAMS.items():
        assert source in hash_list
        assert (
            f'incus file push --mode 0755 {source} \\\n    "$BUILD_CTR{installed}"'
            in script
        )
        assert source not in operational[SIEM_SCRIPT].decode()
        assert os.access(REPO / "deploy" / source, os.X_OK)
    assert f'IMAGE_ALIAS="{COMPETITION_IMAGE}"' in script


def test_sim_host_is_created_from_the_competition_entry_points_image(
    renders,
) -> None:
    """The competition's file gives sim-host the image that build-sim-image.sh
    builds with the competition's entry point (episodes.md §3.4, competition
    image), and `make images` builds that image whenever an additional
    override file applies. The competition's render writes the constants
    scaled by its compression factor to build/sim/sim-constants.toml, against
    which the image compiles the simulator (deployment-config.md §4.1, sim
    projection)."""
    operational, competition = renders
    assert (
        b"incus init ravensport/sim:latest sim-host" in operational["incus-commands.sh"]
    )
    assert (
        b"incus init ravensport/sim-competition:latest sim-host"
        in competition["incus-commands.sh"]
    )
    assert "sim/sim-constants.toml" in competition
    build = (DEPLOY / "scripts" / "build-sim-image.sh").read_text()
    assert 'IMAGE_ALIAS="ravensport/sim-competition:latest"' in build
    makefile = (DEPLOY / "Makefile").read_text()
    assert (
        "images: build-sim-image $(if $(EXTRA_OVERRIDE),build-sim-competition-image) "
        "build-node-images" in makefile
    )
    assert "SCALED_SIM_CONSTANTS := build/sim/sim-constants.toml" in makefile
    assert 'export SIM_CONSTANTS="$(CURDIR)/$(SCALED_SIM_CONSTANTS)"' in makefile


def test_the_competition_override_file_sets_the_compression_factor() -> None:
    assert yaml.safe_load(OVERRIDE.read_text())["compression_factor"] == 5


def test_the_competition_override_file_sets_the_flow_timeouts() -> None:
    """The values of episodes.md §5.3 (timings), and an image name of its own
    for each firewall node."""
    override = yaml.safe_load(OVERRIDE.read_text())
    assert override["flow_timeouts"] == {
        "tcp": {"closed": 20, "new": 30},
        "udp": {"established": 20},
        "icmp": {"established": 20},
    }
    for name in FIREWALLS:
        assert (
            override["nodes"][name]["image_name"]
            == f"ravensport/{name}-competition:latest"
        )


def test_the_override_file_leaves_the_adversary_host_unchanged(renders) -> None:
    """The competition's additional override file neither adds nor patches
    the node adversary. A render with the file and a render without it hold
    the same nodes, and the same rendered files of the adversary host, where
    the topology defines one (episodes.md §6.5, adversary host;
    episodes-tests.md §12, reset and the competition image)."""
    assert ADVERSARY not in _override_nodes()
    operational, competition = renders
    assert _rendered_nodes(operational) == _rendered_nodes(competition)
    own = [path for path in operational if _is_adversarys(path)]
    assert own == [path for path in competition if _is_adversarys(path)]
    for path in own:
        assert operational[path] == competition[path], path
