"""The step schedule (episodes.md §7, step protocol; episodes-tests.md §5,
step protocol).

A fake clock and a scripted defender drive the step schedule. The fake clock
advances only when the step schedule waits, so every time below is exact.

Run with: pytest competition/tests -q
"""

from __future__ import annotations

import json
import logging
import os
import select
import sys
import time
from collections.abc import Callable
from datetime import datetime, timezone

import pytest

from competition.episode_runner import steps

T0 = 1000.0


class FakeClock:
    def __init__(self, now: float = T0 - 1.0) -> None:
        self.t = now

    def now(self) -> float:
        return self.t

    def sleep_until(self, t: float, wake=None) -> None:
        if wake is not None and wake.at < t:
            t = wake.at
        self.t = max(self.t, t)


class FakeWake:
    """A stop that fires at a time on the fake clock."""

    def __init__(self, clock: FakeClock, at: float) -> None:
        self.clock, self.at = clock, at

    def fileno(self) -> int:
        raise AssertionError("the fake clock waits for the stop without a file")

    def fired(self) -> bool:
        return self.clock.now() >= self.at


class Raw(str):
    """A line that the scripted defender writes as it stands, in place of a
    decision message."""


class ScriptedSession:
    """A defender whose decision messages follow a script.

    The responder receives each decision request and the end notice, decoded,
    and returns the decision messages to it as (delay in seconds, decision)
    pairs. A decision of None ends the session at that position, in place of a
    decision message, and a Raw decision is written as it stands.
    """

    def __init__(
        self,
        clock: FakeClock,
        responder: Callable[[dict], list[tuple[float, str | None]]],
    ) -> None:
        self.clock = clock
        self.responder = responder
        self.sent: list[dict] = []
        self.pending: list[tuple[float, str | None]] = []
        self.closed = False
        self.waits, self.last_wait = 0, None

    def send(self, line: str) -> None:
        if self.closed:
            raise BrokenPipeError
        message = json.loads(line)
        self.sent.append(message)
        for delay, decision in self.responder(message):
            self.pending.append((self.clock.now() + delay, decision))
        self.pending.sort(key=lambda item: item[0])

    def receive(self, timeout_s: float, wake=None) -> str | None:
        # A step schedule that ignores a stop that has fired would wait here
        # without end, at one time on the fake clock.
        self.waits = self.waits + 1 if self.clock.now() == self.last_wait else 0
        self.last_wait = self.clock.now()
        assert self.waits < 1000, "the step schedule waits without end"
        deadline = self.clock.now() + timeout_s
        if wake is not None and wake.at <= deadline:
            if not self.pending or wake.at < self.pending[0][0]:
                self.clock.sleep_until(wake.at)
                return None
        if self.pending and self.pending[0][0] <= deadline:
            at, decision = self.pending.pop(0)
            self.clock.sleep_until(at)
            if decision is None:
                self.closed = True
                raise EOFError
            return self.line(decision)
        self.clock.sleep_until(deadline)
        return None

    def arrived(self) -> list[str]:
        lines = []
        while self.pending and self.pending[0][0] <= self.clock.now():
            if self.pending[0][1] is None:
                if lines:
                    return lines
                self.pending.pop(0)
                self.closed = True
                raise EOFError
            lines.append(self.line(self.pending.pop(0)[1]))
        return lines

    @staticmethod
    def line(decision: str) -> str:
        if isinstance(decision, Raw):
            return str(decision)
        return json.dumps({"decision": decision}) + "\n"


class FakeStages:
    """An attack chain's session on the fake clock. The first stage falls due
    its wait after `start`, the onset, and each later stage its wait after the
    previous stage's end, `run_s` after its release. Each due time is known
    only from the start of its wait."""

    def __init__(
        self, clock: FakeClock, start: float, waits: list[float], run_s: float = 1.0
    ) -> None:
        self.clock, self.start, self.waits, self.run_s = clock, start, waits, run_s
        self.releases: list[float] = []

    @property
    def total(self) -> int:
        return len(self.waits)

    @property
    def released(self) -> int:
        return len(self.releases)

    def next_stage(self) -> tuple[int, float] | None:
        n = len(self.releases)
        wait_start = self.start if n == 0 else self.releases[-1] + self.run_s
        if n >= self.total or self.clock.now() < wait_start:
            return None
        return n + 1, wait_start + self.waits[n]

    def release(self, number: int) -> bool:
        assert number == len(self.releases) + 1, "a stage released out of turn"
        assert self.clock.now() >= self.next_stage()[1], "a stage released early"
        self.releases.append(self.clock.now())
        return True


