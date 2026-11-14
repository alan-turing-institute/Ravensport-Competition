"""The onset step and the waits of an adversarial episode (episodes.md §10.1,
onset; §10.2, waits).

Run with: pytest competition/tests -q
"""

from __future__ import annotations

import math
from collections import Counter

import pytest

from competition.episode_runner import pacing, steps
from competition.episode_runner.seeds import (
    ATTACK_START_STEP,
    ATTACK_WAITS,
    derive_seeds,
)

SEED = "6f1d8c2a9b3e4f5061728394a5b6c7d8e9fa0b1c2d3e4f5061728394a5b6c7d8"
DRAWS = 20_000


def test_one_seed_gives_one_onset_step_and_one_list_of_waits() -> None:
    seeds = derive_seeds(SEED)
    assert pacing.onset_step(seeds[ATTACK_START_STEP]) == pacing.onset_step(
        seeds[ATTACK_START_STEP]
    )
    assert pacing.waits(seeds[ATTACK_WAITS], 7) == pacing.waits(seeds[ATTACK_WAITS], 7)
    # A longer chain draws the same first waits.
    assert pacing.waits(seeds[ATTACK_WAITS], 8)[:7] == pacing.waits(
        seeds[ATTACK_WAITS], 7
    )


def test_onset_steps_follow_the_cut_off_geometric_distribution() -> None:
    """A chance of 1 in 7 per step from step 6, where a draw beyond step 24 is
    drawn again, so the onset falls at step 6 with chance
    (1/7) / (1 - (6/7)^19), about 0.151."""
    counts = Counter(pacing.onset_step(n) for n in range(DRAWS))
    assert set(counts) <= set(range(6, 25))
    kept = 1 - (6 / 7) ** 19
    first = (1 / 7) / kept
    assert counts[6] / DRAWS == pytest.approx(first, abs=0.01)
    assert counts[24] / DRAWS == pytest.approx(first * (6 / 7) ** 18, abs=0.005)


def test_waits_follow_the_cut_off_exponential_distribution() -> None:
    """5 seconds plus an exponential value with a mean of 5.0 seconds, drawn
    again above 20 seconds, so the mean is 5 + 5.0 - 15 r / (1 - r), with r
    the chance of a draw above 20 seconds."""
    drawn = pacing.waits(1, DRAWS)
    assert all(5.0 <= w <= 20.0 for w in drawn)
    r = math.exp(-15 / 5.0)
    assert sum(drawn) / DRAWS == pytest.approx(5 + 5.0 - 15 * r / (1 - r), abs=0.15)


def test_the_step_duration_is_at_most_the_waits_lower_bound() -> None:
    """The step schedule reads a stage's due time when it next wakes, at the
    latest at the end of the step in which the stage before ended, and the due
    time lies at least the waits' lower bound after that end. A step no longer
    than the lower bound therefore releases no stage late (episodes.md §10.3,
    onset and waits; §10.4, the attack chain's session)."""
    assert steps.STEP_S <= pacing.WAIT_LOWER_S


def test_bounds_that_admit_no_wait_are_refused() -> None:
    with pytest.raises(ValueError):
        pacing.waits(1, 1, lower_s=30.0, upper_s=5.0)
    with pytest.raises(ValueError):
        pacing.waits(1, 1, mean_s=0.0)
