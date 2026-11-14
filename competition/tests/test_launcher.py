"""The launcher, /usr/local/bin/defender on siem-01 (episodes.md §7, step
protocol; episode-runner-impl.md §1, competition images; episodes-tests.md,
a_volume_without_a_defender_runs_the_idle_defender,
a_volumes_defender_runs_in_place_of_the_idle_defender,
a_defender_that_cannot_run_discards_the_episode and
the_launcher_runs_only_the_tested_program).

Each test runs a copy of the launcher whose paths point into a temporary
directory: the entry's storage volume, mounted at /var/lib/entry, the idle
defender's program, and the program's working directory, /root on siem-01.
The copy runs as the test's own user, who owns the volume's files, so a
permission that the test withholds stands for the permission that root in the
unprivileged siem-01 lacks on a file it does not own.

Run with: pytest competition/tests -q
"""

from __future__ import annotations

import json
import os
import select
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from competition.episode_runner import episode

COMPETITION = Path(__file__).resolve().parents[1]
LAUNCHER = COMPETITION / "launcher" / "defender"
IDLE_DEFENDER = COMPETITION / "idle_defender" / "idle-defender"
VOLUME = "/var/lib/entry"
INSTALLED_IDLE_DEFENDER = "/usr/local/bin/idle-defender"
# The launcher's status when the entry's defender is present but cannot run,
# and the working directory that it sets for the program.
FAULT = 126
HOME = "/root"


def under_this_python(*argv: str) -> str:
    """A program that runs `argv` under the interpreter that runs the tests,
    as one process. The kernel ends the interpreter's path on a `#!` line at
    the first space, and a repository's path can hold one, so the program is
    a /bin/sh script that names the interpreter in quotes and replaces itself
    with it."""
    words = " ".join(shlex.quote(word) for word in (sys.executable, *argv))
    return f'#!/bin/sh\nexec {words} "$@"\n'


# A defender that writes the working directory in which it starts.
WHERE = under_this_python(
    "-c",
    """import json, os
print(json.dumps({"cwd": os.getcwd()}), flush=True)
""",
)
# A defender that writes its process identifier, then raises the alarm at
# each decision request until the end notice.
ALARMING = under_this_python(
    "-c",
    """import json, os, sys
print(json.dumps({"pid": os.getpid()}), flush=True)
for line in sys.stdin:
    if json.loads(line)["ended"]:
        break
    print(json.dumps({"decision": "alarm"}), flush=True)
""",
)
# Root reads and searches whatever its permissions say, so the cases of a
# permission withheld hold only for another user.
AS_ROOT = os.geteuid() == 0


