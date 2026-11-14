"""The predicted episode events, and the attack chains that they support
(episodes.md §10.2, choosing the chain; episode-runner-impl.md §8, ranges of
the predicted events).

A stage that waits for the fuel supplier's poll, or that reads a connection
which exists only while a tug is refuelled, completes only in an episode that
holds that activity while the stage runs. Both activities follow from the
simulator's start state and from each tug's scheduled arrival and requested
volume, which the simulator draws as the tug leaves the dock. At a fork after
fork 0 the episode runner reads each tug's scheduled arrival from the
simulator's log (fork.py). At fork 0, where the run command plans before
anything is deployed, every tug starts away from the dock, and the helper
competition/predicted_events, built against the simulator's crate, repeats
each tug's first draw from the tug seed. The timings come from the render's
projection of world.yaml at the competition's compression factor, computed
here, so the last render on disk plays no part in them.

From the start state and the arrivals the module computes the supplier's
polls, which keep their interval through an order and a delivery, and the
windows in which each refuel's connections exist. The episode runner predicts
no arrival that follows a departure inside the episode, so a refuel that a
stage needs counts only when no tug leaves the dock before its latest start.
Every time is a band, earliest to latest, measured from the simulator's start
at fork 0 and from the fork instant at a later fork. A chain is admitted to an
episode when each activity that a stage needs falls inside that stage's window
in every case the bands allow, with a margin.
"""

from __future__ import annotations

import json
import math
import os
import random
import subprocess
import tomllib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from render import build_sim_constants, load_world, load_yaml, world_at_override_factor

from competition.episode_runner import chains, deploy, pacing, startup
from competition.episode_runner.seeds import (
    ATTACK_START_STEP,
    ATTACK_WAITS,
    SIM_TUG_ARRIVALS,
    derive_seeds,
)
from competition.episode_runner.steps import HORIZON, STEP_S

HELPER_DIR = chains.REPO / "competition" / "predicted_events"
HELPER = HELPER_DIR / "target" / "debug" / "predicted-events"

# Step 0 at fork 0 after the simulator's start, at a compression factor of 5:
# fork-0 episodes on a Linux host reached step 0 69.06 to 76.89 s after the
# start, and living ports' settle periods, which follow the same procedure,
# ended 75.8 to 94.0 s after it on a Linux host, a Mac with an Apple processor
# and a Mac with an Intel processor.
STEP_0_AFTER_START_S = (65.0, 100.0)
# A refuel's first dispenser connection after the tug's arrival, at a
# compression factor of 5, measured on a Linux host, a Mac with an Apple
# processor and a Mac with an Intel processor: 0.14 to 0.18 s where the dock operator's first poll
# of the job's status found the vessel's tag validated, and 6.09 to 6.43 s
# where its second poll did. The range starts at the job's creation and ends
# one poll of the job's status, 6 s, plus 6 s after the arrival.
REFUEL_START_S = (0.0, 12.0)
# Between a refuel's end and the start of a refuel queued behind it.
HANDOVER_S = 6.0
# A refuel's connections close within this long after its dispense ends.
REFUEL_TAIL_S = 5.0
# The range of each poll of the supplier, which fall due on a fixed schedule
# (episode-runner-impl.md §8): it starts POLL_EARLY_S before the poll's due
# time and ends POLL_SLIP_S after it, and no range widens with the intervals
# since the first poll. The figures are provisional, and await measurement
# under a held clock on three platforms (episodes.md §19, open items).
POLL_EARLY_S = 0.3
POLL_SLIP_S = 2.0
# A poll that a stage intercepts can end up to this long after its range:
# 0.6 to 9.2 s in the live adversarial episodes that measured it. It
# delays that poll alone, since the supplier's later polls keep their due
# times.
INTERCEPT_DELAY_S = 10.0
# A stage that waits for a poll catches polls from this long after it begins.
POLL_SETUP_S = 2.0
# The inventory stage scans the FMS's peers, then the tank-gauge relay's, then
# the HMI's, for 75 s each. Two runs on a Linux host measured
# the scans, from siem-01's API log in whole seconds and the same in both, from
# 0 to 74.8, 74.8 to 149.8 and 149.8 to 225.8 s after the stage's beginning.
# The windows below, from the stage's beginning, lie 1 s inside the FMS's and
# the HMI's scans, for the log's resolution (episodes.md §10.1, the competition
# library).
INVENTORY_STAGE = "enumerate-siem-agent-inventory"
SCAN_WINDOWS_S = {
    chains.REFUEL_DURING_FMS_SCAN: (1.0, 74.0),
    chains.REFUEL_DURING_HMI_SCAN: (151.0, 225.0),
}
# The shortest overlap, and the shortest time to the horizon, that admission
# requires of an activity.
MARGIN_S = 15.0
# The run time of a stage that the live episodes never measured.
UNMEASURED_S = (0.0, 30.0)
# The time that the horizon leaves after the last stage begins, for that
# stage's records to arrive (episodes.md §11, horizon and termination): at
# least the flow-record lag, whose longest measure is 42.8 s. The allowance
# stands apart from the settle delay, which a fork does without.
FLOW_RECORD_LAG_S = 45.0
ALLOWANCE_S = FLOW_RECORD_LAG_S