def idle(track: str) -> Callable[[dict], list[tuple[float, str | None]]]:
    decision = steps.NO_DECISION[track]
    return lambda message: [] if message["ended"] else [(0.01, decision)]


def run(responder, track: str = "A", horizon: int = 10, events=()):
    clock = FakeClock()
    session = ScriptedSession(clock, responder)
    result = steps.run_steps(session, clock, track, T0, horizon=horizon, events=events)
    return result, session


def test_step_boundaries_follow_the_step_duration() -> None:
    result, session = run(idle("A"))
    assert [s.start for s in result.steps] == [T0 + 5.0 * k for k in range(10)]
    assert [s.request_sent for s in result.steps] == [T0 + 5.0 * k for k in range(10)]
    assert [n["step"] for n in session.sent] == list(range(11))


def test_every_step_records_a_decision_and_track_a_rewards_are_0() -> None:
    """Track A's reward is 0 at every step until the episode ends. In training
    mode the decision request for step t carries step t - 1's, null at step
    0. With no final_reward, the last step's reward is 0 too, and the end
    notice carries it."""
    clock = FakeClock()
    session = ScriptedSession(clock, idle("A"))
    result = steps.run_steps(session, clock, "A", T0, horizon=120, training=True)
    assert all(s.decision == "no action" for s in result.steps)
    assert all(
        s.decision_arrival == pytest.approx(s.start + 0.01) for s in result.steps
    )
    assert all(s.dropped_decision_messages == 0 for s in result.steps)
    assert session.sent[0]["previous_reward"] is None
    assert all(n["previous_reward"] == 0.0 for n in session.sent[1:-1])
    assert [s.reward for s in result.steps] == [0.0] * 120
    final = session.sent[-1]
    assert final == {"track": "A", "step": 120, "previous_reward": 0.0, "ended": True}
    assert all(not n["ended"] for n in session.sent[:-1])
    assert result.end_notice_sent == T0 + 600.0
    assert result.session_ended is None


def test_track_b_rewards_stay_null() -> None:
    result, session = run(idle("B"), track="B")
    assert all(n["previous_reward"] is None for n in session.sent)
    assert all(s.reward is None for s in result.steps)


def rewards(step: int) -> float:
    """A stand-in for the ground-truth evaluator: the reward of step k is k."""
    return float(step)


def test_the_step_messages_carry_the_previous_reward_and_the_end() -> None:
    """In training mode the decision request for step t carries the reward of
    step t - 1, and the end notice carries the last step's reward and says
    that the episode has ended (episodes.md §7, step protocol)."""
    clock = FakeClock()
    session = ScriptedSession(clock, idle("A"))
    result = steps.run_steps(
        session, clock, "A", T0, horizon=10, training=True, reward=rewards
    )
    requests, notice = session.sent[:-1], session.sent[-1]
    assert [n["previous_reward"] for n in requests] == [None] + [
        float(k) for k in range(9)
    ]
    assert not any(n["ended"] for n in requests)
    assert notice == {"track": "A", "step": 10, "previous_reward": 9.0, "ended": True}
    assert [s.reward for s in result.steps] == [float(k) for k in range(10)]


def test_evaluation_step_messages_carry_no_reward() -> None:
    """In evaluation mode every step message carries a previous_reward of
    null, the end notice included, and each step's line of the record still
    holds the step's reward. So does the end notice after a terminal state in
    step 6 (episodes.md §7, step protocol)."""
    clock = FakeClock()
    session = ScriptedSession(clock, idle("A"))
    result = steps.run_steps(session, clock, "A", T0, horizon=10, reward=rewards)
    assert all(n["previous_reward"] is None for n in session.sent)
    assert session.sent[-1] == {
        "track": "A",
        "step": 10,
        "previous_reward": None,
        "ended": True,
    }
    assert [s.as_line()["reward"] for s in result.steps] == [
        float(k) for k in range(10)
    ]

    def stopped() -> None:
        raise steps.EndEpisode("the simulator's service is crashed")

    clock = FakeClock()
    session = ScriptedSession(clock, idle("A"))
    result = steps.run_steps(
        session,
        clock,
        "A",
        T0,
        horizon=10,
        events=[steps.Event(T0 + 32.5, stopped)],
        reward=rewards,
    )
    assert all(n["previous_reward"] is None for n in session.sent)
    assert session.sent[-1]["step"] == 7 and session.sent[-1]["ended"]
    assert result.steps[-1].reward == 6.0