@pytest.fixture
def launcher(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A copy of the launcher, the volume directory it reads, and the idle
    defender's program it runs. The copy's working directory for the program
    is a directory of its own, `root`, beside them."""
    volume = tmp_path / "entry"
    volume.mkdir()
    (tmp_path / "root").mkdir()
    idle = tmp_path / "idle-defender"
    idle.write_text(under_this_python(str(IDLE_DEFENDER)))
    idle.chmod(0o755)
    text = LAUNCHER.read_text()
    assert f"volume={VOLUME}\n" in text and INSTALLED_IDLE_DEFENDER in text
    assert f"cd {HOME} " in text
    copy = tmp_path / "defender"
    copy.write_text(
        text.replace(f"volume={VOLUME}\n", f"volume={volume}\n")
        .replace(INSTALLED_IDLE_DEFENDER, str(idle))
        .replace(f"cd {HOME} ", f"cd {tmp_path / 'root'} ")
    )
    copy.chmod(0o755)
    yield copy, volume, idle
    # A volume that the test closed opens again, so that pytest can remove it.
    volume.chmod(0o755)


def request(step: int, ended: bool = False) -> str:
    previous = None if step == 0 else 0
    return (
        json.dumps(
            {"track": "A", "step": step, "previous_reward": previous, "ended": ended}
        )
        + "\n"
    )


def start(program: Path, tested: str | Path) -> subprocess.Popen[str]:
    """The launcher as the step session runs it, with the program that the
    episode runner's test found."""
    return subprocess.Popen(
        [str(program), str(tested)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )


def line_within(proc: subprocess.Popen[str], timeout_s: float = 5.0) -> str:
    ready, _, _ = select.select([proc.stdout], [], [], timeout_s)
    assert ready, f"no line within {timeout_s} s"
    return proc.stdout.readline()


def decisions(proc: subprocess.Popen[str], steps: int) -> list[str]:
    got = []
    for step in range(steps):
        proc.stdin.write(request(step))
        proc.stdin.flush()
        got.append(json.loads(line_within(proc))["decision"])
    proc.stdin.write(request(steps, ended=True))
    proc.stdin.flush()
    assert proc.wait(timeout=5.0) == 0
    return got


# Each kind of volume, by what it holds as `defender`, and the cause that a
# defender that cannot run gives.
FAULTS = {
    "not executable": "is not executable",
    "not readable": "is not readable",
    "a directory": "is a directory",
    "a symbolic link to nothing": "is a symbolic link to nothing",
    "a named pipe": "is not a regular file",
}
WITHHELD = ("not readable", "an unsearchable volume")


def make(volume: Path, kind: str) -> None:
    """Put a defender of one kind in the volume, or close the volume."""
    entry = volume / "defender"
    if kind in ("executable", "not executable", "not readable"):
        entry.write_text(ALARMING)
        entry.chmod(
            {"executable": 0o755, "not executable": 0o644, "not readable": 0o311}[kind]
        )
    elif kind == "a directory":
        entry.mkdir(mode=0o755)
    elif kind == "a symbolic link to nothing":
        entry.symlink_to(volume / "absent")
    elif kind == "a named pipe":
        os.mkfifo(entry)
    elif kind == "an unsearchable volume":
        entry.write_text(ALARMING)
        entry.chmod(0o755)
        volume.chmod(0o600)


def test_a_volume_without_a_defender_runs_the_idle_defender(launcher) -> None:
    program, _, idle = launcher
    proc = start(program, idle)
    try:
        assert decisions(proc, 3) == ["no action"] * 3
        assert proc.stderr.read() == ""
    finally:
        proc.kill()


def test_a_volumes_defender_runs_in_place_of_the_idle_defender(launcher) -> None:
    """The entry's program replaces the launcher in the same process, and
    holds the step session's standard input and output."""
    program, volume, _ = launcher
    make(volume, "executable")
    proc = start(program, volume / "defender")
    try:
        assert json.loads(line_within(proc)) == {"pid": proc.pid}
        assert decisions(proc, 2) == ["alarm"] * 2
        assert proc.stderr.read() == ""
    finally:
        proc.kill()


@pytest.mark.parametrize(
    ("kind", "cause"), [*FAULTS.items(), ("an unsearchable volume", None)]
)
def test_a_defender_that_cannot_run_is_a_fault(
    launcher, kind: str, cause: str | None
) -> None:
    """A volume that holds a `defender` that is not a regular file that
    siem-01 can read and execute, or a volume that siem-01 cannot read and
    search, runs no defender: the launcher writes the cause on standard error
    in one line, reads no request, and exits with status 126. A volume that
    cannot be searched is not taken for one that holds no defender."""
    if AS_ROOT and kind in WITHHELD:
        pytest.skip("root reads and searches whatever its permissions say")
    program, volume, _ = launcher
    make(volume, kind)
    entry = volume / "defender"
    proc = start(program, entry)
    try:
        assert proc.wait(timeout=5.0) == FAULT
        assert proc.stdout.read() == ""
        expected = (
            f"{volume} cannot be read and searched"
            if cause is None
            else f"{entry} {cause}"
        )
        assert proc.stderr.read().splitlines() == [f"launcher: {expected}"]
    finally:
        proc.kill()


@pytest.mark.parametrize(
    "kind", ["none", "executable", *FAULTS, "an unsearchable volume"]
)
def test_the_episode_runners_test_agrees_with_the_launcher(launcher, kind: str) -> None:
    """The episode runner's own test of the entry's defender, made before
    the step session opens, gives the program that the launcher runs, or the
    launcher's status of 126 with the same cause (episode-runner-impl.md §1,
    competition images)."""
    if AS_ROOT and kind in WITHHELD:
        pytest.skip("root reads and searches whatever its permissions say")
    program, volume, idle = launcher
    make(volume, kind)
    test = subprocess.run(
        ["sh", "-c", episode.DEFENDER_TEST, "sh", str(volume / "defender"), str(idle)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )
    tested = test.stdout.strip() if test.returncode == 0 else volume / "defender"
    proc = start(program, tested)
    try:
        if kind == "none":
            assert (test.returncode, test.stdout) == (0, f"{idle}\n")
            assert decisions(proc, 1) == ["no action"]
        elif kind == "executable":
            assert (test.returncode, test.stdout) == (0, f"{volume / 'defender'}\n")
            assert json.loads(line_within(proc)) == {"pid": proc.pid}
            assert decisions(proc, 1) == ["alarm"]
        else:
            assert test.returncode == FAULT == proc.wait(timeout=5.0)
            assert proc.stderr.read() == f"launcher: {test.stderr}"
    finally:
        proc.kill()


@pytest.mark.parametrize("kind", ["none", "executable"])
def test_the_launched_program_starts_in_root(
    launcher, tmp_path: Path, kind: str
) -> None:
    """The launcher sets the working directory, /root on siem-01, before it
    replaces itself, whatever directory the step session opens in. Here the
    idle defender's place holds a program that writes its working directory,
    as the entry's defender does."""
    program, volume, idle = launcher
    target = volume / "defender" if kind == "executable" else idle
    target.write_text(WHERE)
    target.chmod(0o755)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    out = subprocess.run(
        [str(program), str(target)],
        cwd=elsewhere,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=True,
        timeout=5.0,
    )
    assert json.loads(out.stdout) == {"cwd": str((tmp_path / "root").resolve())}


@pytest.mark.parametrize("found", ["none", "executable"])
def test_the_launcher_runs_only_the_tested_program(launcher, found: str) -> None:
    """The launcher runs the program that the episode runner tested when its
    own check finds the same one. When its check finds another, as when the
    volume changed between the two, the launcher runs no program and exits
    with status 126 (episodes-tests.md, the_launcher_runs_only_the_tested_program)."""
    program, volume, idle = launcher
    make(volume, found)
    entry = volume / "defender"
    tested, other = (idle, entry) if found == "none" else (entry, idle)
    proc = start(program, tested)
    try:
        expected = ["no action"] if found == "none" else ["alarm"]
        if found == "executable":
            assert json.loads(line_within(proc)) == {"pid": proc.pid}
        assert decisions(proc, 1) == expected
    finally:
        proc.kill()
    proc = start(program, other)
    try:
        assert proc.wait(timeout=5.0) == FAULT
        assert proc.stdout.read() == ""
        assert proc.stderr.read() == (
            f"launcher: the episode runner tested {other}, "
            f"and the launcher found {tested}\n"
        )
    finally:
        proc.kill()


def test_the_launcher_needs_the_tested_program(launcher) -> None:
    program, _, _ = launcher
    out = subprocess.run(
        [str(program)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
        timeout=5.0,
    )
    assert (out.returncode, out.stdout) == (2, "")
    assert out.stderr.startswith("launcher: no program named")


def test_the_launcher_is_a_shell_script_that_chooses_once() -> None:
    """The competition image installs the file as /usr/local/bin/defender, and
    the launcher takes the tested program, sets /root as the working
    directory, then replaces itself through exec with the program its own
    check finds, or exits with status 126."""
    text = LAUNCHER.read_text()
    assert text.startswith("#!/bin/sh\n")
    assert os.access(LAUNCHER, os.X_OK)
    assert subprocess.run(["sh", "-n", str(LAUNCHER)], check=False).returncode == 0
    body = [line.strip() for line in text.splitlines() if not line.startswith("#")]
    assert [line for line in body if line.startswith(("cd ", "exec ", "exit "))] == [
        f"exit {FAULT}",
        "exit 2",
        f"cd {HOME} || exit 1",
        'exec "$program" "$@"',
    ]
