"""Track A's episodic reward (episodes.md §7, step protocol; §13, episode
record).

K is the attack chain's own number of stages. k_n is the number of stages
that the episode runner released before the first Track A alarm arrived, or K
when no alarm arrived (episodes.md §10.4, the attack chain's session). A stage
released before the alarm counts even when its beginning event arrives after
the alarm. An alarm before the first release counts no stage, so k_n is 0,
and scores -1 as a false alarm does.

An adversarial episode has one of four outcomes of Track A:

1. the alarm arrived after the release of stage k_n, from 1 to K - 1, and
   R_n = -(k_n - 1)/(K - 1);
2. the alarm arrived after the last stage's release, which only a suppressed
   episode releases, so k_n = K and R_n = -1;
3. the alarm arrived before the first stage's release, so k_n = 0 and R_n = -1;
4. no alarm arrived, so k_n = K and R_n = -1.

A benign episode scores -1 for an alarm, a false alarm, and 0 without one, and
has neither K nor k_n.
"""

from __future__ import annotations

from dataclasses import dataclass

FALSE_ALARM = "false alarm"
NO_ALARM = "none"


@dataclass(frozen=True)
class TrackA:
    """K, k_n, R_n and Track A's outcome for one episode."""

    K: int | None
    k_n: int | None
    R_n: float
    outcome: int | str


def track_a(K: int | None, alarm: bool, released_before_alarm: int | None) -> TrackA:
    """Track A's reward for an episode whose attack chain has K stages, or
    for a benign episode when K is None. `alarm` says whether a first alarm
    arrived, and released_before_alarm is the number of stages released
    before it."""
    if K is None:
        return (
            TrackA(None, None, -1.0, FALSE_ALARM)
            if alarm
            else TrackA(None, None, 0.0, NO_ALARM)
        )
    if K < 1:
        raise ValueError(f"an attack chain of {K} stages")
    if not alarm:
        return TrackA(K, K, -1.0, 4)
    k_n = released_before_alarm or 0
    if not 0 <= k_n <= K:
        raise ValueError(f"{k_n} stages released before the alarm, of {K}")
    if k_n == 0:
        return TrackA(K, 0, -1.0, 3)
    if k_n == K:
        return TrackA(K, K, -1.0, 2)
    return TrackA(K, k_n, (1 - k_n) / (K - 1), 1)
