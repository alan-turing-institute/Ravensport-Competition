"""The steps of creating a living port that run inside its virtual machine
(VM): the start-up input, the deployment, the identity file and the living
port's settling (the living port contract; episodes.md §4.3, start-up input;
§6.3, the living port; §6.6, settle period).

The manager runs this module over the Secure Shell, once the VM's clock is
held and the tree is at /opt/ravensport with its virtual environment:

    cd /opt/ravensport && deploy/.venv-$(uname -s)-$(uname -m)/bin/python \
        -m competition.living.guest create --seed HEX [--build] \
        [--volume DIR] [--extra-override FILE]

The module writes the start-up input with the simulator's defaults and the
living port's tug seed, renders with the competition's additional override
file, or with an entry's own version of it that --extra-override names in the
tree, checks that every image the render names is built (or builds them with
--build), deploys with the start-up input attached to sim-host through
SIM_STATE_DIR, waits for the simulator's start_state event, and writes the
identity file. In evaluation, the entry's submitted storage volume is attached
read-only to siem-01 at /var/lib/entry straight after the deployment, so that
each fork keeps it, as each fork copies the VM's memory. The module then waits
for the four settle conditions with the episode runner's own checks, waits the
settle delay, and writes the instant at which the living port's settle period
ended: the living port's first fork falls a drawn wait after it. Unlike the
rest of the package, the module runs in the deployment's virtual environment,
because it uses the episode runner's modules.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from competition.episode_runner import (
    deploy,
    predicted_events,
    settle,
    startup,
    volume,
)
from competition.episode_runner.steps import Clock, MonotonicClock
from competition.episode_runner.incus import Incus
from competition.episode_runner.seeds import SIM_TUG_ARRIVALS, derive_generator_seed

STATE = Path("/var/lib/competition")
STATE_DIR = STATE / "sim-state"
IDENTITY = STATE / "living-port.json"
DEPLOY_LOG = STATE / "deploy.log"
SETTLED = STATE / "living-port-settled.json"
# The Incus device of the entry's submitted storage volume on siem-01.
VOLUME_DEVICE = "entry-volume"
# The longest wait for a fresh living port's four settle conditions.
SETTLE_TIMEOUT_S = 900.0
# The settle timeout's scale (episodes.md §6.6, settle period).
START_TIMEOUT_S = 120.0
# Variables that a make in the caller's environment would pass on.
MAKE_VARIABLES = ("MAKEFLAGS", "MFLAGS", "MAKELEVEL", "MAKEOVERRIDES")


class CreateError(Exception):
    """A step of creating the living port failed inside the VM."""


def iso_ms(moment: datetime) -> str:
    """A time in Coordinated Universal Time (UTC), to the millisecond, such as
    2026-11-11T20:00:00.000Z."""
    moment = moment.astimezone(timezone.utc)
    return f"{moment:%Y-%m-%dT%H:%M:%S}.{moment.microsecond // 1000:03d}Z"


def identity(seed: str, startup_text: str, start_line: str) -> dict[str, Any]:
    """The living port's identity file. Its tug seed is the start-up input's,
    which derives from the living port seed as an episode's derives from its
    episode seed."""
    tug_seed = json.loads(startup_text)[SIM_TUG_ARRIVALS]
    if tug_seed != derive_generator_seed(seed, SIM_TUG_ARRIVALS):
        raise CreateError("the start-up input's tug seed is not the living port seed's")
    return {
        "living_port_seed": seed,
        "tug_seed": tug_seed,
        "started_at": iso_ms(settle.start_state_time(start_line)),
        "startup": str(STATE_DIR / "startup.json"),
    }


def write_json(path: Path, data: dict[str, Any]) -> None:
    """Write a file that every user may read, in one rename."""
    part = path.with_name(path.name + ".part")
    part.write_text(json.dumps(data, indent=1) + "\n")
    part.chmod(0o644)
    part.replace(path)


def write_startup(seed: str, state_dir: Path, planned: datetime) -> str:
    """The start-up input, with the simulator's defaults at the competition's
    timings, written to state_dir/startup.json. The unprivileged container
    reads it, so every user may read the file and its directory."""
    text = startup.startup_input(seed, predicted_events.competition_timings(), planned)
    state_dir.mkdir(parents=True, exist_ok=True)
    state_dir.chmod(0o755)
    path = state_dir / "startup.json"
    path.write_text(text)
    path.chmod(0o644)
    return text


def make(
    targets: Sequence[str], variables: Sequence[str], log: Path, run: Callable[..., Any]
) -> None:
    env = {k: v for k, v in os.environ.items() if k not in MAKE_VARIABLES}
    with open(log, "a") as out:
        out.write(f"==> make {' '.join(targets)} {' '.join(variables)}\n")
        out.flush()
        result = run(
            ["make", "-C", str(deploy.DEPLOY), *targets, *variables],
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=subprocess.STDOUT,
            env=env,
        )
    if result.returncode != 0:
        raise CreateError(
            f"make {' '.join(targets)} exited {result.returncode}; see {log}"
        )


def missing_images(rendered: dict[str, Any], incus: Incus) -> list[str]:
    """The images that the render names and Incus does not hold."""
    aliases = {a["name"] for a in incus.query("/1.0/images/aliases?recursion=1")}
    return sorted(
        {name for name in rendered.get("images", {}).values() if name not in aliases}
    )


def wait_start_state(
    incus: Incus,
    timeout_s: float = START_TIMEOUT_S,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> str:
    """The simulator's start_state line, once it is logged."""
    deadline = monotonic() + timeout_s
    while True:
        found = incus.exec(
            settle.SIM_HOST, "grep", "-m", "1", "-F", settle.START_STATE, settle.SIM_LOG
        )
        if found.ok and found.stdout.strip():
            return found.stdout.strip()
        if settle.simulator_status(incus) == "crashed":
            raise CreateError("the simulator crashed before its start_state event")
        if monotonic() > deadline:
            raise CreateError(f"no start_state event within {timeout_s} s")
        sleep(2.0)


