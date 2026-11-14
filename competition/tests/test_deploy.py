"""Deployment and reset (episodes.md §3.1, phases; §3.3, reset; §13, episode
record).

A fake make prints its lines at given times on a fake clock.

Run with: pytest competition/tests -q
"""

from __future__ import annotations

import json
import re
import stat
import subprocess
from pathlib import Path

import pytest

from competition.episode_runner import deploy

import render  # deploy/scripts, which the episode runner puts on the path

STARTUP = (
    '{"sim_tug_arrivals": 4651467801475421837, "initial_volume_l": 35000.0, '
    '"order_pending_s": null, "first_poll_after_s": 180.0, '
    '"first_check_after_s": 17280.0, "next_audit_at": "2026-11-10T09:30:00Z", '
    '"initial_cumulative_pulses": 0, "initial_meter_totaliser_l": 0.0, '
    '"first_bdn_number": 1, "first_ticket_number": 1, "held_since": null, '
    '"held_artefacts": []}\n'
)


class FakeClock:
    def __init__(self, now: float = 100.0) -> None:
        self.t = now

    def now(self) -> float:
        return self.t


class FakeMake:
    """Stands in for subprocess.Popen: prints each line at its time."""

    def __init__(
        self, clock: FakeClock, lines: list[tuple[float, str]], returncode: int = 0
    ):
        self.clock, self.lines, self.returncode = clock, lines, returncode
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, argv: list[str], **kwargs) -> FakeMake:
        self.calls.append((argv, kwargs))
        return self

    @property
    def stdout(self):
        for at, line in self.lines:
            self.clock.t = at
            yield line + "\n"

    def wait(self) -> int:
        return self.returncode


DEPLOY_LINES = [
    (101.0, "==> Building ravensport/siem-01-competition: inputs unchanged, skipping"),
    (130.0, "==> Waiting for digital world to be ready"),
    (139.5, "==> Attaching the simulator's start-up input to sim-host"),
    (140.25, "==> Starting sim-host"),
    (141.0, ""),
    (141.0, "Done."),
]


