"""The idle defender (episodes.md §7, step protocol; episodes-tests.md §5,
step protocol).

The idle defender answers each decision request of the step session with a
decision message that holds the idle defender's decision for the request's
track. The idle defender sends nothing after the end notice, and exits then or
when its standard input closes.

Run with: pytest competition/tests -q
"""

from __future__ import annotations

import json
import os
import select
import subprocess
import sys
from pathlib import Path

import pytest

IDLE_DEFENDER = Path(__file__).resolve().parents[1] / "idle_defender" / "idle-defender"


def _step_message(track: str, step: int, ended: bool = False) -> str:
    previous_reward = None if step == 0 else 0
    message = {
        "track": track,
        "step": step,
        "previous_reward": previous_reward,
        "ended": ended,
    }
    return json.dumps(message) + "\n"


def _start() -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, str(IDLE_DEFENDER)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        bufsize=1,
    )


def _decision_message_within(proc: subprocess.Popen[str], timeout_s: float) -> str:
    ready, _, _ = select.select([proc.stdout], [], [], timeout_s)
    assert ready, f"the idle defender sent no decision message within {timeout_s} s"
    return proc.stdout.readline()


@pytest.mark.parametrize(
    ("track", "decision"), [("A", "no action"), ("B", "no action")]
)
def test_the_idle_defender_answers_each_decision_request(
    track: str, decision: str
) -> None:
    proc = _start()
    try:
        for step in range(3):
            proc.stdin.write(_step_message(track, step))
            proc.stdin.flush()
            assert json.loads(_decision_message_within(proc, 1.0)) == {
                "decision": decision
            }
        proc.stdin.write(_step_message(track, 3, ended=True))
        proc.stdin.flush()
        assert proc.wait(timeout=1.0) == 0
        assert proc.stdout.read() == ""
    finally:
        proc.kill()


def test_the_idle_defender_exits_when_its_input_closes() -> None:
    proc = _start()
    try:
        proc.stdin.write(_step_message("A", 0))
        proc.stdin.flush()
        assert json.loads(_decision_message_within(proc, 1.0)) == {
            "decision": "no action"
        }
        proc.stdin.close()
        assert proc.wait(timeout=1.0) == 0
    finally:
        proc.kill()


def test_the_idle_defender_runs_as_a_program() -> None:
    """The competition image installs the file as /usr/local/bin/idle-defender,
    which the launcher runs when the entry's volume holds no defender
    (episodes.md §7, step protocol)."""
    assert IDLE_DEFENDER.read_text().startswith("#!/usr/bin/env python3\n")
    assert os.access(IDLE_DEFENDER, os.X_OK)
