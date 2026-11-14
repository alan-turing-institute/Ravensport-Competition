"""Simulator stops (episodes.md §12, terminated and discarded episodes).

Each message below is the panic message of one stop site in the simulator's
source, under infra/sim/src, with example values in its placeholders. The
messages of library errors are those that the simulator's versions of Rust,
reqwest and the time crate print.

Run with: pytest competition/tests -q
"""

from __future__ import annotations

import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from competition.episode_runner import stops
from competition.episode_runner.incus import IncusError, Result
from competition.episode_runner.steps import EndEpisode

JOB = "0b5c2b9e-4f21-4c8e-9d6a-2f1e3c4b5a69"
LIVE_JOB = "7d3f1a2b-8c4e-4d5f-a6b7-c8d9e0f1a2b3"
HMI = "http://127.0.0.1:20002"
WINDOW = "2025-01-01T00:00:00Z to 2025-02-01T00:00:00Z"

# The stops below have a cause inside the episode. Such a cause is a request to
# the digital world that is refused, stays unanswered after its retries or gets
# a response that the simulator cannot use, or a terminal state.
INSIDE = [
    pytest.param(
        "dock_operator: POST /api/jobs/refuel returned HTTP 503 Service "
        "Unavailable after 3 retries",
        id="dock_operator: a server error after its retries",
    ),
    pytest.param(
        "dock_operator: POST /api/auth/login failed after 3 retries: error "
        f"sending request for url ({HMI}/api/auth/login)",
        id="dock_operator: a transient failure after its retries",
    ),
    pytest.param(
        "dock_operator: POST /api/jobs/refuel failed after 3 retries",
        id="dock_operator: a create whose response is lost after its retries",
    ),
    pytest.param(
        "dock_operator: GET /api/jobs/current request failed: error following "
        f"redirect for url ({HMI}/api/jobs/current)",
        id="dock_operator: a request error that is not retried",
    ),
    pytest.param(
        "dock_operator: GET /api/auth/challenge unexpected HTTP 401 Unauthorized: ",
        id="dock_operator: a refused request",
    ),
    pytest.param(
        "dock_operator: GET /api/tanks/01/level unexpected HTTP status: HTTP "
        f"status client error (404 Not Found) for url ({HMI}/api/tanks/01/level)",
        id="dock_operator: a refused request whose body is not read",
    ),
    pytest.param(
        "dock_operator: GET /api/tanks/01/level malformed response body: error "
        "decoding response body",
        id="dock_operator: a malformed response body",
    ),
    pytest.param(
        "assertion `left == right` failed: dock_operator: GET /api/jobs/current "
        f"returned job {JOB} but the operator's live job is {LIVE_JOB}\n"
        f'  left: "{JOB}"\n'
        f' right: "{LIVE_JOB}"',
        id="dock_operator: the current job is not the operator's",
    ),
    pytest.param(
        "assertion `left == right` failed: dock_operator: totaliser poll returned "
        f"job {JOB} but the operator's live job is {LIVE_JOB}\n"
        f'  left: "{JOB}"\n'
        f' right: "{LIVE_JOB}"',
        id="dock_operator: the totaliser's job is not the operator's",
    ),
    pytest.param(
        f"dock_operator: completed dispense {LIVE_JOB} reports no opened_at",
        id="dock_operator: a dispense without its opening time",
    ),
    pytest.param(
        "dock_operator: opened_at \"yesterday\" is not RFC 3339: the 'year' "
        "component could not be parsed",
        id="dock_operator: an opening time that cannot be read",
    ),
    pytest.param(
        "fuel_supplier: ATG poll failed after 3 retries: Connection refused "
        "(os error 111)",
        id="fuel_supplier: a poll that fails after its retries",
    ),
    pytest.param(
        "fuel_supplier: response is not SOH…ETX framed",
        id="fuel_supplier: a response without its framing",
    ),
    pytest.param(
        "fuel_supplier: response too short for the trailing checksum",
        id="fuel_supplier: a response without its checksum",
    ),
    pytest.param(
        "fuel_supplier: response too short for its header",
        id="fuel_supplier: a response without its header",
    ),
    pytest.param(
        "fuel_supplier: i201 response too short for the Volume field",
        id="fuel_supplier: an inventory response without its volume",
    ),
    pytest.param(
        "fuel_supplier: non-UTF-8 hex in float field: Utf8Error { valid_up_to: 0, "
        "error_len: Some(1) }",
        id="fuel_supplier: a float field that is not text",
    ),
    pytest.param(
        "fuel_supplier: invalid hex in float field: ParseIntError { kind: "
        "InvalidDigit }",
        id="fuel_supplier: a float field that is not hexadecimal",
    ),
    pytest.param(
        "reconciliation_user: the audit of 2025-01-01 0:00:00.0 to 2025-02-01 "
        "0:00:00.0 found RecordWithoutPaper: BDN-0042; also reported: 1 LostLink",
        id="reconciliation_user: an audit with a fatal finding",
    ),
    pytest.param(
        f"reconciliation_user: the audit of {WINDOW} failed at connecting (the "
        "workstation's certificate did not verify), before any session restart",
        id="reconciliation_user: a certificate that does not verify",
    ),
    pytest.param(
        f"reconciliation_user: the audit of {WINDOW} failed at the session (the "
        "connection was lost), after the session restart",
        id="reconciliation_user: a session lost after the restart",
    ),
    pytest.param(
        f"reconciliation_user: the audit of {WINDOW} failed at terminal start (the "
        "latch returned a different value), after the session restart",
        id="reconciliation_user: a terminal that does not start",
    ),
    pytest.param(
        f"reconciliation_user: the audit of {WINDOW} failed at retrieval (the log "
        "server refused the credential), before any session restart",
        id="reconciliation_user: a refused credential",
    ),
    pytest.param(
        f"reconciliation_user: the audit of {WINDOW} failed at the clipboard (a "
        "span of one day or less reached its deadline), after the session restart",
        id="reconciliation_user: a clipboard that does not deliver",
    ),
    pytest.param(
        "fuel_tank: cavitation — pump drawing from an empty tank (volume 0.000000 L "
        "at 1500 L/min outflow); the FMS cavitation guard has been defeated",
        id="fuel_tank: cavitation",
    ),
]

