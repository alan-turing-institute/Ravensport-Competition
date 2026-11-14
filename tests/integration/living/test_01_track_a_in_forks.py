"""Track A in forks of living ports in training mode: the halves of the tests
that wait on a living port and its forks in the integration suite
(episodes-tests.md §0.6, what waits; §5, step protocol).

Each case takes a living port of its own, created in training mode with the
case's training volume, and one fork of it, made at once through the
manager. The manager carries the volume into the fork, where the episode
runner's `copy` command opens it to siem-01's users and attaches it
read-write at /var/lib/entry. The four living ports, the longest case first:

- `idle`: an empty volume, so the idle defender runs a benign episode to the
  horizon, 144 steps of 5 seconds;
- `alarm`: the test defender, which raises the alarm at step 5 of a benign
  episode and keeps its notes in the volume, which returns them to the host;
- `not-executable` and `directory`: a `defender` that is a file of mode 0644,
  or a directory, which discards the episode before step 0.

The `copy` command opens the training volume before attaching it, so a
`defender` without read permission, and a volume that siem-01 cannot read and
search, cannot reach an episode in a fork. On each discard port's fork, kept
after its episode, the suite therefore also attaches each of the four
`defender`s that cannot run to siem-01 directly through Incus, unopened, and
runs the launcher and the episode runner's test against it, as
test_03_track_a.py does at fork 0.

The living ports are created in parallel as the parallel input allows, each
with a manager, a manager's root, ROOT/CASE, and SSH ports of its own, and a
name with the run's prefix. They are stopped and deleted after the module,
unless a test failed or the keep input is set.

Run with: make -C tests/integration test-living
"""

from __future__ import annotations

import json
import logging
import shlex
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from competition.episode_runner import episode
from competition.living import port as living_port
from competition.living.ssh import Remote
from tests.integration.living.conftest import (
    Inputs,
    Port,
    delete_ports,
    living_ports,
)

log = logging.getLogger(__name__)

TEST_DEFENDER = (
    Path(__file__).resolve().parents[1] / "episodes" / "defender" / "test-defender"
)
ENTRY = "/var/lib/entry"
# The test defender's notes, in the training volume, which returns them.
NOTES = "test-defender.jsonl"
ALARM_STEP = 5
HORIZON_STEPS = 144
TOLERANCE = timedelta(seconds=1)
# The cases, the longest first, so that a limited number of living ports at
# once ends soonest.
CASES = ("idle", "alarm", "not-executable", "directory")
# The cause that a discard port's `defender` gives.
DISCARDED = {"not-executable": "is not executable", "directory": "is a directory"}
# Each `defender` that cannot run, as the suite attaches it unopened to a kept
# fork's siem-01: the shell commands that make it in a directory {d} of the
# fork, and the launcher's message after `launcher: `.
CANNOT_RUN = {
    "a file without execute permission": (
        "mkdir -m 0755 {d} && printf '#!/bin/sh\\n' > {d}/defender && "
        "chmod 0644 {d}/defender",
        f"{ENTRY}/defender is not executable",
    ),
    "a file without read permission": (
        "mkdir -m 0755 {d} && printf '#!/bin/sh\\n' > {d}/defender && "
        "chmod 0311 {d}/defender",
        f"{ENTRY}/defender is not readable",
    ),
    "a directory": (
        "mkdir -m 0755 {d} && mkdir -m 0755 {d}/defender",
        f"{ENTRY}/defender is a directory",
    ),
    "a volume that siem-01 cannot read and search": (
        "mkdir -m 0700 {d} && printf '#!/bin/sh\\n' > {d}/defender && "
        "chmod 0755 {d}/defender",
        f"{ENTRY} cannot be read and searched",
    ),
}
# The device through which the suite attaches each such `defender`.
CHECK_DEVICE = "launcher-check"
# The line that the Incus client adds to the standard error of a command that
# `incus exec` runs, when the command exits with status 126.
INCUS_126 = "Error: Command not executable"


