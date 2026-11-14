"""The fork of a living port: its identity file, its episode seed, and the
start state at the fork instant that the episode runner reads from the fork
(episodes.md §6.3, the living port; §6.4, how a fork is made; §10.2, choosing
the chain; episodes-tests.md §3, start state, living ports and forks).

Each scenario builds the evidence that a fork would hold: the readings of the
tank gauge every 6 seconds, the jobs and transactions of the Fuel Management
System (FMS), the supplier's polls in the relay's journal, the simulator's log
of refused dispenses, scheduled arrivals and the supplier's orders and
deliveries, and the reconciliation user's files.

Run with: pytest competition/tests -q
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from competition.episode_runner import chains, fork, predicted_events, startup
from competition.episode_runner.incus import Result
from competition.episode_runner.seeds import SIM_TUG_ARRIVALS, derive_generator_seed

LIVING_SEED = "000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f"
TUG_SEED = derive_generator_seed(LIVING_SEED, SIM_TUG_ARRIVALS)
START = datetime(2026, 11, 11, 18, 0, tzinfo=timezone.utc)
T0 = START.timestamp()
TIMINGS = predicted_events.competition_timings()
SUPPLIER = "10.10.0.10"
STARTUP = {
    "sim_tug_arrivals": TUG_SEED,
    "initial_volume_l": 35000.0,
    "order_pending_s": None,
    "first_poll_after_s": 180.0,
    "first_check_after_s": 17280.0,
    "next_audit_at": "2026-11-17T19:12:00Z",
    "initial_cumulative_pulses": 0,
    "initial_meter_totaliser_l": 0.0,
    "first_bdn_number": 1,
    "first_ticket_number": 1,
    "held_since": None,
    "held_artefacts": [],
}
LIVING = fork.LivingPort(
    LIVING_SEED, TUG_SEED, START, Path("/var/lib/competition/startup.json"), STARTUP
)


def at(s: float) -> datetime:
    """The instant s seconds after the living port's start."""
    return START + timedelta(seconds=s)


# The roster's three tugs (world.yaml).
A, B, C = 1999997, 1999985, 1999973


def schedule(*draws: tuple[int, float, float, float]) -> tuple[fork.Scheduled, ...]:
    """Each tug's scheduled arrivals as the simulator logs them: the vessel,
    the time of the draw, the scheduled arrival and the requested volume, each
    time from the start. A tug with no draw given is away beyond any fork."""
    given = {draw[0] for draw in draws}
    rows = [
        fork.Scheduled(vessel, T0 + logged, T0 + arrives, volume)
        for vessel, logged, arrives, volume in draws
    ]
    rows += [
        fork.Scheduled(vessel, T0, T0 + 1e7, 1000.0)
        for vessel in (A, B, C)
        if vessel not in given
    ]
    return tuple(sorted(rows, key=lambda row: row.logged))


def supplier(
    first: float, until: float, order_at: float | None = None, *later: tuple
) -> tuple[fork.SupplierEvent, ...]:
    """The supplier's polls in the simulator's log, every 180 s from first to
    until, the poll at order_at placing an order, whose tanker is due one
    compressed lead time later, and any later events as (kind, time from the
    start)."""
    events = [
        fork.SupplierEvent("poll", T0 + s, s == order_at)
        for s in range(int(first), int(until) + 1, 180)
    ]
    if order_at is not None:
        events.append(
            fork.SupplierEvent(
                "order placed", T0 + order_at, tanker_arrival=T0 + order_at + 4320.0
            )
        )
    events += [fork.SupplierEvent(kind, T0 + s) for kind, s in later]
    return tuple(sorted(events, key=lambda event: event.at))


def readings(level: Callable[[float], float], until_s: float) -> tuple:
    """A reading every 6 seconds from the start to until_s."""
    return tuple((T0 + s, level(s)) for s in range(0, int(until_s) + 1, 6))


def dispensing(start_s: float, volume_l: float) -> Callable[[float], float]:
    """The litres a dispense from start_s at 25 L/s has moved by s."""
    return lambda s: min(volume_l, max(0.0, s - start_s) * 25.0)


def served(
    job: str,
    vessel: int,
    created: float,
    pumping: float,
    volume: float,
    open_l: float | None = None,
):
    """A served refuel's job, its dispense and the link of its delivery note.
    open_l is the tank's volume when the job was created."""
    closed = pumping + volume / 25.0
    return (
        fork.Job(job, "dispense", T0 + created, open_l, str(vessel)),
        [
            fork.Transaction(
                job,
                "dispense",
                T0 + closed + 1.0,
                T0 + pumping,
                T0 + closed,
                predicted_events.register_preset_l(volume),
                volume,
            ),
            fork.Transaction(job, "bdn", T0 + closed + 10.0),
        ],
    )


def polls(first: float, until: float, every: float = 180.0) -> tuple[float, ...]:
    times, t = [], first
    while t <= until:
        times.append(T0 + t)
        t += every
    return tuple(times)


def state(fork_s: float, evidence: fork.Evidence) -> fork.ForkStart:
    return fork.start_state(LIVING, at(fork_s), evidence, TIMINGS)


def evidence_of(**fields) -> fork.Evidence:
    """Evidence with every tug away and the supplier's events of its polls,
    unless the fields say otherwise."""
    fields.setdefault("schedule", schedule())
    if "supplier" not in fields:
        fields["supplier"] = tuple(
            fork.SupplierEvent("poll", at) for at in fields.get("polls", ())
        )
    return fork.Evidence(**fields)


# --- The identity file and the episode seed --------------------------------


def identity_files(tmp_path: Path, **changes) -> Path:
    path = tmp_path / "startup.json"
    path.write_text(json.dumps({**STARTUP, **changes.pop("startup", {})}))
    identity = {
        "living_port_seed": LIVING_SEED,
        "tug_seed": TUG_SEED,
        "started_at": "2026-11-11T18:00:00.000Z",
        "startup": str(path),
        **changes,
    }
    (tmp_path / "living-port.json").write_text(json.dumps(identity))
    return tmp_path / "living-port.json"


def test_the_identity_file_gives_the_living_port(tmp_path: Path) -> None:
    living = fork.read_identity(identity_files(tmp_path))
    assert (living.seed, living.tug_seed, living.started_at) == (
        LIVING_SEED,
        TUG_SEED,
        START,
    )
    assert living.startup == STARTUP
    assert living.as_json()["started_at"] == "2026-11-11T18:00:00.000Z"


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"living_port_seed": "00" * 31}, "64 hexadecimal digits"),
        ({"tug_seed": 7}, "not the living port seed's seed"),
        ({"started_at": "2026-11-11T18:00:00"}, "no offset from UTC"),
        ({"startup": {"sim_tug_arrivals": 7}}, "another tug seed"),
    ],
)
def test_an_identity_file_that_does_not_agree_is_refused(
    tmp_path: Path, changes: dict, reason: str
) -> None:
    with pytest.raises(fork.ForkError, match=reason):
        fork.read_identity(identity_files(tmp_path, **changes))


def test_a_start_up_input_without_its_twelve_fields_is_refused(tmp_path: Path) -> None:
    path = identity_files(tmp_path)
    given = json.loads((tmp_path / "startup.json").read_text())
    del given["held_artefacts"]
    (tmp_path / "startup.json").write_text(json.dumps(given))
    with pytest.raises(fork.ForkError, match="fields of a start-up input"):
        fork.read_identity(path)


def test_fork_seeds_derive_from_the_living_port_seed() -> None:
    """The expected seeds come from OpenSSL, as in test_seeds.py."""
    assert fork.episode_seed_of(LIVING, 1) == (
        "e5bfee0bcf01978597b5a6a89108e98698ad609da3fb915c9231c664eb12ac86"
    )
    assert fork.episode_seed_of(LIVING, 0) == LIVING_SEED


def test_the_age_of_a_fork_counts_from_the_simulators_start() -> None:
    assert fork.age_s(LIVING, at(900.25)) == 900.25


def test_an_instant_needs_its_offset_and_drops_digits_beyond_the_microsecond() -> None:
    assert fork.parse_instant("2026-11-11T18:00:01.123456789Z") == at(1.123456)
    assert fork.parse_instant("2026-11-11T19:00:00+01:00") == START
    with pytest.raises(fork.ForkError):
        fork.parse_instant("yesterday")


# --- Reading the evidence ----------------------------------------------------


def test_the_fms_answer_gives_readings_jobs_and_transactions() -> None:
    answer = {
        "readings": [{"t": T0 + 6, "v": 35000.0}],
        "jobs": [
            {"id": "j1", "type": "dispense", "t": T0 + 605, "open_l": 35000.0},
            {"id": "f1", "type": "fill", "t": T0 + 900, "open_l": None},
        ],
        "transactions": [
            {
                "job": "j1",
                "ref": None,
                "preset": 2000.0,
                "dispensed": 2000.0,
                "delivered": None,
                "opened": T0 + 610,
                "closed": T0 + 690,
                "t": T0 + 691,
            },
            {
                "job": "j1",
                "ref": "x",
                "preset": None,
                "dispensed": None,
                "delivered": None,
                "opened": None,
                "closed": None,
                "t": T0 + 700,
            },
            {
                "job": "f1",
                "ref": None,
                "preset": None,
                "dispensed": None,
                "delivered": 18000.0,
                "opened": None,
                "closed": None,
                "t": T0 + 1300,
            },
        ],
    }
    readings_, jobs, rows = fork.parse_fms(json.dumps(answer))
    assert readings_ == ((T0 + 6, 35000.0),)
    assert jobs[1] == fork.Job("f1", "fill", T0 + 900, None)
    assert [r.kind for r in rows] == ["dispense", "bdn", "fill"]
    assert rows[0].preset_l == 2000.0 and rows[2].volume_l == 18000.0


def test_the_supplier_polls_are_its_connections_to_the_relay() -> None:
    lines = [
        {
            "__REALTIME_TIMESTAMP": "1794420180250000",
            "MESSAGE": "Connect from 10.10.0.10:41234 to 10.20.0.10:10001 (atg_in/TCP)",
        },
        {
            "__REALTIME_TIMESTAMP": "1794420181000000",
            "MESSAGE": "Connect from 10.10.0.99:41234 to 10.20.0.10:10001 (atg_in/TCP)",
        },
        {
            "__REALTIME_TIMESTAMP": "1794420182000000",
            "MESSAGE": "Proxy atg_in started.",
        },
    ]
    text = "\n".join(json.dumps(line) for line in lines) + "\nnot json\n"
    assert fork.parse_polls(text, SUPPLIER) == (1794420180.25,)


