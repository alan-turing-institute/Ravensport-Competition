"""The step schedule (episodes.md §7, step protocol).

The step session carries the step messages, each one JavaScript Object
Notation (JSON) object on one line. At the start of each step the episode runner
sends the defender a decision request. The first decision message with a valid
decision that arrives during a step is that step's decision. A decision message
that arrives after the end of the step whose decision request it answers
therefore counts for the step during which it arrives. Any further decision
message in the same step is dropped with a warning. A step without a decision
means no action, in both tracks. The end notice follows the episode's last step
and expects no decision message.

A decision message is a JSON object whose one key is `decision` and whose value
is a string. Track A's decisions are no action and the Track A alarm, written
`alarm`. Track B's are no action and the defensive actions, which await
specification, so every string but `alarm` passes in Track B. Any other line,
and a decision message whose decision is not one of the track's, is an invalid
line: the step schedule drops it with a warning and counts it, and it takes no
step's place (episode-runner-impl.md §6, step messages).

The schedule runs on a monotonic clock, in one order of events: it reads the
step session, runs timed events at their times, such as the scan requests of
episodes.md §6.2 (first occurrences), and releases the attack chain's stages
when they fall due. When a stage falls due, the decision messages that have
already arrived are read first, so a Track A alarm that falls due with a
release leaves the stage unreleased (episodes.md §10.4, the attack chain's
session).

An episode other than a suppressed episode ends at a Track A terminal state: at
the arrival of the Track A alarm, or when the attack chain's last stage falls
due, which the step schedule then never releases (episodes.md §11, horizon and
termination). A suppressed episode, a replay for Competition Rule 5, records
the alarm, carries none out and releases every stage (episodes.md §9,
suppressed episodes for Competition Rule 5).

Track A's reward is 0 at every step until the episode ends, and the final step
carries the episodic reward, which final_reward computes from the first alarm
and the number of stages released before it (episodes.md §7, step protocol).
In training mode the decision request for step t carries the reward of step
t - 1, and the end notice carries the last step's. In evaluation mode no step
message carries a reward, the end notice included, and the episode record
holds every step's reward in both modes.
"""

from __future__ import annotations

import json
import logging
import os
import select
import subprocess
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

# The step duration. The attack chain's session sets a stage's due time at
# the end event of the stage before, and the step schedule reads it when it
# next wakes, at the latest at the end of the step. The due time lies at least
# pacing.WAIT_LOWER_S after that end event, so no release is late while
# STEP_S is at most WAIT_LOWER_S; a longer step would need the session to wake
# the schedule.
STEP_S = 5.0
# The horizon of episodes.md §11 (horizon and termination): 144 steps of 5 s,
# 12 minutes.
HORIZON = 144
# The decision of a step without a decision message: no action, in both
# tracks (episodes.md §7, step protocol).
NO_DECISION = {"A": "no action", "B": "no action"}
# The Track A alarm's value in a decision message.
ALARM = "alarm"
TRACK_A_DECISIONS = frozenset({NO_DECISION["A"], ALARM})
# The causes of the two Track A terminal states, and of a simulator stop that
# the watch on the simulator's process sees.
TRACK_A_ALARM = "the Track A alarm"
LAST_STAGE = "the start of the attack chain's last stage"
SIMULATOR_EXITED = "the simulator's process exited"
# The cause of an episode whose simulator, as a check at the alarm finds, had
# stopped before the alarm arrived.
STOPPED_BEFORE_ALARM = "the simulator's stop, found at the Track A alarm"
DECISION_KEY = "decision"

log = logging.getLogger(__name__)


class Wake(Protocol):
    """A source that wakes the step schedule, such as the watch on the
    simulator's process. The source's file becomes readable when it fires."""

    def fileno(self) -> int: ...

    def fired(self) -> bool: ...


class Clock(Protocol):
    def now(self) -> float: ...

    def sleep_until(self, t: float, wake: Wake | None = None) -> None:
        """Sleep until t, or until wake fires."""


