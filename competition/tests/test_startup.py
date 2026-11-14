"""The simulator's start-up input (episodes.md §4.3, start-up input;
episode-runner-impl.md §2, start-up input).

The first version starts every episode from the simulator's own start state,
with the episode's seed of the generator sim_tug_arrivals.

Run with: pytest competition/tests -q
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

import pytest

from competition.episode_runner import chains, startup, predicted_events
from competition.episode_runner.steps import HORIZON, STEP_S

SEED = "000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f"
PLANNED = datetime(2026, 11, 11, 9, 30, 5, 250000, tzinfo=timezone.utc)
TIMINGS = predicted_events.competition_timings()


def test_the_start_up_input_gives_the_simulators_own_start_state() -> None:
    assert json.loads(startup.startup_input(SEED, TIMINGS, PLANNED)) == {
        "sim_tug_arrivals": 0x24443102C20550C4,
        "initial_volume_l": 35_000.0,
        "order_pending_s": None,
        "first_poll_after_s": 180.0,
        "first_check_after_s": 17_280.0,
        "next_audit_at": "2026-11-17T10:42:05Z",
        "initial_cumulative_pulses": 0,
        "initial_meter_totaliser_l": 0.0,
        "first_bdn_number": 1,
        "first_ticket_number": 1,
        "held_since": None,
        "held_artefacts": [],
    }


def test_the_fields_are_the_entry_points_in_its_order() -> None:
    source = (chains.REPO / "competition" / "sim" / "src" / "input.rs").read_text()
    listed = re.search(
        r"pub const FIELDS: \[&str; 12\] = \[(.*?)\];", source, re.DOTALL
    )
    assert listed is not None
    assert startup.FIELDS == tuple(re.findall(r'"(\w+)"', listed.group(1)))
    text = startup.startup_input(SEED, TIMINGS, PLANNED)
    assert tuple(json.loads(text)) == startup.FIELDS


def test_the_initial_volume_is_the_simulators_own() -> None:
    source = (chains.REPO / "infra" / "sim" / "src" / "coupled.rs").read_text()
    (value,) = re.findall(r"pub const INITIAL_VOLUME_L: f64 = ([0-9_.]+);", source)
    assert startup.INITIAL_VOLUME_L == float(value.replace("_", ""))


@pytest.mark.parametrize("period, seconds", [("144h", 518_400.0), ("33.6h", 120_960.0)])
def test_an_audit_period_in_hours_is_read_in_seconds(period, seconds) -> None:
    assert startup.audit_period_s(period) == pytest.approx(seconds)


@pytest.mark.parametrize("period", ["month", "week", "day", "0h", "h", "-1h", "144"])
def test_an_audit_period_not_in_hours_is_refused(period) -> None:
    with pytest.raises(ValueError):
        startup.audit_period_s(period)


def test_no_audit_falls_inside_an_episode() -> None:
    """An episode, its settle period included, lasts less than an hour from the
    simulator's start, and the next audit comes more than a day after the
    plan."""
    assert predicted_events.STEP_0_AFTER_START_S[1] + HORIZON * STEP_S < 3600
    assert startup.audit_period_s(TIMINGS.audit_period) > 24 * 3600
