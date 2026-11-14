"""One living port seed for two entries (episodes-tests.md §3, start state,
living ports and forks).

The module draws one living port seed for the run and creates two living ports
in evaluation mode with it, as many at once as the parallel input allows, each
with a manager, a manager's root and SSH ports of its own:

- `entry-a`: the volume of an entry whose test defender raises no alarm;
- `entry-b`: an empty volume, so the launcher runs the idle defender.

Each living port then makes two forks on its own drawn schedule with the
manager's loop, and each fork runs a benign Track A episode to the horizon.
Waiting for the drawn fork instants is the point of the test, since the fork
waits are among the draws: fork 1 falls within 6 minutes of the settle period,
and fork 2 14 to 20 minutes after it. The module takes about 40 to 45 minutes
with both living ports at once.

Each tug's time away is its scheduled arrival less the instant at which the
simulator logged the draw, both on the living port's own clock, so the two
living ports' different start times cancel. The events' own times are not
compared (episodes.md §14, reproducibility).

The simulator's start_state event holds the start state. Its tug seed derives
from the living port seed, and its other fields are the simulator's defaults
at the competition's timings, apart from first_audit_after_s, the time from
the simulator's start to the first audit. The first audit is not drawn: the
start-up input places it one audit period and one audit settling delay after
the start of the second in which the input is written (episodes.md §4.3,
start-up input). Its time from the simulator's start is that interval less the
time from that second to the start, which varies with how long each living
port's deployment takes. The module therefore checks it on each living port
alone, so that the comparison holds whether the two living ports start
together or minutes apart.

Run alone with:
make -C tests/integration test-living LIVING_TEST=test_02_same_seed.py
"""

from __future__ import annotations

import json
import re
import secrets
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from competition.episode_runner import episode, fork, predicted_events, startup
from competition.episode_runner.seeds import SIM_TUG_ARRIVALS, derive_generator_seed
from tests.integration.living.conftest import (
    Inputs,
    Port,
    delete_ports,
    living_ports,
)

TEST_DEFENDER = (
    Path(__file__).resolve().parents[1] / "episodes" / "defender" / "test-defender"
)
# The two entries, each a living port of its own.
ENTRIES = ("entry-a", "entry-b")
FORKS = 2
# The SSH ports of this module's living ports lie 100 above the run's first,
# clear of test_01's, whose living ports a failed run keeps.
SSH_PORT_OFFSET = 100
# The simulator logs a tug's scheduled arrival, and the first audit's time from
# its start, on a line whose own instant is rounded to the microsecond and
# taken just after the simulator read its clock for that value. Measured
# against the line's instant, on the same virtual machine's clock, the value is
# therefore off by that rounding and by the moment between the reading and the
# writing, microseconds unless the host is loaded. 10 ms covers both with room,
# while times away drawn with a mean of hours never lie so close by chance.
CLOCK_READING_TOLERANCE_S = 0.01
START_STATE = " start_state: start_state "
# A field of a line of the simulator's log, name=value, with a text value in
# quotation marks.
FIELD = re.compile(r'(\w+)=("[^"]*"|\S+)')
# The one field of the start_state event that depends on the clock.
FIRST_AUDIT = "first_audit_after_s"


@dataclass
class ForkDraws:
    """What one fork drew and ran: its drawn wait, its episode's seed and
    outcome, the simulator's log that its export holds, and the time of the
    first audit that the living port's start-up input gives."""

    wait_s: float
    episode_seed: str
    outcome: str
    sim_log: str
    next_audit_at: datetime


@dataclass
class PortDraws:
    """One living port's seed, the instant at which the manager began to
    create it, the forks that its loop made, and each fork's draws."""

    seed: str
    created_at: datetime
    made: list[int]
    forks: dict[int, ForkDraws]


def entry_volume(directory: Path, entry: str) -> Path:
    """The volume of an entry: the test defender with no alarm, or nothing."""
    directory.mkdir()
    if entry == "entry-a":
        defender = directory / "defender"
        defender.write_bytes(TEST_DEFENDER.read_bytes())
        defender.chmod(0o755)
        script = {"alarm_at": None, "read": False}
        (directory / "script.json").write_text(json.dumps(script))
    return directory


