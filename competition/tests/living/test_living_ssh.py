"""Commands, files and clock readings over the Secure Shell (SSH), with
stand-ins for subprocess.

Run with: pytest competition/tests -q
"""

from __future__ import annotations

import io
import subprocess
from pathlib import Path

import pytest

from competition.living.ssh import Remote, Ssh, SSHError

SSH = Ssh(port=41001, user="aicd", key=Path("/keys/user"))


class Calls:
    """Stands in for subprocess.run, with scripted exit statuses."""

    def __init__(self, *returncodes: int, stdout: str = "", stderr: str = "") -> None:
        self.returncodes = list(returncodes) or [0]
        self.stdout, self.stderr = stdout, stderr
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, argv, **kwargs) -> subprocess.CompletedProcess:
        self.calls.append((list(argv), kwargs))
        code = (
            self.returncodes.pop(0)
            if len(self.returncodes) > 1
            else self.returncodes[0]
        )
        return subprocess.CompletedProcess(argv, code, self.stdout, self.stderr)


class Written(io.StringIO):
    """A standard input that keeps what was written after it is closed."""

    def close(self) -> None:
        self.kept = self.getvalue()
        super().close()


class Session:
    """Stands in for one process that subprocess.Popen starts."""

    def __init__(self, argv, kwargs, lines=()) -> None:
        self.argv, self.kwargs = list(argv), kwargs
        self.stdin = Written()
        self.stdout = io.StringIO("".join(lines))
        self.killed = False

    def wait(self, timeout=None) -> int:
        return 0

    def kill(self) -> None:
        self.killed = True


def test_every_session_closes_its_input_and_reaches_only_the_loopback() -> None:
    run = Calls()
    Remote(SSH, run=run).run("true")
    argv, kwargs = run.calls[0]
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert argv[:5] == ["ssh", "-i", "/keys/user", "-p", "41001"]
    assert "BatchMode=yes" in argv and "IdentitiesOnly=yes" in argv
    assert argv[-2:] == ["aicd@127.0.0.1", "true"]


def test_a_failed_command_raises_with_its_last_lines() -> None:
    run = Calls(2, stderr="make: *** [install] Error 1\n")
    with pytest.raises(SSHError, match=r"exit 2 from 'make install'.*Error 1"):
        Remote(SSH, run=run).run("make install")
    assert Remote(SSH, run=Calls(3)).run("false", check=False).returncode == 3


def test_a_command_that_times_out_raises() -> None:
    def run(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    with pytest.raises(SSHError, match="timed out after 5"):
        Remote(SSH, run=run).run("sleep 60", timeout_s=5)


def test_a_stream_appends_the_output_to_its_log(tmp_path) -> None:
    run = Calls(0)
    status = Remote(SSH, run=run).stream("deploy", tmp_path / "logs" / "create.log")
    assert status == 0
    _, kwargs = run.calls[0]
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.STDOUT
    assert (tmp_path / "logs").is_dir()


def test_the_wait_retries_until_the_vm_answers() -> None:
    run = Calls(255, 255, 0)
    slept: list[float] = []
    waited = Remote(SSH, run=run, sleep=slept.append).wait(timeout_s=60)
    assert len(run.calls) == 3 and slept == [2.0, 2.0]
    assert waited >= 0


def test_the_wait_gives_up_after_its_timeout() -> None:
    ticks = iter(range(0, 1000, 50))
    remote = Remote(
        SSH, run=Calls(255), sleep=lambda s: None, monotonic=lambda: next(ticks)
    )
    with pytest.raises(SSHError, match="no answer on port 41001"):
        remote.wait(timeout_s=120)


def test_a_tree_is_sent_through_tar_into_the_vm(tmp_path) -> None:
    sessions: list[Session] = []

    def popen(argv, **kwargs):
        sessions.append(Session(argv, kwargs))
        return sessions[-1]

    run = Calls(0)
    Remote(SSH, run=run, popen=popen).send_tree(
        tmp_path, "/opt/ravensport", ("./.git", "*/target")
    )
    pack = sessions[0]
    assert pack.argv == [
        "tar",
        "-C",
        str(tmp_path),
        "-cf",
        "-",
        "--exclude",
        "./.git",
        "--exclude",
        "*/target",
        ".",
    ]
    assert pack.kwargs["env"]["COPYFILE_DISABLE"] == "1"
    argv, kwargs = run.calls[0]
    assert argv[-1] == "tar -C /opt/ravensport --no-same-owner -xf -"
    assert kwargs["stdin"] is pack.stdout


def test_an_episodes_outputs_are_fetched_under_their_own_name(tmp_path) -> None:
    sessions: list[Session] = []

    def popen(argv, **kwargs):
        sessions.append(Session(argv, kwargs))
        return sessions[-1]

    run = Calls(0)
    Remote(SSH, run=run, popen=popen).fetch_tree(
        "/var/lib/competition/episodes/fork-3", tmp_path / "episodes"
    )
    assert sessions[0].argv[-1] == "tar -C /var/lib/competition/episodes -cf - fork-3"
    assert sessions[0].kwargs["stdin"] is subprocess.DEVNULL
    assert run.calls[0][0] == ["tar", "-C", str(tmp_path / "episodes"), "-xf", "-"]


def test_the_clock_offset_comes_from_the_shortest_round_trip() -> None:
    sent = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0]
    trips = [0.5, 0.010, 0.004, 0.008, 0.006, 0.005]
    offsets = [-64.0, -65.003, -65.000, -65.002, -65.001, -65.0015]
    times = []
    lines = []
    for at, trip, offset in zip(sent, trips, offsets):
        times += [at, at + trip]
        lines.append(f"{at + trip / 2 + offset:.6f}\n")
    clock = iter(times)
    sessions: list[Session] = []

    def popen(argv, **kwargs):
        sessions.append(Session(argv, kwargs, lines))
        return sessions[-1]

    reading = Remote(SSH, popen=popen, wall=lambda: next(clock)).clock_offset(samples=5)
    assert reading.offset_s == pytest.approx(-65.0, abs=1e-6)
    assert reading.round_trip_s == pytest.approx(0.004)
    assert reading.host_s == pytest.approx(102.002)
    assert sessions[0].kwargs["stdin"] is subprocess.PIPE
    assert sessions[0].argv[-1] == "while read -r _; do date -u +%s.%N; done"
    assert sessions[0].stdin.kept == "\n" * 6