class Session(Protocol):
    """The defender's side of the step session (episodes.md §7, step
    protocol)."""

    def send(self, line: str) -> None:
        """Write one line; raises BrokenPipeError once the session has ended."""

    def receive(self, timeout_s: float, wake: Wake | None = None) -> str | None:
        """One line, or None when no line arrives within timeout_s or wake
        fires first; raises EOFError once the session has ended."""

    def arrived(self) -> list[str]:
        """Each line that has already arrived, without waiting; raises
        EOFError once the session has ended."""


class Stages(Protocol):
    """The attack chain's session, whose stages the step schedule releases
    (episodes.md §10.4, the attack chain's session)."""

    @property
    def total(self) -> int:
        """The chain's number of stages, K in Track A's reward."""

    @property
    def released(self) -> int:
        """The number of stages released so far."""

    def next_stage(self) -> tuple[int, float] | None:
        """The next stage's number and the time at which it falls due, once
        the session knows them."""

    def release(self, number: int) -> bool:
        """Release stage `number`, and return whether it was released."""


@dataclass
class Event:
    """An action that the step schedule runs at a time on its clock."""

    at: float
    action: Callable[[], None]


@dataclass
class Step:
    step: int
    start: float
    end: float
    request_sent: float | None = None
    decision: str = ""
    decision_arrival: float | None = None
    dropped_decision_messages: int = 0
    invalid_lines: int = 0
    reward: float | None = None

    def as_line(self, at: Callable[[float], Any] = lambda t: t) -> dict[str, Any]:
        """The step's line of steps.jsonl (episodes.md §13, episode record),
        with each time passed through `at`, such as a conversion to Coordinated
        Universal Time (UTC)."""

        def time_or_none(t: float | None) -> Any:
            return None if t is None else at(t)

        return {
            "step": self.step,
            "start": at(self.start),
            "end": at(self.end),
            "request_sent": time_or_none(self.request_sent),
            "decision": self.decision,
            "decision_arrival": time_or_none(self.decision_arrival),
            "dropped_decision_messages": self.dropped_decision_messages,
            "invalid_lines": self.invalid_lines,
            "reward": self.reward,
        }


@dataclass
class StepsResult:
    steps: list[Step]
    end_notice_sent: float | None = None
    session_ended: float | None = None
    events_run: list[float] = field(default_factory=list)
    ended_early: float | None = None
    end_cause: str | None = None
    # The Track A terminal state that ended the episode, when one did: the
    # alarm or the start of the attack chain's last stage. A simulator stop
    # leaves it None.
    terminal_state: str | None = None
    # The episode's first Track A alarm that was a step's decision: its step,
    # its arrival, and the number of the attack chain's stages released before
    # it, or None without an attack chain. A suppressed episode keeps running
    # after it.
    alarm_step: int | None = None
    alarm_arrival: float | None = None
    released_before_alarm: int | None = None
    # The last step's reward: in Track A the episodic reward, R_n, which the
    # end notice carries in training mode alone.
    final_reward: float | None = None


class EndEpisode(Exception):
    """Raised at a terminal state, such as a stopped simulator, to end the
    episode at once (episodes.md §11, horizon and termination). The step in
    progress is the episode's last step. `at` is the end's time on the step
    schedule's clock when it precedes the moment of raising, as the Track A
    alarm's arrival does. `terminal` marks a Track A terminal state, as
    against a simulator stop."""

    def __init__(
        self, cause: str, at: float | None = None, terminal: bool = False
    ) -> None:
        super().__init__(cause)
        self.at = at
        self.terminal = terminal


def _step_message(
    track: str, step: int, previous_reward: float | None, ended: bool
) -> str:
    return (
        json.dumps(
            {
                "track": track,
                "step": step,
                "previous_reward": previous_reward,
                "ended": ended,
            }
        )
        + "\n"
    )