def test_a_missing_decision_means_no_action() -> None:
    """A step that ends before any decision message arrives records no
    action, in both tracks (episodes.md §7, step protocol)."""
    for track in ("A", "B"):
        result, _ = run(lambda message: [], track=track)
        assert all(s.decision == "no action" for s in result.steps)
        assert all(s.decision_arrival is None for s in result.steps)


def test_a_slow_decision_message_counts_for_the_step_during_which_it_arrives() -> None:
    def responder(message):
        if message["ended"] or message["step"] == 4:
            return []
        if message["step"] == 3:
            return [(6.0, "slow decision for step 3")]
        return [(0.01, "no action")]

    # Track B, whose decisions the Track B action suite defines, so that any
    # value but the alarm passes.
    result, _ = run(responder, track="B")
    assert result.steps[3].decision == "no action"
    assert result.steps[3].decision_arrival is None
    assert result.steps[4].decision == "slow decision for step 3"
    assert result.steps[4].decision_arrival == pytest.approx(T0 + 21.0)


def test_a_second_decision_message_in_a_step_is_dropped(caplog) -> None:
    def responder(message):
        if message["ended"]:
            return []
        if message["step"] == 5:
            return [(0.1, "first"), (0.2, "second")]
        return [(0.01, "no action")]

    with caplog.at_level(logging.WARNING, logger=steps.__name__):
        result, _ = run(responder, track="B")
    assert result.steps[5].decision == "first"
    assert result.steps[5].dropped_decision_messages == 1
    assert sum(s.dropped_decision_messages for s in result.steps) == 1
    assert any("step 5" in r.getMessage() for r in caplog.records)


def test_a_defender_that_exits_early_leaves_its_remaining_steps_without_a_decision() -> (
    None
):
    def responder(message):
        if message["step"] == 6:
            return [(0.5, None)]
        return [] if message["ended"] else [(0.01, "no action")]

    clock = FakeClock()
    ran: list[float] = []
    # A timed event inside step 8, after the end of the step session, such as
    # a scan request.
    events = [steps.Event(T0 + 42.5, lambda: ran.append(clock.now()))]
    session = ScriptedSession(clock, responder)
    result = steps.run_steps(session, clock, "A", T0, horizon=10, events=events)
    assert result.session_ended == pytest.approx(T0 + 30.5)
    assert [s.decision_arrival is not None for s in result.steps] == [True] * 6 + [
        False
    ] * 4
    assert all(s.decision == "no action" for s in result.steps)
    # No step message follows the end of the step session.
    assert [n["step"] for n in session.sent] == list(range(7))
    assert result.end_notice_sent is None
    # The timed event still runs at its time, and the step schedule still runs
    # to the horizon, the end of step 9.
    assert ran == [T0 + 42.5]
    assert clock.now() == T0 + 50.0


def test_timed_events_run_at_their_times() -> None:
    clock = FakeClock()
    ran: list[tuple[str, float]] = []
    events = [
        steps.Event(T0 + 12.5, lambda: ran.append(("scan request", clock.now()))),
        steps.Event(T0 + 40.0, lambda: ran.append(("late scan request", clock.now()))),
    ]
    session = ScriptedSession(clock, idle("A"))
    result = steps.run_steps(session, clock, "A", T0, horizon=10, events=events)
    assert ran == [("scan request", T0 + 12.5), ("late scan request", T0 + 40.0)]
    assert all(s.decision == "no action" for s in result.steps)


def test_a_terminal_state_ends_the_episode_with_the_number_of_steps_run() -> None:
    """A timed event that finds a terminal state, such as a stopped simulator
    in the middle of step 6, ends the episode at once (episodes.md §11, horizon
    and termination)."""
    clock = FakeClock()
    ran: list[float] = []

    def stopped() -> None:
        raise steps.EndEpisode("the simulator's service is crashed")

    events = [
        steps.Event(T0 + 32.5, stopped),
        steps.Event(T0 + 40.0, lambda: ran.append(clock.now())),
    ]
    session = ScriptedSession(clock, idle("A"))
    result = steps.run_steps(session, clock, "A", T0, horizon=10, events=events)
    assert result.ended_early == T0 + 32.5
    assert result.end_cause == "the simulator's service is crashed"
    assert [s.step for s in result.steps] == list(range(7))
    assert result.steps[-1].end == T0 + 32.5
    assert session.sent[-1] == {
        "track": "A",
        "step": 7,
        "previous_reward": None,
        "ended": True,
    }
    assert result.end_notice_sent == T0 + 32.5
    assert ran == []


