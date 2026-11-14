"""Simulator stops (episodes.md §12, terminated and discarded episodes).

The simulator stops at the first panic of any of its entities. Rust prints each
panic to standard error. The simulator then logs the first entity's panic
message as the cause of its line "sim shutting down", and exits
(`infra/sim/src/orchestrator.rs`). The node `sim-host` writes both streams to
/var/log/sim.log (sim-network.md §7, image, build and supervision).

A stop whose cause lies inside the episode terminates the episode. Such a cause
is a request to the digital world that is refused, stays unanswered after its
retries or gets a response that the simulator cannot use, or a terminal state.
Any other stop discards the episode, because only a defect in the simulator's
own code is left to cause it. The patterns come from the panic messages in the
simulator's source, and a message that matches none of them discards the
episode.
"""

from __future__ import annotations

import logging
import os
import re
import select
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from competition.episode_runner.incus import IncusError
from competition.episode_runner.settle import SIM_HOST, SIM_LOG, Incus, simulator_status
from competition.episode_runner.steps import EndEpisode, Event

TERMINATED = "terminated"
DISCARDED = "discarded"

INSIDE_THE_EPISODE = tuple(
    re.compile(pattern)
    for pattern in (
        # The dock operator (actors/dock_operator/internals.rs) stops at a
        # request that still fails after its retries, or fails in a way that is
        # not retried. A request that cannot be built is a defect.
        r"dock_operator: .+ returned HTTP .+ after \d+ retries",
        r"dock_operator: .+ failed after \d+ retries",
        r"dock_operator: .+ request failed: (?!builder error)",
        # The dock operator also stops at a refused request, and at a response
        # that it cannot read.
        r"dock_operator: .+ unexpected HTTP ",
        r"dock_operator: .+ malformed response body: ",
        r"assertion `left == right` failed: dock_operator: .+ returned job ",
        r"dock_operator: completed dispense .+ reports no opened_at",
        r"dock_operator: opened_at .+ is not RFC 3339: ",
        # The fuel supplier (actors/fuel_supplier/internals.rs) stops at a poll
        # of the tank gauge that still fails after its retries, and at a
        # response that it cannot read.
        r"fuel_supplier: ATG poll failed after \d+ retries: ",
        r"fuel_supplier: response is not SOH…ETX framed",
        r"fuel_supplier: (i201 )?response too short for ",
        r"fuel_supplier: (non-UTF-8|invalid) hex in float field",
        # The reconciliation user (actors/reconciliation_user) stops at an audit
        # with a fatal finding, which has no benign cause (sim-impl.md,
        # reconciliation user, outcome). The reconciliation user also stops at
        # a failure at a site in the digital world. A character that the
        # scancode table lacks fails at typing, and is a defect.
        r"reconciliation_user: the audit of .+ found ",
        r"reconciliation_user: the audit of .+ failed at "
        r"(connecting|the session|terminal start|retrieval|the clipboard) \(",
        # The fuel tank (physical/fuel_tank/internals.rs) stops at the terminal
        # state of cavitation.
        r"fuel_tank: cavitation — ",
    )
)

SHUTDOWN = "sim shutting down cause="
# The simulator's program on sim-host (sim-network.md §7, image, build and
# supervision).
SIM_COMMAND = "/usr/local/bin/sim"

log = logging.getLogger(__name__)
_PANIC = re.compile(r"thread '.*'( \(\d+\))? panicked at .+:$")
_RECORD = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
_NOTE = "note: run with `RUST_BACKTRACE=1`"


@dataclass(frozen=True)
class Stop:
    """A simulator stop, with the cause and the time of its line "sim shutting
    down", and every panic in the simulator's log."""

    cause: str | None
    at: datetime | None
    panics: tuple[str, ...]
    outcome: str


def classify(cause: str | None) -> str:
    """`TERMINATED` for a cause inside the episode, otherwise `DISCARDED`."""
    if cause is not None and any(p.match(cause) for p in INSIDE_THE_EPISODE):
        return TERMINATED
    return DISCARDED