# Stops caused by a defect in the simulator's own code.
DEFECTS = [
    pytest.param("dock_operator: job channel closed", id="a closed channel"),
    pytest.param(
        "road_tanker_driver: dock operator dropped the proceed signal: RecvError(())",
        id="a dropped signal",
    ),
    pytest.param(
        "dispenser: a supervised task exited unexpectedly", id="a supervised task"
    ),
    pytest.param("a sim entity exited unexpectedly", id="an entity that exited"),
    pytest.param("a sim entity was cancelled", id="an entity that was cancelled"),
    pytest.param("the sim supervisor wrapper failed", id="the supervisor"),
    pytest.param(
        "a sim entity panicked (non-string payload)", id="a panic without a message"
    ),
    pytest.param(
        'atg_console: bind TCP listener: Os { code: 98, kind: AddrInUse, message: "'
        'Address in use" }',
        id="a listener that cannot bind",
    ),
    pytest.param(
        "dock_operator: POST /api/auth/login request failed: builder error",
        id="dock_operator: a request that cannot be built",
    ),
    pytest.param(
        f"reconciliation_user: the audit of {WINDOW} failed at typing (a command "
        "held 'é', which the scancode table lacks), before any session restart",
        id="reconciliation_user: a character the scancode table lacks",
    ),
    pytest.param(
        "reconciliation_user: BDN-0042 commenced at 2024-12-31 23:00:00.0, before "
        "the audited window from 2025-01-01 0:00:00.0 to 2025-02-01 0:00:00.0; the "
        "settling delay should make this impossible",
        id="reconciliation_user: an invariant",
    ),
    pytest.param(
        "called `Option::unwrap()` on a `None` value", id="a message of no pattern"
    ),
]


@pytest.mark.parametrize("message", INSIDE)
def test_a_stop_with_a_cause_inside_the_episode_terminates_it(message: str) -> None:
    assert stops.classify(message) == stops.TERMINATED


@pytest.mark.parametrize("message", DEFECTS)
def test_a_simulator_defect_discards_the_episode(message: str) -> None:
    assert stops.classify(message) == stops.DISCARDED


def test_a_stop_without_a_logged_cause_discards_the_episode() -> None:
    assert stops.classify(None) == stops.DISCARDED


def test_every_pattern_matches_a_message_of_the_simulator() -> None:
    messages = [param.values[0] for param in INSIDE]
    for pattern in stops.INSIDE_THE_EPISODE:
        assert any(pattern.match(m) for m in messages), pattern.pattern


START = (
    "2026-11-04T01:13:18.827797Z  INFO start_state: start_state "
    'sim_tug_arrivals=4651467801475421837 delivery_state="no order placed"'
)


