"""Calls to Incus (episodes.md §6.6, settle period; §7, step protocol).

Run with: pytest competition/tests -q
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from competition.episode_runner.incus import Incus, IncusError


class FakeRun:
    """Stands in for subprocess.run, and records each call."""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, argv: list[str], **kwargs) -> subprocess.CompletedProcess:
        self.calls.append((argv, kwargs))
        return subprocess.CompletedProcess(
            argv, self.returncode, self.stdout, self.stderr
        )


def test_every_call_closes_its_standard_input() -> None:
    run = FakeRun(stdout="[]")
    incus = Incus(run)
    incus.exec("siem-01", "/var/ossec/bin/agent_control", "-l")
    incus.pull("sim-host", "/var/log/sim.log", Path("records/sim.log"))
    incus.instances()
    assert [argv for argv, _ in run.calls] == [
        ["incus", "exec", "siem-01", "--", "/var/ossec/bin/agent_control", "-l"],
        ["incus", "file", "pull", "sim-host/var/log/sim.log", "records/sim.log"],
        ["incus", "list", "--format", "json"],
    ]
    assert all(kwargs["stdin"] is subprocess.DEVNULL for _, kwargs in run.calls)


def test_a_call_reports_its_status_and_output() -> None:
    result = Incus(FakeRun(1, "", "Error: Instance not found")).exec("x", "true")
    assert not result.ok
    assert result.stderr == "Error: Instance not found"


def test_a_call_that_times_out_fails() -> None:
    def run(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    result = Incus(run).exec("siem-01", "true", timeout_s=2.0)
    assert not result.ok
    assert "timed out after 2.0 s" in result.stderr


def test_a_failed_listing_raises() -> None:
    with pytest.raises(IncusError, match="not running"):
        Incus(FakeRun(1, "", "Error: incus daemon is not running")).instances()


def test_the_step_session_runs_the_defenders_program_through_incus_exec() -> None:
    """The launcher, with the program that the episode runner's test found."""
    assert Incus().step_session(
        "siem-01", "/usr/local/bin/defender", "/var/lib/entry/defender"
    ) == [
        "incus",
        "exec",
        "siem-01",
        "--",
        "/usr/local/bin/defender",
        "/var/lib/entry/defender",
    ]


def test_a_query_reads_an_object_of_the_incus_api() -> None:
    run = FakeRun(stdout='{"status": "Running", "pid": 4242}')
    assert Incus(run).query("/1.0/instances/sim-host/state")["pid"] == 4242
    ((argv, kwargs),) = run.calls
    assert argv == ["incus", "query", "/1.0/instances/sim-host/state"]
    assert kwargs["stdin"] is subprocess.DEVNULL
    with pytest.raises(IncusError, match="not found"):
        Incus(FakeRun(1, "", "Error: Instance not found")).query("/1.0/x")