def test_the_simulators_log_gives_the_refused_dispenses() -> None:
    job = "0b6f1d2e-1111-4222-8333-944455556666"
    warn = "WARN sim::actors::dock_operator::internals:"
    create = (
        f"2026-11-11T18:20:00.500000Z  {warn} the FMS refused the dispense; no "
        'job was created request="POST /api/jobs/refuel" vessel_id=9876543'
    )
    start = (
        f"2026-11-11T18:30:00.000000Z  {warn} the FMS refused the dispense start; "
        f'cancelling the job request="POST .../dispenser/start" job_id={job}'
    )
    cancel = (
        f"2026-11-11T18:30:01.000000Z  {warn} cancelled the refused job "
        f'request="DELETE /api/jobs/{{id}}" job_id="{job}"'
    )
    first = "2026-11-11T18:10:00.000000Z  INFO start_state: start_state x=1"
    log = f"{first}\n{create}\n{start}\n{cancel}\n"
    assert fork.parse_refusals(log) == (
        fork.Refusal("start", T0 + 1800.0, job),
        fork.Refusal("cancel", T0 + 1801.0, job),
    )


def test_the_reconciliation_users_files_give_the_completed_audits() -> None:
    text = (
        f"{int(T0) + 50} .bash_history\n{int(T0) + 40} reconciliation-audits.txt\n---\n"
        "2026-10-01T00:00:00Z 2026-10-31T00:00:00Z agreed\n"
        "2026-11-11T18:00:00.1Z 2026-11-17T18:00:00Z disagreed BDN-000004\n"
    )
    windows, written = fork.parse_audits(text)
    assert written == T0 + 50
    assert windows[1] == (
        "2026-11-11T18:00:00.1Z",
        "2026-11-17T18:00:00Z",
        "disagreed BDN-000004",
    )


class FakeCopy:
    """The fork's nodes, answering the reads of the start state. A failing
    node fails every call, and a node failing once fails its first."""

    def __init__(self, failing: str = "", failing_once: str = "") -> None:
        self.failing = failing
        self.failing_once = failing_once
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    def fails(self, node: str) -> bool:
        if node == self.failing_once:
            self.failing_once = ""
            return True
        return node == self.failing

    def exec(self, node: str, *argv: str, timeout_s: float = 60.0) -> Result:
        self.calls.append((node, argv))
        if self.fails(node):
            return Result(1, "", f"{node} is down")
        if node == fork.FMS and argv[:2] == ("id", "-u"):
            return Result(0, "103\n", "")
        if node == fork.FMS and argv[:2] == ("id", "-g"):
            return Result(0, "110\n", "")
        if node == fork.RELAY:
            line = {
                "__REALTIME_TIMESTAMP": str(int((T0 + 180) * 1e6)),
                "MESSAGE": f"Connect from {SUPPLIER}:4 to 10.20.0.10:10001 (atg_in/TCP)",
            }
            return Result(0, json.dumps(line) + "\n", "")
        if node == "sim-host":
            return Result(0, "", "")
        if node == fork.RECON:
            return Result(0, "---\n", "")
        raise AssertionError(f"an unexpected call on {node}: {argv}")

    def exec_as(
        self, node: str, uid: int, gid: int, *argv: str, timeout_s: float = 60.0
    ) -> Result:
        self.calls.append((node, (f"--user {uid} --group {gid}", *argv)))
        if self.fails(node):
            return Result(1, "", f"{node} is down")
        assert node == fork.FMS
        return Result(
            0, json.dumps({"readings": [], "jobs": [], "transactions": []}), ""
        )


def test_the_reads_run_through_incus_without_a_login_session() -> None:
    copy = FakeCopy()
    evidence = fork.gather(
        copy, LIVING, at(900), TIMINGS, fork.Sources(SUPPLIER, "/home/recon-user")
    )
    assert evidence.polls == (T0 + 180,)
    assert evidence.failures == ()
    nodes = [node for node, _ in copy.calls]
    assert nodes == [fork.FMS, fork.FMS, fork.FMS, fork.RELAY, "sim-host", fork.RECON]
    # psql runs as the database's superuser through `incus exec --user`.
    psql = copy.calls[2][1]
    assert psql[:2] == ("--user 103 --group 110", "psql")
    assert not {"sudo", "su", "setpriv"} & set(psql)
    # The readings start no earlier than the living port's start.
    assert f"to_timestamp({T0 - 60.0:.3f})" in psql[-1]
    assert copy.calls[3][1][:6] == (
        "journalctl",
        "-o",
        "json",
        "--no-pager",
        "--since",
        f"@{T0 - 60:.0f}",
    )
    assert copy.calls[5][1][-1] == "/home/recon-user"


RECON_SOURCES = fork.Sources(SUPPLIER, "/home/recon-user")


@pytest.mark.parametrize("node", [fork.FMS, fork.RELAY, "sim-host", fork.RECON])
def test_a_read_that_fails_once_is_tried_again(node: str) -> None:
    healthy = fork.gather(FakeCopy(), LIVING, at(900), TIMINGS, RECON_SOURCES)
    copy = FakeCopy(failing_once=node)
    assert fork.gather(copy, LIVING, at(900), TIMINGS, RECON_SOURCES) == healthy
    assert copy.failing_once == ""


@pytest.mark.parametrize(
    ("node", "failure"),
    [
        (
            fork.FMS,
            "the FMS's database could not be read in two attempts: no user "
            "postgres on fms-01",
        ),
        (
            fork.RELAY,
            "the relay's journal could not be read in two attempts: atg-relay-01 "
            "is down",
        ),
        (
            "sim-host",
            "the simulator's log could not be read in two attempts: sim-host is down",
        ),
        (
            fork.RECON,
            "the reconciliation user's files could not be read in two attempts: "
            "recon-01 is down",
        ),
    ],
)
def test_a_read_that_fails_after_one_retry_is_a_fault(node: str, failure: str) -> None:
    """The fault discards the episode, and the record holds the failure alone:
    nothing derives from a read that failed (episodes.md §6.4, faults)."""
    copy = FakeCopy(failing=node)
    evidence = fork.gather(copy, LIVING, at(900), TIMINGS, RECON_SOURCES)
    assert evidence.failures == (failure,)
    with pytest.raises(fork.StartStateFault) as raised:
        state(900, evidence)
    assert raised.value.faults == (failure,)
    assert raised.value.record == {"faults": [failure]}


def test_the_sources_come_from_the_topology_and_the_world() -> None:
    sources = fork.Sources.from_files()
    assert sources == fork.Sources(SUPPLIER, "/home/recon-user")


# --- The start state at the fork --------------------------------------------


def test_level_at_interpolates_and_extrapolates_one_interval() -> None:
    rows = [(0.0, 1000.0), (6.0, 850.0), (12.0, 700.0)]
    assert fork.level_at(rows, 3.0) == 925.0
    assert fork.level_at(rows, 15.0) == 625.0
    assert fork.level_at(rows, 40.0) == 550.0
    assert fork.level_at(rows, -1.0) is None


def idle_port() -> fork.Evidence:
    """Two served tugs by 30 minutes, the supplier polling. Tug A arrives at
    600 s and leaves with its delivery note at 700 s, when the simulator draws
    its next arrival, 2,400 s; tug B arrives at 1,500 s and leaves at 1,560 s,
    next arriving at 3,000 s; tug C stays away."""
    first, rows1 = served("j1", A, 605.0, 610.0, 2000.0, 35000.0)
    second, rows2 = served("j2", B, 1504.0, 1510.0, 1000.0, 33000.0)

    def level(s):
        return 35000.0 - dispensing(610, 2000)(s) - dispensing(1510, 1000)(s)

    return evidence_of(
        readings=readings(level, 1830),
        jobs=(first, second),
        transactions=(*rows1, *rows2),
        polls=polls(180.0, 1620.0),
        schedule=schedule(
            (A, 0.0, 600.0, 2000.0),
            (B, 0.0, 1500.0, 1000.0),
            (A, 700.0, 2400.0, 2500.0),
            (B, 1560.0, 3000.0, 900.0),
        ),
    )


def test_an_idle_fork_takes_each_tugs_logged_scheduled_arrival() -> None:
    start = state(1800.0, idle_port())
    d = start.draws
    assert d.uncertain is None
    assert d.initial_volume_l == 32000.0
    assert (d.order_pending_s, d.under_way) == (None, None)
    # The last poll fell due at 1,620 s, and the next falls due at 1,800 s,
    # the fork instant.
    assert d.first_poll_after_s == 0.0
    # A's and B's next arrivals, and C's first, from the fork instant.
    assert d.arrivals == ((600.0, 2500.0), (1200.0, 900.0), (1e7 - 1800.0, 1000.0))
    assert not d.audit_completed
    record = start.record
    assert [tug["state"] for tug in record["tugs"]] == ["away", "away", "away"]
    assert record["delivery_state"] == "no order placed"
    assert record["arrivals_to_come"][0]["after_fork_s"] == 600.0


def test_a_fork_during_a_refuel_holds_its_remaining_litres() -> None:
    start = state(1530.0, idle_port())
    d = start.draws
    assert d.uncertain is None
    # 20 s of pumping at 25 L/s have moved 500 of the 1,000 litres.
    assert d.initial_volume_l == pytest.approx(32500.0)
    u = d.under_way
    assert (u.fill, u.started, u.start) == (False, True, (0.0, 0.0))
    assert u.remaining_l == pytest.approx(500.0)
    queue = predicted_events.dock(d, TIMINGS, 600.0)
    assert queue.jobs[0].end[0] == pytest.approx(20.0)
    states = {tug["vessel"]: tug["state"] for tug in start.record["tugs"]}
    assert states[B] == "under way"


def test_a_tug_that_arrived_during_a_refuel_waits_at_the_fork() -> None:
    evidence = idle_port()
    evidence = fork.Evidence(
        **{
            **evidence.__dict__,
            "schedule": schedule(
                (A, 0.0, 600.0, 2000.0),
                (B, 0.0, 1500.0, 1000.0),
                (C, 0.0, 1520.0, 2500.0),
                (A, 700.0, 2400.0, 2500.0),
            ),
        }
    )
    d = state(1530.0, evidence).draws
    assert d.uncertain is None
    assert d.arrivals[0] == (-10.0, 2500.0)
    queue = predicted_events.dock(d, TIMINGS, 600.0)
    # The waiting tug starts after the refuel under way and the handover.
    assert queue.jobs[1].start[0] >= queue.jobs[0].end[0]


