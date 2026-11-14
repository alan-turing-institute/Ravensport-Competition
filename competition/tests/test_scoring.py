"""Track A's episodic reward (episodes.md §13, episode record;
episodes-tests.md, track_a_reward_follows_the_outcomes).

Run with: pytest competition/tests -q
"""

from __future__ import annotations

import pytest

from competition.episode_runner.scoring import FALSE_ALARM, NO_ALARM, TrackA, track_a


def _episode(case: str, K: int, alarm: bool, released: int | None, k_n: int, outcome):
    """One row of the table: an adversarial episode whose chain has K stages,
    with R_n = -(k_n - 1)/(K - 1) for outcome 1 and -1 otherwise."""
    R_n = (1 - k_n) / (K - 1) if outcome == 1 else -1.0
    return pytest.param(
        K, alarm, released, TrackA(K, k_n, R_n, outcome), id=f"{case}, K={K}"
    )


OUTCOMES = [
    *(
        _episode("an alarm before the first release", K, True, 0, 0, 3)
        for K in (2, 5, 7)
    ),
    *(
        _episode("an alarm after the first release", K, True, 1, 1, 1)
        for K in (2, 5, 7)
    ),
    *(
        _episode(f"an alarm after release {k}", K, True, k, k, 1)
        for K, k in ((5, 2), (5, 3), (5, 4), (7, 2), (7, 4), (7, 6))
    ),
    *(
        _episode("no alarm, the episode ending at the last stage", K, False, None, K, 4)
        for K in (2, 5, 7)
    ),
    *(
        _episode(f"a chain stopped after stage {j}, no alarm", K, False, None, K, 4)
        for K, j in ((2, 1), (5, 2), (7, 5))
    ),
    *(
        _episode(f"a chain stopped after stage {j}, a later alarm", K, True, j, j, 1)
        for K, j in ((2, 1), (5, 2), (7, 5))
    ),
    pytest.param(
        None,
        True,
        None,
        TrackA(None, None, -1.0, FALSE_ALARM),
        id="a benign episode with an alarm",
    ),
    pytest.param(
        None,
        False,
        None,
        TrackA(None, None, 0.0, NO_ALARM),
        id="a benign episode without one",
    ),
    *(
        _episode("a suppressed alarm after the last release", K, True, K, K, 2)
        for K in (2, 5, 7)
    ),
]


@pytest.mark.parametrize(("K", "alarm", "released", "expected"), OUTCOMES)
def test_track_a_reward_follows_the_outcomes(
    K: int | None, alarm: bool, released: int | None, expected: TrackA
) -> None:
    """A table of episodes, with K of 2, 5 and 7, gives k_n, R_n and the
    outcome of each (episodes-tests.md, track_a_reward_follows_the_outcomes;
    episodes.md §13, episode record). R_n of 0 is zero, not negative zero,
    which JSON would write as -0.0."""
    scored = track_a(K, alarm=alarm, released_before_alarm=released)
    assert scored == expected
    assert str(scored.R_n) == str(expected.R_n) != "-0.0"


def test_a_chain_of_one_stage_has_no_outcome_1() -> None:
    assert track_a(1, alarm=True, released_before_alarm=0).outcome == 3
    assert track_a(1, alarm=True, released_before_alarm=1).outcome == 2
    assert track_a(1, alarm=False, released_before_alarm=None).outcome == 4


def test_an_impossible_count_is_refused() -> None:
    with pytest.raises(ValueError):
        track_a(5, alarm=True, released_before_alarm=6)
    with pytest.raises(ValueError):
        track_a(0, alarm=False, released_before_alarm=None)