def clock_held(run: Callable[..., Any] = subprocess.run) -> bool:
    """Whether no time source sets the clock (the living port contract)."""
    ntp = run(
        ["timedatectl", "show", "-p", "NTP", "--value"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return ntp == "no"


def attach_volume(run: Callable[..., Any], source: Path) -> None:
    """The entry's submitted storage volume, read-only on siem-01 at
    /var/lib/entry, the one path that the launcher and the episode runner's
    test of the entry's defender read."""
    added = run(
        [
            "incus",
            "config",
            "device",
            "add",
            settle.SIEM,
            VOLUME_DEVICE,
            "disk",
            f"source={source}",
            f"path={volume.MOUNT}",
            "readonly=true",
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
    )
    if added.returncode != 0:
        raise CreateError(f"the volume was not attached: {added.stderr.strip()}")


def settled(
    incus: Incus,
    rendered: dict[str, Any],
    start_line: str,
    clock: Clock | None = None,
) -> dict[str, Any]:
    """Wait for the living port's four settle conditions, then wait the settle
    delay, and return the instant each condition held and the instant at which
    the settle period ended, on the living port's clock (episodes.md §6.6,
    settle period)."""
    from render import SIEM_MONITORED_NODES

    clock = clock or MonotonicClock()
    monitored = [n for n in SIEM_MONITORED_NODES if n in rendered["nodes"]]
    start = clock.monotonic(settle.start_state_time(start_line))
    try:
        found = settle.wait_to_settle(
            incus,
            clock,
            monitored,
            start,
            delay_s=settle.SETTLE_DELAY_S,
            timeout_s=SETTLE_TIMEOUT_S,
        )
    except settle.SettleFailure as failure:
        raise CreateError(f"the living port did not settle: {failure}") from failure
    # The settle period ends the settle delay after the last condition held,
    # at found.step_0.
    clock.sleep_until(found.step_0)
    return {
        "settled_at": iso_ms(clock.utc(found.step_0)),
        "conditions": {
            str(c.number): iso_ms(clock.utc(c.held))
            for c in found.conditions
            if c.held is not None
        },
    }


def create(
    seed: str,
    *,
    build: bool = False,
    volume: Path | None = None,
    incus: Incus | None = None,
    run: Callable[..., Any] = subprocess.run,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    state_dir: Path = STATE_DIR,
    identity_path: Path = IDENTITY,
    log: Path = DEPLOY_LOG,
    manifest: Callable[[], dict[str, Any]] = deploy.manifest,
    sleep: Callable[[float], None] = time.sleep,
    settle_wait: Callable[..., dict[str, Any]] = settled,
    settled_path: Path = SETTLED,
    extra_override: str = deploy.EXTRA_OVERRIDE,
) -> dict[str, Any]:
    """Every step of creating the living port inside its VM, with the
    competition's additional override file or an entry's own version of it,
    `extra_override`, which names the entry's images (episodes.md §3.2,
    modes). Returns the identity file's fields, with the living port's
    settling."""
    incus = incus or Incus(run)
    if not re.fullmatch(r"[0-9a-f]{64}", seed):
        raise CreateError("a living port seed is 64 lower-case hexadecimal digits")
    if not clock_held(run):
        raise CreateError(
            "a time source still sets the clock: hold it before the deployment"
        )
    left = [i["name"] for i in incus.instances()]
    if left:
        raise CreateError(f"the VM already holds instances: {', '.join(left)}")
    text = write_startup(seed, state_dir, now())
    override = f"EXTRA_OVERRIDE={extra_override}"
    make(("build",) if build else ("render",), (override,), log, run)
    rendered = manifest()
    missing = missing_images(rendered, incus)
    if missing:
        raise CreateError(
            f"images not built: {', '.join(missing)}; create with --build"
        )
    make(("deploy",), (override, f"SIM_STATE_DIR={state_dir}"), log, run)
    if volume is not None:
        attach_volume(run, volume)
    line = wait_start_state(incus, sleep=sleep)
    made = identity(seed, text, line)
    write_json(identity_path, made)
    settling = settle_wait(incus, rendered, line)
    write_json(settled_path, settling)
    return {**made, **settling}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m competition.living.guest")
    commands = parser.add_subparsers(dest="command", required=True)
    made = commands.add_parser("create", help="deploy the living port inside its VM")
    made.add_argument(
        "--seed", required=True, help="the living port seed, 64 hexadecimal digits"
    )
    made.add_argument(
        "--build", action="store_true", help="build the images before deploying"
    )
    made.add_argument(
        "--volume", type=Path, help="the entry's submitted storage volume, in the VM"
    )
    made.add_argument(
        "--extra-override",
        type=Path,
        help="an entry's own version of the competition's additional override "
        "file, in the tree (default: the competition's)",
    )
    args = parser.parse_args(argv)
    try:
        result = create(
            args.seed.lower(),
            build=args.build,
            volume=args.volume,
            extra_override=(
                str(args.extra_override.resolve())
                if args.extra_override
                else deploy.EXTRA_OVERRIDE
            ),
        )
    except CreateError as error:
        print(f"the living port was not created: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