def test_a_refuel_under_way_but_not_yet_pumping_can_still_be_refused() -> None:
    job = fork.Job("j1", "dispense", T0 + 605.0, 5900.0, str(A))
    evidence = evidence_of(
        readings=readings(lambda s: 5900.0, 610),
        jobs=(job,),
        polls=polls(180.0, 600.0),
        schedule=schedule((A, 0.0, 600.0, 2000.0)),
    )
    d = state(608.0, evidence).draws
    assert d.under_way.started is False
    assert predicted_events.dock(d, TIMINGS, 600.0).jobs[0].refused


def ordering_port(
    until_s: float, *later: tuple, fills: tuple = (), rows: tuple = ()
) -> fork.Evidence:
    """A tank at 14,900 L from the start, whose supplier polls every 180 s, the
    first poll placing an order, and keeps polling through it."""
    return evidence_of(
        readings=readings(lambda s: 14900.0, until_s + 200),
        jobs=fills,
        transactions=rows,
        polls=polls(180.0, until_s),
        supplier=supplier(180.0, until_s, 180.0, *later),
    )


def test_an_order_that_a_poll_placed_is_pending_at_the_fork() -> None:
    start = state(280.0, ordering_port(280.0))
    d = start.draws
    assert d.uncertain is None
    # The tanker comes one compressed lead time after the ordering poll.
    assert d.order_pending_s == pytest.approx(180.0 + 4320.0 - 280.0)
    # The supplier keeps polling through the order (sim.md §5.3).
    assert d.first_poll_after_s == pytest.approx(80.0)
    assert start.record["delivery_state"] == "order placed"
    assert start.record["order_placed_at"] == fork.epoch_text(T0 + 180.0)


def test_the_supplier_polls_through_its_order() -> None:
    d = state(2000.0, ordering_port(2000.0)).draws
    assert d.uncertain is None
    assert d.order_pending_s == pytest.approx(180.0 + 4320.0 - 2000.0)
    assert d.first_poll_after_s == pytest.approx(1980.0 + 180.0 - 2000.0)


def test_a_fill_under_way_comes_from_the_tankers_arrival() -> None:
    arrival = 180.0 + 4320.0
    fill = fork.Job("f1", "fill", T0 + arrival + 5.0, 14900.0)
    evidence = ordering_port(
        arrival + 110.0, ("tanker arrival", arrival), fills=(fill,)
    )

    def level(s):
        return 14900.0 + max(0.0, s - (arrival + 10.0)) * 50.0

    evidence = fork.Evidence(
        **{**evidence.__dict__, "readings": readings(level, arrival + 300.0)}
    )
    start = state(arrival + 110.0, evidence)
    d = start.draws
    assert d.uncertain is None
    assert start.record["delivery_state"] == "fill under way"
    assert d.order_pending_s is None
    assert (d.under_way.fill, d.under_way.started) == (True, True)
    assert d.under_way.remaining_l == pytest.approx(18000.0 - 5000.0)
    # The polls keep their interval through the fill.
    assert d.first_poll_after_s == pytest.approx(4500.0 + 180.0 - (arrival + 110.0))


def test_a_tanker_waiting_behind_a_tug_is_served_before_the_waiting_tugs() -> None:
    """The road tanker arrives at 4,500 s while tug A pumps, and tug B waits.
    The dock operator has opened no fill: the tanker waits at the dock, and
    the dock serves its 18,000 L before tug B (sim.md §6)."""
    order_at = 180.0
    arrival = order_at + 4320.0
    job_a = fork.Job("ja", "dispense", T0 + 4485.0, 14900.0, str(A))

    def level(s):
        return 14900.0 - min(3000.0, max(0.0, s - 4490.0) * 25.0)

    evidence = evidence_of(
        readings=readings(level, 4600),
        jobs=(job_a,),
        polls=polls(180.0, 4520.0),
        supplier=supplier(180.0, 4520.0, order_at, ("tanker arrival", arrival)),
        schedule=schedule((A, 0.0, 4480.0, 3000.0), (B, 0.0, 4495.0, 1000.0)),
    )
    start = state(4520.0, evidence)
    d = start.draws
    assert d.uncertain is None
    assert start.record["delivery_state"] == "tanker waiting at the dock"
    assert start.record["tanker_arrived_at"] == fork.epoch_text(T0 + arrival)
    assert d.order_pending_s == pytest.approx(arrival - 4520.0)
    tug_a, tug_b = predicted_events.dock(d, TIMINGS, 900.0).jobs
    assert tug_a.end == (90.0, 95.0)
    # Tug B starts only after the fill, 360 s at 50 L/s, has ended.
    assert tug_b.start[0] >= tug_a.end[0] + 18000.0 / 50.0


def test_an_order_pending_at_the_start_reaches_every_fork() -> None:
    """A start-up input with an order pending gives no first poll; the
    simulator logs the order with its tanker's arrival at its start. A fork
    before the first poll, and one after polls but before the tanker, both
    hold the order."""
    living = dataclasses.replace(
        LIVING,
        startup={**STARTUP, "order_pending_s": 1200.0, "first_poll_after_s": None},
    )
    order = fork.SupplierEvent("order placed", T0, tanker_arrival=T0 + 1200.0)
    before = evidence_of(readings=readings(lambda s: 14900.0, 200), supplier=(order,))
    early = fork.start_state(living, at(100.0), before, TIMINGS)
    assert early.draws.uncertain is None
    assert early.record["delivery_state"] == "order placed"
    assert early.draws.order_pending_s == pytest.approx(1100.0)
    assert early.draws.first_poll_after_s == pytest.approx(TIMINGS.poll_s - 100.0)
    after = evidence_of(
        readings=readings(lambda s: 14900.0, 700),
        polls=polls(180.0, 600.0),
        supplier=(order, *supplier(180.0, 600.0)),
    )
    later = fork.start_state(living, at(600.0), after, TIMINGS)
    assert later.draws.uncertain is None
    assert later.record["delivery_state"] == "order placed"
    assert later.draws.order_pending_s == pytest.approx(600.0)


def test_a_transcribed_fill_is_not_a_waiting_tanker() -> None:
    """The fill that followed the tanker's arrival has its row, and the
    supplier has not yet logged the delivery's end: the tanker has delivered
    and does not wait at the dock."""
    arrival = 180.0 + 4320.0
    fill = fork.Job("f1", "fill", T0 + arrival + 5.0, 14900.0)
    row = fork.Transaction("f1", "fill", T0 + arrival + 380.0, volume_l=18000.0)

    def level(s):
        return 14900.0 + min(18000.0, max(0.0, s - (arrival + 10.0)) * 50.0)

    evidence = evidence_of(
        readings=readings(level, arrival + 400.0),
        jobs=(fill,),
        transactions=(row,),
        polls=polls(180.0, arrival + 380.0),
        supplier=supplier(180.0, arrival + 380.0, 180.0, ("tanker arrival", arrival)),
    )
    start = state(arrival + 381.0, evidence)
    d = start.draws
    assert d.uncertain is None
    assert start.record["delivery_state"] == "delivered"
    assert (d.order_pending_s, d.under_way) == (None, None)
    assert predicted_events.dock(d, TIMINGS, 900.0).jobs == ()


def test_a_road_tanker_leaving_holds_no_job_when_the_next_tugs_opens() -> None:
    """The fill is transcribed at 4,880 s, and the dock operator opens tug A's
    job at 4,882 s before the supplier logs the delivery's end: the road
    tanker holds no job, and A's job is under way."""
    arrival = 180.0 + 4320.0
    fill = fork.Job("f1", "fill", T0 + arrival + 5.0, 14900.0)
    row = fork.Transaction("f1", "fill", T0 + arrival + 380.0, volume_l=18000.0)
    tug = fork.Job("ja", "dispense", T0 + arrival + 382.0, 32900.0, str(A))
    evidence = evidence_of(
        readings=readings(lambda s: 32900.0, arrival + 400.0),
        jobs=(fill, tug),
        transactions=(row,),
        polls=polls(180.0, arrival + 380.0),
        supplier=supplier(180.0, arrival + 380.0, 180.0, ("tanker arrival", arrival)),
        schedule=schedule((A, 0.0, arrival + 300.0, 2000.0)),
    )
    start = state(arrival + 383.0, evidence)
    d = start.draws
    assert d.uncertain is None
    assert start.record["delivery_state"] == "delivered"
    assert not d.under_way.fill
    assert d.under_way.arrival_s == pytest.approx(-83.0)


@pytest.mark.parametrize("logged", [True, False])
def test_a_road_tanker_that_has_arrived_is_in_the_dock_once(logged: bool) -> None:
    """The order at 180 s brings its road tanker at 4,500 s, and the dock
    operator opens its fill at 4,500.2 s while tug A waits. The supplier logs
    the arrival after it sends the arrival to the driver, so the fill can
    open before the arrival is logged. Either way the tanker is the job under
    way and no later arrival: tug A starts after one fill of 18,000 L, not
    two."""
    arrival = 180.0 + 4320.0
    fill = fork.Job("f1", "fill", T0 + arrival + 0.2, 14900.0)
    later = (("tanker arrival", arrival),) if logged else ()
    evidence = evidence_of(
        readings=readings(lambda s: 14900.0, arrival),
        jobs=(fill,),
        polls=polls(180.0, arrival),
        supplier=supplier(180.0, arrival, 180.0, *later),
        schedule=schedule((A, 0.0, arrival - 10.0, 2000.0)),
    )
    start = state(arrival + 0.5, evidence)
    d = start.draws
    assert d.uncertain is None
    assert start.record["delivery_state"] == "fill under way"
    assert d.order_pending_s is None
    assert d.under_way.fill
    (tug,) = predicted_events.dock(d, TIMINGS, 900.0).jobs
    fill_s = 18000.0 / TIMINGS.inflow_l_per_s
    assert fill_s <= tug.start[0] < 2 * fill_s


def transcribed_and_not_ended(fork_s: float, polls_until: float) -> fork.Evidence:
    """The fill that followed the road tanker's arrival at 4,500 s is
    transcribed at 4,880 s, and the supplier logs no end of the delivery; the
    relay's journal and the simulator's log hold the polls to polls_until."""
    arrival = 180.0 + 4320.0
    fill = fork.Job("f1", "fill", T0 + arrival + 5.0, 14900.0)
    row = fork.Transaction("f1", "fill", T0 + arrival + 380.0, volume_l=18000.0)
    return evidence_of(
        readings=readings(lambda s: 32900.0, fork_s),
        jobs=(fill,),
        transactions=(row,),
        polls=polls(180.0, polls_until),
        supplier=supplier(180.0, polls_until, 180.0, ("tanker arrival", arrival)),
    )