def read_stop(log: str) -> Stop:
    """The cause and every panic in the simulator's log, /var/log/sim.log.

    A message of several lines, such as that of a failed assertion, runs to
    the next line of the log, panic or note."""
    lines = log.splitlines()
    cause: str | None = None
    at: datetime | None = None
    panics: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        is_cause = SHUTDOWN in line
        if not is_cause and not _PANIC.match(line):
            continue
        text = [line.split(SHUTDOWN, 1)[1] if is_cause else line]
        while i < len(lines) and not _starts_an_entry(lines[i]):
            text.append(lines[i])
            i += 1
        message = "\n".join(text).rstrip("\n")
        if is_cause:
            cause = message
            at = (
                datetime.fromisoformat(line.split()[0]) if _RECORD.match(line) else None
            )
        else:
            panics.append(message)
    return Stop(cause, at, tuple(panics), classify(cause))


def _starts_an_entry(line: str) -> bool:
    return bool(_RECORD.match(line) or _PANIC.match(line) or line.startswith(_NOTE))


def logged_stop(incus: Incus) -> Stop:
    """The stop that the simulator's log on sim-host holds so far, read during
    the episode. The cause is None until the simulator stops."""
    return read_stop(incus.exec(SIM_HOST, "cat", SIM_LOG).stdout)


def checks(
    incus: Incus,
    step_0: float,
    horizon: int,
    step_s: float,
    alive: Callable[[], bool | None] | None = None,
) -> list[Event]:
    """A check of the simulator in the middle of each step. A stopped service,
    or a process that alive finds gone, ends the episode. OpenRC can report a
    simulator as started after its process has gone, so the service alone is
    not enough. A status that cannot be read is checked again in the next
    step."""

    def check() -> None:
        status = simulator_status(incus)
        if status is not None and status != "started":
            raise EndEpisode(f"the simulator's service is {status}")
        if alive is not None and alive() is False:
            raise EndEpisode("the simulator's process has gone")

    return [Event(step_0 + (k + 0.5) * step_s, check) for k in range(horizon)]


class StateQuery(Protocol):
    def query(self, path: str, timeout_s: float = 60.0) -> dict[str, Any]: ...


class ProcessWatch:
    """A watch on a process of the virtual machine through a process file
    descriptor (pidfd). The kernel makes the file readable when the process
    exits, so the watch wakes the step schedule at once."""

    def __init__(self, pid: int) -> None:
        self._fd = os.pidfd_open(pid)

    def fileno(self) -> int:
        return self._fd

    def fired(self) -> bool:
        ready, _, _ = select.select([self._fd], [], [], 0)
        return bool(ready)

    def close(self) -> None:
        os.close(self._fd)


def find_process(proc: Path, root: int, command: str) -> int | None:
    """The first process below root, in the tree of processes under proc,
    whose program is command."""
    children: dict[int, list[int]] = {}
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text()
        except OSError:
            continue
        parent = int(stat.rsplit(")", 1)[1].split()[1])
        children.setdefault(parent, []).append(int(entry.name))
    below = [root]
    while below:
        for pid in sorted(children.get(below.pop(), [])):
            try:
                program = (proc / str(pid) / "cmdline").read_bytes().split(b"\0")[0]
            except OSError:
                continue
            if program.decode(errors="replace") == command:
                return pid
            below.append(pid)
    return None


def simulator_process(incus: StateQuery, proc: Path = Path("/proc")) -> bool | None:
    """Whether the simulator's process runs below the first process of
    sim-host. None where the host cannot see the process, because sim-host is a
    virtual machine with its own kernel, or because Incus cannot be read."""
    try:
        if incus.query(f"/1.0/instances/{SIM_HOST}")["type"] != "container":
            return None
        first = incus.query(f"/1.0/instances/{SIM_HOST}/state")["pid"]
        return find_process(proc, first, SIM_COMMAND) is not None
    except (IncusError, OSError, KeyError, AttributeError, TypeError):
        return None


def watch_simulator(
    incus: StateQuery, proc: Path = Path("/proc")
) -> ProcessWatch | None:
    """A watch on the simulator's process, which runs below the first process
    of sim-host on the virtual machine. The result is None where the process
    cannot be found or watched, and the step schedule then checks the
    simulator's service in each step."""
    try:
        first = incus.query(f"/1.0/instances/{SIM_HOST}/state")["pid"]
        pid = find_process(proc, first, SIM_COMMAND)
        if pid is None:
            log.warning("found no process of the simulator below sim-host")
            return None
        watch = ProcessWatch(pid)
    except (IncusError, OSError, KeyError, AttributeError) as error:
        log.warning("cannot watch the simulator's process: %s", error)
        return None
    # The process number could have passed to a new process before the watch
    # opened, so the search runs again.
    if find_process(proc, first, SIM_COMMAND) != pid:
        watch.close()
        return None
    return watch