def decision_request(track: str, step: int, previous_reward: float | None) -> str:
    """The decision request at the start of a step (episodes.md §7, step
    protocol)."""
    return _step_message(track, step, previous_reward, ended=False)


def end_notice(track: str, steps_run: int, previous_reward: float | None) -> str:
    """The end notice after the episode's last step, whose step is the number
    of steps that the episode ran (episodes.md §7, step protocol)."""
    return _step_message(track, steps_run, previous_reward, ended=True)


def decision_of(line: str) -> str | None:
    """The decision of a decision message, a JSON object whose one key is
    `decision` and whose value is a string, or None for any other line."""
    try:
        message = json.loads(line)
    except ValueError:
        return None
    if not isinstance(message, dict) or list(message) != [DECISION_KEY]:
        return None
    decision = message[DECISION_KEY]
    return decision if isinstance(decision, str) else None


def valid_decision(track: str, decision: str) -> bool:
    """Whether a decision is one of the track's decisions. Track A has two,
    no action and the alarm. Track B's defensive actions await specification,
    so every string but the alarm passes."""
    if track == "A":
        return decision in TRACK_A_DECISIONS
    return decision != ALARM


def no_reward(step: int) -> float | None:
    """The reward of a step in Track B in the first version, which leaves
    the ground-truth evaluator unspecified: null."""
    return None


def track_a_reward(step: int) -> float:
    """Track A's reward at every step until the episode ends: 0. The final
    step carries the episodic reward instead (episodes.md §7, step
    protocol)."""
    return 0.0


# Each track's reward of a step before the episode's end.
STEP_REWARD: dict[str, Callable[[int], float | None]] = {
    "A": track_a_reward,
    "B": no_reward,
}


