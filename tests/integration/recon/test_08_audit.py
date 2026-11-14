"""
The first audit of a deployed run (`sim-tests.md`, reconciliation user,
"Integration coverage"): the proof that the actor conducts a whole audit against
the real workstation and log server.

Needs a deployment run with a short period — `audit.period: day` set in the
checkout's `deploy/world.yaml`, not committed — past its first audit at 06:00 UTC
on the day after the sim started. At `lambda_tug` 1.0 the run also needs
`lead_time` in `infra/sim/src/main.rs` shortened to 30 minutes, likewise not
committed, or the tank reaches its cavitation threshold about 20 hours in and
the sim stops. Every test skips while no summary entry exists, so the module can
be run before then without failing.

Run on the VM: python -m pytest tests/integration/recon/test_08_audit.py
"""

from __future__ import annotations

import re
import subprocess

import pytest

from tests.integration.recon.conftest import Node

SUMMARY_FILE = "reconciliation-audits.txt"

# A window from an instant to the following midnight, then the result.
AGREED_ENTRY = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z "
    r"\d{4}-\d{2}-\d{2}T00:00:00Z agreed$"
)

# The window's start to the second, then the attempt's number (`sim-impl.md`,
# "Terminal start").
LATCH_COMMAND = re.compile(r"^echo latch-(\d{14})-(\d+) \| xclip -selection clipboard$")

# The start of a summary entry's window, to the second.
ENTRY_START = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})")


@pytest.fixture(scope="module")
def home(recon: Node, world: dict) -> str:
    """The reconciliation user's home directory, as the directory gives it."""
    username = world["identities"]["reconciliation_user"]["username"]
    entry = recon.stdout("getent", "passwd", username).strip()
    return entry.split(":")[5]


@pytest.fixture(scope="module")
def entries(recon: Node, home: str) -> list[str]:
    """The summary entries, or a skip while the first audit has not run."""
    path = f"{home}/{SUMMARY_FILE}"
    if not recon.path_exists(path):
        pytest.skip(f"no audit has run yet: {path} does not exist")
    return [line for line in recon.read(path).splitlines() if line.strip()]


def test_the_audit_recorded_an_agreed_summary_entry(entries: list[str]) -> None:
    assert entries, "the summary file exists but holds no entry"
    assert AGREED_ENTRY.match(entries[-1]), (
        f"the last summary entry is not a day's window that agreed: {entries[-1]!r}"
    )


def test_the_session_arrived_as_audit_client(recon: Node, entries: list[str]) -> None:
    log = recon.read("/var/log/xrdp.log")
    assert "audit-client" in log, (
        "recon-01's remote-desktop log does not name the client audit-client"
    )


def test_the_audit_commands_are_in_the_shell_history(
    recon: Node, home: str, entries: list[str]
) -> None:
    history = recon.read(f"{home}/.bash_history").splitlines()
    expected = [
        LATCH_COMMAND,
        re.compile(
            r"^\{ curl -sS --fail-with-body -u '[^']+' -G "
            r"'https://hist-dmz-01/api/datasources/proxy/uid/monthly-audit-logs"
            r"/select/logsql/query' --data-urlencode 'query=_time:\[[^,]+, [^)]+\)' "
            r'2>&1; echo "retrieval-end \$\?"; \} \| xclip -selection clipboard$'
        ),
        re.compile(r"^echo .+ agreed >> ~/reconciliation-audits\.txt$"),
        re.compile(
            r"^tail -n 1 ~/reconciliation-audits\.txt \| xclip -selection clipboard$"
        ),
    ]
    position = 0
    for pattern in expected:
        found = next(
            (n for n in range(position, len(history)) if pattern.match(history[n])),
            None,
        )
        assert found is not None, (
            f"no history line after line {position} matches {pattern.pattern}"
        )
        position = found + 1


def test_the_first_terminal_attempt_confirmed_the_latch(
    recon: Node, home: str, entries: list[str]
) -> None:
    start = ENTRY_START.match(entries[-1])
    assert start, f"the last summary entry has no window start: {entries[-1]!r}"
    window = "".join(start.groups())
    history = recon.read(f"{home}/.bash_history").splitlines()
    attempts = [
        int(latch.group(2))
        for latch in map(LATCH_COMMAND.match, history)
        if latch and latch.group(1) == window
    ]
    assert attempts, f"the history holds no latch command for the window {window}"
    assert attempts == [1], (
        f"the latch commands for the window {window} carry attempts {attempts}: "
        "an earlier attempt failed and the budget absorbed it. On a new X session "
        "that is a Ctrl+Alt+T sent before the desktop had started, which the wait "
        "for the desktop (desktop_settle) prevents"
    )


def test_the_sim_audited_without_a_warning_or_an_error(entries: list[str]) -> None:
    status = subprocess.run(
        ["incus", "exec", "sim-host", "--", "rc-service", "sim", "status"],
        capture_output=True,
        text=True,
    )
    assert "started" in status.stdout, (
        f"the sim is not running: {(status.stdout or status.stderr).strip()}"
    )
    log = subprocess.run(
        ["incus", "exec", "sim-host", "--", "cat", "/var/log/sim.log"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    raised = [
        line
        for line in log.splitlines()
        if "reconciliation" in line and re.search(r"\b(WARN|ERROR)\b", line)
    ]
    assert not raised, "the reconciliation user warned or failed:\n" + "\n".join(raised)
