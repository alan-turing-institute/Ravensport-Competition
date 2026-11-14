"""The step protocol and the query interface as the example Track A defenders
use them (competition/baselines/track_a/common/protocol.py; episodes.md §7,
step protocol; §8, query interface; episode-runner-impl.md §6, step messages;
§12, query interface).

The tests drive StepSession through a pipe, run a small defender built on
protocol.run as a process, and serve reads from a stand-in for siem-read that
the environment variable SIEM_READ names.

Run with: pytest competition/tests -q
"""

from __future__ import annotations

import io
import json
import os
import select
import subprocess
import sys
from pathlib import Path

import pytest

COMMON = Path(__file__).resolve().parents[1] / "baselines" / "track_a" / "common"
if str(COMMON) not in sys.path:
    sys.path.insert(0, str(COMMON))

import protocol  # noqa: E402


def request(step: int, ended: bool = False, reward: float | None = None) -> bytes:
    """A step message of Track A. In evaluation mode every step message holds
    a reward of null, the default; in training mode a decision request after
    step 0 holds the previous step's reward, and the end notice R_n."""
    return (
        json.dumps(
            {"track": "A", "step": step, "previous_reward": reward, "ended": ended}
        )
        + "\n"
    ).encode()


@pytest.fixture
def pipe():
    read_end, write_end = os.pipe()
    yield read_end, write_end
    for fd in (read_end, write_end):
        try:
            os.close(fd)
        except OSError:
            pass


def test_a_decision_request_is_parsed(pipe) -> None:
    read_end, write_end = pipe
    session = protocol.StepSession(read_end, io.BytesIO())
    os.write(write_end, request(0))
    assert session.next() == protocol.Request("A", 0, None, False)


@pytest.mark.parametrize(
    ("reward", "end_reward"),
    [(None, None), (0.0, -0.5)],
    ids=["evaluation", "training"],
)
def test_the_step_messages_of_either_mode_are_parsed(
    pipe, reward: float | None, end_reward: float | None
) -> None:
    """Evaluation mode's step messages hold no reward; training mode's hold
    the previous step's reward and, in the end notice, R_n."""
    read_end, write_end = pipe
    session = protocol.StepSession(read_end, io.BytesIO())
    os.write(write_end, request(5, reward=reward))
    assert session.next() == protocol.Request("A", 5, reward, False)
    os.write(write_end, request(6, ended=True, reward=end_reward))
    assert session.next() == protocol.Request("A", 6, end_reward, True)


def test_the_newest_waiting_request_is_answered_and_older_ones_are_skipped(
    pipe,
) -> None:
    read_end, write_end = pipe
    session = protocol.StepSession(read_end, io.BytesIO())
    os.write(write_end, request(4) + request(5) + request(6))
    assert session.next().step == 6
    assert session.skipped == 2


def test_the_end_notice_is_returned_before_waiting_requests(pipe) -> None:
    read_end, write_end = pipe
    session = protocol.StepSession(read_end, io.BytesIO())
    os.write(write_end, request(7) + request(8, ended=True, reward=-0.5))
    notice = session.next()
    assert notice.ended and notice.step == 8 and notice.previous_reward == -0.5


def test_a_closed_input_ends_the_session(pipe) -> None:
    read_end, write_end = pipe
    session = protocol.StepSession(read_end, io.BytesIO())
    os.close(write_end)
    assert session.next() is None


def test_a_line_that_is_no_step_message_is_ignored(pipe, capsys) -> None:
    read_end, write_end = pipe
    session = protocol.StepSession(read_end, io.BytesIO())
    os.write(write_end, b"not json\n" + request(1))
    assert session.next().step == 1
    assert "not a step message" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("track", "alarm", "decision"),
    [
        ("A", True, "alarm"),
        ("A", False, "no action"),
        ("B", True, "no action"),
        ("B", False, "no action"),
    ],
)
def test_each_decision_message_holds_one_decision(
    track: str, alarm: bool, decision: str
) -> None:
    """Track A's decision without an alarm is no action, as in Track B."""
    out = io.BytesIO()
    session = protocol.StepSession(0, out)
    session.decide(protocol.Request(track, 3, None, False), alarm)
    assert json.loads(out.getvalue()) == {"decision": decision}


DEFENDER = f"""#!{sys.executable}
import sys
sys.path.insert(0, {str(COMMON)!r})
import protocol

def decide(request):
    return request.step == 2

def end(notice):
    print("end", notice.step, notice.previous_reward, file=sys.stderr, flush=True)

sys.exit(protocol.run(decide, end))
"""