def run_port(port: Port, seed: str, volume: Path) -> PortDraws:
    """Create the entry's living port with the run's seed, let it make two
    forks on its drawn schedule, each with a benign Track A episode, and read
    what each fork drew."""
    inputs = port.inputs
    port.created = True
    port.manager.create(
        port.name,
        port.base(),
        inputs.tree,
        seed=seed,
        ssh_port=port.ssh_port,
        affinity=inputs.affinity,
        volume=volume,
    )
    made = port.manager.loop(
        port.name, track="A", episode_type=episode.BENIGN, forks=FORKS
    )
    record = port.manager.read(port.name)
    episodes = port.manager.port_dir(port.name) / "episodes"
    forks = {}
    for k in made:
        out = episodes / f"fork-{k}"
        fork_record = json.loads((out / "record.json").read_text())
        # The living port's start-up input, which the fork's record keeps.
        given = json.loads((out / "startup.json").read_text())
        forks[k] = ForkDraws(
            wait_s=record["forks"][str(k)]["drawn"]["wait_s"],
            episode_seed=fork_record["episode_seed"],
            outcome=fork_record["outcome"],
            sim_log=(out / "export" / "sim.log").read_text(),
            next_audit_at=fork.parse_instant(given["next_audit_at"]),
        )
    return PortDraws(
        seed=record["seed"],
        created_at=fork.parse_instant(record["created_at"]),
        made=made,
        forks=forks,
    )


@pytest.fixture(scope="module")
def entries(
    inputs: Inputs, request: pytest.FixtureRequest, tmp_path_factory
) -> Iterator[dict[str, PortDraws | BaseException]]:
    """Each entry's living port's draws, or the exception that ended it, with
    the run's one living port seed."""
    seed = secrets.token_bytes(32).hex()
    names = {entry: f"{inputs.run}-{entry}" for entry in ENTRIES}
    ports = living_ports(inputs, names, inputs.ssh_port + SSH_PORT_OFFSET)
    volumes = tmp_path_factory.mktemp("entry-volumes")
    with ThreadPoolExecutor(max_workers=inputs.parallel) as pool:
        futures = {
            entry: pool.submit(
                run_port, ports[entry], seed, entry_volume(volumes / entry, entry)
            )
            for entry in ENTRIES
        }
        results: dict[str, PortDraws | BaseException] = {}
        for entry, future in futures.items():
            failure = future.exception()
            results[entry] = failure if failure is not None else future.result()
    yield results
    delete_ports(ports.values(), request.session)


def tug_draws(sim_log: str) -> dict[int, list[tuple[float, float]]]:
    """Each tug's times away and requested volumes, in the order drawn."""
    draws: dict[int, list[tuple[float, float]]] = {}
    for scheduled in fork.parse_schedule(sim_log):
        away = scheduled.arrives - scheduled.logged
        draws.setdefault(scheduled.vessel, []).append((away, scheduled.volume_l))
    return draws


def start_state(sim_log: str) -> tuple[datetime, dict[str, str]]:
    """The instant of the simulator's start_state event, which the episode
    runner takes as the simulator's start (episodes.md §4.3, start-up input),
    and the text of each of its fields."""
    for line in sim_log.splitlines():
        if START_STATE in line:
            fields = dict(FIELD.findall(line.split(START_STATE, 1)[1]))
            return fork.parse_instant(line.split()[0]), fields
    raise AssertionError("the simulator's log holds no start_state event")


def first_audit_offset() -> timedelta:
    """One audit period and one audit settling delay at the competition's
    timings: the time from the start of the second in which the start-up
    input is written to the first audit (episodes.md §4.3, start-up input)."""
    timings = predicted_events.competition_timings()
    return timedelta(
        seconds=startup.audit_period_s(timings.audit_period) + timings.settling_delay_s
    )


