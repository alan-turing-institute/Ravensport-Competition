"""Deployment and reset (episodes.md §3.1, phases; §3.3, reset).

The episode runner deploys with `make require-incus require-clock-released
build deploy` in deploy/, the steps of `make up` that check the host, build the
images and deploy them. EXTRA_OVERRIDE
names the competition's additional override file, so the build also builds the
competition images, and the deployment creates each node that the file names,
siem-01 and sim-host among them, from its competition image (episodes.md §3.4,
competition image). SIM_STATE_DIR names a fresh directory that holds only the
start-up input, which the deployment sequence attaches to sim-host before
starting it (deployment.md §8, deployment sequence). The directory sits outside
the episode record, which no RavensPort node may reach. The settle period
starts at the line in which the deployment sequence starts sim-host. The reset
tears the deployment down with `make down`, and the next episode's deployment
recreates every node from the images already built.

topology() gives the topology that the deployment holds: deploy/topology.yaml
patched by the profile's override file and then by the competition's additional
override file, through render.py's own merge (deployment.md §7.3).

The adversary host has no competition image, and the competition's additional
override file neither adds nor patches the node adversary: the adversary host's
own image holds every attack chain's binary from its build, so benign and
adversarial episodes leave the adversary host alike (episodes.md §6.5,
adversary host). The episode runner reads the list of attack chains that the
attack step of `make up` writes, once, before the episodes.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import render

from competition.episode_runner.steps import Clock

DEPLOY = Path(__file__).resolve().parents[2] / "deploy"
MANIFEST = DEPLOY / "build" / "manifest.json"
# Relative to deploy/, where make runs.
EXTRA_OVERRIDE = "../competition/deploy/override.yaml"
# The profile that make deploys when PROFILE names none (deploy/Makefile).
DEFAULT_PROFILE = "ci-minimal"
STARTING_SIM_HOST = "==> Starting sim-host"
TAIL_LINES = 40

log = logging.getLogger(__name__)


class Incus(Protocol):
    def instances(self) -> list[dict[str, Any]]: ...


@dataclass
class MakeRun:
    target: str
    started: float
    ended: float = 0.0
    returncode: int = 0
    sim_host_started: float | None = None
    tail: list[str] = field(default_factory=list)


def sim_state_dir(startup: str, parent: Path | None = None) -> Path:
    """A fresh directory that holds only the start-up input. Every user may
    read the file, because the unprivileged container does not map its owner
    on the deployment host."""
    path = Path(tempfile.mkdtemp(prefix="ravensport-sim-state-", dir=parent))
    path.chmod(0o755)
    (path / "startup.json").write_text(startup)
    (path / "startup.json").chmod(0o644)
    return path


def up(
    state_dir: Path,
    clock: Clock,
    popen: Callable[..., Any] = subprocess.Popen,
    extra_override: str | Path = EXTRA_OVERRIDE,
) -> MakeRun:
    """Deploy with the competition's additional override file, or an entry's
    own version of it (episodes.md §3.2, modes), and the start-up input in
    state_dir."""
    return _make(
        clock,
        popen,
        ("require-incus", "require-clock-released", "build", "deploy"),
        f"EXTRA_OVERRIDE={extra_override}",
        f"SIM_STATE_DIR={state_dir}",
    )


def down(clock: Clock, popen: Callable[..., Any] = subprocess.Popen) -> MakeRun:
    """Tear the deployment down (episodes.md §3.3, reset)."""
    return _make(clock, popen, ("down",))


def topology(profile: str | None = None, deploy_dir: Path = DEPLOY) -> dict[str, Any]:
    """The topology that up() deploys: deploy/topology.yaml patched by the
    profile's override file, then by the competition's additional override
    file, through render.py's own merge (deployment.md §7.3). The profile is
    the one given, or the one that make reads: PROFILE in the environment, or
    its default."""
    name = profile or os.environ.get("PROFILE") or DEFAULT_PROFILE
    profile_override = render.load_yaml(deploy_dir / "overrides" / f"{name}.yaml")
    merged = render.merge_topology(
        render.load_yaml(deploy_dir / "topology.yaml"), profile_override
    )
    extra = render.load_yaml((deploy_dir / EXTRA_OVERRIDE).resolve())
    return render.merge_topology(merged, extra)


def manifest(path: Path = MANIFEST) -> dict[str, Any]:
    """The rendered build's list of nodes, networks and images."""
    return json.loads(path.read_text())


def images(incus: Incus, rendered: dict[str, Any]) -> dict[str, dict[str, str | None]]:
    """For each RavensPort node, the image named in the rendered build and the
    fingerprint of the image from which Incus created the node."""
    created = {i["name"]: i.get("config", {}) for i in incus.instances()}
    named = rendered.get("images", {})
    return {
        node: {
            "image": named.get(node),
            "fingerprint": created.get(node, {}).get("volatile.base_image"),
        }
        for node in rendered["nodes"]
    }


def remaining(incus: Incus, rendered: dict[str, Any]) -> list[str]:
    """The RavensPort nodes that still exist."""
    names = {i["name"] for i in incus.instances()}
    return [node for node in rendered["nodes"] if node in names]


def _make(
    clock: Clock, popen: Callable[..., Any], targets: tuple[str, ...], *variables: str
) -> MakeRun:
    target = " ".join(targets)
    run = MakeRun(target, started=clock.now())
    tail: deque[str] = deque(maxlen=TAIL_LINES)
    proc = popen(
        ["make", *targets, *variables],
        cwd=DEPLOY,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
    )
    for line in proc.stdout:
        line = line.rstrip("\n")
        if line == STARTING_SIM_HOST and run.sim_host_started is None:
            run.sim_host_started = clock.now()
        tail.append(line)
        log.debug("make %s: %s", target, line)
    run.returncode = proc.wait()
    run.ended = clock.now()
    run.tail = list(tail)
    return run