def test_the_monotonic_clock_converts_its_times_to_utc() -> None:
    clock = steps.MonotonicClock()
    now = clock.now()
    at = clock.utc(now)
    assert abs((at - datetime.now(timezone.utc)).total_seconds()) < 1.0
    assert clock.monotonic(at) == pytest.approx(now, abs=1e-5)
    assert (clock.utc(now + 2.5) - at).total_seconds() == pytest.approx(2.5, abs=1e-5)


INVALID_IN_TRACK_A = [
    '{"decision": "continue"}',
    # A test defensive action.
    '{"decision": "isolate fms-01"}',
    '{"decision": "continue", "step": 3}',
    '{"decision": 3}',
    '{"decision": null}',
    '["decision", "continue"]',
    "not json",
    "",
]


def test_a_decision_message_is_valid_only_for_its_track() -> None:
    """episodes-tests.md, a_decision_message_is_valid_only_for_its_track."""
    assert steps.decision_of('{"decision": "no action"}\n') == "no action"
    assert steps.decision_of('{"decision": "alarm"}') == "alarm"
    assert steps.valid_decision("A", "no action")
    assert steps.valid_decision("A", "alarm")
    for line in INVALID_IN_TRACK_A:
        decision = steps.decision_of(line)
        assert decision is None or not steps.valid_decision("A", decision), line
    assert steps.valid_decision("B", "no action")
    assert not steps.valid_decision("B", "alarm")


def test_each_invalid_line_is_counted_with_a_warning(caplog) -> None:
    def responder(message: dict) -> list[tuple[float, str | None]]:
        if message["ended"]:
            return []
        if message["step"] == 2:
            return [
                (0.1 + 0.01 * n, Raw(line + "\n"))
                for n, line in enumerate(INVALID_IN_TRACK_A)
            ]
        return [(0.01, "no action")]

    with caplog.at_level(logging.WARNING, logger=steps.__name__):
        result, _ = run(responder, horizon=5)
    assert result.steps[2].invalid_lines == len(INVALID_IN_TRACK_A)
    assert result.steps[2].decision_arrival is None
    assert result.steps[2].decision == "no action"
    assert result.steps[2].dropped_decision_messages == 0
    warnings = [r for r in caplog.records if "invalid line" in r.getMessage()]
    assert len(warnings) == len(INVALID_IN_TRACK_A)


def test_an_invalid_line_takes_no_steps_place() -> None:
    """episodes-tests.md, an_invalid_line_takes_no_steps_place."""

    def responder(message: dict) -> list[tuple[float, str | None]]:
        if message["ended"]:
            return []
        if message["step"] == 2:
            return [(0.1, Raw("not json\n")), (0.3, "alarm")]
        return [(0.01, "no action")]

    result, session = run(responder, horizon=5)
    assert result.steps[-1].step == 2
    assert result.steps[2].decision == "alarm"
    assert result.steps[2].invalid_lines == 1
    assert result.steps[2].dropped_decision_messages == 0
    assert result.alarm_arrival == pytest.approx(T0 + 10.3)


def test_a_step_line_holds_the_record_of_the_step() -> None:
    result, _ = run(idle("B"), track="B", horizon=2)
    assert result.steps[0].as_line() == {
        "step": 0,
        "start": T0,
        "end": T0 + 5.0,
        "request_sent": T0,
        "decision": "no action",
        "decision_arrival": pytest.approx(T0 + 0.01),
        "dropped_decision_messages": 0,
        "invalid_lines": 0,
        "reward": None,
    }


def alarm_at(step: int, delay: float = 0.4) -> Callable[[dict], list]:
    """A defender that takes no action, and returns the Track A alarm
    `delay` seconds into step `step`."""

    def responder(message: dict) -> list[tuple[float, str | None]]:
        if message["ended"]:
            return []
        if message["step"] == step:
            return [(delay, "alarm")]
        return [(0.01, "no action")]

    return responder


