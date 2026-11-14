"""The fork of a living port (episodes.md §6.3, the living port; §6.4, how a
fork is made; §10.2, choosing the chain).

A fork holds a living port's deployment as it ran at the fork instant, and runs
one episode on it with no deployment and no reset. The living port's manager
writes the identity file when it creates the living port: the living port seed,
the seed of the generator sim_tug_arrivals, the simulator's start and the path
of the start-up input. The fork's episode seed derives from the living port
seed (seeds.fork_seed).

The fork's tug streams continue the living port's, with no new seed. For the
choice of an attack chain, and for the episode record, the episode runner reads
the start state at the fork instant from the fork itself, through Incus:

- from the database of the Fuel Management System (FMS) on fms-01: the tank
  gauge's readings, every job with its counterparty, and every transaction;
- from the simulator's log on sim-host: the dispenses that the FMS refused,
  each tug's scheduled arrival and requested volume, logged as the simulator
  drew them, and the fuel supplier's orders and deliveries;
- from the journal of the tank-gauge relay on atg-relay-01: each poll of the
  fuel supplier, which HAProxy logs as a connection from the supplier's
  address;
- from the reconciliation user's home directory on recon-01: the summary entry
  of each completed audit, and the time at which the shell history was last
  written;
- from the identity file and the start-up input: the simulator's start and the
  schedules.

From these the module derives the tank's volume at the fork, the delivery state,
the supplier's next poll, the dock operator's job under way, the tugs waiting
and the arrivals still to come. Each time counts from the fork instant
(predicted_events.Draws).

The simulator draws a tug's time away and requested volume as the tug leaves
the dock, and logs its scheduled arrival with the volume. For each tug, the
scheduled arrival logged last before the fork instant is either still to come,
or has passed, and the tug is at the dock: waiting, or served by the dock
operator. A tug's next arrival after a departure inside the episode is drawn
only then, so the module predicts none (predicted_events.departure_before).

The fork holds the living port's whole memory, so each procedure under way at
the fork instant continues in the fork. The module reads only the records
written up to the fork instant, so it infers how each procedure under way
ends (episodes.md §6.4, inferences at the fork). A fork can fall as the dock
passes from one job to the next: a delivery note being linked after its tug
has left keeps the dock busy for the link and its retries alone, and a served
or refused tug that is leaving after the next job has opened holds no job at
the dock. Each inference allows a bounded time (episodes.md §6.4, bounds on
the inferences): for a leaving tug, a delivery note's link, a refused job's
cancel, a dispensed tug's delivery note, a refuel or a fill under way, a
transcribed fill's delivery end, and a tug or the road tanker waiting at an
idle dock (LEAVING_S, BDN_LINK_S, CANCEL_S, note_bound_s, refuel_bound_s,
fill_bound_s, WAITING_S). A bound is longer by RELOGIN_S where the
simulator's log holds a login of the dock operator meanwhile, and by a poll's
lateness where a poll under way holds the supplier. Past its bound the module
presumes the inference wrong. A start state that no inference explains is a
fault of the testbed (StartStateFault), which discards the episode with its
cause (episodes.md §12, discarded): a read that fails after one retry, no
reading of the tank gauge, a tug with no scheduled arrival logged, a preset
unlike the logged request, more than one job holding the dock, a job at the
dock that no tug accounts for, an inference past its bound, more than one
fill under way, a tug's job and the road tanker's fill both holding the dock,
a poll of the fuel supplier more than one interval overdue, an ended poll
whose connection the relay's journal lacks, a connection of a later poll
while the simulator's log holds no end of the poll before it, an order with
no scheduled arrival of its road tanker, or a road tanker overdue with no poll
under way that holds it. Only an overdue poll of the supplier, and a road
tanker's arrival that a poll under way holds, leave the start state
uncertain: an overdue poll rules out the chains that need a poll, and an
overdue road tanker, whose poll is overdue too, also the chains that need a
refuel (predicted_events.admit). The episode record states why.

The events that follow a poll are not exact in a fork: a fork can close a
dispense one poll cycle later than the living port does, and the delay passes
along the dock operator's queue. The predicted episode events after the fork
therefore keep their ranges: a refuel's start after its tug's arrival, the
handover between jobs, a refuel's tail, and each poll's earliest and latest
time. Only what has already happened at the fork instant is exact: a refuel
that is pumping started before the fork instant, and in a fork step 0 is known
once the settle conditions hold.
"""

from __future__ import annotations

import json
import logging
import math
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

import yaml

from competition.episode_runner import deploy, startup, predicted_events
from competition.episode_runner.incus import Result
from competition.episode_runner.record import utc_text
from competition.episode_runner.seeds import (
    SIM_TUG_ARRIVALS,
    derive_generator_seed,
    fork_seed,
)
from competition.episode_runner.settle import SIM_HOST, SIM_LOG

IDENTITY = Path("/var/lib/competition/living-port.json")
FMS = "fms-01"
RELAY = "atg-relay-01"
RECON = "recon-01"
# Beyond its interval and its slip, the time after a poll by which the next
# poll has come.
POLL_GAP_S = 10.0
# The litres that a job's pumping has moved once it has started.
STARTED_L = 1.0
# A tug's job is created after its arrival; the log's time and the FMS's may
# differ by this much.
ARRIVAL_SLACK_S = 1.0
# The dock operator's exchange limit: one request to the HMI with its retries
# after 1, 2 and 4 s (infra/sim, dock operator, RETRY_BACKOFFS_SECS),
# provisionally four attempts of 2 s each and the three backoffs, 15 s. The
# limit awaits specification: the dock operator's HTTP client sets no time
# limit of its own on an attempt.
EXCHANGE_S = 1.0 + 2.0 + 4.0 + 4 * 2.0
# After a tug leaves, the dock operator links its delivery note to the FMS's
# transaction with a PATCH, one exchange.
BDN_LINK_S = EXCHANGE_S
# A leaving tug departs within this long after the dock operator links its
# delivery note, or cancels its refused job. The tug engineer signals the
# departure as it hands back the co-signed note, before the link's PATCH, and
# as it learns of the refusal, just after the cancel; the tug arrival generator
# logs the tug's next scheduled arrival on the signal. The departure crosses two
# of the simulator's channels and no network exchange or timer, so it takes
# milliseconds; 5 s allows for the scheduling of the simulator's tasks on a
# loaded host.
LEAVING_S = 5.0
# The time to live of the HMI's login challenge in operation (hmi.md §4,
# authentication bridge). A compressed scenario's is shorter, so a bound that
# takes it holds at every compression factor.
CHALLENGE_TTL_S = 30.0
# The dock operator logs in again when its session has expired (hmi.md §4;
# §7.1, authentication), then retries the request that met the expired session
# (infra/sim, dock operator, Session::send_with): the login's POST, one
# exchange; the challenge's poll, which waits for the operator's card at most
# the challenge's time to live and completes as one exchange; and the retried
# request, one exchange. 15 + 30 + 15 + 15 = 75 s.
RELOGIN_S = EXCHANGE_S + CHALLENGE_TTL_S + EXCHANGE_S + EXCHANGE_S
# A dispense that the FMS refuses at its start is cancelled within one exchange,
# the DELETE with its retries, and RELOGIN_S more when the dock operator logs
# in again meanwhile (relogins).
CANCEL_S = EXCHANGE_S
# A tug or the road tanker waiting at an idle dock has its job opened within
# this long after the later of its arrival and the end of the dock operator's
# previous job: a refuel's first connection comes at most REFUEL_START_S[1]
# after its tug arrives, and an expired session adds a login. The bound keeps
# the login's allowance whether or not the simulator's log holds a login,
# unlike the bounds that add it only then (relogins): in 35 of the 39 refuels
# measured on three platforms the dock operator logged in again as it created
# the job, its session having expired since the previous refuel, and a fault
# discards the episode.
WAITING_S = predicted_events.REFUEL_START_S[1] + RELOGIN_S