@pytest.mark.parametrize(
    ("after", "fault"),
    [(fork.LEAVING_S, False), (fork.LEAVING_S + 1.0, True), (4000.0, True)],
)
def test_a_transcribed_fill_ends_its_delivery_within_its_bound(
    after: float, fault: bool
) -> None:
    """The road tanker driver signals the delivery's end before the dock
    operator transcribes the ticket, so the supplier logs it at once."""
    fork_s = 4880.0 + after
    evidence = transcribed_and_not_ended(fork_s, fork_s)
    if not fault:
        assert state(fork_s, evidence).record["delivery_state"] == "delivered"
        return
    with pytest.raises(
        fork.StartStateFault,
        match="the road tanker's fill, job f1, was transcribed at "
        "2026-11-11T19:21:20.000000Z, and the simulator's log holds no end of its "
        "delivery",
    ):
        state(fork_s, evidence)


def test_a_poll_due_before_the_transcription_holds_the_delivery_end() -> None:
    """The poll due at 4,860 s is under way at the transcription, 4,880 s,
    and at the fork, 4,895 s: it holds the supplier, which logs the delivery's
    end once the poll ends. With that poll ended, the end is overdue."""
    held = transcribed_and_not_ended(4895.0, 4680.0)
    start = state(4895.0, held)
    assert start.record["delivery_state"] == "delivered"
    assert start.draws.poll_overdue is not None
    ended = transcribed_and_not_ended(4895.0, 4860.0)
    with pytest.raises(fork.StartStateFault, match="no end of its delivery"):
        state(4895.0, ended)


def test_a_poll_due_after_the_transcription_holds_no_delivery_end() -> None:
    """The supplier was idle when the road tanker driver signalled the
    delivery's end, before the transcription at 5,037 s. The poll due at
    5,040 s, under way at the fork, fell due after the transcription, so it
    holds no end, and the end is overdue."""
    arrival = 180.0 + 4320.0
    fill = fork.Job("f1", "fill", T0 + arrival + 5.0, 14900.0)
    row = fork.Transaction("f1", "fill", T0 + 5037.0, volume_l=18000.0)
    evidence = evidence_of(
        readings=readings(lambda s: 32900.0, 5047.0),
        jobs=(fill,),
        transactions=(row,),
        polls=(*polls(180.0, 4860.0), T0 + 5040.01),
        supplier=supplier(180.0, 4860.0, 180.0, ("tanker arrival", arrival)),
    )
    with pytest.raises(
        fork.StartStateFault,
        match="the road tanker's fill, job f1, was transcribed at "
        "2026-11-11T19:23:57.000000Z, and the simulator's log holds no end of its "
        "delivery",
    ):
        state(5047.0, evidence)


def test_after_a_delivery_no_order_is_pending() -> None:
    arrival = 180.0 + 4320.0
    fill = fork.Job("f1", "fill", T0 + arrival + 5.0, 14900.0)
    row = fork.Transaction("f1", "fill", T0 + arrival + 380.0, volume_l=18000.0)
    evidence = ordering_port(
        arrival + 500.0,
        ("tanker arrival", arrival),
        ("delivery ended", arrival + 370.0),
        fills=(fill,),
        rows=(row,),
    )
    start = state(arrival + 500.0, evidence)
    d = start.draws
    assert d.uncertain is None
    assert start.record["delivery_state"] == "no order placed"
    assert d.order_pending_s is None
    assert d.under_way is None


# The supplier's polls fall due every 180 s from 180 s after the start, one
# at a time, and each poll's event is logged as it ends.


def test_connections_of_two_due_times_with_no_event_of_the_supplier_are_a_fault() -> (
    None
):
    """The relay logged the polls due at 180 s to 900 s, and the simulator's
    log holds no event of the supplier: the first poll ended before the
    second connected, and its end is missing."""
    evidence = evidence_of(
        readings=readings(lambda s: 30000.0, 1000),
        polls=polls(180.0, 1000.0),
        supplier=(),
    )
    with pytest.raises(
        fork.StartStateFault,
        match="the relay's journal holds a connection at "
        "2026-11-11T18:06:00.000000Z of a later poll, and the simulator's log "
        "no end of the supplier's poll due at 2026-11-11T18:03:00.000000Z",
    ):
        state(1000.0, evidence)


def test_a_later_connection_with_no_event_for_the_earlier_poll_is_a_fault() -> None:
    """The polls due at 180 s and 360 s ended; the relay logged the poll due
    at 540 s and the next, due at 720 s, and the simulator's log holds no end
    of the poll due at 540 s."""
    evidence = evidence_of(
        readings=readings(lambda s: 30000.0, 730),
        polls=polls(180.0, 720.0),
        supplier=tuple(fork.SupplierEvent("poll", s) for s in polls(180.0, 360.0)),
    )
    with pytest.raises(
        fork.StartStateFault,
        match="no end of the supplier's poll due at 2026-11-11T18:09:00.000000Z",
    ):
        state(730.0, evidence)


@pytest.mark.parametrize(
    ("fork_s", "ended"),
    [
        # The first poll retries after 1, 2 and 4 s, each retry a connection
        # of its own, and ends on the last.
        (200.0, (187.05,)),
        # A fork during the retries, before the poll has ended.
        (188.0, ()),
    ],
)
def test_a_retried_first_poll_is_no_fault(fork_s: float, ended: tuple) -> None:
    """Several connections within one due time are one poll."""
    evidence = evidence_of(
        readings=readings(lambda s: 35000.0, fork_s),
        polls=tuple(T0 + s for s in (180.0, 181.0, 183.0, 187.0) if s <= fork_s),
        supplier=tuple(fork.SupplierEvent("poll", T0 + s) for s in ended),
    )
    start = state(fork_s, evidence)
    assert start.record["faults"] == []
    assert start.draws.uncertain is None


def test_a_retried_poll_is_no_fault() -> None:
    """The poll due at 540 s connects four times and ends once. The next poll
    falls due at 720 s on the schedule, not one interval after the last
    retry's connection."""
    evidence = evidence_of(
        readings=readings(lambda s: 30000.0, 700),
        polls=polls(180.0, 360.0) + tuple(T0 + s for s in (540.0, 541.0, 543.0, 547.0)),
        supplier=tuple(
            fork.SupplierEvent("poll", T0 + s) for s in (180.0, 360.0, 547.1)
        ),
    )
    start = state(700.0, evidence)
    assert start.record["faults"] == []
    assert start.draws.uncertain is None
    assert start.draws.first_poll_after_s == pytest.approx(720.0 - 700.0)


def test_a_first_poll_under_way_at_the_fork_has_logged_no_event() -> None:
    """The relay logged the first poll's connection 1 s before the fork, and
    the supplier logs a poll as it ends."""
    evidence = evidence_of(
        readings=readings(lambda s: 35000.0, 181), polls=(T0 + 180.0,), supplier=()
    )
    d = state(181.0, evidence).draws
    assert d.uncertain is None
    assert d.first_poll_after_s == pytest.approx(179.0)


@pytest.mark.parametrize("fork_s", [75.0, 120.0, 179.0, 185.0])
def test_a_first_fork_before_the_first_poll_needs_no_event_of_the_supplier(
    fork_s: float,
) -> None:
    """At a compression factor of 5 the supplier's first poll falls due 180 s
    after the start. A first fork from 75 s on, before that poll has ended,
    finds no poll in the relay's journal and no event of the supplier in the
    simulator's log, and neither a fault nor an uncertain start state."""
    evidence = evidence_of(readings=readings(lambda s: 35000.0, fork_s))
    start = state(fork_s, evidence)
    assert start.record["faults"] == []
    assert start.draws.uncertain is None
    assert start.draws.first_poll_after_s == pytest.approx(180.0 - fork_s)


def held_poll(fork_s: float, connections: tuple[float, ...]) -> fork.Evidence:
    """The polls due at 180 s and 360 s ended; the poll due at 540 s has the
    given connections and no end."""
    return evidence_of(
        readings=readings(lambda s: 30000.0, fork_s),
        polls=polls(180.0, 360.0) + tuple(T0 + s for s in connections),
        supplier=tuple(fork.SupplierEvent("poll", s) for s in polls(180.0, 360.0)),
    )


@pytest.mark.parametrize("connections", [(), (540.0, 541.0, 543.0)])
def test_a_held_poll_leaves_the_start_state_uncertain(
    connections: tuple[float, ...],
) -> None:
    """The supplier's poll due at 540 s has not ended by 600 s, within one
    interval, its connection logged or not: the start state is uncertain,
    and no fault."""
    start = state(600.0, held_poll(600.0, connections))
    assert start.draws.poll_overdue == (
        "the supplier's poll due at 2026-11-11T18:09:00.000000Z had not ended "
        "by the fork"
    )
    assert start.draws.tanker_overdue is None
    assert start.record["faults"] == []
    assert start.record["uncertain"] == start.draws.uncertain
    assert start.record["supplier"]["poll_under_way_due"] == (
        "2026-11-11T18:09:00.000000Z"
    )


@pytest.mark.parametrize("connections", [(), (540.0, 541.0, 543.0)])
def test_a_poll_more_than_one_interval_overdue_is_a_fault(
    connections: tuple[float, ...],
) -> None:
    """The poll due at 540 s has not ended by 721 s: the relay or the
    supplier is stuck."""
    with pytest.raises(
        fork.StartStateFault,
        match="the supplier's poll due at 2026-11-11T18:09:00.000000Z had not "
        "ended by the fork, more than one poll interval after it fell due",
    ):
        state(721.0, held_poll(721.0, connections))


def test_a_connection_just_before_its_due_time_belongs_to_its_own_poll() -> None:
    """Each poll's connection lands 0.2 s before its due time, within the
    0.3 s by which a poll's range starts early: each belongs to its own poll,
    so no ended poll lacks its connection."""
    early = tuple(T0 + s - 0.2 for s in (180.0, 360.0, 540.0))
    evidence = evidence_of(
        readings=readings(lambda s: 30000.0, 600),
        polls=early,
        supplier=tuple(fork.SupplierEvent("poll", at + 0.05) for at in early),
    )
    start = state(600.0, evidence)
    assert start.record["faults"] == []
    assert start.draws.uncertain is None
    assert start.draws.first_poll_after_s == pytest.approx(120.0)


def test_a_poll_within_its_lateness_is_not_overdue() -> None:
    evidence = evidence_of(
        readings=readings(lambda s: 30000.0, 560),
        polls=polls(180.0, 360.0),
    )
    assert (
        state(
            540.0 + predicted_events.POLL_SLIP_S + fork.POLL_GAP_S, evidence
        ).draws.uncertain
        is None
    )