def run_steps(
    session: Session,
    clock: Clock,
    track: str,
    t0: float,
    horizon: int = HORIZON,
    step_s: float = STEP_S,
    events: Iterable[Event] = (),
    stop: Wake | None = None,
    *,
    stages: Stages | None = None,
    suppressed: bool = False,
    training: bool = False,
    reward: Callable[[int], float | None] | None = None,
    final_reward: Callable[[StepsResult], float | None] | None = None,
    stopped_before: Callable[[float], float | None] | None = None,
) -> StepsResult:
    """Run steps 0 to horizon - 1 from t0, then send the end notice, and
    release each of the attack chain's stages, given as stages, when it falls
    due. The episode ends early when stop fires, or, unless suppressed, at a
    Track A terminal state: the alarm's arrival, or the time at which the last
    stage falls due, which is then never released.

    At the alarm, before the end notice, stopped_before gives the time of a
    simulator stop that came before the alarm's arrival, if it finds one. The
    episode then ends at that stop, and the alarm after it no longer counts,
    so that the episodic reward and the episode record agree (episodes.md
    §11, horizon and termination).

    reward gives each step's reward once the step has ended: by default 0 in
    Track A, and null in Track B, whose ground-truth evaluator the first
    version leaves unspecified. The last step's reward is final_reward's
    value where final_reward is given, the episodic reward in Track A. Each
    step's line of the episode record holds its reward in both modes. In
    training mode the decision request for step t carries the reward of step
    t - 1, and the end notice the last step's reward. In evaluation mode no
    step message carries a reward, the end notice included, so that nothing
    the defender receives in an evaluation episode depends on the ground truth
    through a reward (episodes.md §7, step protocol; episode-runner-impl.md
    §6, step messages)."""
    steps = [Step(k, t0 + k * step_s, t0 + (k + 1) * step_s) for k in range(horizon)]
    pending: list[Event] = sorted(events, key=lambda e: e.at)
    result = StepsResult(steps)
    alive = True
    reward = reward or STEP_REWARD[track]
    ends_at_last_stage = track == "A" and not suppressed

    def place(line: str, arrival: float) -> None:
        index = int((arrival - t0) // step_s)
        if not 0 <= index < horizon:
            log.warning(
                "dropped a decision message that arrived outside every step: %r", line
            )
            return
        step = steps[index]
        decision = decision_of(line)
        if decision is None or not valid_decision(track, decision):
            step.invalid_lines += 1
            log.warning(
                "dropped an invalid line for Track %s in step %d: %r",
                track,
                index,
                line,
            )
            return
        if step.decision_arrival is not None:
            step.dropped_decision_messages += 1
            log.warning(
                "dropped a further decision message in step %d: %r", index, line
            )
            return
        step.decision, step.decision_arrival = decision, arrival
        if decision == ALARM and result.alarm_arrival is None:
            result.alarm_step, result.alarm_arrival = index, arrival
            if stages is not None:
                result.released_before_alarm = stages.released
            if not suppressed:
                # No stage is released after the alarm (episodes.md §10.4,
                # the attack chain's session).
                raise EndEpisode(TRACK_A_ALARM, at=arrival, terminal=True)

    def read_arrived() -> None:
        """Read each decision message that has already arrived."""
        nonlocal alive
        if not alive:
            return
        try:
            lines = session.arrived()
        except EOFError:
            alive = False
            result.session_ended = clock.now()
            return
        arrival = clock.now()
        for line in lines:
            place(line, arrival)

    def wait_until(t: float) -> None:
        """Wait until t. Until then, read each decision message for as long
        as the step session lasts, run each timed event at its time, and
        release each stage when it falls due."""
        nonlocal alive
        while True:
            while pending and pending[0].at <= clock.now():
                event = pending.pop(0)
                event.action()
                result.events_run.append(clock.now())
            if stop is not None and stop.fired():
                raise EndEpisode(SIMULATOR_EXITED)
            now = clock.now()
            due = None if stages is None else stages.next_stage()
            if due is not None and now >= due[1]:
                # A decision message that has arrived comes first, so an alarm
                # that falls due with the stage leaves it unreleased.
                read_arrived()
                if ends_at_last_stage and due[0] >= stages.total:
                    raise EndEpisode(LAST_STAGE, terminal=True)
                stages.release(due[0])
                continue
            if now >= t:
                return
            deadline = min(t, pending[0].at) if pending else t
            if due is not None:
                deadline = min(deadline, due[1])
            if not alive:
                clock.sleep_until(deadline, wake=stop)
                continue
            try:
                line = session.receive(deadline - now, wake=stop)
            except EOFError:
                alive = False
                result.session_ended = clock.now()
                continue
            if line is not None:
                place(line, clock.now())

    def send(line: str) -> float | None:
        nonlocal alive
        try:
            session.send(line)
        except (BrokenPipeError, OSError):
            alive = False
            result.session_ended = clock.now()
            return None
        return clock.now()

    try:
        for step in steps:
            wait_until(step.start)
            if alive:
                previous = (
                    steps[step.step - 1].reward if training and step.step > 0 else None
                )
                step.request_sent = send(decision_request(track, step.step, previous))
            wait_until(step.end)
            step.reward = reward(step.step)
    except EndEpisode as end:
        result.ended_early = end.at if end.at is not None else clock.now()
        result.end_cause = str(end)
        if end.terminal:
            result.terminal_state = str(end)
        stop = None
        if str(end) == TRACK_A_ALARM and stopped_before is not None:
            stop = stopped_before(result.alarm_arrival)
        if stop is not None:
            log.warning("the simulator had stopped before the Track A alarm arrived")
            result.ended_early, result.end_cause = stop, STOPPED_BEFORE_ALARM
            result.terminal_state = None
            result.alarm_step = result.alarm_arrival = None
            result.released_before_alarm = None
            steps[:] = cut_at(steps, stop, track)
        else:
            # The steps up to the one that holds the end, which ends there. An
            # alarm read at or after a step's boundary, before the step's
            # decision request went out, belongs to that step, as place gives
            # it, and the step is the episode's last.
            held = int((result.ended_early - t0) // step_s) + 1
            del steps[max(0, held) :]
            if steps:
                steps[-1].end = result.ended_early
        if steps:
            # The episode's last step, which the end cut short.
            steps[-1].reward = reward(steps[-1].step)
    for step in steps:
        if step.decision_arrival is None:
            step.decision = NO_DECISION[track]
    if steps:
        if final_reward is not None:
            # The episodic reward.
            steps[-1].reward = final_reward(result)
        result.final_reward = steps[-1].reward
    if alive:
        carried = result.final_reward if training else None
        result.end_notice_sent = send(end_notice(track, len(steps), carried))
    return result


def cut_at(steps: list[Step], stop: float, track: str) -> list[Step]:
    """The steps that began before the simulator stopped at stop, on the
    episode runner's clock. The last of these steps ends at the stop. A
    decision message that arrived after the stop no longer counts, so that
    step's decision becomes the default and the message counts as dropped."""
    kept = [step for step in steps if step.start < stop]
    if kept:
        last = kept[-1]
        last.end = min(last.end, stop)
        if last.decision_arrival is not None and last.decision_arrival > stop:
            last.decision = NO_DECISION[track]
            last.decision_arrival = None
            last.dropped_decision_messages += 1
            log.warning("dropped a decision message that arrived after the stop")
    return kept


class MonotonicClock:
    """The episode runner's monotonic clock. The clock's times convert to UTC
    on the clock of the virtual machine, which every container shares, by the
    offset between the two clocks at the clock's creation."""

    def __init__(self) -> None:
        self._offset = time.time() - time.monotonic()

    def now(self) -> float:
        return time.monotonic()

    def utc(self, t: float) -> datetime:
        return datetime.fromtimestamp(t + self._offset, timezone.utc)

    def monotonic(self, at: datetime) -> float:
        return at.timestamp() - self._offset

    def sleep_until(self, t: float, wake: Wake | None = None) -> None:
        delay = t - time.monotonic()
        if delay > 0:
            if wake is None:
                time.sleep(delay)
            else:
                select.select([wake], [], [], delay)


class ProcessSession:
    """A defender that runs as a process, such as
    incus exec siem-01 -- /usr/local/bin/defender, whose standard input
    and output carry the step messages."""

    def __init__(self, argv: Sequence[str]) -> None:
        self.proc = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE
        )
        self._buffer = b""
        self._eof = False

    def send(self, line: str) -> None:
        try:
            self.proc.stdin.write(line.encode())
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as error:
            raise BrokenPipeError(str(error)) from error

    def receive(self, timeout_s: float, wake: Wake | None = None) -> str | None:
        deadline = time.monotonic() + timeout_s
        watched = [self.proc.stdout] + ([wake] if wake is not None else [])
        while True:
            if b"\n" in self._buffer:
                line, self._buffer = self._buffer.split(b"\n", 1)
                return line.decode(errors="replace") + "\n"
            if self._eof:
                raise EOFError
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            ready, _, _ = select.select(watched, [], [], remaining)
            if not ready or self.proc.stdout not in ready:
                return None
            self._read()

    def arrived(self) -> list[str]:
        """Each line that has already arrived, from one read of what the pipe
        holds, so that a defender that writes without pause cannot hold the
        step schedule here; raises EOFError once the session has ended and
        every line has been taken."""
        if not self._eof:
            ready, _, _ = select.select([self.proc.stdout], [], [], 0)
            if ready:
                self._read()
        *lines, self._buffer = self._buffer.split(b"\n")
        if not lines and self._eof:
            raise EOFError
        return [line.decode(errors="replace") + "\n" for line in lines]

    def _read(self) -> None:
        chunk = os.read(self.proc.stdout.fileno(), 65536)
        if chunk:
            self._buffer += chunk
        else:
            self._eof = True

    def close(self, timeout_s: float = 5.0) -> int | None:
        """End the session and return the process's exit status."""
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            return self.proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            return self.proc.wait()