def test_the_track_a_alarm_ends_the_episode_at_its_arrival() -> None:
    """The alarm, step 6's decision, ends the episode at once, and the end
    notice carries final_reward's value as the last step's reward, with the
    alarm's step plus 1 as its step (episodes.md §11, horizon and
    termination)."""
    clock = FakeClock()
    session = ScriptedSession(clock, alarm_at(6))
    seen: list[steps.StepsResult] = []

    def reward(result: steps.StepsResult) -> float:
        seen.append(result)
        return -0.5

    result = steps.run_steps(
        session, clock, "A", T0, horizon=10, training=True, final_reward=reward
    )
    assert result.alarm_step == 6
    assert result.released_before_alarm is None
    assert result.alarm_arrival == T0 + 30.4
    assert result.terminal_state == steps.TRACK_A_ALARM
    assert result.end_cause == steps.TRACK_A_ALARM
    assert result.ended_early == T0 + 30.4
    assert [s.step for s in result.steps] == list(range(7))
    assert result.steps[-1].decision == "alarm"
    assert result.steps[-1].end == T0 + 30.4
    assert [s.reward for s in result.steps] == [0.0] * 6 + [-0.5]
    assert seen == [result]
    assert session.sent[-1] == {
        "track": "A",
        "step": 7,
        "previous_reward": -0.5,
        "ended": True,
    }
    assert result.final_reward == -0.5
    assert result.end_notice_sent == T0 + 30.4


def test_a_suppressed_episode_records_the_alarm_and_runs_on() -> None:
    """In a replay for Competition Rule 5 the alarm is recorded, the episode
    runs to its horizon, and every stage, the last too, is released
    (episodes.md §9, suppressed episodes for Competition Rule 5)."""

    def responder(message: dict) -> list[tuple[float, str | None]]:
        if message["ended"]:
            return []
        if message["step"] in (3, 5):
            return [(0.2, "alarm")]
        return [(0.01, "no action")]

    clock = FakeClock()
    session = ScriptedSession(clock, responder)
    stages = FakeStages(clock, T0 + 2.0, [6.0, 6.0, 6.0])
    result = steps.run_steps(
        session,
        clock,
        "A",
        T0,
        horizon=10,
        stages=stages,
        suppressed=True,
        training=True,
        final_reward=lambda r: -1.0,
    )
    assert len(result.steps) == 10
    assert result.ended_early is None
    assert result.terminal_state is None
    # The first alarm is the one that counts, after the first two releases.
    assert (result.alarm_step, result.alarm_arrival) == (3, T0 + 15.2)
    assert stages.releases == [T0 + 8.0, T0 + 15.0, T0 + 22.0]
    assert result.released_before_alarm == 2
    assert [s.decision for s in result.steps].count("alarm") == 2
    assert session.sent[-1]["previous_reward"] == -1.0


def test_the_alarm_is_no_decision_in_track_b() -> None:
    result, _ = run(alarm_at(4), track="B", horizon=8)
    assert len(result.steps) == 8
    assert result.alarm_step is None
    assert result.steps[4].invalid_lines == 1
    assert result.steps[4].decision == "no action"


def test_each_stage_is_released_when_it_falls_due() -> None:
    """The first stage falls due its wait after the onset, here inside step 1,
    and each later one its wait after the end of the stage before."""
    clock = FakeClock()
    session = ScriptedSession(clock, idle("B"))
    stages = FakeStages(clock, T0 + 5.0, [5.5, 7.25, 6.0], run_s=2.0)
    result = steps.run_steps(session, clock, "B", T0, horizon=10, stages=stages)
    assert stages.releases == [T0 + 10.5, T0 + 19.75, T0 + 27.75]
    assert len(result.steps) == 10
    assert result.terminal_state is None