def test_a_first_poll_held_with_no_connection_is_an_overdue_poll() -> None:
    """The first poll, due at 180 s, has neither connected nor ended by 200 s,
    as during a burst of the supplier's retries that the relay does not
    accept."""
    evidence = evidence_of(readings=readings(lambda s: 35000.0, 200))
    d = state(200.0, evidence).draws
    assert d.poll_overdue == (
        "the supplier's poll due at 2026-11-11T18:03:00.000000Z had not ended by "
        "the fork"
    )


def test_an_ended_poll_that_the_journal_lacks_is_a_fault() -> None:
    """The simulator logged the polls due at 540 s and 720 s as ended, and the
    relay's journal holds no connection after 360 s."""
    evidence = evidence_of(
        readings=readings(lambda s: 30000.0, 800),
        polls=polls(180.0, 360.0),
        supplier=tuple(fork.SupplierEvent("poll", s) for s in polls(180.0, 720.0)),
    )
    with pytest.raises(
        fork.StartStateFault,
        match="the simulator's log holds the supplier's poll due at "
        "2026-11-11T18:12:00.000000Z as ended, and the relay's journal holds no "
        "connection for it",
    ):
        state(800.0, evidence)


def _chain_of_stages(*names: str) -> chains.Chain:
    stages = tuple(
        chains.Stage(name, "", chains.Target("adversary", chains.ADVERSARY))
        for name in names
    )
    return chains.Chain("external-informed__test", "external-informed", "test", stages)


def test_the_relays_journal_is_checked_against_the_logged_polls() -> None:
    """Each poll that the simulator logged as ended within the look-back,
    its connection removed from the relay's journal, is a fault that names
    the poll, as a journal that holds no connection is; a fault discards the
    episode (test_episode.py, test_a_fault_in_the_start_state_discards_the_episode).
    A logged poll before the look-back is not checked: the journal is read
    only that far back. A poll held by the relay more than 12 seconds past
    its due time, its event not logged and no later connection in the
    journal, is no fault: it leaves the start state uncertain, and excludes
    the chains that wait for a poll alone (episodes-tests.md,
    the_relays_journal_is_checked_against_the_logged_polls; episodes.md
    §6.4, how a fork is made)."""

    def lacking(due: float) -> str:
        return (
            "the simulator's log holds the supplier's poll due at "
            f"{fork.epoch_text(due)} as ended, and the relay's journal holds no "
            "connection for it"
        )

    def journal_without(
        removed: tuple[float, ...], logged: tuple[float, ...], fork_s: float
    ) -> fork.Evidence:
        return evidence_of(
            readings=readings(lambda s: 30000.0, fork_s),
            polls=tuple(p for p in logged if p not in removed),
            supplier=tuple(fork.SupplierEvent("poll", p) for p in logged),
        )

    logged = polls(180.0, 900.0)
    for removed in (*((p,) for p in logged), logged):
        with pytest.raises(fork.StartStateFault) as raised:
            state(1000.0, journal_without(removed, logged, 1000.0))
        assert raised.value.faults == (lacking(removed[-1]),)

    # A fork 7,000 s after the start reads the journal back to 1,360 s.
    logged = polls(180.0, 6840.0)
    checked_from = T0 + 7000.0 - fork.look_back_s(TIMINGS)
    before = max(p for p in logged if p < checked_from)
    within = min(p for p in logged if p >= checked_from)
    assert (before - T0, within - T0) == (1260.0, 1440.0)
    assert (
        state(7000.0, journal_without((before,), logged, 7000.0)).record["faults"] == []
    )
    with pytest.raises(fork.StartStateFault) as raised:
        state(7000.0, journal_without((within,), logged, 7000.0))
    assert raised.value.faults == (lacking(within),)

    # The poll due at 540 s, its connection in the journal and no end, held
    # 12 s past its due time and then 13 s.
    assert state(552.0, held_poll(552.0, (540.0,))).draws.uncertain is None
    held = state(553.0, held_poll(553.0, (540.0,)))
    assert held.record["faults"] == []
    assert held.draws.poll_overdue == (
        "the supplier's poll due at 2026-11-11T18:09:00.000000Z had not ended "
        "by the fork"
    )
    assert held.draws.tanker_overdue is None
    poll = _chain_of_stages("intercept-supplier-poll", "inject-false-feed")
    plain = _chain_of_stages("discover-gateway", "inject-false-feed")
    excluded = predicted_events.admit(poll, held.draws, TIMINGS, 6, [5.0, 5.0])
    assert not excluded.admitted
    assert excluded.reason == (
        "intercept-supplier-poll needs supplier_poll, and the start state at the "
        f"fork is uncertain: {held.draws.poll_overdue}"
    )
    assert predicted_events.admit(plain, held.draws, TIMINGS, 6, [5.0, 5.0]).admitted


def test_before_the_first_poll_the_start_up_input_gives_it() -> None:
    evidence = evidence_of(readings=readings(lambda s: 35000.0, 100))
    d = state(100.0, evidence).draws
    assert d.uncertain is None
    assert d.first_poll_after_s == 80.0


def refused_at_its_start(*draws: tuple) -> fork.Evidence:
    job = fork.Job("j1", "dispense", T0 + 605.0, 6000.0, str(A))
    refusals = (
        fork.Refusal("start", T0 + 612.0, "j1"),
        fork.Refusal("cancel", T0 + 613.0, "j1"),
    )
    return evidence_of(
        readings=readings(lambda s: 6000.0, 700),
        jobs=(job,),
        refusals=refusals,
        polls=polls(180.0, 650.0),
        schedule=schedule((A, 0.0, 600.0, 2000.0), *draws),
    )


def test_a_tug_refused_at_its_start_leaves_at_its_cancel() -> None:
    """Before its departure the refused tug is at the dock, and the next job
    waits for its cancel; once it has left, the simulator has drawn its next
    arrival."""
    leaving = state(612.5, refused_at_its_start())
    assert leaving.draws.uncertain is None
    assert leaving.draws.under_way.refused
    states = {tug["vessel"]: tug["state"] for tug in leaving.record["tugs"]}
    assert states[A] == "refused at its start, leaving once cancelled"
    (job,) = predicted_events.dock(leaving.draws, TIMINGS, 600.0).jobs
    assert job.refused
    left = state(700.0, refused_at_its_start((A, 613.0, 2000.0, 700.0)))
    assert left.draws.uncertain is None
    assert left.draws.under_way is None
    assert left.draws.arrivals[0] == (1300.0, 700.0)


def test_a_dispense_that_disagrees_with_the_logged_request_is_a_fault() -> None:
    """Tug A's dispense closes at 690 s with a preset of 2,000 L, and the
    simulator logged a request of 2,100 L. The fault's record holds what the
    evidence gave."""
    first, rows1 = served("j1", A, 605.0, 610.0, 2000.0, 35000.0)
    evidence = evidence_of(
        readings=readings(lambda s: 35000.0 - dispensing(610, 2000)(s), 700),
        jobs=(first,),
        transactions=(rows1[0],),
        polls=polls(180.0, 690.0),
        schedule=schedule((A, 0.0, 600.0, 2100.0)),
    )
    with pytest.raises(fork.StartStateFault) as raised:
        state(695.0, evidence)
    fault = (
        "the dispense of job j1 has a preset of 2000.0 L, and vessel 1999997's "
        "logged request is 2100.0 L"
    )
    assert raised.value.faults == (fault,)
    record = raised.value.record
    assert record["faults"] == [fault]
    assert record["tugs"][0]["job"] == "j1"
    assert record["tugs"][0]["state"] == "dispensed, with its delivery note under way"


def test_an_open_dispense_that_no_tug_at_the_dock_accounts_for_is_a_fault() -> None:
    job = fork.Job("j1", "dispense", T0 + 605.0, 30000.0, str(A))
    evidence = evidence_of(
        readings=readings(lambda s: 30000.0, 700),
        jobs=(job,),
        polls=polls(180.0, 690.0),
        schedule=schedule((A, 0.0, 5000.0, 2000.0)),
    )
    with pytest.raises(
        fork.StartStateFault, match="no tug at the dock accounts for it"
    ):
        state(700.0, evidence)


def test_a_tug_with_no_logged_arrival_is_a_fault() -> None:
    rows = tuple(row for row in schedule() if row.vessel != C)
    evidence = evidence_of(readings=readings(lambda s: 35000.0, 100), schedule=rows)
    with pytest.raises(
        fork.StartStateFault, match="no scheduled arrival of vessel 1999973"
    ):
        state(100.0, evidence)


def test_no_reading_of_the_tank_gauge_is_a_fault() -> None:
    evidence = evidence_of(polls=polls(180.0, 900.0))
    with pytest.raises(
        fork.StartStateFault,
        match="the FMS holds no reading of the tank gauge before the fork",
    ):
        state(1000.0, evidence)


def test_two_jobs_holding_the_dock_are_a_fault() -> None:
    """Tugs A and B are both under way: the dock operator serves one job at a
    time."""
    first = fork.Job("ja", "dispense", T0 + 605.0, 35000.0, str(A))
    second = fork.Job("jb", "dispense", T0 + 640.0, 35000.0, str(B))
    evidence = evidence_of(
        readings=readings(lambda s: 35000.0, 700),
        jobs=(first, second),
        polls=polls(180.0, 690.0),
        schedule=schedule((A, 0.0, 600.0, 2000.0), (B, 0.0, 630.0, 1000.0)),
    )
    with pytest.raises(
        fork.StartStateFault, match="more than one job holds the dock at the fork"
    ):
        state(700.0, evidence)


def tanker_at_the_dock(*jobs: fork.Job, **fields) -> fork.Evidence:
    """The order at 180 s, whose road tanker arrived at 4,500 s."""
    arrival = 180.0 + 4320.0
    return evidence_of(
        readings=readings(lambda s: 12900.0, arrival + 30.0),
        jobs=jobs,
        polls=polls(180.0, arrival),
        supplier=supplier(180.0, arrival, 180.0, ("tanker arrival", arrival)),
        **fields,
    )


def test_more_than_one_fill_under_way_is_a_fault() -> None:
    fills = (
        fork.Job("f1", "fill", T0 + 4505.0, 12900.0),
        fork.Job("f2", "fill", T0 + 4509.0, 12900.0),
    )
    with pytest.raises(
        fork.StartStateFault, match="more than one fill is under way at the fork"
    ):
        state(4520.0, tanker_at_the_dock(*fills))


