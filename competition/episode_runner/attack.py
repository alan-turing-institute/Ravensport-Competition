"""The attack chain's session (episodes.md §10.4, the attack chain's session).

At the onset step the episode runner starts the chain's binary on the adversary
host, paced, through the attack chain's session: an `incus exec` whose standard
input and output the episode runner holds through pipes of its own (incus.py).
The binary's standard error, its narration and command trace, goes to a file of
the episode record.

A shell starts first, prints its process identifier and replaces itself with the
binary, so the first line of standard output is the binary's own process
identifier on the adversary host. The paced binary then waits, before each
stage, for the line `begin` on standard input, and writes a stage event to
standard output as each stage begins and ends.

The session reads the binary's standard output on a thread of its own and
times each stage event on the episode runner's clock as it arrives. The first
wait runs from the session's start, and each later wait from the end event of
the stage before (episodes.md §10.3, onset and waits). Once a wait's start is
known, the session holds the next stage's number and the time at which it falls
due. The step schedule releases the stage, by writing `begin`, in the one order
of events in which it also reads the step session (steps.py), so that each
release falls either before or after the Track A alarm. In Track A the step
schedule ends the episode at the time at which the last stage falls due, and
never releases that stage (episodes.md §11, horizon and termination).

At the end of the episode the episode runner stops the binary. During a stage it
first sends SIGTERM inside the adversary host, which a holding impact answers by
restoring what it changed (attack-chains-tests.md §3.2, long-running binaries).
Any other stage ends at the signal. The episode runner then closes standard
input, so a binary still running exits before its next stage. It never sends
SIGKILL (episodes.md §10.5, stopping the chain).
"""

from __future__ import annotations

import json
import logging
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

log = logging.getLogger(__name__)

RELEASE = "begin\n"


@dataclass
class StageEvent:
    """One stage event, timed on the episode runner's clock. A beginning
    event also gives the chain's own number of stages, `total`."""

    kind: str
    position: int
    stage: str
    at: float
    outcome: str | None = None
    reason: str | None = None
    total: int | None = None


@dataclass
class AttackRun:
    """What the attack chain's session did, for the episode record."""

    started: float | None = None
    pid: int | None = None
    releases: list[float] = field(default_factory=list)
    events: list[StageEvent] = field(default_factory=list)
    stdout: list[str] = field(default_factory=list)
    stop: str | None = None
    stop_sent: float | None = None
    # When the session's standard output ended, which is when the binary
    # exited, and when the episode runner had the session's exit status.
    output_ended: float | None = None
    exit_status: int | None = None
    ended: float | None = None
    problems: list[str] = field(default_factory=list)

    @property
    def stages_begun(self) -> int:
        return sum(1 for e in self.events if e.kind == "begin")

    def as_json(self, at: Callable[[float], Any]) -> dict[str, Any]:
        def time_or_none(t: float | None) -> Any:
            return None if t is None else at(t)

        return {
            "started": time_or_none(self.started),
            "pid": self.pid,
            "releases": [at(t) for t in self.releases],
            "events": [
                {
                    "event": e.kind,
                    "stage_number": e.position,
                    "stage": e.stage,
                    "at": at(e.at),
                    **({"outcome": e.outcome} if e.outcome is not None else {}),
                    **({"reason": e.reason} if e.reason is not None else {}),
                }
                for e in self.events
            ],
            "stages_begun": self.stages_begun,
            "stdout": self.stdout,
            "stop": self.stop,
            "stop_sent": time_or_none(self.stop_sent),
            "output_ended": time_or_none(self.output_ended),
            "exit_status": self.exit_status,
            "ended": time_or_none(self.ended),
            "problems": self.problems,
        }


