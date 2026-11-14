"""The attack chain's session (episodes.md §10.4, the attack chain's session;
§10.5, stopping the chain).

A stand-in for a paced binary runs as a local process and follows the pacing
interface of the attack phase's binaries: its first line is its process
identifier, each stage waits for the line `begin`, each stage writes its beginning
and end events, standard input closing stops it with exit status 3, and a
holding stage restores at SIGTERM. A command line that it cannot use exits with
status 2 before its first line. A fake Incus delivers the signal locally.
The tests release each stage when it falls due, as the step schedule does. The
live suite in tests/integration/episodes runs the real binaries on the
adversary host.

Run with: pytest competition/tests -q
"""

from __future__ import annotations

import os
import signal
import sys
import time
from pathlib import Path

import pytest

from competition.episode_runner.attack import AttackChain, early_stop, parse_event
from competition.episode_runner.incus import Result
from competition.episode_runner.steps import MonotonicClock

# Each stage of the plan, given as the argument: ok completes; fail fails with
# a reason; hold holds until SIGTERM, then restores and completes; missing
# fails its requires check, which exits before the stage's release. Any other
# kind, or no plan, is a command line that the stand-in cannot use.
STAND_IN = r"""
import json, os, signal, sys, time
plan = sys.argv[1].split(",") if len(sys.argv) == 2 else []
if not plan or not set(plan) <= {"ok", "fail", "hold", "missing"}:
    print("usage: a plan of ok, fail, hold and missing", file=sys.stderr, flush=True)
    sys.exit(2)
print(os.getpid(), flush=True)
held = False
def restore(signum, frame):
    global held
    held = False
for n, kind in enumerate(plan, start=1):
    if kind == "missing":
        print(f"stage {n}: a required fact is missing", file=sys.stderr, flush=True)
        sys.exit(1)
    if sys.stdin.readline().rstrip("\r\n") != "begin":
        sys.exit(3)
    stage = f"stage-{n}"
    if kind == "hold":
        held = True
        signal.signal(signal.SIGTERM, restore)
    print(json.dumps({"stage_event": "begin", "stage_number": n, "total_num_stages": len(plan), "stage": stage}), flush=True)
    print(f"[+] the headline result of {stage}", flush=True)
    if kind == "fail":
        print(json.dumps({"stage_event": "end", "stage_number": n, "stage": stage, "outcome": "failed", "reason": "no reply"}), flush=True)
        sys.exit(1)
    while held:
        time.sleep(0.01)
    print(json.dumps({"stage_event": "end", "stage_number": n, "stage": stage, "outcome": "completed"}), flush=True)
"""