def test_a_tugs_job_and_the_tankers_both_holding_the_dock_are_a_fault() -> None:
    tug = fork.Job("ja", "dispense", T0 + 4405.0, 12900.0, str(A))
    fill = fork.Job("f1", "fill", T0 + 4515.0, 12900.0)
    evidence = tanker_at_the_dock(
        tug, fill, schedule=schedule((A, 0.0, 4400.0, 2000.0))
    )
    with pytest.raises(
        fork.StartStateFault,
        match="a tug's job and the road tanker's both hold the dock",
    ):
        state(4520.0, evidence)


# --- Inferences at the fork, as the dock passes from one job to the next -------
#
# Tug A arrives at 600 s and pumps 2,000 L from 610 s, closing its dispense at
# 690 s. The engineer co-signs the delivery note, and the tug leaves at once: the
# simulator draws A's next arrival then. The dock operator links the note to the
# FMS's transaction with a PATCH after the tug has left, and only then opens the
# next job.


def served_a(*, linked_at: float | None, left_at: float | None, **fields):
    first, rows = served("ja", A, 605.0, 610.0, 2000.0, 35000.0)
    rows = [row for row in rows if row.kind != "bdn"]
    if linked_at is not None:
        rows.append(fork.Transaction("ja", "bdn", T0 + linked_at))
    draws = [(A, 0.0, 600.0, 2000.0)]
    if left_at is not None:
        draws.append((A, left_at, 9000.0, 800.0))
    draws += fields.pop("draws", [])
    return evidence_of(
        readings=readings(lambda s: 35000.0 - dispensing(610, 2000)(s), 1000),
        jobs=(first, *fields.pop("jobs", ())),
        transactions=tuple(rows),
        polls=polls(180.0, 690.0),
        schedule=schedule(*draws),
        **fields,
    )


def test_a_delivery_note_being_linked_after_the_tug_left_holds_the_dock() -> None:
    """A left at 695 s, and the note is not yet linked at 700 s: the dock is
    busy for the PATCH and its retries, and no tug is at the dock for it."""
    start = state(700.0, served_a(linked_at=None, left_at=695.0))
    d = start.draws
    assert d.uncertain is None
    assert d.under_way.departed
    assert d.under_way.start == (0.0, pytest.approx(695.0 + fork.BDN_LINK_S - 700.0))
    assert start.record["linking_delivery_note_of"] == ["ja"]
    assert d.leaving_s == ()
    # The link's job is no tug's, so it leaves no departure inside the episode.
    assert predicted_events.dock(d, TIMINGS, 600.0).jobs == ()


def test_a_link_older_than_the_retry_schedule_is_a_fault() -> None:
    late = 695.0 + fork.BDN_LINK_S + 5.0
    with pytest.raises(
        fork.StartStateFault,
        match="vessel 1999997's delivery note for job ja is not linked within "
        "15 s of the tug's departure at 2026-11-11T18:11:35.000000Z",
    ):
        state(late, served_a(linked_at=None, left_at=695.0))


@pytest.mark.parametrize(
    ("beyond", "fault"), [(fork.RELOGIN_S, False), (fork.RELOGIN_S + 1.0, True)]
)
def test_a_login_during_the_link_extends_its_bound(beyond: float, fault: bool) -> None:
    """A left at 695 s, and the dock operator logged in again at 696 s before
    its PATCH: the link may take RELOGIN_S more."""
    fork_s = 695.0 + fork.BDN_LINK_S + beyond
    evidence = served_a(linked_at=None, left_at=695.0, relogins=(T0 + 696.0,))
    if not fault:
        start = state(fork_s, evidence)
        assert start.record["linking_delivery_note_of"] == ["ja"]
        assert start.draws.under_way.start == (0.0, 0.0)
        return
    with pytest.raises(
        fork.StartStateFault,
        match=f"is not linked within {fork.BDN_LINK_S + fork.RELOGIN_S:g} s of the "
        "tug's departure",
    ):
        state(fork_s, evidence)


def test_a_dispense_with_no_row_that_no_tug_accounts_for_is_a_fault() -> None:
    job = fork.Job("jx", "dispense", T0 + 640.0, 35000.0, str(B))
    evidence = evidence_of(
        readings=readings(lambda s: 35000.0, 700),
        jobs=(job,),
        polls=polls(180.0, 690.0),
        schedule=schedule((B, 0.0, 9000.0, 1000.0)),
    )
    with pytest.raises(fork.StartStateFault, match="the dispense of job jx is open"):
        state(700.0, evidence)


def test_a_served_tug_leaving_holds_no_job_when_the_next_one_opens() -> None:
    """A's note is linked at 700 s, and the dock operator opens tug B's job at
    702 s before the simulator logs A's departure: A holds no job, B's job is
    under way, and A's departure falls inside the episode."""
    second = fork.Job("jb", "dispense", T0 + 702.0, 33000.0, str(B))
    evidence = served_a(
        linked_at=700.0,
        left_at=None,
        jobs=(second,),
        draws=[(B, 0.0, 650.0, 1000.0)],
    )
    start = state(703.0, evidence)
    d = start.draws
    assert d.uncertain is None
    assert d.under_way.arrival_s == pytest.approx(650.0 - 703.0)
    assert d.leaving_s == (0.0,)
    states = {tug["vessel"]: tug["state"] for tug in start.record["tugs"]}
    assert (states[A], states[B]) == ("served, leaving", "under way")


def test_a_refused_and_cancelled_tug_leaving_holds_no_job() -> None:
    first = fork.Job("ja", "dispense", T0 + 605.0, 6000.0, str(A))
    second = fork.Job("jb", "dispense", T0 + 616.0, 6000.0, str(B))
    evidence = evidence_of(
        readings=readings(lambda s: 6000.0, 700),
        jobs=(first, second),
        refusals=(
            fork.Refusal("start", T0 + 612.0, "ja"),
            fork.Refusal("cancel", T0 + 613.0, "ja"),
        ),
        polls=polls(180.0, 600.0),
        schedule=schedule((A, 0.0, 600.0, 2000.0), (B, 0.0, 605.0, 500.0)),
    )
    start = state(617.0, evidence)
    d = start.draws
    assert d.uncertain is None
    assert d.leaving_s == (0.0,)
    assert d.under_way.arrival_s == pytest.approx(605.0 - 617.0)


def test_a_served_tug_leaving_holds_no_job_when_the_tankers_fill_opens() -> None:
    """A's note is linked at 4,500 s, as the road tanker arrives, and the dock
    operator opens the tanker's fill at 4,502 s, before the simulator logs A's
    departure: A holds no job, and the fill is under way."""
    order_at = 180.0
    arrival = order_at + 4320.0
    first, rows = served("ja", A, arrival - 95.0, arrival - 90.0, 2000.0, 14900.0)
    fill = fork.Job("f1", "fill", T0 + arrival + 2.0, 12900.0)
    evidence = evidence_of(
        readings=readings(lambda s: 12900.0, arrival + 30.0),
        jobs=(first, fill),
        transactions=tuple(rows),
        polls=polls(180.0, arrival),
        supplier=supplier(180.0, arrival, order_at, ("tanker arrival", arrival)),
        schedule=schedule((A, 0.0, arrival - 100.0, 2000.0)),
    )
    start = state(arrival + 4.0, evidence)
    d = start.draws
    assert d.uncertain is None
    assert d.under_way.fill
    assert d.leaving_s == (0.0,)
    assert start.record["delivery_state"] == "fill under way"


# --- The bounds on the inferences -----------------------------------------------


@pytest.mark.parametrize(
    ("after", "fault"),
    [(fork.LEAVING_S, False), (fork.LEAVING_S + 1.0, True), (4000.0, True)],
)
def test_a_served_tug_departs_within_its_bound_after_the_link(
    after: float, fault: bool
) -> None:
    """Tug A's delivery note is linked at 700 s, and the simulator logs no
    departure of A."""
    job, rows = served("j1", A, 605.0, 610.0, 2000.0, 35000.0)
    fork_s = 700.0 + after
    evidence = evidence_of(
        readings=readings(lambda s: 33000.0, fork_s),
        jobs=(job,),
        transactions=tuple(rows),
        polls=polls(180.0, fork_s),
        schedule=schedule((A, 0.0, 600.0, 2000.0)),
    )
    if not fault:
        assert state(fork_s, evidence).draws.leaving_s == (0.0,)
        return
    with pytest.raises(
        fork.StartStateFault,
        match="vessel 1999997's delivery note was linked to job j1 at "
        "2026-11-11T18:11:40.000000Z, and the simulator's log holds no departure "
        "of the tug since",
    ):
        state(fork_s, evidence)


@pytest.mark.parametrize(
    ("after", "fault"),
    [(fork.LEAVING_S, False), (fork.LEAVING_S + 1.0, True), (4000.0, True)],
)
def test_a_refused_tug_departs_within_its_bound_after_the_cancel(
    after: float, fault: bool
) -> None:
    """Tug A's job is refused at its start at 615 s and cancelled at 616 s,
    and the simulator logs no departure of A."""
    job = fork.Job("j2", "dispense", T0 + 605.0, 5900.0, str(A))
    fork_s = 616.0 + after
    evidence = evidence_of(
        readings=readings(lambda s: 5900.0, fork_s),
        jobs=(job,),
        refusals=(
            fork.Refusal("start", T0 + 615.0, "j2"),
            fork.Refusal("cancel", T0 + 616.0, "j2"),
        ),
        polls=polls(180.0, fork_s),
        schedule=schedule((A, 0.0, 600.0, 2000.0)),
    )
    if not fault:
        assert state(fork_s, evidence).draws.leaving_s == (0.0,)
        return
    with pytest.raises(
        fork.StartStateFault,
        match="vessel 1999997's refused job j2 was cancelled at "
        "2026-11-11T18:10:16.000000Z, and the simulator's log holds no departure "
        "of the tug since",
    ):
        state(fork_s, evidence)


def refused_and_not_cancelled(fork_s: float, *relogins: float) -> fork.Evidence:
    """Tug A's job, refused at its start at 612 s, with no cancel logged."""
    job = fork.Job("j1", "dispense", T0 + 605.0, 6000.0, str(A))
    return evidence_of(
        readings=readings(lambda s: 6000.0, fork_s),
        jobs=(job,),
        refusals=(fork.Refusal("start", T0 + 612.0, "j1"),),
        polls=polls(180.0, fork_s),
        schedule=schedule((A, 0.0, 600.0, 2000.0)),
        relogins=tuple(T0 + s for s in relogins),
    )


