"""The onset step and the waits of an adversarial episode (episodes.md §10.1,
onset; §10.2, waits).

Each draw comes from Python's random.Random seeded with the seed of its
generator (episodes.md §4.2, derivation), attack_start_step for the onset step
and attack_waits for the waits. Python keeps random() producing the same
sequence for the same seed across its versions, so one episode seed gives one
onset step and one list of waits.
"""

from __future__ import annotations

import math
import random

# The onset: a chance of 1 in 7 per step from step 6, cut off after step 24,
# where a draw beyond step 24 is drawn again (episodes.md §10.1, onset).
ONSET_FIRST_STEP = 6
ONSET_LAST_STEP = 24
ONSET_CHANCE = 1 / 7

# The waits: 5 seconds plus an exponential value with a mean of 5.0 seconds,
# where a wait beyond 20 seconds is drawn again (episodes.md §10.3, onset and
# waits). The mean puts 95 % of the exponential part below the upper bound:
# 15 / ln 20 = 5.0.
# The step schedule releases each stage at its due time only while the step
# duration, steps.STEP_S, is at most WAIT_LOWER_S (steps.py, STEP_S).
WAIT_LOWER_S = 5.0
WAIT_MEAN_S = 5.0
WAIT_UPPER_S = 20.0


def onset_step(seed: int) -> int:
    """The onset step from the seed of the generator attack_start_step. Each
    step from the first takes one draw, and the first draw below the chance
    marks the onset; a run past the last step starts again from the first."""
    rng = random.Random(seed)
    while True:
        for step in range(ONSET_FIRST_STEP, ONSET_LAST_STEP + 1):
            if rng.random() < ONSET_CHANCE:
                return step


def waits(
    seed: int,
    count: int,
    lower_s: float = WAIT_LOWER_S,
    mean_s: float = WAIT_MEAN_S,
    upper_s: float = WAIT_UPPER_S,
) -> list[float]:
    """count waits, in seconds, from the seed of the generator attack_waits.
    Each draw takes one value u from 0 (included) to 1 (excluded) and gives
    lower_s - mean_s * ln(1 - u); a draw above upper_s is discarded."""
    if not 0 <= lower_s < upper_s or mean_s <= 0:
        raise ValueError(f"bounds {lower_s}, {mean_s}, {upper_s} admit no wait")
    rng = random.Random(seed)
    drawn: list[float] = []
    while len(drawn) < count:
        wait = lower_s - mean_s * math.log(1.0 - rng.random())
        if wait <= upper_s:
            drawn.append(wait)
    return drawn