def test_the_last_stage_ends_a_track_a_episode_when_it_falls_due() -> None:
    """In Track A the episode ends at the time at which the last stage falls
    due, and the stage is never released (episodes-tests.md,
    a_track_a_episode_ends_at_the_start_of_the_last_stage)."""
    clock = FakeClock()
    session = ScriptedSession(clock, idle("A"))
    stages = FakeStages(clock, T0 + 5.0, [5.5, 7.25, 6.0], run_s=2.0)
    result = steps.run_steps(
        session,
        clock,
        "A",
        T0,
        horizon=10,
        stages=stages,
        training=True,
        final_reward=lambda r: -1.0,
    )
    assert stages.releases == [T0 + 10.5, T0 + 19.75]
    assert result.terminal_state == steps.LAST_STAGE
    assert result.end_cause == steps.LAST_STAGE
    assert result.ended_early == T0 + 27.75
    assert [s.step for s in result.steps] == list(range(6))
    assert result.steps[-1].end == T0 + 27.75
    assert result.alarm_step is None
    assert session.sent[-1] == {
        "track": "A",
        "step": 6,
        "previous_reward": -1.0,
        "ended": True,
    }


def test_an_alarm_arriving_with_a_release_leaves_the_stage_unreleased() -> None:
    """The decision message that has arrived is read first, so an alarm that
    arrives as stage 2 falls due ends the episode, and k_n leaves stage 2 out
    (episodes-tests.md, no_stage_is_released_after_the_alarm)."""

    def responder(message: dict) -> list[tuple[float, str | None]]:
        if message["ended"]:
            return []
        if message["step"] == 3:
            return [(4.75, "alarm")]
        return [(0.01, "no action")]

    clock = FakeClock()
    session = ScriptedSession(clock, responder)
    stages = FakeStages(clock, T0 + 5.0, [5.5, 7.25, 6.0], run_s=2.0)
    # The alarm arrives as stage 2 falls due, at T0 + 19.75, while the step
    # schedule runs a timed event.
    events = [steps.Event(T0 + 19.7, lambda: setattr(clock, "t", T0 + 19.75))]
    result = steps.run_steps(
        session, clock, "A", T0, horizon=10, events=events, stages=stages
    )
    assert stages.releases == [T0 + 10.5]
    assert result.terminal_state == steps.TRACK_A_ALARM
    assert result.alarm_arrival == T0 + 19.75
    assert result.released_before_alarm == 1


def test_an_alarm_read_across_a_step_boundary_keeps_its_step() -> None:
    """A timed event runs from T0 + 19.7 to T0 + 20.05, across the boundary of
    step 4, as a scan request through Incus can. Stage 2 falls due at
    T0 + 19.75 and the alarm arrives then, so the step schedule reads the alarm
    at T0 + 20.05, in step 4, before step 4's decision request. Step 4 stays
    the episode's last step, ending at the alarm, with the alarm its decision,
    and the end notice carries step 5 (episodes.md §11, horizon and
    termination; §13, episode record)."""

    def responder(message: dict) -> list[tuple[float, str | None]]:
        if message["ended"]:
            return []
        if message["step"] == 3:
            return [(4.75, "alarm")]
        return [(0.01, "no action")]

    clock = FakeClock()
    session = ScriptedSession(clock, responder)
    stages = FakeStages(clock, T0 + 5.0, [5.5, 7.25, 6.0], run_s=2.0)
    events = [steps.Event(T0 + 19.7, lambda: setattr(clock, "t", T0 + 20.05))]
    result = steps.run_steps(
        session,
        clock,
        "A",
        T0,
        horizon=10,
        events=events,
        stages=stages,
        training=True,
        final_reward=lambda r: -1.0 * r.released_before_alarm,
    )
    assert (result.alarm_step, result.alarm_arrival) == (4, T0 + 20.05)
    assert result.released_before_alarm == 1
    assert [s.step for s in result.steps] == list(range(5))
    assert result.steps[3].end == T0 + 20.0
    assert (result.steps[4].end, result.steps[4].decision) == (T0 + 20.05, "alarm")
    assert result.steps[4].request_sent is None
    assert session.sent[-1] == {
        "track": "A",
        "step": 5,
        "previous_reward": -1.0,
        "ended": True,
    }


def test_an_alarm_that_falls_due_with_the_last_stage_ends_the_episode() -> None:
    """The alarm that has arrived when the last stage falls due is read first,
    so it, not the last stage, ends the episode."""

    def responder(message: dict) -> list[tuple[float, str | None]]:
        if message["ended"]:
            return []
        if message["step"] == 5:
            return [(2.75, "alarm")]
        return [(0.01, "no action")]

    clock = FakeClock()
    session = ScriptedSession(clock, responder)
    stages = FakeStages(clock, T0 + 5.0, [5.5, 7.25, 6.0], run_s=2.0)
    events = [steps.Event(T0 + 27.7, lambda: setattr(clock, "t", T0 + 27.75))]
    result = steps.run_steps(
        session, clock, "A", T0, horizon=10, events=events, stages=stages
    )
    assert stages.releases == [T0 + 10.5, T0 + 19.75]
    assert result.terminal_state == steps.TRACK_A_ALARM
    assert result.released_before_alarm == 2


