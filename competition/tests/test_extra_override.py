"""An entry's own version of the competition's additional override file, which
names the entry's images, in the episode runner's `run` and `copy`
commands (episodes.md §3.2, modes; §6.3, the living port;
episode-runner-impl.md §1, competition images).

The `run` command deploys fork 0 with make, whose render recipe hands the
file to render.py. The `copy` command deploys nothing, and reads the
compression factor from the file.

Run with: pytest competition/tests -q
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import yaml

from competition.episode_runner import __main__ as cli
from competition.episode_runner import deploy, episode, fork
from competition.episode_runner.steps import MonotonicClock

REPO = Path(__file__).resolve().parents[2]
COMPETITION = REPO / "competition" / "deploy" / "override.yaml"
ENTRY_IMAGE = "ravensport/siem-01-entry:latest"
LIVING = fork.LivingPort(
    "ab" * 32,
    7,
    datetime(2026, 11, 11, 20, 0, tzinfo=timezone.utc),
    Path("startup.json"),
    {},
)


class Make:
    """A stand-in for subprocess.Popen, which records each make command."""

    def __init__(self) -> None:
        self.argv: list[list[str]] = []

    def __call__(self, argv, **kwargs):
        self.argv.append(list(argv))
        return SimpleNamespace(stdout=iter(()), wait=lambda: 0)


def entry_override(tmp_path: Path, **values) -> Path:
    """An entry's own version of the competition's file: the competition's,
    with the entry's image of siem-01 and any other values given."""
    data = yaml.safe_load(COMPETITION.read_text())
    data["nodes"]["siem-01"] = {
        "build_script": "siem-01-entry",
        "image_name": ENTRY_IMAGE,
    }
    data.update(values)
    path = tmp_path / "entry" / "override.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data))
    return path


def deployments(tmp_path: Path, *options: str) -> list[list[str]]:
    """The make command of each deployment that the run command makes at
    fork 0, with the given options."""
    makes = Make()

    def run_episode(testbed, scenario, seed, track, records, **kwargs):
        testbed.up(tmp_path / "state", MonotonicClock(), popen=makes)
        return episode.Outcome("horizon", None, records / "episode")

    argv = ["run", "--track", "A", "--records", str(tmp_path / "records"), *options]
    assert (
        cli.main(
            argv,
            scenario=object(),
            run_episode=run_episode,
            with_adversary=lambda: False,
        )
        == 0
    )
    return makes.argv


def test_the_run_command_deploys_fork_0_with_the_entrys_file(tmp_path: Path) -> None:
    entry = entry_override(tmp_path)
    (argv,) = deployments(tmp_path, "--extra-override", str(entry))
    assert argv[:5] == [
        "make",
        "require-incus",
        "require-clock-released",
        "build",
        "deploy",
    ]
    assert f"EXTRA_OVERRIDE={entry.resolve()}" in argv


def test_the_run_command_deploys_with_the_competitions_file_by_default(
    tmp_path: Path,
) -> None:
    (argv,) = deployments(tmp_path)
    assert f"EXTRA_OVERRIDE={COMPETITION.resolve()}" in argv


def test_make_hands_the_file_to_render_which_names_the_entrys_image(
    tmp_path: Path,
) -> None:
    makefile = (REPO / "deploy" / "Makefile").read_text()
    recipe = re.search(r"^render:.*\n((?:\t.*\n)+)", makefile, re.M).group(1)
    assert "scripts/render.py" in recipe
    assert "--extra-override $(EXTRA_OVERRIDE)" in recipe
    render = deploy.render
    profile = render.load_yaml(REPO / "deploy" / "overrides" / "ci-minimal.yaml")
    merged = render.merge_topology(
        render.load_yaml(REPO / "deploy" / "topology.yaml"), profile
    )
    merged = render.merge_topology(merged, render.load_yaml(entry_override(tmp_path)))
    assert merged["nodes"]["siem-01"]["image_name"] == ENTRY_IMAGE


def test_a_copy_reads_the_compression_factor_from_the_given_file(
    tmp_path: Path,
) -> None:
    scenarios = []

    def run_copy(testbed, scenario, forked, track, out, **kwargs):
        scenarios.append(scenario)
        (out / "record.json").write_text("{}\n")
        return episode.Outcome("horizon", None, out)

    entry = entry_override(tmp_path, compression_factor=1)
    argv = [
        "copy",
        "--fork",
        "3",
        "--forked-at",
        "2026-11-11T20:45:00.500Z",
        "--track",
        "A",
        "--type",
        "benign",
        "--out",
        str(tmp_path / "out"),
        "--extra-override",
        str(entry),
    ]
    assert (
        cli.main(
            argv,
            testbed=object(),
            run_copy=run_copy,
            with_adversary=lambda: False,
            read_identity=lambda path: LIVING,
        )
        == 0
    )
    (scenario,) = scenarios
    assert scenario.compression_factor == 1
