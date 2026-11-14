"""The predicted episode events, and the attack chains that they support
(episodes.md §15, episode mix).

data/predicted-events-records.json holds, for 20 earlier live adversarial
episodes, the start state that the simulator drew for
each from the episode seed, and its tug arrivals, as the helper printed them
then, with the chain's stages, the onset step, the waits and what became of
the chain. Those episodes ran one tug, whose arrivals did not wait for its
departures, so each arrival stands for a tug of its own here. The tests use
the records as recorded. The bands of the predicted episode events come from
the same 20 episodes, so the agreement of admission with them checks the
rule's logic; it does not measure how often the bands hold for new seeds.

Run with: pytest competition/tests -q
"""

from __future__ import annotations

import dataclasses
import hashlib
import itertools
import json
import math
import random
import subprocess
from collections import Counter
from pathlib import Path

import pytest

from competition.episode_runner import chains, pacing, predicted_events
from competition.episode_runner.seeds import derive_seeds

RECORDS = json.loads(
    (
        Path(__file__).resolve().parent / "data" / "predicted-events-records.json"
    ).read_text()
)
TIMINGS = predicted_events.Timings(
    **{
        **RECORDS["timings"],
        "roster": tuple(tuple(tug) for tug in RECORDS["timings"]["roster"]),
        "refuel_volume_l": tuple(RECORDS["timings"]["refuel_volume_l"]),
    }
)
EPISODES = {e["record"][-8:]: e for e in RECORDS["episodes"]}
# The one failure that the predicted episode events do not model: the Wazuh
# manager refused active response for the HMI's agent, which accepted it in
# another episode.
UNMODELLED = {"71e22cb2"}
# The poll chains that stalled live because the supplier of an earlier
# version of the simulator stopped polling once it ordered. The supplier now polls through an order
# (sim.md §5.3), so these records no longer show what the simulator does.
ORDERED_LIVE = {"609a2dfb", "580e1292"}


def chain_of(episode: dict) -> chains.Chain:
    stages = tuple(
        chains.Stage(name, "", chains.Target("adversary", chains.ADVERSARY))
        for name in episode["stages"]
    )
    return chains.Chain(episode["chain"], "it-foothold", episode["stages"][-1], stages)


def draws_of(episode: dict) -> predicted_events.Draws:
    return predicted_events.Draws.from_json(episode["draws"])


def test_the_competition_timings_come_from_the_render_at_its_factor() -> None:
    assert (
        predicted_events.Timings(
            lead_time_s=4320.0,
            poll_s=180.0,
            check_s=17280.0,
            audit_period="144h",
            settling_delay_s=4320.0,
            outflow_l_per_s=25.0,
            inflow_l_per_s=50.0,
            cavitation_l=5000.0,
            delivery_volume_l=18000.0,
            roster=((1999997, 14400.0), (1999985, 14400.0), (1999973, 14400.0)),
            refuel_volume_l=(500.0, 3000.0),
            dock_operator_poll_s=6.0,
        )
        == predicted_events.competition_timings()
    )


def test_the_helper_takes_the_tug_seed_the_request_bounds_and_the_roster() -> None:
    """At fork 0 the helper prints each tug's first arrival, and the start
    state is the simulator's own, which the start-up input gives."""
    seen: list[list[str]] = []
    printed = {
        "delivery_volume_l": 18000.0,
        "tug_arrivals": [
            {"vessel": 1999985, "at_s": 640.0, "volume_l": 1200.0},
            {"vessel": 1999997, "at_s": 9000.0, "volume_l": 800.0},
        ],
    }

    def run(argv, **kwargs):
        seen.append(argv)
        assert kwargs["stdin"] is subprocess.DEVNULL
        return subprocess.CompletedProcess(argv, 0, json.dumps(printed), "")

    seed = "ab" * 32
    timings = predicted_events.competition_timings()
    d = predicted_events.draws(seed, timings, helper=Path("helper"), run=run)
    seeds = derive_seeds(seed)
    assert seen == [
        [
            "helper",
            str(seeds["sim_tug_arrivals"]),
            "500.0",
            "3000.0",
            "1999997:14400.0",
            "1999985:14400.0",
            "1999973:14400.0",
        ]
    ]
    assert d == predicted_events.Draws(
        initial_volume_l=35_000.0,
        order_pending_s=None,
        first_poll_after_s=timings.poll_s,
        delivery_volume_l=18000.0,
        arrivals=((640.0, 1200.0), (9000.0, 800.0)),
    )