def test_up_deploys_with_the_competition_override_and_the_start_up_input(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    make = FakeMake(clock, DEPLOY_LINES)
    run = deploy.up(tmp_path, clock, popen=make)
    ((argv, kwargs),) = make.calls
    assert argv == [
        "make",
        "require-incus",
        "require-clock-released",
        "build",
        "deploy",
        "EXTRA_OVERRIDE=../competition/deploy/override.yaml",
        f"SIM_STATE_DIR={tmp_path}",
    ]
    assert kwargs["cwd"] == deploy.DEPLOY
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert run.returncode == 0
    assert (run.started, run.ended) == (100.0, 141.0)


def test_the_deployment_runs_the_steps_of_make_up_less_the_attack_binaries() -> None:
    """The episode runner deploys with the prerequisites of `make up`, less the
    attack phase's build and push of the attack chains' binaries: the binaries
    are built apart, and the adversary host's own image holds them
    (episodes.md §6.5, adversary host)."""
    makefile = (deploy.DEPLOY / "Makefile").read_text()
    (prerequisites,) = re.findall(r"^up:(.*)$", makefile, re.MULTILINE)
    attack_binaries = {"stage-attacks", "build-attacks", "push-attacks"}
    steps = [s for s in prerequisites.split() if s not in attack_binaries]
    assert steps == ["require-incus", "require-clock-released", "build", "deploy"]


def test_the_settle_period_starts_at_the_line_that_starts_sim_host(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    run = deploy.up(tmp_path, clock, popen=FakeMake(clock, DEPLOY_LINES))
    assert run.sim_host_started == 140.25


def test_a_failed_deployment_keeps_its_last_lines(tmp_path: Path) -> None:
    clock = FakeClock()
    lines = [(100.0 + n, f"line {n}") for n in range(60)] + [
        (170.0, "ERROR: no readable start-up input at /x/startup.json")
    ]
    run = deploy.up(tmp_path, clock, popen=FakeMake(clock, lines, returncode=2))
    assert run.returncode == 2
    assert run.sim_host_started is None
    assert len(run.tail) == deploy.TAIL_LINES
    assert run.tail[-1] == "ERROR: no readable start-up input at /x/startup.json"


def test_the_start_up_directory_holds_only_the_start_up_input(tmp_path: Path) -> None:
    """Every user may read the start-up input, because the unprivileged
    container does not map its owner on the deployment host."""
    state = deploy.sim_state_dir(STARTUP, parent=tmp_path)
    assert state.parent == tmp_path
    assert [p.name for p in state.iterdir()] == ["startup.json"]
    assert (state / "startup.json").read_text() == STARTUP
    assert stat.S_IMODE(state.stat().st_mode) == 0o755
    assert stat.S_IMODE((state / "startup.json").stat().st_mode) == 0o644


def test_down_runs_make_down() -> None:
    clock = FakeClock()
    make = FakeMake(clock, [(105.0, "Teardown complete.")])
    run = deploy.down(clock, popen=make)
    assert make.calls[0][0] == ["make", "down"]
    assert make.calls[0][1]["cwd"] == deploy.DEPLOY
    assert (run.returncode, run.ended) == (0, 105.0)


class FakeIncus:
    def __init__(self, instances: list[dict]) -> None:
        self._instances = instances

    def instances(self) -> list[dict]:
        return self._instances


MANIFEST = {
    "profile": "ci-minimal",
    "nodes": ["fw-ext-dmz", "siem-01", "sim-host"],
    "images": {
        "fw-ext-dmz": "ravensport/fw-ext-dmz:latest",
        "siem-01": "ravensport/siem-01-competition:latest",
    },
}


def test_the_rendered_build_names_the_nodes(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(MANIFEST))
    assert deploy.manifest(path) == MANIFEST


def test_each_node_records_the_image_it_was_created_from() -> None:
    incus = FakeIncus(
        [
            {"name": "siem-01", "config": {"volatile.base_image": "c0ffee"}},
            {"name": "fw-ext-dmz", "config": {"volatile.base_image": "beef01"}},
            {"name": "sim-host", "config": {"volatile.base_image": "5a11ad"}},
            {"name": "unrelated", "config": {"volatile.base_image": "000000"}},
        ]
    )
    assert deploy.images(incus, MANIFEST) == {
        "fw-ext-dmz": {
            "image": "ravensport/fw-ext-dmz:latest",
            "fingerprint": "beef01",
        },
        "siem-01": {
            "image": "ravensport/siem-01-competition:latest",
            "fingerprint": "c0ffee",
        },
        "sim-host": {"image": None, "fingerprint": "5a11ad"},
    }


def test_the_reset_leaves_no_ravensport_node() -> None:
    incus = FakeIncus(
        [{"name": "unrelated", "config": {}}, {"name": "siem-01", "config": {}}]
    )
    assert deploy.remaining(incus, MANIFEST) == ["siem-01"]


# --- The deployed topology (deployment.md §7.3) ----------------------------------


def test_the_deployed_topology_holds_the_competition_override_files_patches() -> None:
    nodes = deploy.topology("ci-minimal")["nodes"]
    assert set(render.load_yaml(deploy.DEPLOY / "topology.yaml")["nodes"]) <= set(nodes)
    assert nodes["siem-01"]["image_name"] == "ravensport/siem-01-competition:latest"
    assert nodes["sim-host"]["image"] == "ravensport/sim-competition:latest"
    assert {node["type"] for node in nodes.values()} == {"container"}


def test_the_deployed_topology_takes_the_profile_that_make_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PROFILE", "full")
    assert deploy.topology()["nodes"]["fms-01"]["type"] == "vm"
    monkeypatch.delenv("PROFILE")
    assert deploy.topology()["nodes"]["fms-01"]["type"] == "container"
