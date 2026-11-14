"""When a living port forks and when it keeps a snapshot (episodes.md §6.3,
the living port).

The living port's first fork falls one drawn wait after its settle period.
Fork k+1 falls 14 minutes and one drawn wait after fork k's instant: the
horizon of an episode and the settle timeout, so that a living port runs one
episode at a time. Every instant counts on the living port's own clock, from
the fork instants alone, never from an episode's end, so the fork instants
depend on the living port seed alone. Each wait is uniform on 0 to 6 minutes,
drawn in turn from the generator `fork_wait` of the living port seed, whose
seed derives as the episode runner derives a generator's seed from a seed and
a name.

The snapshots for replay fall every 15 minutes for the living port's first
hour, every 30 minutes for the next 2 hours and every hour for the next 4
hours, and the interval then doubles after every four snapshots. A run keeps
the latest snapshots that fit its snapshot cache.
"""

from __future__ import annotations

import itertools
import random
from collections.abc import Iterator, Sequence

from competition.episode_runner.seeds import derive_generator_seed

FORK_WAIT = "fork_wait"
FORK_WAIT_MAX_S = 360.0
# The horizon, 144 steps of 5 s, and the settle timeout of 120 s, as the
# episode runner's steps.py and settle.py hold them.
FORK_SPACING_S = 840.0
SNAPSHOT_FIRST_INTERVAL_S = 900.0
SNAPSHOTS_PER_INTERVAL = 4


def fork_waits(living_port_seed: str) -> Iterator[float]:
    """The successive waits before forks 1, 2, 3 and so on, in seconds."""
    rng = random.Random(derive_generator_seed(living_port_seed, FORK_WAIT))
    while True:
        yield rng.uniform(0.0, FORK_WAIT_MAX_S)


def fork_wait_s(living_port_seed: str, k: int) -> float:
    """The drawn wait of fork k, the generator's k-th draw. Fork 1 waits it
    from the living port's settling, and fork k from fork k-1's instant and
    the spacing."""
    if k < 1:
        raise ValueError(
            "forks are numbered from 1; fork 0 runs on the living port's own deployment"
        )
    return next(itertools.islice(fork_waits(living_port_seed), k - 1, None))


def snapshot_ages_s(
    until_s: float,
    first_interval_s: float = SNAPSHOT_FIRST_INTERVAL_S,
    per_interval: int = SNAPSHOTS_PER_INTERVAL,
) -> list[float]:
    """The ages of the living port at which its snapshots fall, up to
    `until_s`: the n-th snapshot, from 0, follows the one before it by the
    first interval doubled n // per_interval times."""
    ages: list[float] = []
    age = 0.0
    while True:
        age += first_interval_s * 2 ** (len(ages) // per_interval)
        if age > until_s:
            return ages
        ages.append(age)


def kept_snapshots(sizes: Sequence[int], cache_bytes: int) -> int:
    """How many of the latest snapshots fit the cache, given each snapshot's
    size from the oldest to the latest."""
    total = 0
    count = 0
    for size in reversed(sizes):
        if total + size > cache_bytes:
            break
        total += size
        count += 1
    return count