Band = tuple[float, float]


class PredictedEventsError(RuntimeError):
    """A helper that cannot be built or run."""


@dataclass(frozen=True)
class Timings:
    """The simulator's timings at the competition's compression factor, with
    the fuel supplier's ordering values and the roster: each tug's IMO number
    and mean time away, and the bounds of a tug's requested volume."""

    lead_time_s: float
    poll_s: float
    check_s: float
    audit_period: str
    settling_delay_s: float
    outflow_l_per_s: float
    inflow_l_per_s: float
    cavitation_l: float
    delivery_volume_l: float
    roster: tuple[tuple[int, float], ...]
    refuel_volume_l: tuple[float, float]
    # How often the dock operator checks a job's state on the HMI.
    dock_operator_poll_s: float


@lru_cache(maxsize=1)
def competition_timings(
    world: Path = deploy.DEPLOY / "world.yaml",
    override: Path = deploy.DEPLOY / deploy.EXTRA_OVERRIDE,
) -> Timings:
    """The timings that sim-constants.toml holds for a competition deployment,
    from the render's own projection of world.yaml at the override's factor."""
    at_factor = world_at_override_factor(load_world(world), {}, load_yaml(override))
    built = tomllib.loads(build_sim_constants(at_factor))
    vessels = built["vessels"]
    return Timings(
        lead_time_s=float(built["fuel_supplier"]["lead_time_s"]),
        poll_s=float(built["fuel_supplier"]["supplier_poll_interval_s"]),
        check_s=float(built["intervals"]["admin_check_interval_s"]),
        audit_period=str(built["audit"]["audit_period"]),
        settling_delay_s=float(built["audit"]["settling_delay_s"]),
        outflow_l_per_s=float(built["process"]["outflow_rate_l_per_min"]) / 60.0,
        inflow_l_per_s=float(built["fuel_supplier"]["inflow_rate_l_per_min"]) / 60.0,
        cavitation_l=float(at_factor.process.tank_capacity_l)
        * float(at_factor.process.cavitation_threshold_pct)
        / 100.0,
        delivery_volume_l=float(built["fuel_supplier"]["delivery_volume_l"]),
        roster=tuple(
            (
                int(vessels[f"vessel_{n}_imo_number"]),
                float(vessels[f"vessel_{n}_mean_time_away_s"]),
            )
            for n in range(1, int(vessels["vessel_count"]) + 1)
        ),
        refuel_volume_l=(
            float(built["roster"]["refuel_volume_min_l"]),
            float(built["roster"]["refuel_volume_max_l"]),
        ),
        dock_operator_poll_s=float(built["intervals"]["dock_operator_poll_s"]),
    )