def test_a_failing_helper_is_reported() -> None:
    def run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 2, "", "usage: predicted-events ...")

    with pytest.raises(predicted_events.PredictedEventsError, match="usage"):
        predicted_events.draws("ab" * 32, TIMINGS, run=run)


def test_a_refuel_queues_behind_the_one_before_it() -> None:
    """Episode 580e1292: two tugs arrived 4 s apart, and one run of dispenser
    connections lasted about 190 s."""
    first, second = predicted_events.refuels(
        draws_of(EPISODES["580e1292"]), TIMINGS, 840.0
    )[:2]
    assert second.arrival_s - first.arrival_s < 5.0
    assert second.start[0] == first.end[0]
    assert second.end[1] - first.start[0] == pytest.approx(
        (first.volume_l + second.volume_l) / 25.0
        + predicted_events.REFUEL_START_S[1]
        - predicted_events.REFUEL_START_S[0]
        + 6.0
        + 2 * 5.0,
        abs=0.01,
    )


def test_the_supplier_polls_every_interval() -> None:
    """Episode caf7178c: no order, 21,397 L; polls 180 s apart."""
    d = draws_of(EPISODES["caf7178c"])
    polls = predicted_events.polls(d, TIMINGS, 840.0)
    starts = [p[0] for p in polls.times]
    ends = [p[1] for p in polls.times]
    # Every range runs from 0.3 s before its due time to 2 s after it, and no
    # range widens (episode-runner-impl.md §8).
    assert starts[0] == d.first_poll_after_s - predicted_events.POLL_EARLY_S
    assert ends[0] == d.first_poll_after_s + predicted_events.POLL_SLIP_S
    assert [round(b - a, 6) for a, b in itertools.pairwise(starts)] == [180.0] * (
        len(starts) - 1
    )
    assert [round(b - a, 6) for a, b in itertools.pairwise(ends)] == [180.0] * (
        len(ends) - 1
    )
    assert polls.end == "the horizon"


def test_an_order_does_not_end_the_polls() -> None:
    """Episode 609a2dfb: no order, 14,057 L, so the first poll orders, and the
    supplier keeps polling through the order (sim.md §5.3)."""
    d = draws_of(EPISODES["609a2dfb"])
    polls = predicted_events.polls(d, TIMINGS, 840.0)
    assert len(polls.times) > 1
    assert polls.end == "the horizon"


def test_the_polls_keep_their_interval_through_a_pending_order() -> None:
    """Episode 64827930: an order with 1,189 s of lead time left, and a first
    poll at 180 s; the polls keep their interval through the order."""
    d = dataclasses.replace(draws_of(EPISODES["64827930"]), first_poll_after_s=180.0)
    polls = predicted_events.polls(d, TIMINGS, 840.0)
    assert polls.times[0] == (180.0 - predicted_events.POLL_EARLY_S, 182.0)
    assert len(polls.times) == 4
    assert polls.end == "the horizon"


# The inventory stage as the earlier live episodes ran it, with
# the FMS's and the HMI's scans alone: its windows and run time before the
# relay's scan came between them (chains.py, the inventory stage's review).
RECORDED_SCAN_WINDOWS_S = {
    chains.REFUEL_DURING_FMS_SCAN: (1.0, 75.0),
    chains.REFUEL_DURING_HMI_SCAN: (77.0, 150.0),
}
RECORDED_INVENTORY = chains.Review(seconds=(150.5, 152.5))