def test_a_refused_job_holds_the_dock_until_its_cancel_is_due() -> None:
    """The cancel's DELETE and its retries take one exchange, 15 s."""
    assert fork.CANCEL_S == fork.EXCHANGE_S == 15.0
    d = state(620.0, refused_and_not_cancelled(620.0)).draws
    assert d.under_way.refused
    assert d.under_way.start == (-8.0, 7.0)


@pytest.mark.parametrize(
    ("relogins", "bound", "fault"),
    [
        ((), fork.CANCEL_S, False),
        ((), fork.CANCEL_S + 1.0, True),
        # The DELETE met an expired session, and the dock operator logged in
        # again before its retry.
        ((612.2,), fork.CANCEL_S + fork.RELOGIN_S, False),
        ((612.2,), fork.CANCEL_S + fork.RELOGIN_S + 1.0, True),
    ],
)
def test_a_refused_job_is_cancelled_within_its_bound(
    relogins: tuple, bound: float, fault: bool
) -> None:
    fork_s = 612.0 + bound
    evidence = refused_and_not_cancelled(fork_s, *relogins)
    if not fault:
        d = state(fork_s, evidence).draws
        assert d.under_way.start[1] == pytest.approx(0.0)
        return
    limit = fork.CANCEL_S + (fork.RELOGIN_S if relogins else 0.0)
    with pytest.raises(
        fork.StartStateFault,
        match="vessel 1999997's job j1 was refused at its start at "
        "2026-11-11T18:10:12.000000Z, and the simulator's log holds no cancel of "
        f"it within {limit:g} s",
    ):
        state(fork_s, evidence)


def test_the_simulators_log_gives_the_dock_operators_logins() -> None:
    warn = "WARN sim::actors::dock_operator::internals:"
    log = "\n".join(
        [
            f"2026-11-11T18:10:12.200000Z  {warn} HMI session expired; "
            're-authenticating request="DELETE /api/jobs/{id}"',
            f"2026-11-11T18:10:13.000000Z  {warn} cancelled the refused job "
            'request="DELETE /api/jobs/{id}" job_id="0b6f1d2e-1111-4222-8333-944455556666"',
            "2026-11-11T18:20:00.000000Z  WARN sim::other: HMI session expired; "
            "re-authenticating",
        ]
    )
    assert fork.parse_relogins(log) == (T0 + 612.2,)


def test_the_note_bound_takes_a_poll_three_exchanges_and_the_departure() -> None:
    """At a compression factor of 5 the dock operator polls every 6 s: 6 s,
    three exchanges of 15 s and LEAVING_S, 56 s."""
    assert TIMINGS.dock_operator_poll_s == 6.0
    assert fork.note_bound_s(TIMINGS) == 6.0 + 3 * fork.EXCHANGE_S + fork.LEAVING_S


def dispensed_and_not_departed(fork_s: float, *relogins: float) -> fork.Evidence:
    """Tug A's dispense closes at 690 s, with no delivery note linked and no
    departure of A logged."""
    job, rows = served("j1", A, 605.0, 610.0, 2000.0, 35000.0)
    return evidence_of(
        readings=readings(lambda s: 33000.0, fork_s),
        jobs=(job,),
        transactions=(rows[0],),
        polls=polls(180.0, fork_s),
        schedule=schedule((A, 0.0, 600.0, 2000.0)),
        relogins=tuple(T0 + s for s in relogins),
    )


@pytest.mark.parametrize(
    ("relogins", "beyond", "fault"),
    [
        ((), 0.0, False),
        ((), 1.0, True),
        ((692.0,), fork.RELOGIN_S, False),
        ((692.0,), fork.RELOGIN_S + 1.0, True),
        # The totaliser poll under way at the close met the expired session
        # 10 s before it, and the login spans the close.
        ((680.0,), fork.RELOGIN_S, False),
        ((680.0,), fork.RELOGIN_S + 1.0, True),
    ],
)
def test_a_dispensed_tug_departs_within_its_bound(
    relogins: tuple, beyond: float, fault: bool
) -> None:
    fork_s = 690.0 + fork.note_bound_s(TIMINGS) + beyond
    evidence = dispensed_and_not_departed(fork_s, *relogins)
    if not fault:
        start = state(fork_s, evidence)
        assert start.record["tugs"][0]["state"] == (
            "dispensed, with its delivery note under way"
        )
        return
    limit = fork.note_bound_s(TIMINGS) + (fork.RELOGIN_S if relogins else 0.0)
    with pytest.raises(
        fork.StartStateFault,
        match="vessel 1999997's dispense of job j1 closed at "
        "2026-11-11T18:11:30.000000Z, and the simulator's log holds no departure "
        f"of the tug within {limit:g} s",
    ):
        state(fork_s, evidence)


def test_the_pumping_bounds_take_world_yamls_rates_at_the_factor() -> None:
    """At a compression factor of 5 the dispenser pumps 25 L/s and the road
    tanker 50 L/s: 2,000 L take 12 + 80 + 15 = 107 s, and the 18,000 L
    delivery 12 + 360 + 15 = 387 s."""
    assert (TIMINGS.outflow_l_per_s, TIMINGS.inflow_l_per_s) == (25.0, 50.0)
    assert fork.refuel_bound_s(TIMINGS, 2000.0) == 107.0
    assert fork.fill_bound_s(TIMINGS) == 387.0


@pytest.mark.parametrize(("beyond", "fault"), [(0.0, False), (1.0, True)])
def test_a_refuel_closes_within_its_bound(beyond: float, fault: bool) -> None:
    """Tug A's job opens at 605 s and its 2,000 L are pumped by 690 s, and the
    FMS records no close of the dispense."""
    job = fork.Job("j1", "dispense", T0 + 605.0, 35000.0, str(A))
    fork_s = 605.0 + fork.refuel_bound_s(TIMINGS, 2000.0) + beyond
    evidence = evidence_of(
        readings=readings(lambda s: 35000.0 - dispensing(610, 2000)(s), fork_s),
        jobs=(job,),
        polls=polls(180.0, fork_s),
        schedule=schedule((A, 0.0, 600.0, 2000.0)),
    )
    if not fault:
        assert state(fork_s, evidence).record["tugs"][0]["state"] == "under way"
        return
    with pytest.raises(
        fork.StartStateFault,
        match="vessel 1999997's refuel, job j1, opened at "
        "2026-11-11T18:10:05.000000Z, has no dispense closed within 107 s: the "
        "pump is stuck",
    ):
        state(fork_s, evidence)


@pytest.mark.parametrize(("beyond", "fault"), [(0.0, False), (1.0, True)])
def test_a_fill_is_transcribed_within_its_bound(beyond: float, fault: bool) -> None:
    """The road tanker arrives at 4,500 s and its fill opens at 4,505 s, and
    the FMS records no transcription of it."""
    arrival = 180.0 + 4320.0
    fill = fork.Job("f1", "fill", T0 + arrival + 5.0, 14900.0)
    fork_s = arrival + 5.0 + fork.fill_bound_s(TIMINGS) + beyond
    evidence = evidence_of(
        readings=readings(lambda s: 14900.0, fork_s),
        jobs=(fill,),
        polls=polls(180.0, fork_s),
        supplier=supplier(180.0, fork_s, 180.0, ("tanker arrival", arrival)),
    )
    if not fault:
        start = state(fork_s, evidence)
        assert start.record["delivery_state"] == "fill under way"
        return
    with pytest.raises(
        fork.StartStateFault,
        match="the road tanker's fill, job f1, opened at "
        "2026-11-11T19:15:05.000000Z, is not transcribed within 387 s: the fill "
        "is stuck",
    ):
        state(fork_s, evidence)


def test_the_waiting_bound_takes_a_refuels_start_and_a_login() -> None:
    """12 s for a refuel's first connection after its tug's arrival, and a
    login: the POST, the challenge's 30 s time to live, its poll, and the
    retried request."""
    assert fork.RELOGIN_S == fork.EXCHANGE_S + 30.0 + fork.EXCHANGE_S + fork.EXCHANGE_S
    assert (fork.RELOGIN_S, fork.WAITING_S) == (75.0, 87.0)
    assert fork.WAITING_S == 12.0 + fork.RELOGIN_S


@pytest.mark.parametrize(
    ("waited", "fault"), [(fork.WAITING_S, False), (fork.WAITING_S + 1.0, True)]
)
def test_a_tug_waits_at_an_idle_dock_within_its_bound(
    waited: float, fault: bool
) -> None:
    """Tug A arrives at 600 s at an idle dock, and the dock operator opens no
    job for it."""
    fork_s = 600.0 + waited
    evidence = evidence_of(
        readings=readings(lambda s: 35000.0, fork_s),
        polls=polls(180.0, fork_s),
        schedule=schedule((A, 0.0, 600.0, 2000.0)),
    )
    if not fault:
        start = state(fork_s, evidence)
        assert start.record["tugs"][0]["state"] == "waiting"
        return
    with pytest.raises(
        fork.StartStateFault,
        match="vessel 1999997 has waited at an idle dock since "
        "2026-11-11T18:10:00.000000Z, more than the 87 s within which the dock "
        "operator opens its job",
    ):
        state(fork_s, evidence)


@pytest.mark.parametrize(
    ("after", "fault"), [(fork.WAITING_S, False), (fork.WAITING_S + 1.0, True)]
)
def test_a_tugs_wait_counts_from_the_end_of_the_previous_job(
    after: float, fault: bool
) -> None:
    """Tug B arrives at 650 s while tug A is served. A's note is linked at
    700 s and A's departure logged at 702 s: B's wait at the idle dock counts
    from 702 s, not from its arrival."""
    job, rows = served("ja", A, 605.0, 610.0, 2000.0, 35000.0)
    fork_s = 702.0 + after
    evidence = evidence_of(
        readings=readings(lambda s: 33000.0, fork_s),
        jobs=(job,),
        transactions=tuple(rows),
        polls=polls(180.0, fork_s),
        schedule=schedule(
            (A, 0.0, 600.0, 2000.0), (B, 0.0, 650.0, 1000.0), (A, 702.0, 9000.0, 800.0)
        ),
    )
    if not fault:
        states = {
            tug["vessel"]: tug["state"]
            for tug in state(fork_s, evidence).record["tugs"]
        }
        assert states[B] == "waiting"
        return
    with pytest.raises(
        fork.StartStateFault,
        match="vessel 1999985 has waited at an idle dock since "
        "2026-11-11T18:11:42.000000Z",
    ):
        state(fork_s, evidence)


