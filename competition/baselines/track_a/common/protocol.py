"""The step protocol and the query interface, for the example Track A
defenders (episodes.md §7, step protocol; §8, query interface;
episode-runner-impl.md §6, step messages; §12, query interface).

The module uses the standard library alone, so that a defender's program
runs on the interpreter of siem-01's operational image. A defender's program
imports it from beside itself in the entry's storage volume, /var/lib/entry,
and from ../common in the repository.

The step session carries one JavaScript Object Notation (JSON) object on each
line. The episode runner writes a decision request at the start of each step
and the end notice after the last step; the defender answers each decision
request with one decision message. In Track A the decision message is
{"decision": "no action"} or {"decision": "alarm"}, and the alarm ends the
episode. A decision message that arrives after the end of its step counts for
the step during which it arrives, and only the first decision message of a
step counts. A defender that is late, with several decision requests waiting
on its standard input, therefore answers only the newest: StepSession.next
skips the older ones, whose steps take the default decision, no action.

In evaluation mode no step message carries a reward: `previous_reward` is
null in every decision request and in the end notice. In training mode the
decision request for step t carries the reward of step t - 1, 0 in Track A,
and the end notice carries Track A's R_n.

The query interface is the program siem-read, run once for each read. A read
names an output file, alerts or archives, may select by originating host and
input, and may give a start, an instant in Coordinated Universal Time (UTC).
It returns the records that the Wazuh manager has written since the previous
read with the same request. A request's first read starts at step 0 without
a start, and otherwise at the first record whose timestamp is at or after the
start, so that a start before step 0 reads the security monitoring's history.
A read before the first decision request fails, because the episode runner
records where the reads start just before it sends that request. Under
Competition Rule 3 an entry reads only the data that its telemetry
specification declares: Telemetry checks each request against the
specification before the read runs.

The environment variable SIEM_READ names the program in place of
/usr/local/bin/siem-read, as the offline replay does (replay.py).
"""

from __future__ import annotations

import json
import os
import select
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, BinaryIO

SIEM_READ = "/usr/local/bin/siem-read"
OUTPUTS = ("alerts", "archives")
# A read that takes longer than this has missed its step.
READ_TIMEOUT_S = 4.0
# The decisions, as a decision message writes them: the Track A alarm, and no
# action, the decision without an alarm in Track A and without a defensive
# action in Track B (episode-runner-impl.md §6, step messages).
ALARM = "alarm"
NO_ACTION = "no action"


@dataclass(frozen=True)
class Request:
    """A decision request, or the end notice when `ended` is true. The end
    notice's `step` is the number of steps that the episode ran. Its
    `previous_reward` is Track A's R_n in training mode, and None in
    evaluation mode, where no step message carries a reward."""

    track: str
    step: int
    previous_reward: float | None
    ended: bool