# After a tug's dispense closes, the dock operator sees the close at its next
# poll of the totaliser, at most one poll interval later, the poll itself one
# exchange; it reads the dispense's start and the tank's level, an exchange
# each, and writes and signs the delivery note, which the tug engineer
# co-signs at once, and the tug departs on the co-signature (infra/sim, dock
# operator, run_dispense; tug engineer).
def note_bound_s(t: predicted_events.Timings) -> float:
    """How long after its dispense closes a tug departs, its delivery note
    signed: one poll interval, three exchanges and LEAVING_S."""
    return t.dock_operator_poll_s + 3 * EXCHANGE_S + LEAVING_S


# A refuel's dispense closes within its first connection after the job opens,
# REFUEL_START_S[1], the preset's pumping at the dispenser's rate, and one
# exchange for the FMS's close; the road tanker's fill is transcribed within
# the same start, the delivery's pumping at the tanker's rate, and one exchange
# for the transcription. The rates are world.yaml's at the render's
# compression factor (Timings). Beyond either bound the pump is stuck.
def refuel_bound_s(t: predicted_events.Timings, volume_l: float) -> float:
    """How long after its job opens a refuel's dispense closes."""
    return (
        predicted_events.REFUEL_START_S[1]
        + predicted_events.register_preset_l(volume_l) / t.outflow_l_per_s
        + EXCHANGE_S
    )


def fill_bound_s(t: predicted_events.Timings) -> float:
    """How long after its job opens the road tanker's fill is transcribed."""
    return (
        predicted_events.REFUEL_START_S[1]
        + t.delivery_volume_l / t.inflow_l_per_s
        + EXCHANGE_S
    )


# The register's resolution, against which a dispense's preset matches the
# volume that the tug requested.
PRESET_TOLERANCE_L = 0.06

log = logging.getLogger(__name__)

_FRACTION = re.compile(r"(\.\d{6})\d+")
_JOB_ID = re.compile(r'job_id="?([0-9a-fA-F-]{36})"?')
_CONNECT = re.compile(r"Connect from (\S+):\d+ to \S+ \(atg_in/TCP\)")
# The tug arrival generator's event of each draw (sim-impl.md, "Tug arrival
# generator"; infra/sim/tests/tug_arrival_log.rs).
_SCHEDULED = re.compile(
    r"^(\S+)\s+INFO tug_arrivals: scheduled arrival vessel_id=(\d+) "
    r"scheduled_arrival=(\S+) volume_l=(\S+)\s*$"
)
# The fuel supplier's events: each poll, each tanker's arrival and the end of
# each delivery.
_SUPPLIER = re.compile(
    r"^(\S+)\s+INFO fuel_supplier: "
    r"(poll|order placed|tanker arrival|delivery ended)\b(.*)$"
)
_TANKER_ARRIVAL = re.compile(r"\btanker_arrival=(\S+)")


class ForkError(ValueError):
    """An identity file or a start-up input that the episode runner cannot
    use."""


class StartStateFault(Exception):
    """A start state at the fork that no inference explains, or an inference
    past its bound: a fault of the testbed, which discards the episode
    (episodes.md §6.4, faults; §12, discarded). The record holds what the
    evidence gave."""

    def __init__(self, faults: Sequence[str], record: dict[str, Any]) -> None:
        super().__init__("; ".join(faults))
        self.faults = tuple(faults)
        self.record = record


class Incus(Protocol):
    def exec(self, node: str, *argv: str, timeout_s: float = 60.0) -> Result: ...

    def exec_as(
        self, node: str, uid: int, gid: int, *argv: str, timeout_s: float = 60.0
    ) -> Result: ...


def parse_instant(text: str) -> datetime:
    """An instant in the form of RFC 3339 with its offset, such as
    2026-11-11T20:15:00.000Z, as an aware time in UTC. Digits of a second
    beyond the microsecond are dropped."""
    try:
        at = datetime.fromisoformat(_FRACTION.sub(r"\1", text.strip()))
    except ValueError as error:
        raise ForkError(f"{text!r} is not an instant: {error}") from error
    if at.tzinfo is None:
        raise ForkError(f"the instant {text!r} has no offset from UTC")
    return at.astimezone(timezone.utc)


def epoch_text(t: float) -> str:
    return utc_text(datetime.fromtimestamp(t, timezone.utc))


def instant_text(at: datetime) -> str:
    """An instant in ISO 8601 with milliseconds and a Z, as the contract
    writes the fork instant, such as 2026-11-11T20:15:00.000Z."""
    at = at.astimezone(timezone.utc)
    return at.strftime("%Y-%m-%dT%H:%M:%S.") + f"{at.microsecond // 1000:03d}Z"


@dataclass(frozen=True)
class LivingPort:
    """A living port's identity file, and its start-up input."""

    seed: str
    tug_seed: int
    started_at: datetime
    startup_path: Path
    startup: dict[str, Any]

    def as_json(self) -> dict[str, Any]:
        return {
            "living_port_seed": self.seed,
            "tug_seed": self.tug_seed,
            "started_at": instant_text(self.started_at),
            "startup": str(self.startup_path),
        }