@dataclass
class CopyRun:
    """One case's fork: the manager's episode, the fork's episode record and
    steps, the training volume that came back, and the launcher's runs."""

    k: int
    episode: dict[str, Any]
    record: dict[str, Any]
    steps: list[dict[str, Any]]
    volume_back: Path | None
    launcher: dict[str, dict[str, tuple[int, str, str]]] = field(default_factory=dict)

    def notes(self) -> list[dict[str, Any]]:
        if self.volume_back is None or not (self.volume_back / NOTES).is_file():
            return []
        text = (self.volume_back / NOTES).read_text()
        return [json.loads(line) for line in text.splitlines() if line.strip()]

    def messages(self) -> list[dict[str, Any]]:
        return [e["message"] for e in self.notes() if e["event"] == "message"]


def parse(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def standard_error(text: str) -> list[str]:
    """The lines of a command's standard error through `incus exec`, without
    the Incus client's own line for status 126, which may follow them."""
    lines = text.splitlines()
    return lines[:-1] if lines and lines[-1] == INCUS_126 else lines


def training_volume(directory: Path, case: str) -> Path:
    """The training volume that a case's living port starts from."""
    directory.mkdir()
    entry = directory / "defender"
    if case == "alarm":
        entry.write_bytes(TEST_DEFENDER.read_bytes())
        entry.chmod(0o755)
        script = {"alarm_at": ALARM_STEP, "read": True, "log": f"{ENTRY}/{NOTES}"}
        (directory / "script.json").write_text(json.dumps(script))
    elif case == "not-executable":
        entry.write_text("#!/bin/sh\nexit 0\n")
        entry.chmod(0o644)
    elif case == "directory":
        entry.mkdir(mode=0o755)
    return directory


def launcher_against(remote: Remote, k: int) -> dict[str, dict[str, tuple]]:
    """In a kept fork, attach each `defender` that cannot run, unopened, to
    siem-01 at /var/lib/entry through Incus, and run the launcher and the
    episode runner's test of the entry's defender against it."""
    entry = f"{ENTRY}/defender"
    found = {}

    def run(command: str) -> tuple[int, str, str]:
        done = remote.run(living_port.login(command), check=False)
        return done.returncode, done.stdout, done.stderr

    for n, (variant, (make, _)) in enumerate(CANNOT_RUN.items()):
        directory = f"/tmp/launcher-check-{k}-{n}"
        made = run(make.format(d=shlex.quote(directory)))
        assert made[0] == 0, (variant, made)
        added = run(
            f"incus config device add siem-01 {CHECK_DEVICE} disk "
            f"source={directory} path={ENTRY} readonly=true"
        )
        assert added[0] == 0, (variant, added)
        try:
            found[variant] = {
                "launcher": run(f"incus exec siem-01 -- {episode.DEFENDER} {entry}"),
                "test": run(
                    "incus exec siem-01 -- sh -c "
                    + shlex.quote(episode.DEFENDER_TEST)
                    + f" sh {entry} {episode.IDLE_DEFENDER}"
                ),
            }
        finally:
            run(f"incus config device remove siem-01 {CHECK_DEVICE}")
    return found


def run_case(port: Port, volume: Path) -> CopyRun:
    """Create the case's living port, fork it at once, run one benign Track
    A episode in the fork, and read what it left. A discard port's fork is
    kept for the launcher's runs, then deleted."""
    inputs = port.inputs
    log.info("%s: creating the living port %s", port.case, port.name)
    port.created = True
    port.manager.create(
        port.name,
        port.base(),
        inputs.tree,
        ssh_port=port.ssh_port,
        affinity=inputs.affinity,
        training_volume=volume,
    )
    fork = port.manager.fork(port.name)
    k = int(fork["k"])
    keep_copy = port.case in DISCARDED
    launcher: dict[str, dict[str, tuple]] = {}
    try:
        made = port.manager.episode(
            port.name,
            k,
            keep_copy=keep_copy,
            track="A",
            episode_type=episode.BENIGN,
            chain=None,
        )
        if keep_copy:
            launcher = launcher_against(port.remote(int(fork["copy"]["ssh_port"])), k)
    finally:
        if keep_copy:
            try:
                port.manager.delete_copy(port.name, k)
            except Exception as error:  # noqa: BLE001 - reported, the case goes on
                log.error("%s: fork %d was not deleted: %s", port.name, k, error)
    out = port.manager.port_dir(port.name) / "episodes" / f"fork-{k}"
    steps_file = out / "steps.jsonl"
    steps = (
        [json.loads(line) for line in steps_file.read_text().splitlines()]
        if steps_file.is_file()
        else []
    )
    back = (made.get("training_out") or {}).get("version")
    lineage = port.manager.port_dir(port.name) / "training"
    return CopyRun(
        k=k,
        episode=made,
        record=json.loads((out / "record.json").read_text()),
        steps=steps,
        volume_back=None if back is None else lineage / f"v{back}",
        launcher=launcher,
    )


@pytest.fixture(scope="module")
def cases(
    inputs: Inputs, request: pytest.FixtureRequest, tmp_path_factory
) -> Iterator[dict[str, CopyRun | BaseException]]:
    """Every case's fork, or the exception that ended it, with the living
    ports created as many at once as the parallel input allows."""
    names = {case: f"{inputs.run}-{case}" for case in CASES}
    ports = living_ports(inputs, names, inputs.ssh_port)
    volumes = tmp_path_factory.mktemp("training-volumes")
    with ThreadPoolExecutor(max_workers=inputs.parallel) as pool:
        futures = {
            case: pool.submit(
                run_case, ports[case], training_volume(volumes / case, case)
            )
            for case in CASES
        }
        results: dict[str, CopyRun | BaseException] = {}
        for case, future in futures.items():
            failure = future.exception()
            results[case] = failure if failure is not None else future.result()
    yield results
    delete_ports(ports.values(), request.session)


def outcome_of(results: dict[str, CopyRun | BaseException], name: str) -> CopyRun:
    """A case's fork, or the exception that ended it, raised again."""
    found = results[name]
    if isinstance(found, BaseException):
        raise found
    return found


@pytest.fixture(scope="module")
def discards(cases) -> dict[str, CopyRun]:
    return {name: outcome_of(cases, name) for name in DISCARDED}


@pytest.fixture(scope="module")
def alarm(cases) -> CopyRun:
    return outcome_of(cases, "alarm")


@pytest.fixture(scope="module")
def idle(cases) -> CopyRun:
    return outcome_of(cases, "idle")


def test_a_defender_that_cannot_run_discards_the_episode(discards) -> None:
    """episodes-tests.md, a_defender_that_cannot_run_discards_the_episode, in
    a fork in training mode: a `defender` that is a file of mode 0644, or a
    directory, discards the episode before step 0 with that cause in the
    episode record. Attached unopened through Incus, each `defender` that
    cannot run, a file without execute permission, a file without read
    permission, a directory and a volume that siem-01 cannot read and search,
    makes the launcher exit with status 126 and run no program, as the
    episode runner's own test finds (episodes.md §7, step protocol)."""
    for name, run in discards.items():
        cause = f"the entry's defender cannot run: {ENTRY}/defender {DISCARDED[name]}"
        assert run.record["mode"] == "training", name
        assert (run.record["outcome"], run.record["cause"]) == ("discarded", cause)
        assert run.steps == [], name
        assert "query_interface_start" not in run.record, name
        assert set(run.launcher) == set(CANNOT_RUN), name
        for variant, (_, message) in CANNOT_RUN.items():
            status, stdout, stderr = run.launcher[variant]["launcher"]
            assert (status, stdout) == (126, ""), (name, variant)
            assert standard_error(stderr) == [f"launcher: {message}"], (name, variant)
            status, stdout, stderr = run.launcher[variant]["test"]
            assert (status, stdout) == (126, ""), (name, variant)
            assert standard_error(stderr) == [message], (name, variant)


def test_a_track_a_alarm_ends_the_episode_at_its_arrival(alarm) -> None:
    """episodes-tests.md, a_track_a_alarm_ends_the_episode_at_its_arrival, in
    training mode: the end notice follows the test defender's alarm within 1
    second, with the alarm's step plus 1, and the episode record ends with
    the alarm's step, with the end cause `alarm` and the outcome `alarm`."""
    record = alarm.record
    assert record["mode"] == "training"
    assert (record["outcome"], record["cause"]) == ("alarm", None)
    found = record["track_a"]["alarm"]
    assert found["step"] == ALARM_STEP
    sent = parse(record["steps"]["end_notice_sent"])
    assert timedelta(0) <= sent - parse(found["arrival"]) <= TOLERANCE
    assert record["steps"]["end_notice_step"] == ALARM_STEP + 1
    assert [line["step"] for line in alarm.steps] == list(range(ALARM_STEP + 1))
    assert alarm.steps[-1]["decision"] == "alarm"
    assert record["track_a"]["end_cause"] == "alarm"
    assert alarm.messages()[-1] == {
        "track": "A",
        "step": ALARM_STEP + 1,
        "previous_reward": -1.0,
        "ended": True,
    }


def test_evaluation_step_messages_carry_no_reward(alarm) -> None:
    """episodes-tests.md, evaluation_step_messages_carry_no_reward, the half
    of training mode: each decision request after step 0 holds the reward of
    the step before it, 0, and the end notice holds the reward of the last
    step, R_n, -1 for the false alarm, as the episode record holds them
    (episodes.md §7, step protocol). The half of evaluation mode is
    test_03_track_a.py's, at fork 0."""
    *requests, notice = alarm.messages()
    assert [m["step"] for m in requests] == [s["step"] for s in alarm.steps]
    assert [m["previous_reward"] for m in requests] == [None] + [
        s["reward"] for s in alarm.steps[:-1]
    ]
    assert all(s["reward"] == 0.0 for s in alarm.steps[:-1])
    assert notice["ended"] is True
    assert notice["previous_reward"] == alarm.steps[-1]["reward"]
    assert notice["previous_reward"] == alarm.record["track_a"]["R_n"] == -1.0


def test_a_volumes_defender_runs_in_place_of_the_idle_defender(alarm) -> None:
    """episodes-tests.md, a_volumes_defender_runs_in_place_of_the_idle_defender,
    in a fork in training mode: the episode record holds the test defender's
    scripted decisions and names /var/lib/entry/defender, and the step
    session's process runs the test defender as root in /root, with no
    process running the launcher."""
    assert alarm.record["defender_program"] == f"{ENTRY}/defender"
    assert [line["decision"] for line in alarm.steps] == ["no action"] * ALARM_STEP + [
        "alarm"
    ]
    (start,) = [e for e in alarm.notes() if e["event"] == "start"]
    assert f"{ENTRY}/defender" in start["argv"]
    assert start["launchers"] == []
    assert (start["uid"], start["cwd"]) == (0, "/root")


def test_a_volume_without_a_defender_runs_the_idle_defender(idle) -> None:
    """episodes-tests.md, a_volume_without_a_defender_runs_the_idle_defender,
    in a fork in training mode: the episode record holds the idle defender's
    decision for every step of the horizon, and names
    /usr/local/bin/idle-defender as the program that ran."""
    assert idle.record["mode"] == "training"
    assert idle.record["defender_program"] == episode.IDLE_DEFENDER
    assert (idle.record["outcome"], idle.record["track_a"]["end_cause"]) == (
        "horizon",
        "horizon",
    )
    assert [line["step"] for line in idle.steps] == list(range(HORIZON_STEPS))
    assert all(line["decision"] == "no action" for line in idle.steps)
    assert all(line["decision_arrival"] is not None for line in idle.steps)
    assert idle.notes() == []