def test_an_alarm_before_the_first_release_counts_no_stage() -> None:
    clock = FakeClock()
    session = ScriptedSession(clock, alarm_at(1))
    stages = FakeStages(clock, T0 + 5.0, [5.5, 7.25, 6.0], run_s=2.0)
    result = steps.run_steps(session, clock, "A", T0, horizon=10, stages=stages)
    assert stages.releases == []
    assert result.terminal_state == steps.TRACK_A_ALARM
    assert result.released_before_alarm == 0


def test_a_stop_before_the_alarm_ends_the_episode_at_the_stop() -> None:
    """At the alarm of step 6, before the end notice, stopped_before finds a
    simulator stop in step 5; the episode ends at the stop, the alarm no
    longer counts, and the end notice carries the reward of an episode with
    no alarm."""
    clock = FakeClock()
    session = ScriptedSession(clock, alarm_at(6))
    asked: list[float] = []

    def stopped_before(arrival: float) -> float | None:
        asked.append(arrival)
        return T0 + 27.5

    result = steps.run_steps(
        session,
        clock,
        "A",
        T0,
        horizon=10,
        training=True,
        final_reward=lambda r: -1.0 if r.alarm_arrival is not None else 0.0,
        stopped_before=stopped_before,
    )
    assert asked == [T0 + 30.4]
    assert (result.ended_early, result.end_cause) == (
        T0 + 27.5,
        steps.STOPPED_BEFORE_ALARM,
    )
    assert result.terminal_state is None
    assert (result.alarm_step, result.alarm_arrival) == (None, None)
    assert [s.step for s in result.steps] == list(range(6))
    assert result.steps[-1].end == T0 + 27.5
    assert session.sent[-1] == {
        "track": "A",
        "step": 6,
        "previous_reward": 0.0,
        "ended": True,
    }


def test_no_stop_before_the_alarm_leaves_the_alarms_end() -> None:
    clock = FakeClock()
    session = ScriptedSession(clock, alarm_at(6))
    result = steps.run_steps(
        session,
        clock,
        "A",
        T0,
        horizon=10,
        training=True,
        final_reward=lambda r: -1.0,
        stopped_before=lambda arrival: None,
    )
    assert result.terminal_state == steps.TRACK_A_ALARM
    assert result.alarm_step == 6
    assert session.sent[-1]["previous_reward"] == -1.0


def test_track_b_releases_the_last_stage() -> None:
    clock = FakeClock()
    session = ScriptedSession(clock, idle("B"))
    stages = FakeStages(clock, T0 + 2.0, [6.0, 6.0])
    result = steps.run_steps(session, clock, "B", T0, horizon=8, stages=stages)
    assert len(result.steps) == 8
    assert stages.releases == [T0 + 8.0, T0 + 15.0]
    assert result.terminal_state is None


def test_a_process_session_carries_lines_both_ways() -> None:
    """The session that runs the defender as a process, here a short Python
    program whose decision echoes the step of each decision request."""
    program = (
        "import json, sys\n"
        "for line in sys.stdin:\n"
        "    n = json.loads(line)\n"
        "    if n['ended']: break\n"
        "    print(json.dumps({'decision': str(n['step'])}), flush=True)\n"
    )
    session = steps.ProcessSession([sys.executable, "-c", program])
    try:
        session.send(steps.decision_request("A", 7, None))
        assert json.loads(session.receive(2.0)) == {"decision": "7"}
        assert session.receive(0.2) is None
        session.send(steps.end_notice("A", 8, None))
        with pytest.raises(EOFError):
            session.receive(2.0)
    finally:
        session.close()