def test_the_stop_is_read_from_the_simulators_log() -> None:
    """Rust prints the panic to standard error, and the simulator logs the
    first entity's panic message as the cause. The node sim-host writes both
    streams to /var/log/sim.log."""
    assertion = (
        "assertion `left == right` failed: dock_operator: GET /api/jobs/current "
        f"returned job {JOB} but the operator's live job is {LIVE_JOB}\n"
        f'  left: "{JOB}"\n'
        f' right: "{LIVE_JOB}"'
    )
    log = "\n".join(
        [
            START,
            "2026-11-04T01:28:28.794535Z  WARN sim::actors::dock_operator::internals: "
            'HMI session expired; re-authenticating request="POST /api/jobs/refuel"',
            "",
            "thread 'main' (812) panicked at "
            "src/actors/dock_operator/internals.rs:946:5:",
            assertion,
            "note: run with `RUST_BACKTRACE=1` environment variable to display a "
            "backtrace",
            "2026-11-04T01:30:02.104115Z ERROR sim::orchestrator: sim shutting down "
            f"cause={assertion}",
            "",
        ]
    )
    stop = stops.read_stop(log)
    assert stop.cause == assertion
    assert stop.panics == (
        "thread 'main' (812) panicked at src/actors/dock_operator/internals.rs:946:5:\n"
        + assertion,
    )
    assert stop.outcome == stops.TERMINATED
    assert stop.at == datetime(2026, 11, 4, 1, 30, 2, 104115, timezone.utc)


def test_a_log_without_a_shutdown_line_gives_no_cause() -> None:
    """A simulator that is killed logs no cause."""
    stop = stops.read_stop(START + "\n")
    assert stop.cause is None
    assert stop.at is None
    assert stop.panics == ()
    assert stop.outcome == stops.DISCARDED


class FakeSimHost:
    def __init__(self, status: str) -> None:
        self.status = status
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    def exec(self, node: str, *argv: str, timeout_s: float = 60.0) -> Result:
        self.calls.append((node, argv))
        if self.status == "unreadable":
            return Result(1, "", "Error: websocket: close 1006")
        return Result(0, f" * status: {self.status}\n", "")


def test_the_simulator_is_checked_in_the_middle_of_each_step() -> None:
    sim_host = FakeSimHost("started")
    checks = stops.checks(sim_host, step_0=1000.0, horizon=3, step_s=5.0)
    assert [c.at for c in checks] == [1002.5, 1007.5, 1012.5]
    for check in checks:
        check.action()
    assert sim_host.calls == [("sim-host", ("rc-service", "sim", "status"))] * 3


def test_a_stopped_simulator_ends_the_episode() -> None:
    (check,) = stops.checks(FakeSimHost("crashed"), step_0=0.0, horizon=1, step_s=5.0)
    with pytest.raises(EndEpisode, match="crashed"):
        check.action()


def test_a_status_that_cannot_be_read_leaves_the_episode_running() -> None:
    """A failed read of sim-host is checked again in the next step."""
    (check,) = stops.checks(
        FakeSimHost("unreadable"), step_0=0.0, horizon=1, step_s=5.0
    )
    check.action()


def test_the_patterns_name_no_message_twice() -> None:
    """Each message of the simulator matches one pattern at most, so that each
    pattern stays traceable to its stop sites."""
    for param in INSIDE:
        matched = [p for p in stops.INSIDE_THE_EPISODE if p.match(param.values[0])]
        assert len(matched) == 1, (param.id, [p.pattern for p in matched])


def fake_proc(root: Path, processes: dict[int, tuple[int, str, str]]) -> Path:
    """A /proc tree of processes, each as (parent, name, command line)."""
    for pid, (parent, name, command) in processes.items():
        entry = root / str(pid)
        entry.mkdir()
        (entry / "stat").write_text(f"{pid} ({name}) S {parent} {pid} {pid} 0 -1\n")
        (entry / "cmdline").write_bytes(command.replace(" ", "\0").encode() + b"\0")
    return root


PROCESSES = {
    1: (0, "systemd", "/sbin/init"),
    500: (1, "init", "/sbin/init"),
    510: (500, "haproxy", "/usr/sbin/haproxy -f /etc/haproxy/haproxy.cfg"),
    520: (500, "sim", stops.SIM_COMMAND),
    530: (1, "sim", stops.SIM_COMMAND),
    540: (500, "a ) b", "/bin/sh -c a"),
}


class FakeStateIncus:
    def __init__(self, init: int) -> None:
        self.init = init

    def query(self, path: str, timeout_s: float = 60.0) -> dict:
        assert path == "/1.0/instances/sim-host/state"
        return {"status": "Running", "pid": self.init}