def read_identity(path: Path = IDENTITY) -> LivingPort:
    """The living port's identity file at path, with the start-up input that
    it names. The tug seed must derive from the living port seed, and the
    start-up input must hold the same tug seed."""
    try:
        data = json.loads(Path(path).read_text())
        seed = str(data["living_port_seed"]).lower()
        tug_seed = data["tug_seed"]
        started_at = parse_instant(str(data["started_at"]))
        startup_path = Path(data["startup"])
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise ForkError(f"cannot read the identity file {path}: {error}") from error
    if not re.fullmatch(r"[0-9a-f]{64}", seed):
        raise ForkError("the living port seed is not 64 hexadecimal digits")
    if type(tug_seed) is not int or tug_seed != derive_generator_seed(
        seed, SIM_TUG_ARRIVALS
    ):
        raise ForkError(
            f"the tug seed {tug_seed!r} is not the living port seed's seed of "
            f"{SIM_TUG_ARRIVALS}"
        )
    try:
        given = json.loads(startup_path.read_text())
    except (OSError, ValueError) as error:
        raise ForkError(
            f"cannot read the start-up input {startup_path}: {error}"
        ) from error
    if not isinstance(given, dict) or set(given) != set(startup.FIELDS):
        raise ForkError(f"{startup_path} does not hold the fields of a start-up input")
    if given[SIM_TUG_ARRIVALS] != tug_seed:
        raise ForkError(f"{startup_path} holds another tug seed than the identity file")
    return LivingPort(seed, tug_seed, started_at, startup_path, given)


# --- The evidence ------------------------------------------------------------


@dataclass(frozen=True)
class Job:
    """One row of the FMS's table of jobs. The counterparty of a dispense is
    the vessel's IMO number."""

    id: str
    kind: str
    created: float
    open_l: float | None = None
    counterparty: str | None = None


@dataclass(frozen=True)
class Transaction:
    """One row of the FMS's table of transactions: a dispense, a fill, or the
    link of a bunker delivery note (BDN) to its dispense."""

    job: str
    kind: str
    created: float
    opened: float | None = None
    closed: float | None = None
    preset_l: float | None = None
    volume_l: float | None = None


@dataclass(frozen=True)
class Refusal:
    """A line of the simulator's log about a dispense that the FMS refused at
    its start, or the cancel of its job."""

    stage: str
    at: float
    job: str | None = None


@dataclass(frozen=True)
class Scheduled:
    """A tug's scheduled arrival and requested volume, as the simulator logged
    them when it drew them."""

    vessel: int
    logged: float
    arrives: float
    volume_l: float


@dataclass(frozen=True)
class SupplierEvent:
    """A poll of the fuel supplier, an order with its tanker's scheduled
    arrival, a tanker's arrival or the end of a delivery, from the
    simulator's log."""

    kind: str
    at: float
    order_placed: bool = False
    tanker_arrival: float | None = None


@dataclass(frozen=True)
class Evidence:
    """What the episode runner reads from the fork at its start. Each time is
    in seconds since the epoch."""

    readings: tuple[tuple[float, float], ...] = ()
    jobs: tuple[Job, ...] = ()
    transactions: tuple[Transaction, ...] = ()
    refusals: tuple[Refusal, ...] = ()
    polls: tuple[float, ...] = ()
    audits: tuple[tuple[str, str, str], ...] = ()
    history_written: float | None = None
    failures: tuple[str, ...] = field(default=())
    schedule: tuple[Scheduled, ...] = ()
    supplier: tuple[SupplierEvent, ...] = ()
    # The times at which the dock operator logged in again, its session
    # having expired.
    relogins: tuple[float, ...] = ()


# One query, so the FMS's database records one session for the read.
FMS_QUERY = """SELECT json_build_object(
 'readings', (SELECT coalesce(json_agg(json_build_object(
   't', extract(epoch FROM recorded_at), 'v', volume_l) ORDER BY recorded_at), '[]')
   FROM atg_readings WHERE recorded_at >= to_timestamp({since:.3f})),
 'jobs', (SELECT coalesce(json_agg(json_build_object(
   'id', job_id, 'type', type, 't', extract(epoch FROM created_at),
   'open_l', atg_volume_at_open_l, 'counterparty', counterparty)
   ORDER BY created_at), '[]') FROM jobs),
 'transactions', (SELECT coalesce(json_agg(json_build_object(
   'job', job_id, 'ref', references_transaction_id, 'preset', preset_volume_l,
   'dispensed', volume_dispensed_l, 'delivered', delivered_volume_l,
   'opened', extract(epoch FROM opened_at), 'closed', extract(epoch FROM closed_at),
   't', extract(epoch FROM created_at)) ORDER BY created_at), '[]')
   FROM transactions))"""


def _float(value: Any) -> float | None:
    return None if value is None else float(value)


def parse_fms(text: str) -> tuple[tuple, tuple, tuple]:
    """The readings, jobs and transactions of the FMS's answer to FMS_QUERY."""
    data = json.loads(text)
    readings = tuple((float(r["t"]), float(r["v"])) for r in data["readings"])
    jobs = tuple(
        Job(
            str(j["id"]),
            str(j["type"]),
            float(j["t"]),
            _float(j["open_l"]),
            None if j.get("counterparty") is None else str(j["counterparty"]),
        )
        for j in data["jobs"]
    )
    transactions = []
    for x in data["transactions"]:
        if x["ref"] is not None:
            kind, volume = "bdn", None
        elif x["delivered"] is not None:
            kind, volume = "fill", _float(x["delivered"])
        else:
            kind, volume = "dispense", _float(x["dispensed"])
        transactions.append(
            Transaction(
                str(x["job"]),
                kind,
                float(x["t"]),
                _float(x["opened"]),
                _float(x["closed"]),
                _float(x["preset"]),
                volume,
            )
        )
    return readings, jobs, tuple(transactions)


def read_fms(incus: Incus, since: float) -> Result:
    """The FMS's answer to FMS_QUERY, from its database as the database's own
    superuser through the local socket. `incus exec --user` runs psql as
    that user with no login session (the living-port contract), so the read
    leaves only the database's own record of the session."""
    ids = incus.exec(FMS, "id", "-u", "postgres").stdout.split()
    groups = incus.exec(FMS, "id", "-g", "postgres").stdout.split()
    if not (ids and groups and ids[0].isdigit() and groups[0].isdigit()):
        return Result(1, "", "no user postgres on fms-01")
    return incus.exec_as(
        FMS,
        int(ids[0]),
        int(groups[0]),
        "psql",
        "-X",
        "-q",
        "-A",
        "-t",
        "-d",
        "fms",
        "-c",
        FMS_QUERY.format(since=since),
    )


