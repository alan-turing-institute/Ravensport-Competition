"""When a living port forks and when it keeps a snapshot (episodes.md §6.3,
the living port).

Run with: pytest competition/tests -q
"""

from __future__ import annotations

import random

import pytest

from competition.episode_runner.seeds import derive_generator_seed
from competition.living.snapshots import (
    FORK_WAIT_MAX_S,
    fork_wait_s,
    fork_waits,
    kept_snapshots,
    snapshot_ages_s,
)

SEED = "24ae59ca6a17a9f01e1ae6f215189bee1aee1bac8ec7e2dadd5b3f31ca865302"


def test_the_fork_waits_are_successive_draws_of_the_generator_fork_wait() -> None:
    """The generator's seed derives from the living port seed as the episode
    runner derives a generator's seed from a seed and a name, and each wait is
    uniform on 0 to 6 minutes."""
    rng = random.Random(derive_generator_seed(SEED, "fork_wait"))
    expected = [rng.uniform(0.0, 360.0) for _ in range(5)]
    waits = fork_waits(SEED)
    assert [next(waits) for _ in range(5)] == expected
    assert [fork_wait_s(SEED, k) for k in range(1, 6)] == expected
    assert FORK_WAIT_MAX_S == 360.0


def test_the_waits_lie_within_six_minutes_and_differ_by_seed() -> None:
    waits = fork_waits(SEED)
    drawn = [next(waits) for _ in range(500)]
    assert all(0.0 <= wait <= 360.0 for wait in drawn)
    assert min(drawn) < 30.0 and max(drawn) > 330.0
    other = fork_waits("00" * 32)
    assert [next(other) for _ in range(3)] != drawn[:3]


def test_fork_zero_has_no_wait() -> None:
    with pytest.raises(ValueError):
        fork_wait_s(SEED, 0)


def test_snapshots_follow_their_schedule() -> None:
    """Every 15 minutes for the first hour, every 30 minutes for the next 2
    hours, every hour for the next 4 hours, and then the interval doubles
    after every four snapshots."""
    minutes = [age / 60 for age in snapshot_ages_s(15 * 3600)]
    assert minutes == [
        15, 30, 45, 60, 90, 120, 150, 180, 240, 300, 360, 420, 540, 660, 780, 900
    ]  # fmt: skip
    assert snapshot_ages_s(899) == []


def test_the_cache_keeps_the_latest_snapshots_that_fit() -> None:
    sizes = [8, 8, 9, 8]
    assert kept_snapshots(sizes, 17) == 2
    assert kept_snapshots(sizes, 24) == 2
    assert kept_snapshots(sizes, 25) == 3
    assert kept_snapshots(sizes, 7) == 0
    assert kept_snapshots(sizes, 100) == 4


def test_the_spacing_is_the_horizon_and_the_settle_timeout() -> None:
    from competition.episode_runner.settle import SETTLE_TIMEOUT_S
    from competition.episode_runner.steps import HORIZON, STEP_S
    from competition.living.snapshots import FORK_SPACING_S

    assert FORK_SPACING_S == HORIZON * STEP_S + SETTLE_TIMEOUT_S == 840.0