def test_a_tug_waiting_behind_a_job_under_way_has_no_bound() -> None:
    """Tug B has waited 250 s while A's 10,000 L dispense is pumping."""
    job = fork.Job("ja", "dispense", T0 + 605.0, 35000.0, str(A))
    evidence = evidence_of(
        readings=readings(lambda s: 35000.0 - dispensing(610, 10000)(s), 900),
        jobs=(job,),
        polls=polls(180.0, 900.0),
        schedule=schedule((A, 0.0, 600.0, 10000.0), (B, 0.0, 650.0, 1000.0)),
    )
    start = state(900.0, evidence)
    states = {tug["vessel"]: tug["state"] for tug in start.record["tugs"]}
    assert (states[A], states[B]) == ("under way", "waiting")


@pytest.mark.parametrize(
    ("waited", "fault"), [(fork.WAITING_S, False), (fork.WAITING_S + 1.0, True)]
)
def test_a_road_tanker_waits_at_an_idle_dock_within_its_bound(
    waited: float, fault: bool
) -> None:
    """The road tanker arrives at 4,500 s at an idle dock, and the dock
    operator opens no fill for it."""
    arrival = 180.0 + 4320.0
    fork_s = arrival + waited
    evidence = evidence_of(
        readings=readings(lambda s: 14900.0, fork_s),
        polls=polls(180.0, fork_s),
        supplier=supplier(180.0, fork_s, 180.0, ("tanker arrival", arrival)),
    )
    if not fault:
        start = state(fork_s, evidence)
        assert start.record["delivery_state"] == "tanker waiting at the dock"
        return
    with pytest.raises(
        fork.StartStateFault,
        match="the road tanker has waited at an idle dock since "
        "2026-11-11T19:15:00.000000Z",
    ):
        state(fork_s, evidence)


def test_a_leaving_tug_excludes_a_refuel_chain_whose_refuel_follows_it() -> None:
    """The leaving tug's return is not predicted, so a refuel that starts after
    its departure cannot serve a chain (episodes.md §10.2, condition 2)."""
    d = predicted_events.Draws(
        initial_volume_l=35000.0,
        order_pending_s=None,
        first_poll_after_s=300.0,
        delivery_volume_l=18000.0,
        arrivals=((440.0, 3000.0),),
        leaving_s=(0.0,),
    )
    (refuel,) = predicted_events.dock(d, TIMINGS, 840.0).jobs
    assert predicted_events.departure_before((refuel,), refuel, d.leaving_s) == 0.0


@pytest.mark.parametrize(
    ("entries", "written", "completed"),
    [
        ((("2026-11-11T18:00:00Z", "2026-11-17T18:00:00Z", "agreed"),), T0 + 900, True),
        (
            (("2026-10-01T00:00:00Z", "2026-10-31T00:00:00Z", "agreed"),),
            T0 + 900,
            False,
        ),
        (
            (("2026-11-11T18:00:00Z", "2026-11-17T18:00:00Z", "agreed"),),
            T0 - 900,
            False,
        ),
        ((), None, False),
    ],
)
def test_a_completed_audit_is_one_of_the_living_ports_before_the_fork(
    entries: tuple, written: float | None, completed: bool
) -> None:
    evidence = evidence_of(
        readings=readings(lambda s: 35000.0, 1000),
        polls=polls(180.0, 1000.0),
        audits=entries,
        history_written=written,
    )
    start = state(1000.0, evidence)
    assert start.draws.audit_completed is completed
    assert start.record["audits"]["completed_before_fork"] is completed


def test_the_record_gives_the_schedules_at_the_fork() -> None:
    record = state(1800.0, idle_port()).record
    assert record["audits"]["next_audit_at"] == "2026-11-17T19:12:00.000000Z"
    assert record["routine_checks"]["first_at"] == "2026-11-11T22:48:00.000000Z"
    # Twelve days on, past the first audit, the next is one period after it.
    later_s = 1800.0 + 2 * startup.audit_period_s(TIMINGS.audit_period)
    later = state(
        later_s,
        evidence_of(
            readings=((T0 + later_s - 6.0, 32000.0),), polls=polls(180.0, later_s)
        ),
    ).record
    assert later["audits"]["next_audit_at"] == "2026-11-23T19:12:00.000000Z"


def test_the_simulators_log_gives_the_scheduled_arrivals_and_the_suppliers_events() -> (
    None
):
    """The lines as the competition's entry point writes them
    (infra/sim/tests/tug_arrival_log.rs gives the generator's form)."""
    log = "\n".join(
        [
            "2026-11-11T18:00:00.250000Z  INFO tug_arrivals: scheduled arrival "
            "vessel_id=1999997 scheduled_arrival=2026-11-11T20:00:00.5Z "
            "volume_l=1585.7912345",
            "2026-11-11T18:03:00.000000Z  INFO fuel_supplier: poll volume_l=14900 "
            "alarm_active=false order_placed=true",
            "2026-11-11T18:03:00.000000Z  INFO fuel_supplier: order placed "
            "volume_l=14900 tanker_arrival=2026-11-11T19:15:00Z",
            "2026-11-11T18:06:00.000000Z  INFO fuel_supplier: poll volume_l=14800 "
            "alarm_active=false order_placed=false",
            "2026-11-11T19:15:00.000000Z  INFO fuel_supplier: tanker arrival "
            "delivery_volume_l=18000.0",
            "2026-11-11T19:21:10.000000Z  INFO fuel_supplier: delivery ended",
            "2026-11-11T20:00:00.600000Z  INFO tug_arrivals: arrival "
            "vessel_id=1999997 scheduled_arrival=2026-11-11T20:00:00.5Z "
            "volume_l=1585.7912345",
        ]
    )
    assert fork.parse_schedule(log) == (
        fork.Scheduled(A, T0 + 0.25, T0 + 7200.5, 1585.7912345),
    )
    assert fork.parse_supplier(log) == (
        fork.SupplierEvent("poll", T0 + 180.0, True),
        fork.SupplierEvent("order placed", T0 + 180.0, tanker_arrival=T0 + 4500.0),
        fork.SupplierEvent("poll", T0 + 360.0, False),
        fork.SupplierEvent("tanker arrival", T0 + 4500.0),
        fork.SupplierEvent("delivery ended", T0 + 4870.0),
    )


def test_an_order_without_its_tankers_scheduled_arrival_is_a_fault() -> None:
    evidence = evidence_of(
        readings=readings(lambda s: 14900.0, 400),
        polls=polls(180.0, 360.0),
        supplier=(
            fork.SupplierEvent("poll", T0 + 180.0, True),
            fork.SupplierEvent("order placed", T0 + 180.0),
            fork.SupplierEvent("poll", T0 + 360.0),
        ),
    )
    with pytest.raises(fork.StartStateFault) as raised:
        state(400.0, evidence)
    assert raised.value.record["delivery_state"] == "order placed"
    assert raised.value.faults == (
        "the supplier's order placed at 2026-11-11T18:03:00.000000Z gives no "
        "scheduled arrival of its road tanker",
    )


# The order at 180 s brings its road tanker at 4,500 s, when the supplier's
# poll due at 4,500 s also falls due. The supplier serves its polls and the
# tanker's arrival in turn, so only a poll under way can hold the arrival.
TANKER_DUE = "2026-11-11T19:15:00.000000Z"


def tanker_due_at_4500(
    fork_s: float, connections: tuple[float, ...], ended_until: float
) -> fork.Evidence:
    """The relay's journal holds the polls every 180 s to 4,320 s and the
    given connections; the simulator's log holds the polls that ended by
    ended_until, and no arrival of the tanker."""
    return evidence_of(
        readings=readings(lambda s: 14900.0, fork_s),
        polls=polls(180.0, 4320.0) + tuple(T0 + s for s in connections),
        supplier=supplier(180.0, ended_until, 180.0),
    )


def test_a_road_tanker_arriving_within_its_lateness_is_due() -> None:
    """The poll at 4,500 s has ended, and the supplier logs the arrival next:
    a fork 5 s after the tanker fell due finds it arriving."""
    start = state(4505.0, tanker_due_at_4500(4505.0, (4500.0,), 4500.0))
    assert start.draws.uncertain is None
    assert start.draws.order_pending_s == pytest.approx(-5.0)
    assert start.record["delivery_state"] == "order placed"


@pytest.mark.parametrize(
    "connections",
    [
        # The relay logged no connection of the poll due at 4,500 s, which
        # is still retrying.
        (),
        # The relay logged the poll's connection and its retries, and the
        # simulator's log no end of it.
        (4500.0, 4501.0, 4503.0, 4507.0),
    ],
)
def test_a_road_tanker_held_by_a_poll_under_way_is_uncertain(
    connections: tuple[float, ...],
) -> None:
    """A fork 20 s after the tanker fell due, with the poll due at 4,500 s in
    progress: the dock's order of service is uncertain, the poll is overdue,
    and no fault discards the episode."""
    start = state(4520.0, tanker_due_at_4500(4520.0, connections, 4320.0))
    d = start.draws
    assert d.tanker_overdue == (
        "the road tanker of the order placed at 2026-11-11T18:03:00.000000Z was "
        f"due at {TANKER_DUE}, and the simulator's log holds no arrival of it"
    )
    assert d.poll_overdue == (
        f"the supplier's poll due at {TANKER_DUE} had not ended by the fork"
    )
    assert d.uncertain == f"{d.poll_overdue}; {d.tanker_overdue}"
    assert d.order_pending_s == pytest.approx(-20.0)
    assert start.record["faults"] == []
    assert start.record["supplier"]["poll_under_way_due"] == TANKER_DUE


def test_a_road_tanker_held_by_a_poll_an_interval_overdue_is_that_fault() -> None:
    """The poll due at 4,500 s, which holds the tanker's arrival, has not
    ended 190 s on."""
    with pytest.raises(
        fork.StartStateFault,
        match=f"the supplier's poll due at {TANKER_DUE} had not ended by the "
        "fork, more than one poll interval after it fell due",
    ):
        state(4690.0, tanker_due_at_4500(4690.0, (), 4320.0))


def test_a_road_tanker_overdue_with_no_poll_under_way_is_a_fault() -> None:
    """The poll due at 4,500 s has ended, and 20 s after the tanker fell due
    its arrival is not logged: no poll holds it."""
    with pytest.raises(
        fork.StartStateFault,
        match=f"was due at {TANKER_DUE}, and the simulator's log holds no arrival "
        "of it, and no poll of the supplier under way holds it",
    ):
        state(4520.0, tanker_due_at_4500(4520.0, (4500.0,), 4500.0))