def test_the_simulator_is_found_below_the_first_process_of_sim_host(
    tmp_path: Path,
) -> None:
    """Another simulator elsewhere on the virtual machine is left alone."""
    proc = fake_proc(tmp_path, PROCESSES)
    assert stops.find_process(proc, 500, stops.SIM_COMMAND) == 520
    assert stops.find_process(proc, 1, "/usr/sbin/haproxy") == 510


def test_a_simulator_that_cannot_be_found_leaves_the_watch_off(tmp_path: Path) -> None:
    proc = fake_proc(tmp_path, {k: v for k, v in PROCESSES.items() if k != 520})
    assert stops.watch_simulator(FakeStateIncus(500), proc) is None


@pytest.mark.skipif(not hasattr(os, "pidfd_open"), reason="Linux only")
def test_the_watch_fires_when_the_process_exits() -> None:
    process = subprocess.Popen(["sleep", "30"])
    watch = stops.ProcessWatch(process.pid)
    try:
        assert not watch.fired()
        process.kill()
        process.wait()
        assert watch.fired()
    finally:
        watch.close()


EARLY_EOF = "fuel_supplier: ATG poll failed after 3 retries: early eof"


class FakeLogHost:
    """sim-host, whose simulator's log holds the lines given."""

    def __init__(self, *lines: str) -> None:
        self.lines = lines

    def exec(self, node: str, *argv: str, timeout_s: float = 60.0) -> Result:
        assert (node, argv) == ("sim-host", ("cat", "/var/log/sim.log"))
        return Result(0, "\n".join(self.lines) + "\n", "")


def test_the_log_on_sim_host_gives_a_stop_before_step_0() -> None:
    """A simulator whose first poll of the tank gauge fails during its start-up
    stops before step 0, and its log on sim-host holds the cause."""
    stop = stops.logged_stop(
        FakeLogHost(
            START,
            f"2026-11-09T05:59:41.317145Z ERROR sim::orchestrator: "
            f"sim shutting down cause={EARLY_EOF}",
        )
    )
    assert stop.cause == EARLY_EOF
    assert stop.at == datetime(2026, 11, 9, 5, 59, 41, 317145, tzinfo=timezone.utc)


def test_the_log_of_a_running_simulator_gives_no_cause() -> None:
    assert stops.logged_stop(FakeLogHost(START)).cause is None


def test_a_gone_process_ends_the_episode_while_openrc_reports_started() -> None:
    """OpenRC can still report the simulator as started after its process has
    gone, so the check also asks whether the process runs."""
    (check,) = stops.checks(
        FakeSimHost("started"), step_0=0.0, horizon=1, step_s=5.0, alive=lambda: False
    )
    with pytest.raises(EndEpisode, match="process has gone"):
        check.action()


def test_a_process_that_cannot_be_seen_leaves_the_episode_running() -> None:
    (check,) = stops.checks(
        FakeSimHost("started"), step_0=0.0, horizon=1, step_s=5.0, alive=lambda: None
    )
    check.action()


class FakeInstanceIncus:
    """Incus, with sim-host as an instance of the type given."""

    def __init__(self, kind: str, init: int = 500, failing: bool = False) -> None:
        self.kind, self.init, self.failing = kind, init, failing

    def query(self, path: str, timeout_s: float = 60.0) -> dict:
        if self.failing:
            raise IncusError("Error: not found")
        if path == "/1.0/instances/sim-host":
            return {"name": "sim-host", "type": self.kind}
        assert path == "/1.0/instances/sim-host/state"
        return {"status": "Running", "pid": self.init}


def test_the_simulators_process_is_seen_in_a_container(tmp_path: Path) -> None:
    proc = fake_proc(tmp_path, PROCESSES)
    assert stops.simulator_process(FakeInstanceIncus("container"), proc) is True


def test_a_container_without_the_simulator_has_lost_its_process(tmp_path: Path) -> None:
    """The simulator elsewhere on the virtual machine does not count."""
    proc = fake_proc(tmp_path, {k: v for k, v in PROCESSES.items() if k != 520})
    assert stops.simulator_process(FakeInstanceIncus("container"), proc) is False


def test_a_virtual_machine_hides_the_simulators_process(tmp_path: Path) -> None:
    proc = fake_proc(tmp_path, {k: v for k, v in PROCESSES.items() if k != 520})
    assert stops.simulator_process(FakeInstanceIncus("virtual-machine"), proc) is None


def test_an_unreadable_incus_hides_the_simulators_process(tmp_path: Path) -> None:
    proc = fake_proc(tmp_path, PROCESSES)
    incus = FakeInstanceIncus("container", failing=True)
    assert stops.simulator_process(incus, proc) is None