@pytest.mark.parametrize("record", sorted(EPISODES))
def test_admission_agrees_with_each_live_episode(
    record: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A chain that completed live is admitted, and a chain that stalled or
    failed for want of a modelled activity is not. The live episodes ran the
    inventory stage before the relay's scan joined it, so they are judged with
    its timings of then."""
    monkeypatch.setattr(predicted_events, "SCAN_WINDOWS_S", RECORDED_SCAN_WINDOWS_S)
    monkeypatch.setitem(
        chains.REVIEWED, predicted_events.INVENTORY_STAGE, RECORDED_INVENTORY
    )
    episode = EPISODES[record]
    admission = predicted_events.admit(
        chain_of(episode),
        draws_of(episode),
        TIMINGS,
        episode["onset_step"],
        episode["waits_s"],
    )
    if record in ORDERED_LIVE:
        pytest.skip("the supplier of this episode stopped polling once it ordered")
    expected = episode["outcome"] == "completed" or record in UNMODELLED
    assert admission.admitted is expected, admission.reason


def _needing_no_port_activity() -> list[chains.Chain]:
    """The chains of the library, from the sample catalogue and the recorded
    episodes, whose stages need neither a port activity nor a completed
    audit."""
    candidates = {c.id: c for c in SAMPLE_LIBRARY}
    for episode in EPISODES.values():
        candidates.setdefault(episode["chain"], chain_of(episode))
    return [c for c in chains.library(list(candidates.values())) if not c.needs]


def _latest_last_begin_s(chain: chains.Chain) -> float:
    """The latest time after step 0 at which the chain's last stage can
    begin: the latest onset step, every wait at its upper bound, and each
    earlier stage's longest run time."""
    latest = pacing.ONSET_LAST_STEP * predicted_events.STEP_S + pacing.WAIT_UPPER_S
    for stage in chain.stages[:-1]:
        review = chains.REVIEWED.get(stage.name, chains.Review())
        latest += (review.seconds or predicted_events.UNMEASURED_S)[
            1
        ] + pacing.WAIT_UPPER_S
    return latest


def _fits_the_horizon(chain: chains.Chain) -> bool:
    """Whether the chain's last stage begins at least the allowance before
    the horizon at the latest onset with the longest waits."""
    horizon_s = predicted_events.HORIZON * predicted_events.STEP_S
    return _latest_last_begin_s(chain) + predicted_events.ALLOWANCE_S <= horizon_s


def test_a_chain_that_needs_no_port_activity_is_excluded_only_by_the_allowance() -> (
    None
):
    """Over 10,000 episode seeds, each chain of the library whose stages need
    neither a port activity nor a completed audit is admitted, or excluded by
    condition 4 alone: its last stage may begin within the allowance of the
    horizon. Such a chain's admission depends on the onset step and the waits
    alone, so one recorded start state serves every seed. A chain whose last
    stage begins the allowance before the horizon at the latest onset with
    the longest waits is admitted to every episode; a chain that ends with a
    null route may begin its last stage later (episodes-tests.md,
    a_chain_that_needs_no_port_activity_is_excluded_only_by_the_allowance;
    episodes.md §10.2, choosing the chain, and §11, horizon and
    termination)."""
    free = _needing_no_port_activity()
    fitting = {c.id for c in free if _fits_the_horizon(c)}
    assert DISCLOSE.id in fitting
    d = draws_of(EPISODES["609a2dfb"])
    allowance = {
        c.id: f"{c.stages[-1].name} may begin within {predicted_events.ALLOWANCE_S:g} s "
        "of the horizon"
        for c in free
    }
    # At the latest onset with the longest waits, the chains that fit the
    # horizon are admitted and the others are excluded by condition 4.
    for c in free:
        latest = predicted_events.admit(
            c,
            d,
            TIMINGS,
            pacing.ONSET_LAST_STEP,
            [pacing.WAIT_UPPER_S] * len(c.stages),
        )
        assert latest.admitted is (c.id in fitting), latest.reason
        assert latest.admitted or latest.reason == allowance[c.id]
    for n in range(10_000):
        seed = hashlib.sha256(f"episode {n}".encode()).hexdigest()
        for cid, admission in predicted_events.admissions_at(
            free, d, seed, TIMINGS
        ).items():
            if cid in fitting:
                assert admission.admitted, (seed, admission.reason)
            elif not admission.admitted:
                assert admission.reason == allowance[cid], (seed, admission.reason)


@pytest.mark.parametrize(("first_s", "admitted"), [(585.0, True), (590.0, False)])
def test_an_intercepted_poll_delays_only_itself(first_s: float, admitted: bool) -> None:
    """The supplier's polls keep their due times, so a poll that a stage
    catches delays no later poll. At fork 0 the deadline falls 770 s after the
    simulator's start. With the first poll due at 585 s, the second poll's
    range ends at 767 s, inside the deadline, and the chain that catches both
    is admitted; with the first due at 590 s, the second's range ends at
    772 s, past the deadline."""
    d = predicted_events.Draws(
        initial_volume_l=30000.0,
        order_pending_s=None,
        first_poll_after_s=first_s,
        delivery_volume_l=18000.0,
        arrivals=(),
    )
    stages = tuple(
        chains.Stage(name, "", chains.Target("adversary", chains.ADVERSARY))
        for name in ("intercept-supplier-poll", "capture-relay-reply")
    )
    chain = chains.Chain("external-blind__test", "external-blind", "test", stages)
    admission = predicted_events.admit(chain, d, TIMINGS, 6, [5.0, 5.0])
    assert admission.admitted is admitted
    if not admitted:
        assert admission.reason == (
            "capture-relay-reply finds no certain poll (the horizon)"
        )


def _poll_chain(*names: str) -> chains.Chain:
    stages = tuple(
        chains.Stage(name, "", chains.Target("adversary", chains.ADVERSARY))
        for name in names
    )
    return chains.Chain("external-informed__test", "external-informed", "test", stages)


def _polls_from(first_s: float) -> predicted_events.Draws:
    return predicted_events.Draws(
        initial_volume_l=30000.0,
        order_pending_s=None,
        first_poll_after_s=first_s,
        delivery_volume_l=18000.0,
        arrivals=(),
    )


def test_a_chain_whose_last_stage_begins_inside_the_allowance_is_excluded() -> None:
    """With the poll at 740 s, the last stage begins as late as 757 s, 692 s
    after the earliest step 0, and the allowance of 45 s overruns the 720-second
    horizon. With the poll at 700 s, the last stage begins by 652 s after step 0."""
    chain = _poll_chain("intercept-supplier-poll", "inject-false-feed")
    late = predicted_events.admit(chain, _polls_from(740.0), TIMINGS, 6, [5.0, 5.0])
    assert not late.admitted
    assert late.reason == "inject-false-feed may begin within 45 s of the horizon"
    assert predicted_events.admit(
        chain, _polls_from(700.0), TIMINGS, 6, [5.0, 5.0]
    ).admitted


def test_a_chain_that_waits_for_a_poll_is_admitted_with_an_order_pending() -> None:
    """The supplier polls through an order and its delivery, so an order
    pending at the start, or a fill under way at the fork, leaves a chain that
    waits for a poll admitted (episodes.md §10.2)."""
    chain = _poll_chain("intercept-supplier-poll", "inject-false-feed")
    pending = _fork(order_pending_s=100.0)
    assert predicted_events.admit(chain, pending, TIMINGS, 6, [5.0, 5.0]).admitted
    fill = predicted_events.UnderWay(True, (0.0, 0.0), 9000.0, True)
    filling = _fork(under_way=fill)
    assert predicted_events.admit(chain, filling, TIMINGS, 6, [5.0, 5.0]).admitted


def test_a_refused_refuel_makes_no_connections() -> None:
    """The first refuel takes the tank from 5,500 L to 5,100 L. The FMS
    refuses the next at its start, because the tank's volume less its
    request, 4,500 L, is at or below the cavitation threshold, although the
    tank holds more than the threshold as the job opens."""
    d = predicted_events.Draws(
        initial_volume_l=5500.0,
        order_pending_s=None,
        first_poll_after_s=10000.0,
        delivery_volume_l=18000.0,
        arrivals=((100.0, 400.0), (300.0, 600.0)),
    )
    first, second = predicted_events.dock(d, TIMINGS, 840.0).jobs
    assert not first.refused
    assert second.refused
    assert second.start == second.end
    assert predicted_events.refuels(d, TIMINGS, 840.0) == [first]


def test_a_tug_waits_for_the_road_tankers_fill() -> None:
    """The road tanker arrives at 100 s and pumps 18,000 L at 50 L/s; the tug
    that arrives at 150 s waits for the fill, and the supplier's polls keep
    their interval through the fill (sim.md §5.3)."""
    d = predicted_events.Draws(
        initial_volume_l=20000.0,
        order_pending_s=100.0,
        first_poll_after_s=180.0,
        delivery_volume_l=18000.0,
        arrivals=((150.0, 1000.0),),
    )
    (refuel,) = predicted_events.dock(d, TIMINGS, 840.0).jobs
    # The fill pumps from 100.5 to 112 s for 360 s, and its end carries the
    # 5 s tail and an unmeasured stage's 30 s.
    fill_end = (100.0 + 360.0, 112.0 + 360.0 + 5.0 + 30.0)
    assert refuel.start == (fill_end[0], fill_end[1] + predicted_events.HANDOVER_S)
    polls = predicted_events.polls(d, TIMINGS, 840.0)
    assert polls.times[0] == (180.0 - predicted_events.POLL_EARLY_S, 182.0)
    assert polls.times[1] == (360.0 - predicted_events.POLL_EARLY_S, 362.0)


def test_a_waiting_tanker_is_served_before_a_waiting_tug() -> None:
    """At the fork a refuel ends at 20 to 25 s, and a tug waits from before the
    fork. A road tanker that arrives at 10 s waits when the refuel ends, and
    comes first (sim.md §6); so does one that arrives at 22 s and may or may
    not wait by then. Either way, the tug that the refuel's end lets go leaves
    before the waiting tug's latest start, so condition 2 excludes any chain
    that needs the waiting tug's refuel, and the order matters to no
    admission."""
    under = predicted_events.UnderWay(False, (0.0, 0.0), 500.0, True, -60.0, 1000.0)
    for tanker_s in (10.0, 22.0):
        d = _fork(
            under_way=under,
            order_pending_s=tanker_s,
            initial_volume_l=20000.0,
            arrivals=((-10.0, 800.0),),
        )
        queue = predicted_events.dock(d, TIMINGS, 840.0)
        first, tug = queue.jobs
        fill_start = max(tanker_s + predicted_events.REFUEL_START_S[0], first.end[0])
        assert tug.start[0] == fill_start + 18000.0 / 50.0
        assert predicted_events.departure_before(queue.jobs, tug) == first.end[0]


# A chain whose last stage needs a refuel during the HMI's scan.
NEEDS_REFUEL = chains.Chain(
    "it-foothold__test",
    "it-foothold",
    "test",
    tuple(
        chains.Stage(name, "", chains.Target("adversary", chains.ADVERSARY))
        for name in (
            "scan-it-segment",
            "acquire-siem-management-credential",
            "enumerate-siem-agent-inventory",
            "deny-operator-control-via-route-null",
        )
    ),
)


def test_a_departure_before_a_needed_refuel_excludes_that_chain() -> None:
    """A tug leaves the dock inside the episode before the latest start of the
    refuel that the HMI's scan needs, so its return could come first, and the
    episode runner excludes that chain alone. The chain that waits for the
    supplier's poll stays admitted (episodes.md §10.2, condition 2)."""
    needs_refuel = NEEDS_REFUEL
    waits = [5.0, 5.0, 5.0, 5.0]
    # One tug alone, whose refuel spans the HMI's scan: admitted.
    alone = _fork(arrivals=((520.0, 3000.0),))
    assert predicted_events.admit(needs_refuel, alone, TIMINGS, 6, waits).admitted
    # The same refuel behind another tug's, which ends before it starts.
    behind = _fork(arrivals=((300.0, 500.0), (520.0, 3000.0)))
    admission = predicted_events.admit(needs_refuel, behind, TIMINGS, 6, waits)
    assert not admission.admitted
    assert "may leave the dock" in admission.reason
    assert "its return is not predicted" in admission.reason
    poll = _poll_chain("intercept-supplier-poll", "inject-false-feed")
    assert predicted_events.admit(poll, behind, TIMINGS, 6, [5.0, 5.0]).admitted


# A fork after fork 0 (episodes.md §10.2, choosing the chain): every time
# counts from the fork instant.


def _fork(**changes) -> predicted_events.Draws:
    return dataclasses.replace(_polls_from(300.0), **changes)


def test_a_refuel_under_way_at_the_fork_comes_first_in_the_dock() -> None:
    """The refuel under way has 500 L still to pump at 25 L/s, so the tug that
    waited at the fork starts after its end and the handover."""
    under = predicted_events.UnderWay(False, (0.0, 0.0), 500.0, True, -26.0, 1000.0)
    d = _fork(under_way=under, arrivals=((-10.0, 800.0), (600.0, 900.0)))
    first, second, third = predicted_events.dock(d, TIMINGS, 840.0).jobs
    assert (first.start, first.end) == ((0.0, 0.0), (20.0, 25.0))
    assert second.start == (20.0, 25.0 + predicted_events.HANDOVER_S)
    assert third.start == (600.0, 612.0)


def test_every_poll_range_runs_from_before_its_due_time_to_after_it() -> None:
    """From 0.3 s before each due time to 2 s after it, the first poll at a
    fork included, and no range widens (episode-runner-impl.md §8)."""
    found = predicted_events.polls(_fork(first_poll_after_s=100.0), TIMINGS, 840.0)
    assert found.times[0] == (100.0 - predicted_events.POLL_EARLY_S, 102.0)
    assert found.times[1] == (280.0 - predicted_events.POLL_EARLY_S, 282.0)
    assert found.times[3] == (640.0 - predicted_events.POLL_EARLY_S, 642.0)


def test_a_fill_under_way_at_the_fork_leaves_the_polls_on_their_interval() -> None:
    under = predicted_events.UnderWay(True, (0.0, 0.0), 5000.0, True)
    d = _fork(
        first_poll_after_s=60.0,
        under_way=under,
        initial_volume_l=28000.0,
        arrivals=((10.0, 500.0),),
    )
    # The fill's 5,000 L end at 100 s, and the tug that waits for it starts
    # after the tail, an unmeasured stage and the handover.
    (tug,) = predicted_events.dock(d, TIMINGS, 840.0).jobs
    assert tug.start == (100.0, 100.0 + 5.0 + 30.0 + predicted_events.HANDOVER_S)
    found = predicted_events.polls(d, TIMINGS, 840.0)
    assert found.times[0] == (60.0 - predicted_events.POLL_EARLY_S, 62.0)


def test_an_unknown_next_poll_admits_no_poll() -> None:
    found = predicted_events.polls(_fork(first_poll_after_s=None), TIMINGS, 840.0)
    assert found == predicted_events.Polls(
        (), "the supplier's next poll is unknown at the fork"
    )


def test_step_0_at_a_fork_counts_from_the_fork_instant() -> None:
    """With the poll at 740 s, a step 0 at 65 to 100 s leaves the last stage
    inside the allowance. A fork's step 0, known at 120 s after the fork
    instant, moves the horizon's end to 840 s, and the chain is admitted."""
    chain = _poll_chain("intercept-supplier-poll", "inject-false-feed")
    d = _polls_from(740.0)
    assert not predicted_events.admit(chain, d, TIMINGS, 6, [5.0, 5.0]).admitted
    assert predicted_events.admit(
        chain, d, TIMINGS, 6, [5.0, 5.0], step_0_s=(120.0, 120.0)
    ).admitted


HISTORY = chains.Chain(
    "it-foothold__test",
    "it-foothold",
    "test",
    tuple(
        chains.Stage(name, "", chains.Target("adversary", chains.ADVERSARY))
        for name in ("scan-it-segment", "harvest-grafana-credential-from-history")
    ),
)


def test_a_chain_that_reads_what_an_audit_left_needs_a_completed_audit() -> None:
    """At fork 0 the living port has completed no audit, and a later fork
    admits the chain once the living port has completed one."""
    assert not _fork().audit_completed
    before = predicted_events.admit(HISTORY, _fork(), TIMINGS, 6, [5.0, 5.0])
    assert not before.admitted
    assert before.reason == (
        "harvest-grafana-credential-from-history reads what a completed audit "
        "left, and the living port completed no audit before the fork"
    )
    assert predicted_events.admit(
        HISTORY, _fork(audit_completed=True), TIMINGS, 6, [5.0, 5.0]
    ).admitted


def test_an_overdue_poll_excludes_only_the_poll_chains() -> None:
    """A poll overdue at the fork leaves the start state uncertain, which
    excludes the chains that need a poll; a chain that needs a refuel, which
    the predicted episode events admit, stays admitted (episodes.md §6.4, an
    overdue poll)."""
    overdue = (
        "the supplier's poll due at 2026-11-11T18:09:00.000000Z had not come by "
        "the fork"
    )
    d = _fork(poll_overdue=overdue, arrivals=((520.0, 3000.0),))
    poll = predicted_events.admit(
        _poll_chain("intercept-supplier-poll", "inject-false-feed"),
        d,
        TIMINGS,
        6,
        [5.0, 5.0],
    )
    assert not poll.admitted
    assert poll.reason == (
        "intercept-supplier-poll needs supplier_poll, and the start state at the "
        f"fork is uncertain: {overdue}"
    )
    plain = _poll_chain("discover-gateway", "inject-false-feed")
    assert predicted_events.admit(plain, d, TIMINGS, 6, [5.0, 5.0]).admitted
    assert predicted_events.admit(NEEDS_REFUEL, d, TIMINGS, 6, [5.0] * 4).admitted


def test_an_overdue_road_tanker_with_its_overdue_poll_excludes_both_kinds() -> None:
    """A poll under way holds the road tanker's arrival: the dock's order of
    service is uncertain, which excludes the chains that need a refuel, and
    the poll is overdue, which excludes those that need a poll. A chain that
    needs neither stays admitted (episodes.md §6.4, an overdue poll)."""
    poll = (
        "the supplier's poll due at 2026-11-11T19:15:00.000000Z had not come by "
        "the fork"
    )
    tanker = (
        "the road tanker of the order placed at 2026-11-11T18:03:00.000000Z was "
        "due at 2026-11-11T19:15:00.000000Z, and the simulator's log holds no "
        "arrival of it"
    )
    d = _fork(poll_overdue=poll, tanker_overdue=tanker, arrivals=((520.0, 3000.0),))
    assert d.uncertain == f"{poll}; {tanker}"
    by_poll = predicted_events.admit(
        _poll_chain("intercept-supplier-poll", "inject-false-feed"),
        d,
        TIMINGS,
        6,
        [5.0, 5.0],
    )
    assert by_poll.reason == (
        "intercept-supplier-poll needs supplier_poll, and the start state at the "
        f"fork is uncertain: {poll}"
    )
    by_refuel = predicted_events.admit(NEEDS_REFUEL, d, TIMINGS, 6, [5.0] * 4)
    assert not by_refuel.admitted
    assert by_refuel.reason == (
        "deny-operator-control-via-route-null needs refuel_during_hmi_scan, and "
        f"the dock's order of service at the fork is uncertain: {tanker}"
    )
    plain = _poll_chain("discover-gateway", "inject-false-feed")
    assert predicted_events.admit(plain, d, TIMINGS, 6, [5.0, 5.0]).admitted


SAMPLE_CHAINS = chains.load(
    Path(__file__).resolve().parent / "data" / "chains-sample.json"
)
SAMPLE_LIBRARY = chains.library(SAMPLE_CHAINS)
DISCLOSE = chains.find(SAMPLE_CHAINS, "it-foothold__disclose-account-inventory")
EXFILTRATE = chains.find(SAMPLE_CHAINS, "external-informed__exfiltrate-inventory")
# A chain that needs a refuel during the HMI's peer scan.
REFUEL = chains.Chain(
    "it-foothold__deny-operator-control-via-route-null",
    "it-foothold",
    "deny-operator-control-via-route-null",
    tuple(
        chains.Stage(name, "", chains.Target("adversary", chains.ADVERSARY))
        for name in (
            "scan-it-segment",
            "acquire-siem-management-credential",
            "enumerate-siem-agent-inventory",
            "deny-operator-control-via-route-null",
        )
    ),
)
FORK_SEED = "e5bfee0bcf01978597b5a6a89108e98698ad609da3fb915c9231c664eb12ac86"


def test_a_given_chain_that_the_fork_admits_is_chosen() -> None:
    choice = predicted_events.choose_at_fork(
        SAMPLE_LIBRARY, DISCLOSE, _fork(), FORK_SEED, TIMINGS, (50.0, 50.0)
    )
    assert choice.chain == DISCLOSE
    assert choice.chosen["by"] == "the command line"
    assert choice.chosen["seeds_drawn"] == 1
    assert choice.chosen["step_0_after_fork_s"] == [50.0, 50.0]
    assert "not_admitted" not in choice.chosen


def test_a_given_chain_that_the_fork_rules_out_is_not_admitted() -> None:
    """A fork whose predicted episode events hold no refuel during the
    inventory stage's peer scan rules out a chain that needs one, and the
    fork's seed is the only seed; the chain passes to the next fork."""
    choice = predicted_events.choose_at_fork(
        SAMPLE_LIBRARY, REFUEL, _fork(), FORK_SEED, TIMINGS, (50.0, 50.0)
    )
    assert choice.chain is None
    assert choice.chosen["not_admitted"] == (
        "deny-operator-control-via-route-null finds no refuel in its scan"
    )
    assert choice.chosen["seeds_drawn"] == 1
    assert REFUEL.id in choice.chosen["excluded"]


def chi_squared_survival(x: float, df: int) -> float:
    """The chance that a chi-squared variable with df degrees of freedom
    exceeds x, in closed form for a whole df."""
    if df % 2 == 0:
        term = total = math.exp(-x / 2)
        for i in range(1, df // 2):
            term *= x / (2 * i)
            total += term
        return total
    total = math.erfc(math.sqrt(x / 2))
    term = math.sqrt(2 * x / math.pi) * math.exp(-x / 2)
    for i in range(3, df + 1, 2):
        total += term
        term *= x / i
    return total


def test_chi_squared_survival_gives_the_tabled_critical_values() -> None:
    # The 1 % points of the chi-squared distribution for 1 to 4 degrees of
    # freedom.
    for df, critical in ((1, 6.635), (2, 9.210), (3, 11.345), (4, 13.277)):
        assert chi_squared_survival(critical, df) == pytest.approx(0.01, abs=1e-4)


def test_the_chain_is_drawn_uniformly_from_the_admitted_chains() -> None:
    """Over 10,000 draws for one episode, whose fork admits a fixed set of
    four chains of the library, each admitted chain is drawn equally often:
    a chi-squared test at the 1 % level decides. The chain that the fork
    excludes is never drawn (episodes-tests.md,
    the_chain_is_drawn_uniformly_from_the_admitted_chains; episodes.md
    §10.2, choosing the chain; episodes-tests.md §0.5, statistical tests)."""
    d = _fork(first_poll_after_s=None)
    always = [
        c
        for c in _needing_no_port_activity()
        if _fits_the_horizon(c) and c.id != DISCLOSE.id
    ]
    library = [*SAMPLE_LIBRARY, *always]
    rng = random.Random(2026)
    counts: Counter[str] = Counter()
    for _ in range(10_000):
        choice = predicted_events.choose_at_fork(
            library, None, d, FORK_SEED, TIMINGS, (50.0, 50.0), rng=rng
        )
        assert choice.chosen["by"] == "a uniform draw from the chains the fork admits"
        counts[choice.chain.id] += 1
    admitted = choice.chosen["admitted"]
    assert len(admitted) == 4 and EXFILTRATE.id not in admitted
    assert set(counts) == set(admitted)
    expected = 10_000 / len(admitted)
    statistic = sum((counts[c] - expected) ** 2 / expected for c in admitted)
    assert chi_squared_survival(statistic, len(admitted) - 1) > 0.01, counts


def test_a_fork_that_admits_no_chain_of_the_library_is_reported() -> None:
    d = _fork(first_poll_after_s=None)
    choice = predicted_events.choose_at_fork(
        [EXFILTRATE], None, d, FORK_SEED, TIMINGS, (50.0, 50.0)
    )
    assert choice.chain is None
    assert choice.chosen["not_admitted"] == "the fork admits no chain of the library"


def test_the_helper_builds_without_cargos_incremental_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = tmp_path / "predicted-events"
    helper.touch()
    monkeypatch.setattr(predicted_events, "HELPER", helper)
    seen: list[dict] = []

    def run(argv, **kwargs):
        seen.append(kwargs)
        return subprocess.CompletedProcess(argv, 0, "", "")

    assert predicted_events.build(run=run) == helper
    (kwargs,) = seen
    assert kwargs["env"]["CARGO_INCREMENTAL"] == "0"
    assert kwargs["stdin"] is subprocess.DEVNULL
    # rustup takes the toolchain that the nearest rust-toolchain.toml above the
    # directory that cargo runs in names: the repository's.
    assert kwargs["cwd"] == predicted_events.HELPER_DIR
    assert not (predicted_events.HELPER_DIR / "rust-toolchain.toml").exists()
    assert (predicted_events.HELPER_DIR.parents[1] / "rust-toolchain.toml").is_file()