def early_stop(run: AttackRun, stages: list[str]) -> dict[str, Any] | None:
    """Where and why the attack chain stopped before completing its last stage
    (episodes.md §12, terminated and discarded episodes), or None when every
    stage completed. stages names the chain's stages in order."""
    ends = {e.position: e for e in run.events if e.kind == "end"}
    begun = {e.position for e in run.events if e.kind == "begin"}
    if all(
        p in ends and ends[p].outcome == "completed" for p in range(1, len(stages) + 1)
    ):
        return None
    for position in range(1, len(stages) + 1):
        stage = stages[position - 1]
        end = ends.get(position)
        if end is not None and end.outcome == "completed":
            continue
        if end is not None:
            cause = f"the stage failed: {end.reason}"
        elif position in begun:
            cause = (
                "the episode ended during the stage"
                if run.stop
                else f"the binary exited with status {run.exit_status} during the stage"
            )
        elif run.started is None:
            cause = "the episode ended before the onset step"
        elif run.stop:
            cause = "the episode ended before the stage began"
        else:
            cause = f"the binary exited with status {run.exit_status} before the stage began"
        return {"stage_number": position, "stage": stage, "cause": cause}
    return None


def parse_event(line: str, at: float) -> StageEvent | None:
    """The stage event on a line of standard output, or None for any other
    line."""
    try:
        data = json.loads(line)
    except ValueError:
        return None
    if not isinstance(data, dict) or "stage_event" not in data:
        return None
    return StageEvent(
        kind=str(data["stage_event"]),
        position=int(data["stage_number"]),
        stage=str(data["stage"]),
        at=at,
        outcome=data.get("outcome"),
        reason=data.get("reason"),
        total=(
            int(data["total_num_stages"])
            if data.get("total_num_stages") is not None
            else None
        ),
    )