def test_a_process_session_takes_the_lines_that_have_arrived() -> None:
    """arrived takes, from one read of the pipe, each whole line, keeps a
    partial line for later, and decodes a byte sequence that is not UTF-8
    into an invalid line rather than failing."""
    program = (
        "import sys\n"
        'sys.stdout.buffer.write(b\'{"decision": "no action"}\\n\\xff\\xfe\\n{"dec\')\n'
        "sys.stdout.flush()\n"
        "sys.stdin.readline()\n"
        'sys.stdout.buffer.write(b\'ision": "alarm"}\\n\')\n'
    )
    session = steps.ProcessSession([sys.executable, "-c", program])
    try:
        lines: list[str] = []
        deadline = time.monotonic() + 5.0
        while len(lines) < 2 and time.monotonic() < deadline:
            lines += session.arrived()
            time.sleep(0.01)
        assert lines[0] == '{"decision": "no action"}\n'
        assert steps.decision_of(lines[1]) is None
        assert session.arrived() == []
        session.send("go\n")
        assert session.receive(5.0) == '{"decision": "alarm"}\n'
        with pytest.raises(EOFError):
            while True:
                session.arrived()
                time.sleep(0.01)
    finally:
        session.close()


def test_a_stop_wakes_the_step_schedule_at_once() -> None:
    """The watch on the simulator's process wakes the step schedule when the
    simulator stops, in the middle of step 6 (episodes.md §11, horizon and
    termination)."""
    clock = FakeClock()
    session = ScriptedSession(clock, idle("A"))
    stop = FakeWake(clock, T0 + 32.5)
    result = steps.run_steps(session, clock, "A", T0, horizon=10, stop=stop)
    assert result.ended_early == T0 + 32.5
    assert result.end_cause == "the simulator's process exited"
    assert [s.step for s in result.steps] == list(range(7))
    assert session.sent[-1]["step"] == 7 and session.sent[-1]["ended"]
    assert result.end_notice_sent == T0 + 32.5


def test_a_stop_wakes_the_step_schedule_after_the_step_session_ends() -> None:
    def responder(message):
        if message["step"] == 2:
            return [(0.5, None)]
        return [] if message["ended"] else [(0.01, "no action")]

    clock = FakeClock()
    session = ScriptedSession(clock, responder)
    stop = FakeWake(clock, T0 + 32.5)
    result = steps.run_steps(session, clock, "A", T0, horizon=10, stop=stop)
    assert result.session_ended == T0 + 10.5
    assert result.ended_early == T0 + 32.5
    assert len(result.steps) == 7


def test_the_steps_end_at_the_time_of_the_stop() -> None:
    """The simulator stopped at the end of step 5, and the step schedule saw
    the stop only after step 6 had begun."""
    result, _ = run(idle("A"), horizon=10)
    kept = steps.cut_at(result.steps[:7], T0 + 29.9, "A")
    assert [s.step for s in kept] == list(range(6))
    assert kept[-1].end == T0 + 29.9
    assert kept[-1].decision == "no action"


def test_a_decision_message_after_the_stop_no_longer_counts() -> None:
    def responder(message):
        if message["ended"]:
            return []
        delay = 4.95 if message["step"] == 5 else 0.01
        return [(delay, "decision of step %d" % message["step"])]

    result, _ = run(responder, track="B", horizon=10)
    kept = steps.cut_at(result.steps[:6], T0 + 29.9, "B")
    assert kept[4].decision == "decision of step 4"
    assert kept[5].decision == "no action"
    assert kept[5].decision_arrival is None
    assert kept[5].dropped_decision_messages == 1


class PipeWake:
    """A wake source whose file is the read end of a pipe, as a stand-in for
    the process file descriptor of the simulator."""

    def __init__(self) -> None:
        self.read_end, self.write_end = os.pipe()

    def fileno(self) -> int:
        return self.read_end

    def fired(self) -> bool:
        ready, _, _ = select.select([self.read_end], [], [], 0)
        return bool(ready)

    def fire(self) -> None:
        os.write(self.write_end, b"x")


def test_a_process_session_returns_when_the_stop_fires() -> None:
    session = steps.ProcessSession(
        [sys.executable, "-c", "import time; time.sleep(30)"]
    )
    wake = PipeWake()
    try:
        wake.fire()
        started = time.monotonic()
        assert session.receive(5.0, wake=wake) is None
        assert time.monotonic() - started < 1.0
    finally:
        session.proc.kill()
        session.close()


def test_the_monotonic_clock_wakes_when_the_stop_fires() -> None:
    clock = steps.MonotonicClock()
    wake = PipeWake()
    wake.fire()
    started = clock.now()
    clock.sleep_until(started + 5.0, wake=wake)
    assert clock.now() - started < 1.0