def check_first_audit(
    entry: str, port: PortDraws, start: datetime, logged: str, next_audit: datetime
) -> None:
    """The first audit's time on one living port. The simulator measures it
    from its start to the start-up input's next_audit_at, which lies one audit
    period and one audit settling delay after the start of the second in which
    the input was written, during the living port's creation."""
    value = re.fullmatch(r"Some\((\S+)\)", logged)
    assert value is not None, (entry, logged)
    measured = (next_audit - start).total_seconds()
    assert abs(float(value.group(1)) - measured) <= CLOCK_READING_TOLERANCE_S, (
        entry,
        logged,
        measured,
    )
    # No record holds the time of writing, but it lies after the manager began
    # to create the living port and before the simulator started. The manager
    # reads created_at from the host's clock before the virtual machine boots,
    # and the virtual machine takes its clock from the host's when it boots.
    written = next_audit - first_audit_offset()
    assert port.created_at.replace(microsecond=0) <= written <= start, (
        entry,
        port.created_at,
        written,
        start,
    )


def test_every_entry_runs_over_the_same_living_port_seeds(entries) -> None:
    """episodes-tests.md, every_entry_runs_over_the_same_living_port_seeds:
    two living ports created with the same living port seed and the volumes
    of two different entries make the same draws: the same fork waits and
    episode seeds, and each tug's times away and requested volumes.

    The two start states are compared as well, apart from the time from the
    simulator's start to the first audit, which is checked on each living port
    alone. The tugs' draws come from each living port's simulator log as its
    fork 2 exported it, which holds the living port's history to fork 2 and
    fork 2's episode. The two logs end at different moments, so each tug's
    draws are compared as far as both logs hold them, and one log may hold one
    more draw of a tug than the other."""
    found = {}
    for entry, draws in entries.items():
        if isinstance(draws, BaseException):
            raise draws
        found[entry] = draws
    a, b = (found[entry] for entry in ENTRIES)
    assert a.seed == b.seed
    assert a.made == b.made == list(range(1, FORKS + 1))
    for k in a.made:
        assert a.forks[k].wait_s == b.forks[k].wait_s, k
        assert a.forks[k].episode_seed == b.forks[k].episode_seed, k
        assert (a.forks[k].outcome, b.forks[k].outcome) == ("horizon", "horizon"), k

    last = a.made[-1]
    # Every entry runs over the same living port seeds, so that every entry
    # meets the same operation of the port (episodes.md §6.3, the living port).
    # A living port seed reproduces the living port's draws, while other times
    # vary between runs (§14, reproducibility). The tug seed derives from the
    # living port seed, and the other fields of the start state are the
    # simulator's defaults at the competition's timings, so each is compared
    # exactly. The time from the simulator's start to the first audit is not a
    # draw: the first audit lies one audit period and one audit settling delay
    # after the start of the second in which each living port's start-up input
    # is written (§4.3, start-up input), and the time from that second to the
    # simulator's start varies with how long each deployment takes. It is
    # checked on each living port alone.
    started = {
        entry: start_state(found[entry].forks[last].sim_log) for entry in ENTRIES
    }
    fields_a, fields_b = (started[entry][1] for entry in ENTRIES)
    assert int(fields_a[SIM_TUG_ARRIVALS]) == derive_generator_seed(
        a.seed, SIM_TUG_ARRIVALS
    )
    assert {n: v for n, v in fields_a.items() if n != FIRST_AUDIT} == {
        n: v for n, v in fields_b.items() if n != FIRST_AUDIT
    }
    for entry in ENTRIES:
        start, fields = started[entry]
        port = found[entry]
        check_first_audit(
            entry, port, start, fields[FIRST_AUDIT], port.forks[last].next_audit_at
        )

    draws_a = tug_draws(a.forks[last].sim_log)
    draws_b = tug_draws(b.forks[last].sim_log)
    assert draws_a and set(draws_a) == set(draws_b)
    compared: dict[int, Any] = {}
    for vessel, ours in draws_a.items():
        theirs = draws_b[vessel]
        assert ours and theirs, vessel
        assert abs(len(ours) - len(theirs)) <= 1, (vessel, len(ours), len(theirs))
        # The draws that both logs hold, in the order drawn.
        for n, ((away_a, volume_a), (away_b, volume_b)) in enumerate(zip(ours, theirs)):
            assert abs(away_a - away_b) <= CLOCK_READING_TOLERANCE_S, (vessel, n)
            assert volume_a == volume_b, (vessel, n)
        compared[vessel] = min(len(ours), len(theirs))
    assert all(count >= 1 for count in compared.values()), compared