def parse_polls(text: str, supplier: str) -> tuple[float, ...]:
    """The time of each connection from the supplier's address to the relay,
    from the relay's journal as `journalctl -o json` prints it."""
    found = []
    for line in text.splitlines():
        try:
            entry = json.loads(line)
            match = _CONNECT.search(str(entry["MESSAGE"]))
            at = int(entry["__REALTIME_TIMESTAMP"]) / 1e6
        except (ValueError, KeyError, TypeError):
            continue
        if match and match.group(1) == supplier:
            found.append(at)
    return tuple(sorted(found))


def read_polls(incus: Incus, since: float) -> Result:
    """The relay's journal of HAProxy since `since`."""
    return incus.exec(
        RELAY,
        "journalctl",
        "-o",
        "json",
        "--no-pager",
        "--since",
        f"@{since:.0f}",
        "SYSLOG_IDENTIFIER=haproxy",
    )


def _log_time(line: str) -> float | None:
    try:
        return parse_instant(line.split()[0]).timestamp()
    except (ForkError, IndexError):
        return None


def parse_refusals(sim_log: str) -> tuple[Refusal, ...]:
    """The refused dispenses in the simulator's log that a fork's start state
    reads: the dock operator's warnings of a refusal at a dispense's start, and
    of the cancel that follows it. A tug refused at its job's creation leaves
    at once, and its next scheduled arrival accounts for it."""
    found = []
    for line in sim_log.splitlines():
        if " WARN " not in line or "dock_operator" not in line:
            continue
        at = _log_time(line)
        if at is None:
            continue
        job = _JOB_ID.search(line)
        if "refused the dispense start" in line and job:
            found.append(Refusal("start", at, job.group(1)))
        elif "the refused job" in line and job:
            found.append(Refusal("cancel", at, job.group(1)))
    return tuple(found)


# The dock operator's warning as it logs in again, its HMI session having
# expired (infra/sim, dock operator, Session::reauthenticate).
_RELOGIN = "HMI session expired; re-authenticating"


def parse_relogins(sim_log: str) -> tuple[float, ...]:
    """The times at which the dock operator logged in again, from its
    warnings in the simulator's log."""
    found = []
    for line in sim_log.splitlines():
        if " WARN " not in line or "dock_operator" not in line or _RELOGIN not in line:
            continue
        at = _log_time(line)
        if at is not None:
            found.append(at)
    return tuple(found)


def parse_schedule(sim_log: str) -> tuple[Scheduled, ...]:
    """Each tug's scheduled arrivals and requested volumes in the simulator's
    log, in the order logged."""
    found = []
    for line in sim_log.splitlines():
        match = _SCHEDULED.match(line)
        if match is None:
            continue
        try:
            logged = parse_instant(match.group(1)).timestamp()
            arrives = parse_instant(match.group(3)).timestamp()
            found.append(
                Scheduled(int(match.group(2)), logged, arrives, float(match.group(4)))
            )
        except (ForkError, ValueError):
            continue
    return tuple(found)


def parse_supplier(sim_log: str) -> tuple[SupplierEvent, ...]:
    """The fuel supplier's polls, tanker arrivals and ends of delivery in the
    simulator's log, in the order logged."""
    found = []
    for line in sim_log.splitlines():
        match = _SUPPLIER.match(line)
        if match is None:
            continue
        at = _log_time(line)
        if at is None:
            continue
        kind = match.group(2)
        due = _TANKER_ARRIVAL.search(match.group(3))
        try:
            tanker = None if due is None else parse_instant(due.group(1)).timestamp()
        except ForkError:
            continue
        found.append(
            SupplierEvent(
                kind, at, kind == "poll" and "order_placed=true" in line, tanker
            )
        )
    return tuple(found)


AUDIT_FILES = """cd "$1" 2>/dev/null || exit 0
for f in .bash_history reconciliation-audits.txt; do
    if [ -f "$f" ]; then echo "$(stat -c %Y "$f") $f"; fi
done
echo ---
if [ -f reconciliation-audits.txt ]; then cat reconciliation-audits.txt; fi
"""


def read_audits(incus: Incus, home: str) -> Result:
    """The times at which the reconciliation user's shell history and summary
    file were last written, and the summary file's entries."""
    return incus.exec(RECON, "sh", "-c", AUDIT_FILES, "sh", home)


def parse_audits(text: str) -> tuple[tuple[tuple[str, str, str], ...], float | None]:
    """Each summary entry as its window's start, its end and its result, and
    the time at which the shell history was last written."""
    head, _, entries = text.partition("---\n")
    written = None
    for line in head.splitlines():
        stamp, _, name = line.partition(" ")
        if name == ".bash_history" and stamp.isdigit():
            written = float(stamp)
    windows = []
    for line in entries.splitlines():
        parts = line.split()
        if len(parts) >= 3:
            windows.append((parts[0], parts[1], " ".join(parts[2:])))
    return tuple(windows), written


@dataclass(frozen=True)
class Sources:
    """Where the fork keeps what the start state needs: the supplier's
    address and the reconciliation user's home directory, from topology.yaml
    and world.yaml."""

    supplier: str
    recon_home: str

    @classmethod
    def from_files(cls, deploy_dir: Path = deploy.DEPLOY) -> Sources:
        topology = yaml.safe_load((deploy_dir / "topology.yaml").read_text())
        supplier = next(
            nic["ipv4"]
            for nic in topology["nodes"][SIM_HOST]["nics"]
            if nic.get("name") == "ext-supplier"
        )
        world = yaml.safe_load((deploy_dir / "world.yaml").read_text())
        user = world["identities"]["reconciliation_user"]["username"]
        return cls(str(supplier), f"/home/{user}")


def look_back_s(t: predicted_events.Timings) -> float:
    """How far before the fork the readings and the polls are read: a lead
    time, a fill and two poll intervals, with room."""
    return t.lead_time_s + 18_000.0 / t.inflow_l_per_s + 2 * t.poll_s + 600.0


def _retried(read: Callable[[], Result]) -> Result:
    """A read of the fork, tried once more if it fails (episodes.md §6.4,
    faults)."""
    result = read()
    return result if result.ok else read()