def build(run: Callable[..., Any] = subprocess.run) -> Path:
    """Build the helper with cargo, and return its path. The run command plans
    fork 0 with it; the `copy` command reads the fork's simulator log instead.
    Each deployment renders the simulator's constants afresh, so the next build
    recompiles the simulator's crate; the build skips cargo's incremental
    cache, since copying into it failed once on the Lima virtual machine's
    shared directory. The build runs in the helper's directory, where rustup
    takes the toolchain that the repository's rust-toolchain.toml names; the
    lock file needs cargo 1.85 or later, which Cargo.toml states as
    rust-version."""
    result = run(
        [
            "cargo",
            "build",
            "--quiet",
            "--manifest-path",
            str(HELPER_DIR / "Cargo.toml"),
        ],
        cwd=HELPER_DIR,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        env={**os.environ, "CARGO_INCREMENTAL": "0"},
    )
    if result.returncode != 0 or not HELPER.is_file():
        raise PredictedEventsError(
            "the helper that repeats the tugs' draws (competition/predicted_events) "
            f"did not build: {result.stderr.strip()}"
        )
    return HELPER


@dataclass(frozen=True)
class UnderWay:
    """The dock operator's job under way at a fork: a tug's refuel, or the
    road tanker's fill. `start` is the band in which its pumping starts, from
    the fork instant, and `remaining_l` the litres that it has still to move.
    A refuel whose pumping has not started can still be refused at its
    start."""

    fill: bool
    start: Band
    remaining_l: float
    started: bool
    arrival_s: float = 0.0
    volume_l: float = 0.0
    # A tug whose start the FMS refused before the fork, which leaves the dock
    # once its job is cancelled, the end of `start`.
    refused: bool = False
    # A tug that has left, whose delivery note the dock operator is linking:
    # the dock is busy until the end of `start`, and no tug is at the dock for
    # this job.
    departed: bool = False


@dataclass(frozen=True)
class Draws:
    """An episode's start state, and the tug arrivals that the episode runner
    knows of at its start. At fork 0 every time counts from the simulator's
    start, and the arrivals are each tug's first. At a fork after fork 0, every
    time counts from the fork instant: the arrivals hold the tugs that wait at
    the fork, at or before it, and the scheduled arrival of each tug away from
    the dock. The job under way, whether the living port has completed an
    audit, and a poll or a road tanker overdue at the fork, which leave the
    start state uncertain, belong to such a fork; at fork 0 they keep their
    defaults."""

    initial_volume_l: float
    order_pending_s: float | None
    first_poll_after_s: float | None
    delivery_volume_l: float
    arrivals: tuple[tuple[float, float], ...]
    under_way: UnderWay | None = None
    # The departures inside the episode of tugs that hold no job at the dock:
    # a served tug, or a refused and cancelled one, whose departure the
    # simulator has not yet logged at the fork.
    leaving_s: tuple[float, ...] = ()
    # At fork 0 the living port has completed no audit
    # (chains.READ_AT_THE_FORK).
    audit_completed: bool = False
    # A poll of the supplier overdue at the fork, which leaves the times of the
    # polls uncertain and rules out the stages that need a poll.
    poll_overdue: str | None = None
    # A road tanker overdue at the fork, whose arrival a poll under way
    # holds: the dock's order of service is uncertain, which rules out the
    # stages that need a refuel. The poll that holds it is overdue too, so
    # poll_overdue is set with it.
    tanker_overdue: str | None = None

    @property
    def uncertain(self) -> str | None:
        """Why the start state at the fork is uncertain, if it is."""
        reasons = [r for r in (self.poll_overdue, self.tanker_overdue) if r]
        return "; ".join(reasons) or None

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Draws:
        start = data["start_state"]
        return cls(
            initial_volume_l=float(start["initial_volume_l"]),
            order_pending_s=start["order_pending_s"],
            first_poll_after_s=start["first_poll_after_s"],
            delivery_volume_l=float(data["delivery_volume_l"]),
            arrivals=tuple(
                (float(a["at_s"]), float(a["volume_l"])) for a in data["tug_arrivals"]
            ),
        )