def _answer(proc: subprocess.Popen, line: bytes) -> dict:
    proc.stdin.write(line)
    proc.stdin.flush()
    ready, _, _ = select.select([proc.stdout], [], [], 10)
    assert ready, "the defender sent no decision message within 10 s"
    return json.loads(proc.stdout.readline())


@pytest.mark.parametrize(
    ("reward", "end_reward"), [(None, None), (0.0, 0.0)], ids=["evaluation", "training"]
)
def test_run_answers_each_request_and_calls_end_at_the_end_notice(
    tmp_path: Path, reward: float | None, end_reward: float | None
) -> None:
    program = tmp_path / "defender"
    program.write_text(DEFENDER)
    proc = subprocess.Popen(
        [sys.executable, str(program)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        decisions = [
            _answer(proc, request(k, reward=None if k == 0 else reward))["decision"]
            for k in range(3)
        ]
        assert decisions == ["no action", "no action", "alarm"]
        proc.stdin.write(request(3, ended=True, reward=end_reward))
        proc.stdin.close()
        assert proc.wait(timeout=10) == 0
        assert proc.stdout.read() == b""
        assert f"end 3 {end_reward}" in proc.stderr.read().decode()
    finally:
        proc.kill()


SIEM_READ = """import json, os, sys
with open(os.environ["SIEM_READ_LOG"], "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\\n")
if os.environ.get("SIEM_READ_FAIL"):
    print("siem-read: no offsets are recorded", file=sys.stderr)
    sys.exit(2)
print(json.dumps({"rule": {"level": 3}, "location": "journald"}))
print("not json")
print(json.dumps({"rule": {"level": 9}, "location": "wazuh-agent"}))
"""


@pytest.fixture
def siem_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A stand-in for siem-read: a shell script that runs the Python above,
    since an interpreter's path with a space cannot follow #! in a script."""
    script = tmp_path / "siem_read.py"
    script.write_text(SIEM_READ)
    program = tmp_path / "siem-read"
    program.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
    program.chmod(0o755)
    log = tmp_path / "reads.log"
    monkeypatch.setenv("SIEM_READ", str(program))
    monkeypatch.setenv("SIEM_READ_LOG", str(log))
    return log


def test_a_read_runs_siem_read_with_the_request_and_parses_each_record(
    siem_read: Path,
) -> None:
    records = protocol.read("archives", host="fw-ext-dmz", location="journald")
    assert [r["rule"]["level"] for r in records] == [3, 9]
    assert json.loads(siem_read.read_text()) == [
        "archives",
        "--host",
        "fw-ext-dmz",
        "--input",
        "journald",
    ]


def test_a_read_with_a_start_gives_it_to_siem_read(siem_read: Path) -> None:
    protocol.read("alerts", since="2026-11-12T20:15:05.250000Z")
    assert json.loads(siem_read.read_text()) == [
        "alerts",
        "--since",
        "2026-11-12T20:15:05.250000Z",
    ]


def test_a_failed_read_returns_no_records(
    siem_read: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setenv("SIEM_READ_FAIL", "1")
    assert protocol.read("alerts") == []
    assert "status 2" in capsys.readouterr().err


def test_an_undeclared_read_raises_and_runs_nothing(siem_read: Path) -> None:
    telemetry = protocol.Telemetry.from_json({"reads": [{"output": "alerts"}]})
    with pytest.raises(protocol.Undeclared):
        protocol.read("archives", telemetry=telemetry)
    assert not siem_read.exists()


def test_a_declared_read_covers_every_host_and_input_it_leaves_open() -> None:
    telemetry = protocol.Telemetry.from_json(
        {
            "reads": [
                {"output": "alerts"},
                {"output": "archives", "input": "/var/log/suricata/eve.json"},
            ]
        }
    )
    assert telemetry.allows("alerts", None, None)
    assert telemetry.allows("alerts", "fms-01", "journald")
    assert telemetry.allows("archives", None, "/var/log/suricata/eve.json")
    assert telemetry.allows("archives", "fw-ext-dmz", "/var/log/suricata/eve.json")
    assert not telemetry.allows("archives", None, None)
    assert not telemetry.allows("archives", "fms-01", "journald")


def test_a_telemetry_specification_names_only_the_two_output_files() -> None:
    with pytest.raises(ValueError):
        protocol.Telemetry.from_json({"reads": [{"output": "alerts.json"}]})