def gather(
    incus: Incus,
    living: LivingPort,
    fork_at: datetime,
    t: predicted_events.Timings,
    sources: Sources,
) -> Evidence:
    """Read the evidence from the fork. A read that fails is tried once more,
    and a read that fails again is noted, a fault of the testbed
    (start_state)."""
    tf = fork_at.timestamp()
    since = max(living.started_at.timestamp(), tf - look_back_s(t)) - 60.0
    failures: list[str] = []
    readings: tuple = ()
    jobs: tuple = ()
    transactions: tuple = ()
    for last in (False, True):
        result = read_fms(incus, since)
        try:
            if not result.ok:
                raise ValueError(
                    result.stderr.strip() or f"exit status {result.returncode}"
                )
            readings, jobs, transactions = parse_fms(result.stdout)
            break
        except (ValueError, KeyError, TypeError) as error:
            if last:
                failures.append(
                    f"the FMS's database could not be read in two attempts: {error}"
                )
    result = _retried(lambda: read_polls(incus, since))
    polls: tuple[float, ...] = ()
    if result.ok:
        polls = parse_polls(result.stdout, sources.supplier)
    else:
        failures.append(
            "the relay's journal could not be read in two attempts: "
            f"{result.stderr.strip()}"
        )
    result = _retried(lambda: incus.exec(SIM_HOST, "cat", SIM_LOG))
    refusals: tuple[Refusal, ...] = ()
    schedule: tuple[Scheduled, ...] = ()
    supplier: tuple[SupplierEvent, ...] = ()
    relogins: tuple[float, ...] = ()
    if result.ok:
        refusals = parse_refusals(result.stdout)
        schedule = parse_schedule(result.stdout)
        supplier = parse_supplier(result.stdout)
        relogins = parse_relogins(result.stdout)
    else:
        failures.append(
            "the simulator's log could not be read in two attempts: "
            f"{result.stderr.strip()}"
        )
    result = _retried(lambda: read_audits(incus, sources.recon_home))
    audits: tuple = ()
    written = None
    if result.ok:
        audits, written = parse_audits(result.stdout)
    else:
        failures.append(
            "the reconciliation user's files could not be read in two attempts: "
            f"{result.stderr.strip()}"
        )
    return Evidence(
        readings,
        jobs,
        transactions,
        refusals,
        polls,
        audits,
        written,
        tuple(failures),
        schedule,
        supplier,
        relogins,
    )


# --- The start state at the fork ---------------------------------------------


def level_at(readings: list[tuple[float, float]], at: float) -> float | None:
    """The tank's volume at `at`, between the readings either side of it, or
    from the last two readings before it for at most one reading's interval
    beyond the last."""
    before = [r for r in readings if r[0] <= at]
    after = [r for r in readings if r[0] > at]
    if not before:
        return None
    t1, v1 = before[-1]
    if after:
        t2, v2 = after[0]
        return v1 + (v2 - v1) * (at - t1) / (t2 - t1)
    if len(before) < 2:
        return v1
    t0, v0 = before[-2]
    if t1 <= t0:
        return v1
    return v1 + (v1 - v0) / (t1 - t0) * min(at - t1, t1 - t0)


@dataclass(frozen=True)
class ForkStart:
    """The start state at a fork: the draws for the predicted episode events,
    and the record."""

    draws: predicted_events.Draws
    record: dict[str, Any]


def _matches(preset: float, volume: float) -> bool:
    return (
        min(
            abs(preset - volume),
            abs(preset - predicted_events.register_preset_l(volume)),
        )
        <= PRESET_TOLERANCE_L
    )