def helper_output(
    tug_seed: int,
    timings: Timings,
    helper: Path = HELPER,
    run: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    """What the helper prints for a seed of the generator sim_tug_arrivals: the
    delivery volume, and each tug's first arrival and
    requested volume, at its time from the generator's start, in the order of
    the arrivals. Every tug starts away from the dock, and draws its first time
    away and volume on its own stream (sim.md §6)."""
    low, high = timings.refuel_volume_l
    result = run(
        [
            str(helper),
            str(tug_seed),
            repr(low),
            repr(high),
            *(f"{imo}:{mean_s!r}" for imo, mean_s in timings.roster),
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise PredictedEventsError(
            "the helper that repeats the tugs' draws (competition/predicted_events) "
            f"failed: {result.stderr.strip()}"
        )
    return json.loads(result.stdout)


def draws(
    episode_seed: str,
    timings: Timings,
    helper: Path = HELPER,
    run: Callable[..., Any] = subprocess.run,
) -> Draws:
    """The simulator's own start state, which the start-up input gives, and
    each tug's first arrival, which the helper prints for the episode seed: the
    plan of fork 0, before anything is deployed."""
    printed = helper_output(
        derive_seeds(episode_seed)[SIM_TUG_ARRIVALS], timings, helper, run
    )
    return Draws.from_json({"start_state": startup.start_state(timings), **printed})


@dataclass(frozen=True)
class Refuel:
    """One tug's refuel: when its dispenser connections can start and end. A
    refused refuel makes no connections, and its start and end are the time
    its start is refused."""

    arrival_s: float
    volume_l: float
    start: Band
    end: Band
    refused: bool = False

    @property
    def certain(self) -> Band:
        """The span in which the refuel's connections exist in every case."""
        return (self.start[1], self.end[0])


@dataclass(frozen=True)
class Dock:
    """The dock operator's jobs by a time: each tug's refuel, refused or not."""

    jobs: tuple[Refuel, ...]


def dock(d: Draws, t: Timings, until_s: float) -> Dock:
    """The jobs that the dock operator serves by until_s, one at a time: the
    refuel of each tug that arrives by then, and the fill of the road tanker
    whose order is pending at the start. Waiting tugs are served in the order
    they arrived, and a road tanker waiting when the dock operator finishes a
    job before any waiting tug (sim.md §6). The FMS refuses a refuel at its
    start when the tank's volume less the preset, as the dispenser's register
    holds it, is at or below the cavitation threshold (fms.md §5.1, §7.4). A
    refused refuel makes no connections, and the dock operator cancels its job
    and serves the next at once. A fill's run time beyond its pumping was never
    measured, so its end carries the range of an unmeasured stage.

    At a fork, the job under way comes first. Where the road tanker may or may
    not be waiting when a job ends, it is served first. The other order matters
    to no admission: the job that ends is a tug's, since at most one order is
    pending, and that tug leaves before the latest start of every later refuel,
    which condition 2 of episodes.md §10.2 then excludes (departure_before)."""
    pending = [(at, volume, False) for at, volume in d.arrivals if at <= until_s]
    if d.order_pending_s is not None and d.order_pending_s <= until_s:
        pending.append((d.order_pending_s, d.delivery_volume_l, True))
    pending.sort(key=lambda arrival: arrival[0])
    jobs: list[Refuel] = []
    level = d.initial_volume_l
    before: Band | None = None
    u = d.under_way
    if u is not None and u.fill:
        pumping = u.remaining_l / t.inflow_l_per_s
        before = (
            u.start[0] + pumping,
            u.start[1] + pumping + REFUEL_TAIL_S + UNMEASURED_S[1],
        )
        level += u.remaining_l
    elif u is not None and u.departed:
        before = u.start
    elif u is not None:
        if u.refused or (
            not u.started and level - register_preset_l(u.volume_l) <= t.cavitation_l
        ):
            jobs.append(Refuel(u.arrival_s, u.volume_l, u.start, u.start, refused=True))
            before = u.start
        else:
            dispense = u.remaining_l / t.outflow_l_per_s
            before = (u.start[0] + dispense, u.start[1] + dispense + REFUEL_TAIL_S)
            jobs.append(Refuel(u.arrival_s, u.remaining_l, u.start, before))
            level -= u.remaining_l
    while pending:
        at, volume, fill = pending[0]
        if before is not None and not fill and at <= before[1]:
            tanker = next((p for p in pending if p[2] and p[0] <= before[1]), None)
            if tanker is not None:
                at, volume, fill = tanker
        pending.remove((at, volume, fill))
        start = (at + REFUEL_START_S[0], at + REFUEL_START_S[1])
        if before is not None:
            start = (max(start[0], before[0]), max(start[1], before[1] + HANDOVER_S))
        if fill:
            pumping = volume / t.inflow_l_per_s
            before = (
                start[0] + pumping,
                start[1] + pumping + REFUEL_TAIL_S + UNMEASURED_S[1],
            )
            level += volume
            continue
        if level - register_preset_l(volume) <= t.cavitation_l:
            jobs.append(Refuel(at, volume, start, start, refused=True))
            before = start
            continue
        dispense = volume / t.outflow_l_per_s
        before = (start[0] + dispense, start[1] + dispense + REFUEL_TAIL_S)
        jobs.append(Refuel(at, volume, start, before))
        level -= volume
    return Dock(tuple(jobs))


def departure_before(
    jobs: Sequence[Refuel], needed: Refuel, leaving: Sequence[float] = ()
) -> float | None:
    """The earliest time inside the episode at which a tug served before
    `needed` can leave the dock, when that falls before the latest start of
    `needed`, or None. A tug leaves once its refuel ends, or once the FMS has
    refused it, and every tug in the dock's jobs is at the dock at the start,
    or comes to it later, so it leaves inside the episode. Its return is not
    predicted, since the simulator draws its next time away only as it leaves
    (episodes.md §10.2, condition 2)."""
    times = [max(0.0, job.end[0]) for job in jobs if job is not needed]
    times += [max(0.0, at) for at in leaving]
    before = [at for at in times if at < needed.start[1]]
    return min(before) if before else None


def register_preset_l(volume_l: float) -> float:
    """The preset that the dispenser's register holds for a request: the
    request at the register's resolution of 0.1 L, a half rounded away from
    zero, as the FMS checks it (fms.md §7.4)."""
    return math.floor(volume_l * 10.0 + 0.5) / 10.0


def refuels(d: Draws, t: Timings, until_s: float) -> list[Refuel]:
    """The refuels that make connections, of the tugs that arrive by until_s."""
    return [r for r in dock(d, t, until_s).jobs if not r.refused]


@dataclass(frozen=True)
class Polls:
    """The supplier's polls up to a time, and why the list ends there."""

    times: tuple[Band, ...]
    end: str


def polls(d: Draws, t: Timings, until_s: float) -> Polls:
    """The supplier's polls by until_s, each as its range around its due time.
    The supplier's polls fall due every poll interval from the first,
    including while an order is pending and through a delivery, so an order
    does not move the polls (sim.md §5.3). Every range runs from POLL_EARLY_S
    before the due time to POLL_SLIP_S after it."""
    if d.first_poll_after_s is None:
        return Polls((), "the supplier's next poll is unknown at the fork")
    due = float(d.first_poll_after_s)
    times: list[Band] = []
    while due - POLL_EARLY_S <= until_s:
        times.append((due - POLL_EARLY_S, due + POLL_SLIP_S))
        due += t.poll_s
    if not times:
        return Polls((), "the first poll falls after the horizon")
    return Polls(tuple(times), "the horizon")


@dataclass(frozen=True)
class Admission:
    chain: str
    admitted: bool
    reason: str | None = None


def admit(
    chain: chains.Chain,
    d: Draws,
    t: Timings,
    onset_step: int,
    waits_s: Sequence[float],
    horizon_s: float = HORIZON * STEP_S,
    step_0_s: Band = STEP_0_AFTER_START_S,
) -> Admission:
    """Whether the episode holds every activity the chain's stages need, and
    leaves the allowance after the last stage begins, in every case the bands
    allow. Every time counts from the simulator's start at fork 0, and from
    the fork instant at a later fork, where step 0 falls at step_0_s."""
    until = step_0_s[1] + horizon_s
    deadline = step_0_s[0] + horizon_s - MARGIN_S
    queue = dock(d, t, until)
    refuels_ = [r for r in queue.jobs if not r.refused]
    polls_ = polls(d, t, until)
    lo = step_0_s[0] + onset_step * STEP_S
    hi = step_0_s[1] + onset_step * STEP_S
    scans: dict[str, Band] = {}
    # Stage times count from step 0 until a stage waits for a poll, whose time
    # counts from the simulator's start.
    from_step_0 = True
    for stage, wait in zip(chain.stages, waits_s, strict=True):
        lo, hi = lo + wait, hi + wait
        last_begin, last_from_step_0 = hi, from_step_0
        review = chains.REVIEWED.get(stage.name, chains.Review())
        if chains.AUDIT_COMPLETED in review.needs and not d.audit_completed:
            return Admission(
                chain.id,
                False,
                f"{stage.name} reads what a completed audit left, and the living "
                "port completed no audit before the fork",
            )
        if chains.SUPPLIER_POLL in review.needs and d.poll_overdue is not None:
            return Admission(
                chain.id,
                False,
                f"{stage.name} needs {chains.SUPPLIER_POLL}, and the start state "
                f"at the fork is uncertain: {d.poll_overdue}",
            )
        refuels = sorted(review.needs & set(SCAN_WINDOWS_S))
        if refuels and d.tanker_overdue is not None:
            return Admission(
                chain.id,
                False,
                f"{stage.name} needs {', '.join(refuels)}, and the dock's order of "
                f"service at the fork is uncertain: {d.tanker_overdue}",
            )
        if chains.SUPPLIER_POLL in review.needs:
            times = polls_.times
            certain = [p for p in times if p[0] >= hi + POLL_SETUP_S]
            if not certain or certain[0][1] > deadline:
                return Admission(
                    chain.id,
                    False,
                    f"{stage.name} finds no certain poll ({polls_.end})",
                )
            possible = [p for p in times if p[1] >= lo]
            # The intercepted poll can end INTERCEPT_DELAY_S after its range,
            # and delays no later poll.
            lo, hi = possible[0][0], certain[0][1] + INTERCEPT_DELAY_S
            from_step_0 = False
            continue
        for need in review.needs & set(SCAN_WINDOWS_S):
            window = scans.get(need)
            covering = [
                r
                for r in refuels_
                if window is not None
                and min(window[1], r.certain[1]) - max(window[0], r.certain[0])
                >= MARGIN_S
            ]
            if not covering:
                return Admission(
                    chain.id, False, f"{stage.name} finds no refuel in its scan"
                )
            # Condition 2: a tug that leaves the dock before the refuel's
            # latest start may return first, and its return is not predicted.
            leaving = [departure_before(queue.jobs, r, d.leaving_s) for r in covering]
            if all(at is not None for at in leaving):
                return Admission(
                    chain.id,
                    False,
                    f"{stage.name} needs a refuel in its scan, and a tug may "
                    f"leave the dock {min(leaving):.0f} s into the episode, "
                    "before that refuel's latest start; its return is not "
                    "predicted",
                )
        if stage.name == INVENTORY_STAGE:
            scans = {
                need: (hi + offsets[0], lo + offsets[1])
                for need, offsets in SCAN_WINDOWS_S.items()
            }
        seconds = review.seconds or UNMEASURED_S
        lo, hi = lo + seconds[0], hi + seconds[1]
    step_0 = step_0_s[1] if last_from_step_0 else step_0_s[0]
    if last_begin - step_0 + ALLOWANCE_S > horizon_s:
        return Admission(
            chain.id,
            False,
            f"{chain.stages[-1].name} may begin within {ALLOWANCE_S:g} s of the horizon",
        )
    return Admission(chain.id, True)


# The most seeds drawn in search of one that admits a chain the command line
# gives.
MAX_SEEDS = 1000


def admissions(
    candidates: Sequence[chains.Chain],
    episode_seed: str,
    t: Timings,
    draws_: Callable[[str, Timings], Draws] = draws,
) -> dict[str, Admission]:
    """The admission of each candidate chain to the episode of the seed, with
    the episode's own onset step and waits (episodes.md §10, onset and
    pacing)."""
    return admissions_at(candidates, draws_(episode_seed, t), episode_seed, t)


def admissions_at(
    candidates: Sequence[chains.Chain],
    d: Draws,
    episode_seed: str,
    t: Timings,
    step_0_s: Band = STEP_0_AFTER_START_S,
) -> dict[str, Admission]:
    """The admission of each candidate chain to an episode whose start state
    gives `d`, with the onset step and the waits of the episode seed."""
    if not candidates:
        return {}
    seeds = derive_seeds(episode_seed)
    onset = pacing.onset_step(seeds[ATTACK_START_STEP])
    longest = max(len(c.stages) for c in candidates)
    waits = pacing.waits(seeds[ATTACK_WAITS], longest)
    return {
        c.id: admit(c, d, t, onset, waits[: len(c.stages)], step_0_s=step_0_s)
        for c in candidates
    }


@dataclass(frozen=True)
class Choice:
    """The chain of an episode in a fork, or None when the fork admits no
    chain that the episode may run, and how the chain was chosen."""

    chain: chains.Chain | None
    chosen: dict[str, Any]


def choose_at_fork(
    library: Sequence[chains.Chain],
    given: chains.Chain | None,
    d: Draws,
    episode_seed: str,
    t: Timings,
    step_0_s: Band,
    rng: random.Random | None = None,
) -> Choice:
    """The chain of an episode in a fork, whose episode seed the fork fixes:
    one drawn uniformly from the chains of the library that the fork admits,
    or the chain the command line gives. A given chain that the fork does not
    admit is not admitted, and the living port's manager moves on to the next
    fork."""
    candidates = list(library)
    if given is not None and given not in candidates:
        candidates.append(given)
    result = admissions_at(candidates, d, episode_seed, t, step_0_s)
    admitted = [c for c in library if result[c.id].admitted]
    chosen: dict[str, Any] = {
        "library": [c.id for c in library],
        "admitted": [c.id for c in admitted],
        "excluded": {cid: a.reason for cid, a in result.items() if not a.admitted},
        "seeds_drawn": 1,
        "step_0_after_fork_s": [round(step_0_s[0], 3), round(step_0_s[1], 3)],
    }
    if given is None:
        chosen["by"] = "a uniform draw from the chains the fork admits"
        if not admitted:
            chosen["not_admitted"] = "the fork admits no chain of the library"
            return Choice(None, chosen)
        return Choice(chains.choose(admitted, rng), chosen)
    chosen["by"] = "the command line"
    if not result[given.id].admitted:
        chosen["not_admitted"] = result[given.id].reason
        return Choice(None, chosen)
    return Choice(given, chosen)


@dataclass(frozen=True)
class Plan:
    """An adversarial episode's seed and chain, and how the chain was chosen."""

    seed: str
    chain: chains.Chain
    chosen: dict[str, Any]


def plan(
    library: Sequence[chains.Chain],
    given: chains.Chain | None,
    seed: str | None,
    t: Timings,
    *,
    new_seed: Callable[[], str],
    draws_: Callable[[str, Timings], Draws] = draws,
    rng: random.Random | None = None,
) -> Plan:
    """The seed comes first, then the chain: one drawn uniformly from the
    chains of the library that the episode admits, or the chain the command
    line gives. At fork 0 the episode seed is the living port seed, so for a
    given chain fresh living port seeds are drawn until one admits it, up to
    MAX_SEEDS. A seed the command line gives, for a replay, is kept, and the
    record notes a given chain it does not admit."""
    candidates = list(library)
    if given is not None and given not in candidates:
        candidates.append(given)
    for drawn in range(1, MAX_SEEDS + 1):
        episode_seed = seed or new_seed()
        result = admissions(candidates, episode_seed, t, draws_)
        admitted = [c for c in library if result[c.id].admitted]
        chosen: dict[str, Any] = {
            "library": [c.id for c in library],
            "admitted": [c.id for c in admitted],
            "excluded": {cid: a.reason for cid, a in result.items() if not a.admitted},
            "seeds_drawn": drawn,
        }
        if given is None:
            chosen["by"] = "a uniform draw from the chains the episode admits"
            return Plan(episode_seed, chains.choose(admitted, rng), chosen)
        if result[given.id].admitted or seed is not None:
            chosen["by"] = "the command line"
            if not result[given.id].admitted:
                chosen["not_admitted"] = result[given.id].reason
            return Plan(episode_seed, given, chosen)
    raise chains.CatalogueError(f"no seed of {MAX_SEEDS} admits the chain {given.id}")
