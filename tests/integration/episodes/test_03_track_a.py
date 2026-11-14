"""Track A after the first version: the alarm, the end at the start of the last
stage, R_n, the launcher and the query interface (episodes-tests.md §5, step
protocol; §6, query interface; §9, horizon and termination; §11, episode
record).

Four Track A episodes at fork 0, none suppressed, each from deployment to
reset:

- the false alarm: a benign episode whose test defender raises the alarm at
  step 20;
- the adversarial alarm: an adversarial episode of
  it-foothold__deny-directory-availability, whose test defender raises the
  alarm in a step after the release of stage 1;
- the last stage: an adversarial episode of the same chain whose test
  defender raises no alarm, which ends when the chain's last stage, the
  denial of the directory, falls due;
- the idle defender: an adversarial episode of the same chain with an empty
  volume, which runs the idle defender and ends at the start of the last
  stage too.

Each episode runs with the entry's storage volume, a directory that the
episode runner attaches read-only to siem-01 at /var/lib/entry. The suite
places the test defender there as `defender`, with its script beside it,
before the episode starts, and the launcher runs it (episodes-tests.md §0.2, a
test defender); the idle defender's episode has an empty volume. The test
defender notes each step message that it receives, and in the false alarm
reads archives.json through the query interface at each step, and once more
at step 0 with a start a day before, which returns the security monitoring's
history. The notes stay in /root, its working directory, which the suite reads
before the reset. Each test names
the episodes that serve it.

The suite needs the chain catalogue and the binaries from `make build-attacks`
in deploy/, as test_02_adversarial_episode.py does.

Run with: make -C tests/integration test-episodes
"""

from __future__ import annotations

import gzip
import hashlib
import json
import math
import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from competition.episode_runner import chains, deploy, episode, export, pacing
from competition.episode_runner.incus import Incus
from competition.episode_runner.seeds import (
    ATTACK_START_STEP,
    ATTACK_WAITS,
    derive_seeds,
)
from competition.episode_runner.steps import MonotonicClock, ProcessSession
from tests.integration.episodes.conftest import ADVERSARY, EpisodeRun, node, parse

CHAIN = "it-foothold__deny-directory-availability"
TEST_DEFENDER = Path(__file__).resolve().parent / "defender" / "test-defender"
ENTRY = "/var/lib/entry"
# The test defender's notes, in its working directory, since the volume is
# read-only.
DEFENDER_LOG = "/root/test-defender.jsonl"
BENIGN_SEED = hashlib.sha256(b"episodes suite track A benign 0").hexdigest()
ALARM_SEED = hashlib.sha256(b"episodes suite track A alarm 0").hexdigest()
LAST_STAGE_SEED = hashlib.sha256(b"episodes suite track A last stage 0").hexdigest()
IDLE_SEED = hashlib.sha256(b"episodes suite track A idle 0").hexdigest()
# How far before step 0 the false alarm's test defender starts its read of
# the history: a day, before the deployment of fork 0 and its first record.
HISTORY_S = 86_400.0
FALSE_ALARM_STEP = 20
STEP = timedelta(seconds=5)
TOLERANCE = timedelta(seconds=1)
LDAP_ROOT = (
    "timeout",
    "10",
    "ldapsearch",
    "-x",
    "-H",
    "ldapi:///",
    "-b",
    "",
    "-s",
    "base",
    "namingContexts",
)


def entry_volume(
    tmp_path_factory: pytest.TempPathFactory, script: dict[str, Any] | None
) -> Path:
    """The entry's submitted storage volume, a directory that the episode
    runner attaches read-only to siem-01 at /var/lib/entry: the test defender
    as `defender`, with its script beside it, or nothing for the idle
    defender. The suite places the test defender before the episode runner's
    test of the entry's defender (episodes-tests.md §0.2, a test
    defender)."""
    directory = tmp_path_factory.mktemp("entry-volume")
    if script is not None:
        (directory / "defender").write_bytes(TEST_DEFENDER.read_bytes())
        (directory / "defender").chmod(0o755)
        (directory / "script.json").write_text(json.dumps(script))
    return directory