def start_state(
    living: LivingPort,
    fork_at: datetime,
    evidence: Evidence,
    t: predicted_events.Timings,
) -> ForkStart:
    """The start state at the fork from the evidence: each tug's scheduled
    arrival logged last before the fork, the dock operator's jobs, the
    supplier's polls and orders, and the audits. A start state that no
    inference explains, or an inference past its bound, raises
    StartStateFault, with the faults and the record of what the evidence
    gave."""
    if evidence.failures:
        raise StartStateFault(evidence.failures, {"faults": list(evidence.failures)})
    t0 = living.started_at.timestamp()
    tf = fork_at.timestamp()
    # A disagreement that no inference explains is a fault. An overdue poll,
    # and a road tanker's arrival that a poll under way holds, leave the start
    # state uncertain.
    faults: list[str] = []
    poll_overdue: str | None = None
    tanker_overdue: str | None = None
    delivery = t.delivery_volume_l

    def rel(at: float) -> float:
        return round(at - tf, 3)

    # The tank's volume at the fork.
    readings = sorted(r for r in evidence.readings)
    level = level_at([r for r in readings if r[0] <= tf], tf)
    if level is None:
        faults.append("the FMS holds no reading of the tank gauge before the fork")
        level = float(living.startup["initial_volume_l"])

    jobs = [j for j in evidence.jobs if j.created <= tf]
    rows = [x for x in evidence.transactions if x.created <= tf]
    refusals = [r for r in evidence.refusals if r.at <= tf]
    dispensed_rows = {x.job: x for x in rows if x.kind == "dispense"}
    filled_rows = {x.job: x for x in rows if x.kind == "fill"}
    bdn_rows = {x.job: x for x in rows if x.kind == "bdn"}
    start_refused = {r.job: r for r in refusals if r.stage == "start"}
    cancelled = {r.job: r for r in refusals if r.stage == "cancel"}
    relogins = [at for at in evidence.relogins if at <= tf]

    def relogin_since(at: float) -> float:
        """RELOGIN_S when the dock operator has logged in again since `at`,
        which delays the exchange that met the expired session, and nothing
        otherwise."""
        return RELOGIN_S if any(r >= at for r in relogins) else 0.0

    # Each tug's scheduled arrival logged last before the fork: still to come,
    # or passed, with the tug at the dock.
    latest: dict[int, Scheduled] = {}
    for s in evidence.schedule:
        if s.logged <= tf:
            latest[s.vessel] = s
    to_come: list[tuple[float, float]] = []
    at_dock: list[Scheduled] = []
    tugs: list[dict[str, Any]] = []
    for imo, _ in t.roster:
        s = latest.get(imo)
        if s is None:
            faults.append(
                f"the simulator's log holds no scheduled arrival of vessel {imo} "
                "before the fork"
            )
            continue
        tugs.append(
            {
                "vessel": imo,
                "scheduled_arrival": epoch_text(s.arrives),
                "volume_l": round(s.volume_l, 3),
                "state": "away" if s.arrives > tf else "at the dock",
            }
        )
        if s.arrives > tf:
            to_come.append((rel(s.arrives), s.volume_l))
        else:
            at_dock.append(s)

    # The tugs at the dock, in the order they arrived: the one the dock
    # operator serves, those waiting, and those leaving. A served tug, or a
    # refused and cancelled one, holds no job at the dock: the dock operator
    # may already have opened the next job before the simulator logs the
    # tug's departure, which falls inside the episode.
    under: predicted_events.UnderWay | None = None
    waiting: list[tuple[float, float]] = []
    # Each waiting tug's arrival, and its vessel.
    waiting_since: list[tuple[float, int]] = []
    leaving: list[float] = []
    accounted: set[str] = set()

    def holds(job: predicted_events.UnderWay) -> None:
        nonlocal under
        if under is not None:
            faults.append("more than one job holds the dock at the fork")
        under = job

    for s in sorted(at_dock, key=lambda s: s.arrives):
        record = next(r for r in tugs if r["vessel"] == s.vessel)
        mine = [
            j
            for j in jobs
            if j.kind == "dispense"
            and j.counterparty == str(s.vessel)
            and j.created >= s.arrives - ARRIVAL_SLACK_S
        ]
        if not mine:
            record["state"] = "waiting"
            waiting.append((rel(s.arrives), s.volume_l))
            waiting_since.append((s.arrives, s.vessel))
            continue
        job = mine[-1]
        accounted.add(job.id)
        record["job"] = job.id
        if job.id in start_refused:
            refused = start_refused[job.id]
            if job.id in cancelled:
                cancel = cancelled[job.id].at
                if tf - cancel > LEAVING_S:
                    faults.append(
                        f"vessel {s.vessel}'s refused job {job.id} was cancelled at "
                        f"{epoch_text(cancel)}, and the simulator's log holds no "
                        "departure of the tug since"
                    )
                record["state"] = "refused and cancelled, leaving"
                leaving.append(0.0)
                continue
            # The dock operator cancels the refused job within CANCEL_S,
            # and the tug leaves on the cancel.
            cancel_by = refused.at + CANCEL_S + relogin_since(refused.at)
            if tf > cancel_by:
                faults.append(
                    f"vessel {s.vessel}'s job {job.id} was refused at its start at "
                    f"{epoch_text(refused.at)}, and the simulator's log holds no "
                    f"cancel of it within {cancel_by - refused.at:g} s"
                )
            record["state"] = "refused at its start, leaving once cancelled"
            holds(
                predicted_events.UnderWay(
                    False,
                    (rel(refused.at), rel(cancel_by)),
                    0.0,
                    True,
                    rel(s.arrives),
                    s.volume_l,
                    refused=True,
                )
            )
            continue
        row = dispensed_rows.get(job.id)
        if row is not None:
            if row.preset_l is not None and not _matches(row.preset_l, s.volume_l):
                faults.append(
                    f"the dispense of job {job.id} has a preset of {row.preset_l} L, "
                    f"and vessel {s.vessel}'s logged request is {s.volume_l:.1f} L"
                )
            if job.id in bdn_rows:
                linked = bdn_rows[job.id].created
                if tf - linked > LEAVING_S:
                    faults.append(
                        f"vessel {s.vessel}'s delivery note was linked to job "
                        f"{job.id} at {epoch_text(linked)}, and the simulator's log "
                        "holds no departure of the tug since"
                    )
                record["state"] = "served, leaving"
                leaving.append(0.0)
                continue
            closed = row.closed if row.closed is not None else row.created
            # The close happens on the FMS's side: the totaliser poll in
            # flight at the close may have met an expired session just before
            # it, its login spanning the close.
            depart_by = closed + note_bound_s(t) + relogin_since(closed - RELOGIN_S)
            if tf > depart_by:
                faults.append(
                    f"vessel {s.vessel}'s dispense of job {job.id} closed at "
                    f"{epoch_text(closed)}, and the simulator's log holds no "
                    f"departure of the tug within {depart_by - closed:g} s"
                )
            record["state"] = "dispensed, with its delivery note under way"
            holds(
                predicted_events.UnderWay(
                    False,
                    (rel(closed), rel(closed)),
                    0.0,
                    True,
                    rel(s.arrives),
                    s.volume_l,
                )
            )
            continue
        record["state"] = "under way"
        close_by = (
            job.created + refuel_bound_s(t, s.volume_l) + relogin_since(job.created)
        )
        if tf > close_by:
            faults.append(
                f"vessel {s.vessel}'s refuel, job {job.id}, opened at "
                f"{epoch_text(job.created)}, has no dispense closed within "
                f"{close_by - job.created:g} s: the pump is stuck"
            )
        moved = 0.0 if job.open_l is None else max(0.0, job.open_l - level)
        started = moved >= STARTED_L
        if started:
            start: predicted_events.Band = (0.0, 0.0)
        else:
            start = (
                0.0,
                max(0.0, rel(job.created + predicted_events.REFUEL_START_S[1])),
            )
        holds(
            predicted_events.UnderWay(
                False,
                start,
                max(0.0, s.volume_l - moved),
                started,
                rel(s.arrives),
                s.volume_l,
            )
        )
    # A dispense that no tug at the dock accounts for, unfinished at the fork.
    # Its tug may have left with its delivery note co-signed, the simulator
    # drawing its next arrival then, while the dock operator links the note:
    # the dock is busy for the PATCH and its retries. Otherwise, or once the
    # retries are spent, the dispense disagrees with the logged arrivals.
    linking: list[str] = []
    for job in jobs:
        unfinished = (
            job.kind == "dispense"
            and job.id not in bdn_rows
            and job.id not in cancelled
            and job.id not in start_refused
        )
        if not unfinished or job.id in accounted:
            continue
        row = dispensed_rows.get(job.id)
        tug = latest.get(int(job.counterparty)) if job.counterparty else None
        dispensed_at = None if row is None else (row.closed or row.created)
        # The link is one exchange, and RELOGIN_S more when the dock
        # operator logs in again meanwhile.
        if (
            row is not None
            and tug is not None
            and dispensed_at is not None
            and tug.logged >= dispensed_at
        ):
            link_by = tug.logged + BDN_LINK_S + relogin_since(tug.logged)
            if tf > link_by:
                faults.append(
                    f"vessel {tug.vessel}'s delivery note for job {job.id} is not "
                    f"linked within {link_by - tug.logged:g} s of the tug's "
                    f"departure at {epoch_text(tug.logged)}"
                )
                continue
            linking.append(job.id)
            holds(
                predicted_events.UnderWay(
                    False,
                    (0.0, max(0.0, rel(link_by))),
                    0.0,
                    True,
                    departed=True,
                )
            )
            continue
        faults.append(
            f"the dispense of job {job.id} is open at the fork, and no tug at "
            "the dock accounts for it"
        )

    # The road tanker's fill under way.
    fills = [j for j in jobs if j.kind == "fill"]
    filling = [j for j in fills if j.id not in filled_rows]
    if len(filling) > 1:
        faults.append("more than one fill is under way at the fork")
    if filling:
        if under is not None:
            faults.append("a tug's job and the road tanker's both hold the dock")
        j = filling[-1]
        fill_by = j.created + fill_bound_s(t) + relogin_since(j.created)
        if tf > fill_by:
            faults.append(
                f"the road tanker's fill, job {j.id}, opened at "
                f"{epoch_text(j.created)}, is not transcribed within "
                f"{fill_by - j.created:g} s: the fill is stuck"
            )
        moved = 0.0 if j.open_l is None else max(0.0, level - j.open_l)
        started = moved >= STARTED_L
        start = (
            (0.0, 0.0)
            if started
            else (0.0, max(0.0, rel(j.created + predicted_events.REFUEL_START_S[1])))
        )
        under = predicted_events.UnderWay(
            True, start, max(0.0, delivery - moved), started
        )

    # The fuel supplier makes one poll at a time on its fixed schedule, the
    # first poll and then one each interval, which an order does not move
    # (sim.md §5.3), and logs each poll's event as the poll ends. The relay's
    # HAProxy writes each connection to the journal as it accepts it, and
    # each retry of a poll opens a connection of its own. The simulator's log
    # also holds an order pending at the start, logged as the supplier
    # starts, each order, and each road tanker's arrival and the end of its
    # delivery.
    polls = sorted(p for p in evidence.polls if p <= tf)
    events = [e for e in evidence.supplier if e.at <= tf]
    ended = [e.at for e in events if e.kind == "poll"]
    slip = predicted_events.POLL_SLIP_S
    # Beyond this lateness past its due time, a poll or a road tanker's
    # arrival is overdue.
    late = slip + POLL_GAP_S
    # With an order pending at the start, the start-up input gives no first
    # poll, and the supplier polls first one interval after the start.
    given = living.startup.get("first_poll_after_s")
    first_after = t.poll_s if given is None else float(given)

    def due(k: int) -> float:
        """The due time of the supplier's poll k, counted from the first."""
        return t0 + first_after + k * t.poll_s

    def poll_of(at: float) -> int:
        """The poll that a connection at `at` belongs to: poll k takes the
        connections from the start of its range, POLL_EARLY_S before its due
        time (predicted_events.polls), until the start of poll k + 1's."""
        return math.floor((at - due(0) + predicted_events.POLL_EARLY_S) / t.poll_s)

    # Polls 0 to len(ended) - 1 have ended. The next is under way once it has
    # fallen due: normal up to `late` past its due time, and overdue beyond,
    # as during a burst of the supplier's retries. A poll still under way
    # more than an interval past its due time shows the relay or the
    # supplier stuck, a fault; a retry that crosses into the next due time's
    # connections is the same fault.
    under_way: float | None = due(len(ended))
    if under_way > tf:
        under_way = None
    elif under_way + t.poll_s < tf:
        faults.append(
            f"the supplier's poll due at {epoch_text(under_way)} had not ended "
            "by the fork, more than one poll interval after it fell due"
        )
    elif under_way + late < tf:
        poll_overdue = (
            f"the supplier's poll due at {epoch_text(under_way)} had not ended "
            "by the fork"
        )
    # An ended poll in the look-back whose connection the journal lacks.
    connected = {poll_of(p) for p in polls}
    checked_from = max(t0, tf - look_back_s(t))
    lacking = [
        k
        for k, at in enumerate(ended)
        if due(k) >= checked_from and at <= tf - POLL_GAP_S and k not in connected
    ]
    if lacking:
        faults.append(
            "the simulator's log holds the supplier's poll due at "
            f"{epoch_text(due(lacking[-1]))} as ended, and the relay's journal "
            "holds no connection for it"
        )
    # A connection of a poll later than the one under way: the supplier
    # polls one at a time, so that poll has ended, and the simulator's log
    # lacks its end. With no event of the supplier at all, this is the only
    # fault: until connections of two due times appear, the first poll is
    # not yet due, under way or overdue.
    later = [p for p in polls if poll_of(p) > len(ended)]
    if later:
        faults.append(
            f"the relay's journal holds a connection at {epoch_text(later[0])} "
            "of a later poll, and the simulator's log no end of the supplier's "
            f"poll due at {epoch_text(due(len(ended)))}"
        )
    # The next poll among the predicted episode events, at its due time on the
    # schedule: the first that has neither ended nor connected. A retry's
    # connection lies seconds past its poll's due time, so the schedule, and
    # not the journal's last connection, gives the next due time.
    next_poll = due(max([len(ended)] + [poll_of(p) + 1 for p in polls]))

    # The supplier's order, from its events in the simulator's log: placed by
    # a poll, which logs the tanker's scheduled arrival, pending until the
    # tanker arrives, and ended with the delivery.
    order_at: float | None = None
    tanker_due: float | None = None
    tanker_at: float | None = None
    for event in events:
        if event.kind == "order placed":
            order_at, tanker_due, tanker_at = event.at, event.tanker_arrival, None
        elif event.kind == "tanker arrival":
            tanker_at = event.at
        elif event.kind == "delivery ended":
            order_at = tanker_due = tanker_at = None
    # The supplier sends the road tanker's arrival to its driver before it logs
    # the arrival, and the dock operator may open the tanker's fill in between:
    # a fill opened since the tanker fell due shows that it has arrived, and
    # the tanker is then the fill, not a later arrival as well.
    if (
        order_at is not None
        and tanker_at is None
        and tanker_due is not None
        and any(j.created >= tanker_due - ARRIVAL_SLACK_S for j in fills)
    ):
        tanker_at = tanker_due
    delivery_state = "no order placed"
    order_pending_s: float | None = None
    if order_at is not None and tanker_at is None:
        delivery_state = "order placed"
        if tanker_due is None:
            faults.append(
                f"the supplier's order placed at {epoch_text(order_at)} gives no "
                "scheduled arrival of its road tanker"
            )
        else:
            if tanker_due + late < tf:
                overdue = (
                    f"the road tanker of the order placed at "
                    f"{epoch_text(order_at)} was due at {epoch_text(tanker_due)}, "
                    "and the simulator's log holds no arrival of it"
                )
                if under_way is not None and under_way <= tanker_due:
                    # The supplier serves its polls and the road tanker's
                    # arrival in turn, the arrival before a poll that falls
                    # due after it, so only the poll under way since before
                    # the tanker fell due holds the arrival, as a slow or
                    # retried poll delays the next poll. The dock's order of
                    # service is uncertain, which rules out the chains that
                    # need a refuel. That poll is more than `late` past its
                    # due time too, an overdue poll, which rules out the
                    # chains that need a poll, or more than an interval, a
                    # fault.
                    tanker_overdue = overdue
                else:
                    faults.append(
                        f"{overdue}, and no poll of the supplier under way holds it"
                    )
            # Due after the fork, or arriving: the supplier logs the arrival
            # once a poll under way has ended.
            order_pending_s = rel(tanker_due)
    elif tanker_at is not None:
        tanker_due = None
        since = [j for j in fills if j.created >= tanker_at - ARRIVAL_SLACK_S]
        if not since:
            # The tanker has arrived and the dock operator has not yet opened
            # its fill, behind a tug's job or before its create: it waits at
            # the dock, and is served before any waiting tug (sim.md §6).
            delivery_state = "tanker waiting at the dock"
            order_pending_s = rel(tanker_at)
        elif any(j.id not in filled_rows for j in since):
            delivery_state = "fill under way"
        else:
            # The fill is transcribed, and the supplier has not yet logged the
            # delivery's end. The road tanker driver signals the end as it
            # hands over the ticket, before the dock operator transcribes it,
            # and the supplier, whose wait puts the end before a poll, logs it
            # at once (LEAVING_S), unless it is in the middle of a poll. A
            # poll that holds the end fell due before the end's signal, so no
            # later than the transcription; it holds the end while it lasts.
            delivery_state = "delivered"
            transcribed = max(filled_rows[j.id].created for j in since)
            held = under_way is not None and under_way <= transcribed
            if not held and tf - transcribed > LEAVING_S:
                faults.append(
                    f"the road tanker's fill, job {since[-1].id}, was transcribed "
                    f"at {epoch_text(transcribed)}, and the simulator's log holds "
                    "no end of its delivery"
                )

    # A job waiting at an idle dock: the dock operator opens the road
    # tanker's fill before any waiting tug's job, and the tugs' in the order
    # they arrived. It opens the next job within WAITING_S of the later of
    # that job's arrival and the end of its previous job: a delivery note's
    # link, a refused job's cancel, a fill's transcription, or a tug's
    # departure, which follows a refusal of its job's creation.
    if under is None:
        ends = [x.created for x in rows if x.kind in ("bdn", "fill")]
        ends += [r.at for r in refusals if r.stage == "cancel"]
        ends += [s.logged for s in evidence.schedule if s.logged <= tf]
        idle_since = max(ends, default=t0)
        next_job: tuple[str, float] | None = None
        if delivery_state == "tanker waiting at the dock" and tanker_at is not None:
            next_job = ("the road tanker", tanker_at)
        elif waiting_since:
            arrived, vessel = min(waiting_since)
            next_job = (f"vessel {vessel}", arrived)
        if next_job is not None:
            what, arrived = next_job
            since = max(arrived, idle_since)
            if tf - since > WAITING_S:
                faults.append(
                    f"{what} has waited at an idle dock since {epoch_text(since)}, "
                    f"more than the {WAITING_S:g} s within which the dock operator "
                    "opens its job"
                )

    # The audits.
    own = [w for w in evidence.audits if _ends_after(w[1], t0)]
    written = evidence.history_written
    completed = bool(own) and written is not None and t0 < written <= tf
    period_s = startup.audit_period_s(t.audit_period)
    next_audit = parse_instant(str(living.startup["next_audit_at"])).timestamp()
    while next_audit < tf:
        next_audit += period_s

    draws = predicted_events.Draws(
        initial_volume_l=level,
        order_pending_s=order_pending_s,
        first_poll_after_s=rel(next_poll),
        delivery_volume_l=delivery,
        arrivals=tuple(sorted(waiting + to_come)),
        under_way=under,
        leaving_s=tuple(leaving),
        audit_completed=completed,
        poll_overdue=poll_overdue,
        tanker_overdue=tanker_overdue,
    )
    record: dict[str, Any] = {
        "volume_l": round(level, 3),
        "delivery_state": delivery_state,
        "order_placed_at": None if order_at is None else epoch_text(order_at),
        "tanker_due": None if tanker_due is None else epoch_text(tanker_due),
        "tanker_arrived_at": None if tanker_at is None else epoch_text(tanker_at),
        "supplier": {
            "polls_read": len(polls),
            "last_poll": epoch_text(polls[-1]) if polls else None,
            "next_poll": epoch_text(next_poll),
            "poll_under_way_due": None if under_way is None else epoch_text(under_way),
            "next_poll_slip_s": slip,
        },
        "under_way": None
        if under is None
        else {
            "job": "fill" if under.fill else "refuel",
            "start_after_fork_s": list(under.start),
            "remaining_l": round(under.remaining_l, 3),
            "started": under.started,
            "refused": under.refused,
            "linking_delivery_note": under.departed,
        },
        "linking_delivery_note_of": linking,
        "tugs_leaving": len(leaving),
        "tugs": tugs,
        "tugs_waiting": [
            {"arrived": epoch_text(tf + at), "volume_l": round(v, 3)}
            for at, v in waiting
        ],
        "arrivals_to_come": [
            {"at": epoch_text(tf + at), "after_fork_s": at, "volume_l": round(v, 3)}
            for at, v in sorted(to_come)
        ],
        "audits": {
            "living_port_windows": [list(w) for w in own],
            "history_written": None if written is None else epoch_text(written),
            "completed_before_fork": completed,
            "next_audit_at": epoch_text(next_audit),
        },
        "routine_checks": {
            "first_at": epoch_text(t0 + float(living.startup["first_check_after_s"])),
            "interval_s": t.check_s,
            "note": "each check delays the later checks by its own duration",
        },
        "faults": faults,
        "uncertain": draws.uncertain,
    }
    if faults:
        raise StartStateFault(faults, record)
    return ForkStart(draws, record)


def _ends_after(text: str, t0: float) -> bool:
    try:
        return parse_instant(text).timestamp() > t0
    except ForkError:
        return False


def read_start_state(
    incus: Incus,
    living: LivingPort,
    fork_at: datetime,
    t: predicted_events.Timings,
    *,
    sources: Sources | None = None,
) -> ForkStart:
    """The start state at the fork instant, read from the fork. A fault in it
    raises StartStateFault."""
    sources = sources or Sources.from_files()
    evidence = gather(incus, living, fork_at, t, sources)
    return start_state(living, fork_at, evidence, t)


def episode_seed_of(living: LivingPort, fork: int) -> str:
    """The episode seed of fork `fork` of the living port."""
    return fork_seed(living.seed, fork)


def age_s(living: LivingPort, fork_at: datetime) -> float:
    """The age of the fork: the fork instant less the simulator's start."""
    return round((fork_at - living.started_at) / timedelta(seconds=1), 3)