class AttackChain:
    """The attack chain's session for one episode, whose stages the step
    schedule releases through next_stage and release."""

    def __init__(
        self,
        incus: Any,
        clock: Any,
        command: list[str],
        node: str,
        waits_s: list[float],
        stderr_path: Path,
        popen: Callable[..., Any] = subprocess.Popen,
    ) -> None:
        self.incus = incus
        self.clock = clock
        self.command = command
        self.node = node
        self.waits_s = list(waits_s)
        self.stderr_path = stderr_path
        self.popen = popen
        self.run = AttackRun()
        self._lock = threading.Lock()
        self._stopping = threading.Event()
        self._in_stage = False
        # The next stage's number and the time at which it falls due, once
        # the start of its wait is known.
        self._next: tuple[int, float] | None = None
        # Why the session failed to start or gave no process identifier,
        # which discards the episode (episodes.md §12, terminated and
        # discarded episodes).
        self.failure: str | None = None
        self._proc: Any = None
        self._stdin: IO[bytes] | None = None
        self._thread: threading.Thread | None = None

    @property
    def total(self) -> int:
        """The chain's number of stages, K in Track A's reward."""
        return len(self.waits_s)

    @property
    def released(self) -> int:
        """The number of stages released so far."""
        with self._lock:
            return len(self.run.releases)

    def next_stage(self) -> tuple[int, float] | None:
        """The next stage's number and the time at which it falls due, or None
        while the start of its wait is unknown, and once no stage remains."""
        with self._lock:
            return self._next

    def release(self, number: int) -> bool:
        """Write `begin` for stage `number`, the next stage, and return whether
        the stage was released."""
        with self._lock:
            if self._stopping.is_set() or self._next is None or self._next[0] != number:
                return False
            self._next = None
            try:
                self._stdin.write(RELEASE.encode())
                self._stdin.flush()
            except (BrokenPipeError, OSError, ValueError) as error:
                self.run.problems.append(
                    f"the release of stage {number} failed: {error}"
                )
                return False
            self._in_stage = True
            self.run.releases.append(self.clock.now())
            return True

    # The step schedule calls start at the onset step, from its own thread, so
    # start returns at once and the session reads on a thread of its own.
    def start(self) -> None:
        try:
            with open(self.stderr_path, "wb") as stderr:
                self._proc = self.popen(
                    self.command,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=stderr,
                )
        except OSError as error:
            self.failure = f"the attack chain's session failed to start: {error}"
            self.run.problems.append(self.failure)
            log.warning("%s", self.failure)
            return
        self._stdin = self._proc.stdin
        self.run.started = self.clock.now()
        self._thread = threading.Thread(
            target=self._control, name="attack-chain", daemon=True
        )
        self._thread.start()

    def _readline(self) -> str | None:
        """The next line of standard output, or None at its end."""
        line = self._proc.stdout.readline()
        if not line:
            return None
        return line.decode(errors="replace").rstrip("\n")

    def _due(self, number: int, wait_start: float) -> None:
        """Note when stage `number` falls due, its wait running from
        wait_start. Called with the lock held."""
        if number <= len(self.waits_s) and not self._stopping.is_set():
            self._next = (number, wait_start + self.waits_s[number - 1])

    def _control(self) -> None:
        try:
            first = self._readline()
            if first is None or not first.strip().isdigit():
                self.failure = (
                    f"the attack chain's session gave no process identifier: its "
                    f"first line was {first!r}"
                )
                self.run.problems.append(self.failure)
                return
            self.run.pid = int(first.strip())
            with self._lock:
                self._due(1, self.run.started)
            while (line := self._readline()) is not None:
                event = self._take(line)
                if event is None or event.kind != "end":
                    continue
                with self._lock:
                    # Only the end event of the stage in progress starts the
                    # next wait; a failed stage ends the releases.
                    if not self._in_stage or event.position != len(self.run.releases):
                        continue
                    self._in_stage = False
                    if event.outcome == "completed":
                        self._due(event.position + 1, event.at)
        except Exception as error:  # noqa: BLE001 - recorded, never raised
            self.run.problems.append(f"the attack chain's controller failed: {error!r}")
            log.exception("the attack chain's controller failed")
        finally:
            with self._lock:
                self._next = None
                self._in_stage = False
            try:
                self._drain()
            except Exception as error:  # noqa: BLE001 - recorded, never raised
                self.run.problems.append(
                    f"the attack chain's controller failed: {error!r}"
                )

    def _take(self, line: str) -> StageEvent | None:
        event = parse_event(line, self.clock.now())
        if event is None:
            self.run.stdout.append(line)
        else:
            self.run.events.append(event)
            # K is the chain's own number of stages, total_num_stages, which
            # the catalogue lists as the chain's stages, one wait for each
            # (episodes.md §10.4, the attack chain's session).
            if (
                event.total is not None
                and event.total != len(self.waits_s)
                and not any("total_num_stages" in p for p in self.run.problems)
            ):
                self.run.problems.append(
                    f"stage {event.position}'s beginning event gives "
                    f"total_num_stages {event.total}, and the catalogue lists "
                    f"{len(self.waits_s)} stages"
                )
            log.info(
                "attack chain: stage %d %s %s%s",
                event.position,
                event.stage,
                event.kind,
                f" ({event.outcome})" if event.outcome else "",
            )
        return event

    def _drain(self) -> None:
        while (line := self._readline()) is not None:
            self._take(line)
        self.run.output_ended = self.clock.now()

    def stop(self, timeout_s: float = 60.0) -> AttackRun:
        """Stop the binary at the end of the episode, wait for it to exit, and
        return what the session did."""
        if self._proc is None:
            return self.run
        with self._lock:
            self._stopping.set()
            self._next = None
            in_stage = self._in_stage
        if self._proc.poll() is None:
            self.run.stop_sent = self.clock.now()
            self.run.stop = "standard input closed"
            if in_stage and self.run.pid is not None:
                self.run.stop = "SIGTERM, then standard input closed"
                result = self.incus.exec(self.node, "kill", "-TERM", str(self.run.pid))
                if not result.ok:
                    self.run.problems.append(
                        f"kill -TERM {self.run.pid} failed: {result.stderr.strip()}"
                    )
            self._close_stdin()
        if self._thread is not None:
            self._thread.join(timeout_s)
            if self._thread.is_alive():
                self.run.problems.append(
                    f"the attack chain had not exited {timeout_s:g} s after the stop"
                )
        self._close_stdin()
        try:
            self.run.exit_status = self._proc.wait(timeout=10.0)
        except subprocess.TimeoutExpired:
            self.run.problems.append("the attack chain's session did not end")
        self.run.ended = self.clock.now()
        return self.run

    def _close_stdin(self) -> None:
        try:
            if self._stdin is not None and not self._stdin.closed:
                self._stdin.close()
        except OSError:
            pass