class LocalIncus:
    """Delivers `incus exec <node> -- kill -TERM <pid>` to a local process."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def exec(self, node: str, *argv: str, timeout_s: float = 60.0) -> Result:
        self.calls.append((node, *argv))
        assert argv[:2] == ("kill", "-TERM")
        os.kill(int(argv[2]), signal.SIGTERM)
        return Result(0, "", "")


def chain(
    plan: str, waits: list[float], tmp_path: Path
) -> tuple[AttackChain, LocalIncus]:
    incus = LocalIncus()
    attack = AttackChain(
        incus,
        MonotonicClock(),
        [sys.executable, "-c", STAND_IN, plan],
        "adversary",
        waits,
        tmp_path / "attack-chain.log",
    )
    return attack, incus


def wait_for(condition, timeout_s: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("the condition never held")
        time.sleep(0.01)


def drive(
    attack: AttackChain,
    condition,
    timeout_s: float = 10.0,
    up_to: int | None = None,
) -> None:
    """Release each stage when it falls due, as the step schedule does, up to
    stage `up_to` when given, until the condition holds."""
    deadline = time.monotonic() + timeout_s
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("the condition never held")
        due = attack.next_stage()
        if (
            due is not None
            and time.monotonic() >= due[1]
            and (up_to is None or due[0] <= up_to)
        ):
            assert attack.release(due[0])
        time.sleep(0.005)


def finished(attack: AttackChain) -> bool:
    """Whether the binary has exited and its session has been read to the
    end."""
    return (
        attack._thread is not None
        and not attack._thread.is_alive()
        and attack._proc.poll() is not None
    )


def between_stages(attack: AttackChain, events: int) -> bool:
    """Whether the session has read `events` events and no stage runs."""
    with attack._lock:
        return len(attack.run.events) == events and not attack._in_stage


def kinds(attack: AttackChain) -> list[tuple[str, int, str | None]]:
    return [(e.kind, e.position, e.outcome) for e in attack.run.events]


def test_each_stage_is_released_after_its_wait(tmp_path: Path) -> None:
    attack, _ = chain("ok,ok,ok", [0.2, 0.3, 0.1], tmp_path)
    attack.start()
    drive(attack, lambda: finished(attack))
    run = attack.stop()
    assert kinds(attack) == [
        ("begin", 1, None),
        ("end", 1, "completed"),
        ("begin", 2, None),
        ("end", 2, "completed"),
        ("begin", 3, None),
        ("end", 3, "completed"),
    ]
    ends = [e.at for e in run.events if e.kind == "end"]
    # The first wait counts from the start, and each later one from the end
    # event of the stage before.
    assert run.releases[0] - run.started >= 0.2
    assert run.releases[1] - ends[0] >= 0.3
    assert run.releases[2] - ends[1] >= 0.1
    assert run.stdout == [f"[+] the headline result of stage-{n}" for n in (1, 2, 3)]
    assert (run.exit_status, run.stop, run.problems) == (0, None, [])
    # The binary exited after its last end event, before the stop.
    assert ends[-1] <= run.output_ended <= run.ended
    assert run.pid is not None
    assert early_stop(run, ["stage-1", "stage-2", "stage-3"]) is None
    assert (attack.total, attack.released, attack.next_stage()) == (3, 3, None)


def test_each_stage_falls_due_once_its_wait_starts(tmp_path: Path) -> None:
    """The first stage falls due its wait after the session's start, and each
    later stage its wait after the end event of the stage before. Only the
    next stage can be released, and only once."""
    attack, _ = chain("ok,ok", [0.1, 0.2], tmp_path)
    assert attack.next_stage() is None
    attack.start()
    wait_for(lambda: attack.next_stage() is not None)
    assert attack.next_stage() == (1, attack.run.started + 0.1)
    assert not attack.release(2)
    assert attack.release(1)
    assert not attack.release(1)
    wait_for(lambda: attack.next_stage() is not None)
    (end,) = [e for e in attack.run.events if e.kind == "end"]
    assert attack.next_stage() == (2, end.at + 0.2)
    drive(attack, lambda: finished(attack))
    assert attack.stop().exit_status == 0


def test_a_stage_left_unreleased_never_begins(tmp_path: Path) -> None:
    """A stage that falls due and is never released, as after the Track A
    alarm or at the start of the last stage in Track A, never begins: the
    stand-in receives no further `begin`, and exits with status 3 once its
    standard input closes (episodes-tests.md, no_stage_is_released_after_the_alarm)."""
    attack, incus = chain("ok,ok", [0.05, 0.05], tmp_path)
    attack.start()
    drive(
        attack,
        lambda: attack.next_stage() is not None and attack.released == 1,
        up_to=1,
    )
    due = attack.next_stage()
    assert due is not None and due[0] == 2
    wait_for(lambda: time.monotonic() > due[1] + 0.2)
    run = attack.stop()
    assert (run.stop, incus.calls, run.exit_status) == (
        "standard input closed",
        [],
        3,
    )
    assert len(run.releases) == 1
    assert kinds(attack) == [("begin", 1, None), ("end", 1, "completed")]
    assert not attack.release(2)
    assert early_stop(run, ["stage-1", "stage-2"]) == {
        "stage_number": 2,
        "stage": "stage-2",
        "cause": "the episode ended before the stage began",
    }


def test_a_failed_stage_ends_the_releases(tmp_path: Path) -> None:
    attack, _ = chain("ok,fail,ok", [0.05, 0.05, 0.05], tmp_path)
    attack.start()
    drive(attack, lambda: finished(attack))
    run = attack.stop()
    assert kinds(attack)[-1] == ("end", 2, "failed")
    assert run.events[-1].reason == "no reply"
    assert len(run.releases) == 2
    assert run.exit_status == 1
    assert attack.next_stage() is None
    assert early_stop(run, ["stage-1", "stage-2", "stage-3"]) == {
        "stage_number": 2,
        "stage": "stage-2",
        "cause": "the stage failed: no reply",
    }


def test_a_stage_whose_requires_fail_never_begins(tmp_path: Path) -> None:
    attack, _ = chain("ok,missing", [0.05, 0.05], tmp_path)
    attack.start()
    drive(attack, lambda: finished(attack), up_to=1)
    run = attack.stop()
    assert kinds(attack) == [("begin", 1, None), ("end", 1, "completed")]
    assert run.exit_status == 1
    assert "a required fact is missing" in (tmp_path / "attack-chain.log").read_text()
    assert early_stop(run, ["stage-1", "stage-2"]) == {
        "stage_number": 2,
        "stage": "stage-2",
        "cause": "the binary exited with status 1 before the stage began",
    }


def test_a_stop_between_stages_closes_standard_input(tmp_path: Path) -> None:
    attack, incus = chain("ok,ok", [0.05, 60.0], tmp_path)
    attack.start()
    drive(attack, lambda: between_stages(attack, 2))
    run = attack.stop()
    assert run.stop == "standard input closed"
    assert incus.calls == []
    assert run.exit_status == 3
    assert len(run.releases) == 1
    assert early_stop(run, ["stage-1", "stage-2"]) == {
        "stage_number": 2,
        "stage": "stage-2",
        "cause": "the episode ended before the stage began",
    }


def test_a_stop_during_a_holding_stage_sends_sigterm(tmp_path: Path) -> None:
    attack, incus = chain("ok,hold", [0.05, 0.05], tmp_path)
    attack.start()
    drive(attack, lambda: len(attack.run.events) == 3)
    run = attack.stop()
    assert run.stop == "SIGTERM, then standard input closed"
    assert incus.calls == [("adversary", "kill", "-TERM", str(run.pid))]
    # The holding stage restores and completes, so the chain completes.
    assert kinds(attack)[-1] == ("end", 2, "completed")
    assert run.exit_status == 0
    assert early_stop(run, ["stage-1", "stage-2"]) is None


@pytest.mark.parametrize(
    ("plan", "waits", "until", "status", "stop"),
    [
        pytest.param("ok,ok", [0.05, 0.05], "exited", 0, None, id="every stage"),
        pytest.param(
            "ok,hold",
            [0.05, 0.05],
            "holding",
            0,
            "SIGTERM, then standard input closed",
            id="a holding last stage restored at SIGTERM",
        ),
        pytest.param("ok,fail", [0.05, 0.05], "exited", 1, None, id="a failed stage"),
        pytest.param("ok,missing", [0.05, 0.05], "exited", 1, None, id="missing facts"),
        pytest.param(
            "ok,unknown",
            [0.05, 0.05],
            "exited",
            2,
            None,
            id="a command line that it cannot use",
        ),
        pytest.param(
            "ok,ok",
            [0.05, 60.0],
            "between",
            3,
            "standard input closed",
            id="a stop before a stage",
        ),
    ],
)
def test_the_binary_exit_status_follows_its_stop(
    tmp_path: Path,
    plan: str,
    waits: list[float],
    until: str,
    status: int,
    stop: str | None,
) -> None:
    """The stand-in binary's exit status in the episode record follows its
    stop: 0 when every stage completed, a holding last stage restored at
    SIGTERM among them; 1 at a failed stage or missing facts; 2 on a command
    line that it cannot use; and 3 when the episode runner stops it before a
    stage. The only signal that the episode runner sends is SIGTERM
    (episodes-tests.md, the_binary_exit_status_follows_its_stop;
    episode-runner-impl.md §9, the attack chain's session)."""
    attack, incus = chain(plan, waits, tmp_path)
    attack.start()
    if until == "holding":
        drive(attack, lambda: len(attack.run.events) == 3)
    elif until == "between":
        drive(attack, lambda: between_stages(attack, 2))
    else:
        drive(attack, lambda: finished(attack), up_to=1 if "missing" in plan else None)
    run = attack.stop()
    record = run.as_json(lambda t: t)
    assert (record["exit_status"], record["stop"]) == (status, stop)
    assert all(call[1:3] == ("kill", "-TERM") for call in incus.calls)
    assert len(incus.calls) == (1 if until == "holding" else 0)
    if status == 2:
        assert run.pid is None and attack.failure is not None


def test_a_session_that_never_starts_is_recorded(tmp_path: Path) -> None:
    attack = AttackChain(
        LocalIncus(),
        MonotonicClock(),
        [str(tmp_path / "absent")],
        "adversary",
        [0.05],
        tmp_path / "attack-chain.log",
    )
    attack.start()
    run = attack.stop()
    assert run.started is None
    assert run.problems and "failed to start" in run.problems[0]
    assert attack.failure == run.problems[0]
    assert early_stop(run, ["stage-1"]) == {
        "stage_number": 1,
        "stage": "stage-1",
        "cause": "the episode ended before the onset step",
    }


def test_a_first_line_other_than_a_process_identifier_is_a_problem(
    tmp_path: Path,
) -> None:
    attack = AttackChain(
        LocalIncus(),
        MonotonicClock(),
        [sys.executable, "-c", "print('Error: Instance not found')"],
        "adversary",
        [0.05],
        tmp_path / "attack-chain.log",
    )
    attack.start()
    wait_for(lambda: finished(attack))
    run = attack.stop()
    assert run.releases == []
    assert run.problems == [
        "the attack chain's session gave no process identifier: its first line "
        "was 'Error: Instance not found'"
    ]
    assert attack.failure == run.problems[0]


def test_a_beginning_event_whose_stage_count_differs_is_a_problem(
    tmp_path: Path,
) -> None:
    """K comes from the catalogue, one wait for each stage, and a beginning
    event's total_num_stages must agree with it: the session records the
    first difference as a problem, once, and the releases go on (episodes.md
    §10.4, the attack chain's session)."""
    attack, _ = chain("ok,ok", [0.05, 0.05, 0.05], tmp_path)
    attack.start()
    drive(attack, lambda: finished(attack))
    run = attack.stop()
    assert [e.total for e in run.events if e.kind == "begin"] == [2, 2]
    assert run.problems == [
        "stage 1's beginning event gives total_num_stages 2, and the catalogue "
        "lists 3 stages"
    ]
    assert run.exit_status == 0
    agreeing, _ = chain("ok,ok", [0.05, 0.05], tmp_path / "agreeing")
    (tmp_path / "agreeing").mkdir()
    agreeing.start()
    drive(agreeing, lambda: finished(agreeing))
    assert agreeing.stop().problems == []


def test_only_lines_with_the_stage_event_key_are_stage_events() -> None:
    assert parse_event('{"rows": 3}', 1.0) is None
    assert parse_event("[+] exfiltrated 3 rows", 1.0) is None
    event = parse_event(
        '{"stage_event": "end", "stage_number": 2, "stage": "s", "outcome": "failed",'
        ' "reason": "r"}',
        4.5,
    )
    assert event is not None
    assert (event.kind, event.position, event.stage, event.at) == ("end", 2, "s", 4.5)
    assert (event.outcome, event.reason, event.total) == ("failed", "r", None)
    begin = parse_event(
        '{"stage_event": "begin", "stage_number": 1, "total_num_stages": 4,'
        ' "stage": "s"}',
        1.0,
    )
    assert begin is not None and begin.total == 4