def parse_request(line: bytes | str) -> Request | None:
    """The step message on one line, or None for a line that is not one."""
    try:
        message = json.loads(line)
    except ValueError:
        return None
    if not isinstance(message, dict):
        return None
    try:
        return Request(
            track=str(message["track"]),
            step=int(message["step"]),
            previous_reward=message["previous_reward"],
            ended=bool(message["ended"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


class StepSession:
    """The defender's side of the step session: step messages in on a file
    descriptor, decision messages out on a binary stream."""

    def __init__(self, fd: int = 0, out: BinaryIO | None = None) -> None:
        self.fd = fd
        self.out = out if out is not None else sys.stdout.buffer
        self._buffer = b""
        self._eof = False
        # Decision requests skipped because a newer one was waiting.
        self.skipped = 0

    def _readable(self, timeout_s: float | None) -> bool:
        ready, _, _ = select.select([self.fd], [], [], timeout_s)
        return bool(ready)

    def _read(self) -> None:
        chunk = os.read(self.fd, 65536)
        if chunk:
            self._buffer += chunk
        else:
            self._eof = True

    def _lines(self) -> list[bytes]:
        *lines, self._buffer = self._buffer.split(b"\n")
        return [line for line in lines if line.strip()]

    def next(self) -> Request | None:
        """The next step message to act on: the end notice when it has
        arrived, and otherwise the newest decision request waiting. Blocks
        until a step message arrives, and returns None once the standard
        input has closed with no step message left."""
        waiting: list[Request] = []
        while True:
            for line in self._lines():
                request = parse_request(line)
                if request is None:
                    print(f"defender: not a step message: {line!r}", file=sys.stderr)
                elif request.ended:
                    self.skipped += len(waiting)
                    return request
                else:
                    waiting.append(request)
            if waiting:
                if self._eof or not self._readable(0):
                    self.skipped += len(waiting) - 1
                    return waiting[-1]
            elif self._eof:
                return None
            else:
                self._readable(None)
            self._read()

    def decide(self, request: Request, alarm: bool) -> None:
        """Write the decision message that answers a decision request: the
        Track A alarm, or no action, which is also every decision in another
        track, since this module makes no defensive action."""
        decision = ALARM if alarm and request.track == "A" else NO_ACTION
        self.out.write((json.dumps({"decision": decision}) + "\n").encode())
        self.out.flush()


def run(
    decide: Callable[[Request], bool],
    end: Callable[[Request], None] | None = None,
    session: StepSession | None = None,
) -> int:
    """The defender's loop. For each decision request, decide(request) says
    whether to raise the Track A alarm, and the loop writes the decision
    message. At the end notice, end(notice) runs and the loop returns 0, as
    it does when the standard input closes. Neither the loop nor the
    decisions read a step message's reward."""
    session = session if session is not None else StepSession()
    while True:
        request = session.next()
        if request is None:
            return 0
        if request.ended:
            if end is not None:
                end(request)
            return 0
        # Any fault of the policy answers no action, so that the step still
        # gets its decision and the defender keeps running.
        try:
            alarm = bool(decide(request))
        except Exception as error:  # noqa: BLE001
            print(f"defender: step {request.step}: {error!r}", file=sys.stderr)
            alarm = False
        try:
            session.decide(request, alarm)
        except BrokenPipeError:
            return 0


class Undeclared(Exception):
    """A read that the entry's telemetry specification does not declare."""


@dataclass(frozen=True)
class Telemetry:
    """An entry's telemetry specification: the reads that it declares. Each
    declared read names an output file, and optionally a host and an input; a
    declared read without a host or an input covers every host or input."""

    reads: tuple[tuple[str, str | None, str | None], ...]

    @classmethod
    def from_json(cls, document: dict[str, Any]) -> Telemetry:
        reads = []
        for read in document.get("reads", []):
            output = read["output"]
            if output not in OUTPUTS:
                raise ValueError(f"a declared read names {output!r}")
            reads.append((output, read.get("host"), read.get("input")))
        return cls(tuple(reads))

    @classmethod
    def load(cls, path: str) -> Telemetry:
        with open(path, encoding="utf-8") as f:
            return cls.from_json(json.load(f))

    def allows(self, output: str, host: str | None, location: str | None) -> bool:
        """Whether a request reads only declared data: a declared read covers
        the request when it names the same output file, and names either no
        host or the request's host, and either no input or the request's
        input."""
        return any(
            output == o and (h is None or h == host) and (i is None or i == location)
            for o, h, i in self.reads
        )


def read(
    output: str,
    host: str | None = None,
    location: str | None = None,
    telemetry: Telemetry | None = None,
    timeout_s: float = READ_TIMEOUT_S,
    *,
    since: str | None = None,
) -> list[dict[str, Any]]:
    """The records of one read through the query interface, each parsed from
    its line. With `since`, the request's start, an instant in UTC such as
    2026-11-12T20:15:05.250000Z, the request's first read starts at the first
    record whose timestamp is at or after that instant, and the request keeps
    a position of its own apart from the same request without a start. With
    a telemetry specification, a request that it does not declare raises
    Undeclared and runs nothing. A read that fails returns no records, with
    the cause on standard error, so that the step still gets its decision."""
    if telemetry is not None and not telemetry.allows(output, host, location):
        raise Undeclared(f"{output} host={host} input={location}")
    argv = [os.environ.get("SIEM_READ", SIEM_READ), output]
    if host is not None:
        argv += ["--host", host]
    if location is not None:
        argv += ["--input", location]
    if since is not None:
        argv += ["--since", since]
    try:
        done = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout_s,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        print(f"defender: siem-read {output}: {error}", file=sys.stderr)
        return []
    if done.returncode != 0:
        message = done.stderr.decode(errors="replace").strip()
        print(
            f"defender: siem-read {output}: status {done.returncode}: {message}",
            file=sys.stderr,
        )
        return []
    records = []
    for line in done.stdout.split(b"\n"):
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records