def track_a_run(
    tmp_path_factory: pytest.TempPathFactory,
    seed: str,
    script: dict[str, Any] | None,
    chain_id: str | None = None,
) -> EpisodeRun:
    """One Track A episode at fork 0 with the test defender and `script`, or
    with the idle defender when script is None, adversarial when chain_id
    names a chain."""
    topology = deploy.topology()
    present = {i["name"] for i in Incus().instances()} & set(topology["nodes"])
    if present:
        pytest.fail(
            f"RavensPort nodes already exist ({', '.join(sorted(present))}); "
            "run `make down` in deploy/ first"
        )
    chain, chosen = None, None
    if chain_id is not None:
        if ADVERSARY not in topology["nodes"]:
            pytest.skip("the deployed topology holds no adversary host")
        try:
            catalogue = chains.load()
            chains.check_review()
        except chains.CatalogueError as error:
            pytest.fail(str(error))
        chain = chains.find(catalogue, chain_id)
        chosen = {
            "by": "the episode suite",
            "library": [c.id for c in chains.library(catalogue)],
        }
    records = tmp_path_factory.mktemp("track-a-records")
    volume = entry_volume(tmp_path_factory, script)
    observed: dict[str, dict[str, Any]] = {
        "at_step_0": {},
        "before_reset": {},
        "after_reset": {},
    }

    def session(argv: list[str]) -> ProcessSession:
        observed["at_step_0"]["directory"] = node("corp-it", *LDAP_ROOT).returncode
        return ProcessSession(argv)

    def down(clock: MonotonicClock) -> deploy.MakeRun:
        before = observed["before_reset"]
        log = node("siem-01", "cat", DEFENDER_LOG)
        before["defender_log"] = [
            json.loads(line) for line in log.stdout.splitlines() if line.strip()
        ]
        before["directory"] = node("corp-it", *LDAP_ROOT).returncode
        # A write into the volume from siem-01, which its read-only mount
        # refuses, and the device's own setting.
        written = node("siem-01", "touch", f"{ENTRY}/written")
        before["volume_write"] = (written.returncode, written.stderr)
        before["volume_readonly"] = subprocess.run(
            ["incus", "config", "device", "get", "siem-01", "entry-volume", "readonly"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        if script is None:
            before["faults"] = defender_faults(volume)
        made = deploy.down(clock)
        names = {i["name"] for i in Incus().instances()}
        observed["after_reset"]["remaining"] = sorted(names & set(topology["nodes"]))
        return made

    testbed = episode.Testbed(
        incus=Incus(), clock=MonotonicClock(), session=session, down=down
    )
    outcome = episode.run_episode(
        testbed,
        episode.Scenario.from_files(),
        seed,
        "A",
        records,
        episode_type=episode.BENIGN if chain is None else episode.ADVERSARIAL,
        chain=chain,
        chosen=chosen,
        entry_volume=volume,
    )
    if outcome.outcome == episode.DISCARDED:
        pytest.fail(f"the episode was discarded: {outcome.cause}")
    lines = (outcome.record / "steps.jsonl").read_text().splitlines()
    return EpisodeRun(
        seed=seed,
        outcome=outcome,
        record=json.loads((outcome.record / "episode.json").read_text()),
        steps=[json.loads(line) for line in lines],
        records=records,
        topology=topology,
        **observed,
    )


def defender_faults(volume: Path) -> dict[str, dict[str, Any]]:
    """After an episode, before the reset, make in the entry's volume each
    defender that cannot run, a file that is not executable and a directory,
    which siem-01 sees through its read-only mount, and run the launcher and
    the episode runner's test of the entry's defender against it."""
    entry = f"{ENTRY}/defender"
    made = volume / "defender"
    found = {}
    for cause in ("is not executable", "is a directory"):
        if made.is_dir():
            shutil.rmtree(made)
        elif made.exists():
            made.unlink()
        if cause == "is a directory":
            made.mkdir(mode=0o755)
        else:
            made.write_text("#!/bin/sh\n")
            made.chmod(0o644)
        launched = node("siem-01", episode.DEFENDER, entry)
        tested = node(
            "siem-01",
            "sh",
            "-c",
            episode.DEFENDER_TEST,
            "sh",
            entry,
            episode.IDLE_DEFENDER,
        )
        found[cause] = {
            "launcher": (launched.returncode, launched.stdout, launched.stderr),
            "test": (tested.returncode, tested.stdout, tested.stderr),
        }
    return found


def alarm_step(seed: str) -> int:
    """The first step that begins at least a second after the planned release
    of stage 1, which a defender's alarm in that step follows. Stage 2 of the
    chain falls due only after the scan of the IT segment, minutes later."""
    seeds = derive_seeds(seed)
    onset = pacing.onset_step(seeds[ATTACK_START_STEP]) * STEP.total_seconds()
    first_wait = pacing.waits(seeds[ATTACK_WAITS], 2)[0]
    return math.floor((onset + first_wait + 1.0) / STEP.total_seconds()) + 1


@pytest.fixture(scope="module")
def false_alarm(tmp_path_factory: pytest.TempPathFactory) -> EpisodeRun:
    return track_a_run(
        tmp_path_factory,
        BENIGN_SEED,
        {"alarm_at": FALSE_ALARM_STEP, "read": True, "since_s": HISTORY_S},
    )


@pytest.fixture(scope="module")
def adversarial_alarm(tmp_path_factory: pytest.TempPathFactory) -> EpisodeRun:
    return track_a_run(
        tmp_path_factory,
        ALARM_SEED,
        {"alarm_at": alarm_step(ALARM_SEED), "read": False},
        CHAIN,
    )


@pytest.fixture(scope="module")
def last_stage(tmp_path_factory: pytest.TempPathFactory) -> EpisodeRun:
    """The test defender, with no alarm, notes every step message up to the
    end notice at the start of the last stage."""
    return track_a_run(
        tmp_path_factory,
        LAST_STAGE_SEED,
        {"alarm_at": None, "read": False},
        CHAIN,
    )


@pytest.fixture(scope="module")
def idle(tmp_path_factory: pytest.TempPathFactory) -> EpisodeRun:
    """An empty volume, so the launcher runs the idle defender."""
    return track_a_run(tmp_path_factory, IDLE_SEED, None, CHAIN)


def messages(run: EpisodeRun) -> list[dict[str, Any]]:
    """The step messages that the test defender received, in order."""
    return [
        e["message"]
        for e in run.before_reset["defender_log"]
        if e["event"] == "message"
    ]


def releases(run: EpisodeRun) -> list[Any]:
    return [parse(t) for t in run.record["attack"]["session"]["releases"]]


def begun(run: EpisodeRun) -> list[int]:
    return [
        e["stage_number"]
        for e in run.record["attack"]["session"]["events"]
        if e["event"] == "begin"
    ]


# §5, step protocol


def test_a_benign_false_alarm_ends_the_episode(false_alarm) -> None:
    """The test defender's alarm at step 20 ends the episode at step 20, a
    false alarm with R_n = -1. Served by the false alarm."""
    run = false_alarm
    assert (run.outcome.outcome, run.outcome.cause) == ("alarm", None)
    assert [line["step"] for line in run.steps] == list(range(FALSE_ALARM_STEP + 1))
    assert [line["decision"] for line in run.steps] == [
        "no action"
    ] * FALSE_ALARM_STEP + ["alarm"]
    track_a = run.record["track_a"]
    assert track_a["alarm"]["step"] == FALSE_ALARM_STEP
    assert (track_a["K"], track_a["k_n"]) == (None, None)
    assert (track_a["R_n"], track_a["outcome"]) == (-1.0, "false alarm")
    assert track_a["end_cause"] == "alarm"


def test_a_track_a_alarm_ends_the_episode_at_its_arrival(
    false_alarm, adversarial_alarm
) -> None:
    """The end notice follows the alarm within 1 second, with the alarm's
    step plus 1, and the episode record ends with the alarm's step. Served by
    the two episodes that an alarm ends, the false alarm and the adversarial
    alarm."""
    for run in (false_alarm, adversarial_alarm):
        alarm = run.record["track_a"]["alarm"]
        arrival = parse(alarm["arrival"])
        sent = parse(run.record["steps"]["end_notice_sent"])
        assert timedelta(0) <= sent - arrival <= TOLERANCE
        assert run.record["steps"]["end_notice_step"] == alarm["step"] + 1
        assert run.steps[-1]["step"] == alarm["step"]
        assert run.steps[-1]["decision"] == "alarm"
        assert run.record["track_a"]["end_cause"] == "alarm"
    notice = messages(false_alarm)[-1]
    assert notice == {
        "track": "A",
        "step": FALSE_ALARM_STEP + 1,
        "previous_reward": None,
        "ended": True,
    }


def test_the_end_notice_carries_r_n(false_alarm, adversarial_alarm) -> None:
    """The half of evaluation mode, in which fork 0 runs: the end notice's
    previous_reward holds null, and the episode record holds R_n, which the
    last step's line of steps.jsonl carries too. Served by the false alarm,
    whose R_n is -1, and the adversarial alarm (episodes-tests.md,
    the_end_notice_carries_r_n; episodes.md §7, step protocol). The half of
    training mode is the unit tests'."""
    for run in (false_alarm, adversarial_alarm):
        notice = messages(run)[-1]
        assert notice["ended"] is True
        assert notice["previous_reward"] is None
        assert run.record["track_a"]["R_n"] == run.steps[-1]["reward"]
    assert false_alarm.record["track_a"]["R_n"] == -1.0


def test_the_step_messages_carry_the_previous_reward_and_the_end(
    false_alarm, adversarial_alarm, last_stage
) -> None:
    """The half of both modes: each decision request carries its step and
    states that the episode goes on, and the end notice carries the number of
    steps that the episode ran and states that the episode has ended. Served
    by the three episodes whose test defender notes the step messages: the
    false alarm, the adversarial alarm and the last stage, which end at a
    false alarm, at an alarm during the attack chain and at the start of the
    last stage (episodes-tests.md,
    the_step_messages_carry_the_previous_reward_and_the_end; episodes.md §7,
    step protocol). The rewards that training mode carries are the living-port
    suite's, under evaluation_step_messages_carry_no_reward."""
    for run in (false_alarm, adversarial_alarm, last_stage):
        *requests, notice = messages(run)
        assert [m["step"] for m in requests] == [s["step"] for s in run.steps]
        assert not any(m["ended"] for m in requests)
        assert notice["ended"] is True
        assert notice["step"] == run.steps[-1]["step"] + 1


def test_evaluation_step_messages_carry_no_reward(
    false_alarm, adversarial_alarm, last_stage
) -> None:
    """The half of evaluation mode, in which fork 0 runs: the previous_reward
    of every decision request and of the end notice holds null, and the
    episode record holds the reward of every step, 0 until the last step,
    which holds R_n. Served by the three episodes whose test defender notes
    the step messages (episodes-tests.md,
    evaluation_step_messages_carry_no_reward; episodes.md §7, step
    protocol)."""
    for run in (false_alarm, adversarial_alarm, last_stage):
        received = messages(run)
        assert received and all(m["previous_reward"] is None for m in received)
        rewards = [s["reward"] for s in run.steps]
        assert rewards == [0.0] * (len(rewards) - 1) + [run.record["track_a"]["R_n"]]


def test_no_stage_is_released_after_the_alarm(adversarial_alarm) -> None:
    """k_n counts the stages released before the alarm, here stage 1 alone;
    the binary receives no further `begin`, and R_n = -(1 - 1)/(2 - 1). The
    alarm arrives during stage 1, the scan of the IT segment, so the episode
    runner stops the binary with SIGTERM, then closes its standard input, and
    the session reports the binary's status for that stop. The exit status 3
    of an alarm during a wait is the unit tests'. The specification gives no
    number for the status after SIGTERM, only that the session reports the
    signal's status (episode-runner-impl.md §9), so the test checks that a
    status is recorded. Served by the adversarial alarm (episodes.md §10.4,
    the attack chain's session; §10.5, stopping the chain)."""
    run = adversarial_alarm
    arrival = parse(run.record["track_a"]["alarm"]["arrival"])
    released = releases(run)
    assert len(released) == 1 and released[0] < arrival
    assert begun(run) == [1]
    track_a = run.record["track_a"]
    assert (track_a["K"], track_a["k_n"], track_a["outcome"]) == (2, 1, 1)
    assert track_a["R_n"] == 0.0
    session = run.record["attack"]["session"]
    ends = [parse(e["at"]) for e in session["events"] if e["event"] == "end"]
    assert all(end > arrival for end in ends)
    assert session["stop"] == "SIGTERM, then standard input closed"
    assert isinstance(session["exit_status"], int)
    assert session["problems"] == []
    assert run.after_reset["remaining"] == []


# §9, horizon and termination


def test_a_track_a_episode_ends_at_the_start_of_the_last_stage(last_stage) -> None:
    """With no alarm, the episode ends at the instant at which the last stage
    falls due; no `begin` is written for it, and the outcome is 4 with
    R_n = -1. The directory's denial never runs, so a query of the directory
    succeeds at step 0 and before the reset. Served by the last stage, whose
    test defender raises no alarm."""
    run = last_stage
    assert (run.outcome.outcome, run.outcome.cause) == ("last_stage", None)
    plan = run.record["attack"]
    assert len(releases(run)) == 1 and begun(run) == [1]
    (end,) = [e for e in plan["session"]["events"] if e["event"] == "end"]
    due = parse(end["at"]) + timedelta(seconds=plan["waits_s"][1])
    assert abs(parse(run.record["steps"]["ended_early"]) - due) <= TOLERANCE
    track_a = run.record["track_a"]
    assert track_a == {
        "alarm": None,
        "K": 2,
        "k_n": 2,
        "R_n": -1.0,
        "outcome": 4,
        "end_cause": "last_stage",
    }
    assert plan["early_stop"]["stage_number"] == 2
    assert run.at_step_0["directory"] == 0
    assert run.before_reset["directory"] == 0


# §5, the launcher


def test_a_volumes_defender_runs_in_place_of_the_idle_defender(false_alarm) -> None:
    """The step session's process runs the test defender from the entry's
    storage volume, and no process runs the launcher. Served by the false
    alarm."""
    (start,) = [
        e for e in false_alarm.before_reset["defender_log"] if e["event"] == "start"
    ]
    assert f"{ENTRY}/defender" in start["argv"]
    assert start["launchers"] == []
    assert (start["uid"], start["cwd"]) == (0, "/root")
    assert false_alarm.record["defender_program"] == f"{ENTRY}/defender"
    assert false_alarm.steps[FALSE_ALARM_STEP]["decision"] == "alarm"


# The line that the Incus client adds to the standard error of a command that
# `incus exec` runs, when the command exits with status 126.
INCUS_126 = "Error: Command not executable"


def standard_error(text: str) -> list[str]:
    """The lines of a command's standard error through `incus exec`, without
    the Incus client's own line for status 126, which may follow them."""
    lines = text.splitlines()
    return lines[:-1] if lines and lines[-1] == INCUS_126 else lines


def test_a_defender_that_cannot_run_makes_the_launcher_exit_126(idle) -> None:
    """A defender that is not executable, or is a directory, makes the
    launcher exit with status 126 and run no program, as the episode runner's
    own test finds before the step session opens. Through `incus exec`, the
    Incus client may add its own line for status 126 after the launcher's.
    Served by the idle defender's episode, whose empty volume the suite fills
    after the steps (episodes-tests.md,
    a_defender_that_cannot_run_discards_the_episode; episodes.md §7, step
    protocol)."""
    entry = f"{ENTRY}/defender"
    for cause, found in idle.before_reset["faults"].items():
        status, stdout, stderr = found["launcher"]
        assert (status, stdout) == (126, ""), cause
        assert standard_error(stderr) == [f"launcher: {entry} {cause}"], cause
        status, stdout, stderr = found["test"]
        assert (status, stdout) == (126, ""), cause
        assert standard_error(stderr) == [f"{entry} {cause}"], cause


def test_a_fork_0_episode_holds_the_volume_read_only(
    false_alarm, adversarial_alarm, last_stage, idle
) -> None:
    """At fork 0 the run command attaches the entry's volume read-only at
    /var/lib/entry: a write from siem-01 fails, and the volume on the host
    gains no file. Served by all four episodes (episodes-tests.md,
    a_fork_0_episode_holds_the_volume_read_only; episodes.md §3.2, modes)."""
    for run in (false_alarm, adversarial_alarm, last_stage, idle):
        status, stderr = run.before_reset["volume_write"]
        assert status != 0 and "Read-only file system" in stderr
        assert run.before_reset["volume_readonly"] == "true"
        source = Path(run.record["entry_volume"]["source"])
        assert not (source / "written").exists()


def test_a_volume_without_a_defender_runs_the_idle_defender(idle) -> None:
    """With an empty volume, the launcher runs the idle defender, which
    answers every decision request and keeps no notes, and the episode record
    names it. Served by the idle defender's episode."""
    assert all(line["decision"] == "no action" for line in idle.steps)
    assert all(line["decision_arrival"] is not None for line in idle.steps)
    assert idle.before_reset["defender_log"] == []
    assert idle.record["defender_program"] == "/usr/local/bin/idle-defender"


# §6, query interface


def test_siem_read_starts_before_the_decision_request_of_step_0(false_alarm) -> None:
    """siem-read --start runs at step 0 and exits with status 0 before step
    0's decision request. Served by the false alarm."""
    start = false_alarm.record["query_interface_start"]
    assert start["exit_status"] == 0
    step_0 = parse(false_alarm.record["settle"]["step_0"])
    assert step_0 <= parse(start["sent"]) <= parse(false_alarm.steps[0]["request_sent"])


def exported_reads(run: EpisodeRun) -> list[dict[str, Any]]:
    text = (run.path / "reads.jsonl").read_text()
    return [json.loads(line) for line in text.splitlines()]


def test_every_read_enters_the_record_of_reads(false_alarm) -> None:
    """Each read of the test defender, one at each step and one at step 0
    with a start before it, enters the record of reads. Served by the false
    alarm, whose test defender reads."""
    reads = exported_reads(false_alarm)
    per_step = [r for r in reads if r["request"]["since"] is None]
    assert [r["step"] for r in per_step] == list(range(FALSE_ALARM_STEP + 1))
    assert len(reads) == len(per_step) + 1
    for read in reads:
        request = read["request"]
        assert (request["output"], request["host"], request["input"]) == (
            "archives",
            None,
            None,
        )
        assert read["ranges"] and read["time"]


def dated_day(name: str) -> tuple[int, int, int]:
    """The day of a dated file by its path below /var/ossec/logs, such as
    archives/2026/Nov/ossec-archive-13.json.gz."""
    _, year, month, base = name.split("/")
    day = base.split(".")[0].rsplit("-", 1)[1]
    return int(year), export.MONTHS.index(month), int(day)


def test_a_read_with_a_start_before_step_0_returns_the_history(false_alarm) -> None:
    """episodes-tests.md, a_read_with_a_start_before_step_0_returns_the_history,
    in a live episode. At step 0 the test defender reads archives.json with
    --since a day before, an instant before the security monitoring's first
    record, which at fork 0 comes with the deployment. The record of reads
    holds the read with its start. Its ranges begin at the first record of
    the earliest dated file, hold records written before step 0, and name
    only files that the export holds. Served by the false alarm."""
    step_0 = parse(false_alarm.record["settle"]["step_0"])
    (read,) = [
        r for r in exported_reads(false_alarm) if r["request"]["since"] is not None
    ]
    assert read["step"] == 0
    assert parse(read["request"]["since"]) < step_0 - timedelta(seconds=HISTORY_S - 60)
    exported = sorted(
        (
            path.relative_to(false_alarm.path).as_posix().removesuffix(".gz")
            for path in (false_alarm.path / "archives").rglob("*")
            if path.is_file()
        ),
        key=dated_day,
    )
    assert read["ranges"][0] == {
        "file": exported[0],
        "start": 0,
        "end": read["ranges"][0]["end"],
    }
    data = b""
    for r in read["ranges"]:
        assert r["file"] in exported, r["file"]
        path = false_alarm.path / r["file"]
        content = (
            path.read_bytes()
            if path.exists()
            else gzip.decompress(Path(f"{path}.gz").read_bytes())
        )
        data += content[r["start"] : r["end"]]
    stamps = [
        datetime.strptime(json.loads(line)["timestamp"], "%Y-%m-%dT%H:%M:%S.%f%z")
        for line in data.splitlines()
    ]
    assert read["records"] == len(stamps)
    assert any(stamp < step_0 for stamp in stamps)


def test_reads_replay_from_the_episode_record(false_alarm) -> None:
    """Each read in the record of reads, replayed against the exported dated
    files, returns byte for byte what the test defender received. Served by
    the false alarm, whose test defender reads."""
    noted = [
        e for e in false_alarm.before_reset["defender_log"] if e["event"] == "read"
    ]
    reads = exported_reads(false_alarm)
    assert len(noted) == len(reads)
    for read, got in zip(reads, noted, strict=True):
        assert got["status"] == 0
        data = b""
        for r in read["ranges"]:
            path = false_alarm.path / r["file"]
            content = (
                path.read_bytes()
                if path.exists()
                else gzip.decompress(Path(f"{path}.gz").read_bytes())
            )
            data += content[r["start"] : r["end"]]
        assert hashlib.sha256(data).hexdigest() == got["sha256"], read["step"]
        assert data.count(b"\n") == got["records"] == read["records"]


# The traces of the episode runner's own commands that the history before
# step 0 must not hold: the query interface's program and the step session's
# launcher (episode-runner-impl.md §12, query interface).
RUNNER_TRACES = (b"siem-read", episode.DEFENDER.encode())


def records_before_step_0(run: EpisodeRun) -> list[tuple[bytes, dict[str, Any]]]:
    """Each record of the exported dated files of alerts.json and
    archives.json whose timestamp comes before step 0, as its line and its
    JSON object."""
    step_0 = parse(run.record["settle"]["step_0"])
    found = []
    for output in ("alerts", "archives"):
        for path in sorted((run.path / output).rglob("*")):
            if not path.is_file():
                continue
            data = path.read_bytes()
            if path.name.endswith(".gz"):
                data = gzip.decompress(data)
            for line in data.splitlines():
                try:
                    record = json.loads(line)
                    stamp = datetime.strptime(
                        record["timestamp"], "%Y-%m-%dT%H:%M:%S.%f%z"
                    )
                except (ValueError, KeyError, TypeError):
                    continue
                if stamp < step_0:
                    found.append((line, record))
    return found


def test_the_history_before_step_0_holds_no_trace_of_the_episode_runner(
    false_alarm, adversarial_alarm
) -> None:
    """episodes-tests.md, the_history_before_step_0_holds_no_trace_of_the_episode_runner:
    in a benign and an adversarial episode, no record of alerts.json or
    archives.json written before step 0 holds a trace of the check of the
    chain's binary, of the step session or of siem-read --start. No such
    record names siem-read, the launcher /usr/local/bin/defender or the
    chain's binary, and none comes from the adversary host, so a read of the
    history reveals nothing of the episode's type (episode-runner-impl.md
    §12, query interface). Served by the false alarm and the adversarial
    alarm."""
    binary = adversarial_alarm.record["attack"]["binary"].encode()
    for run in (false_alarm, adversarial_alarm):
        before = records_before_step_0(run)
        assert before, f"{run.seed}: the export holds no record from before step 0"
        for line, record in before:
            for trace in (*RUNNER_TRACES, binary):
                assert trace not in line, (run.seed, trace, line[:200])
            agent = record.get("agent")
            assert not (isinstance(agent, dict) and agent.get("name") == ADVERSARY), (
                run.seed,
                line[:200],
            )


# §11, episode record


def test_the_episode_record_holds_track_a_fields(
    false_alarm, adversarial_alarm, last_stage, idle
) -> None:
    """Each record holds Track A's fields, and its outcome is its end cause.
    Served by all four episodes."""
    for run in (false_alarm, adversarial_alarm, last_stage, idle):
        track_a = run.record["track_a"]
        assert set(track_a) == {"alarm", "K", "k_n", "R_n", "outcome", "end_cause"}
        assert track_a["end_cause"] in episode.END_CAUSES
        # An end cause and an outcome that name the same end take one value.
        assert run.record["outcome"] == track_a["end_cause"]
        assert run.record["suppressed"] is False
        assert run.after_reset["remaining"] == []
