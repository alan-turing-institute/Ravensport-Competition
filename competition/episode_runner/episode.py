"""One episode, from plan to reset (episodes.md §3.1, phases; §12, terminated
and discarded episodes).

The step session runs the launcher, /usr/local/bin/defender on siem-01, which
runs the entry's defender from the entry's volume, or the idle defender when
the volume holds none. Before the step session opens at step 0, the episode
runner tests the entry's defender itself, and the episode record names the
program that the launcher runs. A defender in the volume that cannot run is a
fault of the entry, which discards the episode before the step session opens
(episodes.md §7, step protocol). The episode runner carries out each decision
in either mode, and in Track A the alarm ends the episode. A suppressed
episode, a replay for Competition Rule 5 that the command line marks, records
each decision and carries none out (episodes.md §9, suppressed episodes for
Competition Rule 5). An episode is benign or adversarial. An adversarial
episode runs one attack chain from the adversary host, started at the onset
step and paced by the episode runner (episodes.md §10, adversarial episodes).

At step 0, once the step session has opened and before the decision request
of step 0, the episode runner records where the query interface's reads
start, through `siem-read --start` on siem-01 (episode-runner-impl.md §12,
query interface).

An episode ends at the horizon, with the outcome `horizon`; at a Track A
terminal state, with the outcome `alarm` or `last_stage`; or terminates at a
simulator stop whose cause lies inside the episode, with the outcome
`terminated` (episodes.md §11, horizon and termination; §12, terminated and
discarded episodes). At the alarm, before the end notice, one read of the
simulator's log finds a stop that came before the alarm, which then ends the
episode as a simulator stop. In Track A the episode record holds K, k_n, R_n,
Track A's outcome and the end cause (episode-runner-impl.md §11, Track A's
fields in the record). A failed deployment, a failed export, an entry's
defender that cannot run or any other simulator stop discards the episode. A
discarded episode keeps its episode record, which states the cause, and is not
run again.

An episode in a fork of a living port (run_copy_episode) runs on the deployment
that the fork holds, with no deployment and no reset: the living port's manager
made the fork and deletes it. The fork runs one episode, whatever its end, and
the living port continues with its next fork. A fault in the start state at
the fork instant discards the episode (fork.StartStateFault). A fork's step 0
begins once the settle conditions hold, with no settle delay. A training
episode, which runs only in a fork, attaches the training volume that the
manager imports into the fork to siem-01 first, and detaches it after the steps
(volume.py).
"""

from __future__ import annotations

import logging
import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from render import (
    SIEM_MONITORED_NODES,
    load_world,
    load_yaml,
    siem_schedules,
    world_at_override_factor,
)

from competition.episode_runner import (
    chains,
    deploy,
    export,
    fork,
    pacing,
    scans,
    schedule,
    scoring,
    settle,
    stops,
    predicted_events,
    volume,
)
from competition.episode_runner.attack import AttackChain, early_stop
from competition.episode_runner.deploy import MakeRun
from competition.episode_runner.incus import IncusError
from competition.episode_runner.record import CopyRecord, EpisodeRecord, utc_text
from competition.episode_runner.seeds import (
    ATTACK_START_STEP,
    ATTACK_WAITS,
    derive_seeds,
)
from competition.episode_runner.startup import startup_input
from competition.episode_runner.steps import (
    ALARM,
    HORIZON,
    LAST_STAGE,
    STEP_S,
    TRACK_A_ALARM,
    Event,
    ProcessSession,
    Session,
    Step,
    StepsResult,
    Wake,
    cut_at,
    run_steps,
)

# The launcher, which the step session runs (episodes.md §7, step protocol),
# the two programs it can run, and the query interface's program
# (episode-runner-impl.md §12).
DEFENDER = "/usr/local/bin/defender"
ENTRY_DEFENDER = "/var/lib/entry/defender"
IDLE_DEFENDER = "/usr/local/bin/idle-defender"
SIEM_READ = "/usr/local/bin/siem-read"
# The test of the entry's defender, which the episode runner makes as the
# launcher chooses, with the entry's defender and the idle defender's program
# as its arguments. It writes the program that the launcher runs, or writes
# the cause on standard error and exits with the launcher's status of 126.
# The volume is the entry's directory, and a volume that siem-01 cannot read
# and search is a fault of the entry, which the test tells apart from a volume
# that holds no defender (episodes.md §7, step protocol).
DEFENDER_TEST = """
entry=$1
volume=${entry%/*}
fault() {
    echo "$1" >&2
    exit 126
}
if [ -e "$volume" ] && { [ ! -d "$volume" ] || [ ! -r "$volume" ] || [ ! -x "$volume" ]; }; then
    fault "$volume cannot be read and searched"
fi
if [ ! -e "$entry" ] && [ ! -L "$entry" ]; then
    echo "$2"
elif [ -f "$entry" ] && [ -r "$entry" ] && [ -x "$entry" ]; then
    echo "$entry"
elif [ -d "$entry" ]; then
    fault "$entry is a directory"
elif [ ! -e "$entry" ]; then
    fault "$entry is a symbolic link to nothing"
elif [ ! -f "$entry" ]; then
    fault "$entry is not a regular file"
elif [ ! -r "$entry" ]; then
    fault "$entry is not readable"
else
    fault "$entry is not executable"
fi
"""
FAULT = 126
# The longest wait for the check of the simulator's log at the Track A alarm,
# which keeps the end notice within 1 second of the alarm's arrival.
ALARM_CHECK_S = 0.5
BENIGN = "benign"
ADVERSARIAL = "adversarial"
HORIZON_REACHED = "horizon"
# The outcomes of a Track A episode that its alarm, or the start of its attack
# chain's last stage, ends. Only a simulator stop gives TERMINATED (episodes.md
# §12, terminated and discarded episodes).
ALARM_ENDED = "alarm"
LAST_STAGE_ENDED = "last_stage"
TRACK_A_ENDS = {TRACK_A_ALARM: ALARM_ENDED, LAST_STAGE: LAST_STAGE_ENDED}
TERMINATED = stops.TERMINATED
DISCARDED = stops.DISCARDED
# The end causes of Track A's fields in the record, in the outcomes' form,
# recorded under track_a in Track A alone (episode-runner-impl.md §11, Track
# A's fields in the record).
SIMULATOR_STOP_ENDED = "simulator_stop"
END_CAUSES = (HORIZON_REACHED, ALARM_ENDED, LAST_STAGE_ENDED, SIMULATOR_STOP_ENDED)
# An episode in a fork that admits no chain that the episode may run.
NOT_ADMITTED = "not admitted"
# The one method of making a fork (living-port contract).
COPY_METHOD = "copying the whole virtual machine"

log = logging.getLogger(__name__)


class Clock(Protocol):
    def now(self) -> float: ...

    def sleep_until(self, t: float, wake: Wake | None = None) -> None: ...

    def utc(self, t: float) -> datetime: ...

    def monotonic(self, at: datetime) -> float: ...


@dataclass
class Testbed:
    """What an episode acts on."""

    incus: Any
    clock: Clock
    up: Callable[[Path, Clock], MakeRun] = deploy.up
    down: Callable[[Clock], MakeRun] = deploy.down
    manifest: Callable[[], dict[str, Any]] = deploy.manifest
    session: Callable[[list[str]], Session] = ProcessSession
    watch: Callable[[Any], Wake | None] = stops.watch_simulator
    alive: Callable[[Any], bool | None] = stops.simulator_process
    attack: Callable[..., Any] = AttackChain


@dataclass(frozen=True)
class Scenario:
    """What the episode runner reads from the files of its deployments: the
    compression factor, the compressed interval of the file-integrity scan,
    and the simulator's timings. The compression factor comes from the
    competition's additional override file, the file that every deployment of
    the episode runner applies (episodes.md §4.1, inputs). The interval is the
    agents' own, which the render computes from world.yaml at that factor, and
    the timings are those that the render projects for the simulator at that
    factor."""

    compression_factor: int
    file_integrity_s: int
    timings: predicted_events.Timings

    @classmethod
    def from_files(
        cls,
        world: Path = deploy.DEPLOY / "world.yaml",
        override: Path = deploy.DEPLOY / deploy.EXTRA_OVERRIDE,
    ) -> Scenario:
        # A profile's override file cannot set the compression factor, so the
        # competition's file alone sets it, as it does for the render.
        at_factor = world_at_override_factor(load_world(world), {}, load_yaml(override))
        return cls(
            compression_factor=at_factor.compression_factor,
            file_integrity_s=siem_schedules(at_factor)["file_integrity_s"],
            timings=predicted_events.competition_timings(world, override),
        )


@dataclass(frozen=True)
class Outcome:
    outcome: str
    cause: str | None
    record: Path


class Discard(Exception):
    """A failure of the testbed, which discards the episode."""


class ResetFailure(RuntimeError):
    """A reset that leaves RavensPort nodes behind, after which no further
    episode can start from a clean deployment."""


@dataclass
class _Attack:
    """An adversarial episode's attack chain, its plan, and its session once
    the steps have started it. A benign episode has no chain."""

    chain: chains.Chain | None = None
    plan: dict[str, Any] = field(default_factory=dict)
    onset: int | None = None
    waits_s: list[float] = field(default_factory=list)
    binary: str = ""
    session: Any = None

    @classmethod
    def planned(
        cls,
        chain: chains.Chain | None,
        seeds: dict[str, int],
        dist: Path,
        chosen: dict[str, Any] | None,
    ) -> _Attack:
        """The chain's onset step, waits and binary, from the episode's
        seeds."""
        if chain is None:
            return cls()
        onset = pacing.onset_step(seeds[ATTACK_START_STEP])
        waits_s = pacing.waits(seeds[ATTACK_WAITS], len(chain.stages))
        binary = f"{chains.BINARY_DIR}/{chain.id}"
        plan = {
            "chain": chain.as_json(),
            "chosen": chosen,
            "binary": binary,
            "sha256": chain.sha256(dist),
            "onset_step": onset,
            "waits_seed": seeds[ATTACK_WAITS],
            "wait_bounds_s": {
                "lower": pacing.WAIT_LOWER_S,
                "mean": pacing.WAIT_MEAN_S,
                "upper": pacing.WAIT_UPPER_S,
            },
            "waits_s": [round(w, 3) for w in waits_s],
        }
        return cls(chain, plan, onset, waits_s, binary)

    def record(
        self,
        record: EpisodeRecord,
        step_0: float | None,
        utc: Callable[[float | None], str | None],
    ) -> None:
        """The attack chain's plan and session, with each beginning event's
        target, and where the chain stopped early."""
        chain = self.chain
        if chain is None:
            return
        if self.session is None:
            record.set(
                attack={
                    **self.plan,
                    "session": None,
                    "early_stop": {
                        "stage_number": 1,
                        "stage": chain.stages[0].name,
                        "cause": "the episode ended before its steps began",
                    },
                }
            )
            return
        session_record = self.session.run.as_json(at=utc)
        for event in session_record["events"]:
            position = event["stage_number"]
            stage = (
                chain.stages[position - 1]
                if 1 <= position <= len(chain.stages)
                else None
            )
            if stage is None or stage.name != event["stage"]:
                session_record["problems"].append(
                    f"the event {event} names no stage of {chain.id} at its position"
                )
            elif event["event"] == "begin":
                event["target"] = stage.target.as_json()
        record.set(
            attack={
                **self.plan,
                "onset": utc(step_0 + self.onset * STEP_S)
                if step_0 is not None and self.onset is not None
                else None,
                "session": session_record,
                "early_stop": early_stop(
                    self.session.run, [s.name for s in chain.stages]
                ),
            }
        )


@dataclass
class _Steps:
    """What the steps leave for the episode's end: in an adversarial episode,
    the attack chain's number of stages, K."""

    result: StepsResult | None = None
    exit_status: int | None = None
    detection: str = ""
    stages: int | None = None


# Whether the path that follows holds an executable file: prints present or
# absent.
BINARY_CHECK = 'if [ -f "$0" ] && [ -x "$0" ]; then echo present; else echo absent; fi'


def check_binary(incus: Any, binary: str) -> None:
    """Discard the episode, before any stage, when the adversary host holds
    no executable file at the path of the chain's binary. Until the adversary
    host's own image holds every attack chain's binary, the episode runner
    discards every adversarial episode that lacks one (episodes.md §19, open
    items; §12, terminated and discarded episodes). A check that Incus fails
    to run raises IncusError, a failed call to Incus."""
    result = incus.exec(chains.ADVERSARY, "sh", "-c", BINARY_CHECK, binary)
    answer = result.stdout.strip()
    if not result.ok or answer not in ("present", "absent"):
        raise IncusError(
            f"the check of {binary} on the adversary host failed: "
            f"{result.stderr.strip() or result.returncode}"
        )
    if answer == "absent":
        raise Discard(f"the adversary host holds no executable {binary}")


def _run_the_steps(
    testbed: Testbed,
    record: EpisodeRecord,
    track: str,
    step_0: float,
    events: list[Event],
    attack: _Attack,
    ran: _Steps,
    utc: Callable[[float | None], str | None],
    suppressed: bool,
    training: bool = False,
) -> None:
    """Run the steps from step 0, with the timed events and the attack chain's
    start at the onset step, and release each of the chain's stages when it
    falls due. At step 0 the episode runner tests the entry's defender, opens
    the step session, and then runs the query interface's start, which
    finishes before the decision request of step 0. The watch on the
    simulator's process wakes the step schedule when the simulator stops.
    Without the watch, the simulator may already have stopped, which fails
    the deployment (episodes.md §12, terminated and discarded episodes);
    otherwise the step schedule checks the simulator in the middle of each
    step. Only in training mode does a step message carry a reward, so the end
    notice carries Track A's R_n in training mode alone, and the episode
    record holds it in both modes (episodes.md §7, step protocol). At the
    alarm, one read of the simulator's log finds a stop that came before
    it."""
    clock = testbed.clock
    log.info("step 0 at %s", utc(step_0))
    ran.stages = len(attack.chain.stages) if attack.chain is not None else None

    def start_reads() -> None:
        """Record where the query interface's reads start, through an
        `incus exec` of its own, apart from the step session, as for a scan
        request. Any exit but status 0 discards the episode as a failure of
        the testbed (episode-runner-impl.md §12, query interface)."""
        sent = clock.now()
        # Step 0's instant on the episode runner's clock, which siem-01 shares,
        # so that siem-read numbers each read's step from step 0's boundary and
        # not from its own later run.
        result = testbed.incus.exec(
            settle.SIEM,
            SIEM_READ,
            "--start",
            "--step-0",
            utc(step_0),
            "--step-s",
            f"{STEP_S:g}",
        )
        output = (result.stdout + result.stderr).strip()
        record.set(
            query_interface_start={
                "sent": utc(sent),
                "exit_status": result.returncode,
                "output": output,
            }
        )
        if not result.ok:
            raise Discard(
                f"siem-read --start exited with status {result.returncode}: {output}"
            )

    def final_reward(result: StepsResult) -> float | None:
        if track != "A":
            return None
        alarm = result.alarm_arrival is not None
        return scoring.track_a(ran.stages, alarm, result.released_before_alarm).R_n

    def stopped_before(arrival: float) -> float | None:
        """The time of a simulator stop before the alarm's arrival, from one
        read of the simulator's log through Incus at the alarm, before the end
        notice, within ALARM_CHECK_S."""
        read = testbed.incus.exec(
            settle.SIM_HOST, "cat", settle.SIM_LOG, timeout_s=ALARM_CHECK_S
        )
        if not read.ok:
            log.warning("the check of the simulator at the alarm failed")
            return None
        stop = stops.read_stop(read.stdout)
        if stop.cause is None or stop.at is None:
            return None
        at = clock.monotonic(stop.at)
        return at if at <= arrival else None

    watch = testbed.watch(testbed.incus)
    checks = []
    if watch is None:
        before = stops.logged_stop(testbed.incus)
        if before.cause is not None:
            raise Discard(f"the simulator stopped before step 0: {before.cause}")
        checks = stops.checks(
            testbed.incus,
            step_0,
            HORIZON,
            STEP_S,
            alive=lambda: testbed.alive(testbed.incus),
        )
    ran.detection = (
        "status check in each step" if watch is None else "process file descriptor"
    )
    # The query interface's start comes first among step 0's events, all of
    # which run before the decision request of step 0.
    events = [Event(step_0, start_reads), *events, *checks]
    if attack.chain is not None and attack.onset is not None:
        attack.session = testbed.attack(
            testbed.incus,
            clock,
            testbed.incus.attack_session(chains.ADVERSARY, attack.binary),
            chains.ADVERSARY,
            attack.waits_s,
            record.attack_log_path(),
        )
        onset_at = step_0 + attack.onset * STEP_S
        record.set(attack={**attack.plan, "onset": utc(onset_at)})
        events.append(Event(onset_at, attack.session.start))
    # The step session opens at the start of step 0, once the entry's defender
    # passes its test, and before the query interface records where the reads
    # start (episodes.md §7, step protocol).
    clock.sleep_until(step_0, wake=watch)
    try:
        program = _defender_program(testbed.incus, record)
        session = testbed.session(
            testbed.incus.step_session(settle.SIEM, DEFENDER, program)
        )
    except BaseException:
        if watch is not None:
            watch.close()
        raise
    try:
        ran.result = run_steps(
            session,
            clock,
            track,
            step_0,
            events=events,
            stop=watch,
            stages=attack.session,
            suppressed=suppressed,
            training=training,
            final_reward=final_reward,
            stopped_before=stopped_before if track == "A" else None,
        )
    finally:
        # The end notice has gone, so the attack chain stops first
        # (episodes.md §11, horizon and termination).
        try:
            if attack.session is not None:
                attack.session.stop()
        finally:
            ran.exit_status = session.close()
            if watch is not None:
                watch.close()
    # An attack chain whose session failed to start, or gave no process
    # identifier, never ran, so the episode is a failure of the testbed and is
    # not scored (episodes.md §12, terminated and discarded episodes).
    if attack.session is not None and getattr(attack.session, "failure", None):
        raise Discard(attack.session.failure)


def _defender_program(incus: Any, record: EpisodeRecord) -> str:
    """Test /var/lib/entry/defender, before the step session opens, through
    an `incus exec` of its own into siem-01, and record and return the program
    that the launcher is to run: the entry's defender, or the idle defender
    when the path holds nothing. The episode runner passes the program to the
    launcher, which runs it only when its own check finds the same program, so
    the record names exactly the program that ran. A path that holds anything
    else is a fault of the entry, which discards the episode with its cause
    (episode-runner-impl.md §1, competition images; episodes.md §7, step
    protocol)."""
    result = incus.exec(
        settle.SIEM, "sh", "-c", DEFENDER_TEST, "sh", ENTRY_DEFENDER, IDLE_DEFENDER
    )
    program = result.stdout.strip()
    if result.ok and program in (ENTRY_DEFENDER, IDLE_DEFENDER):
        record.set(defender_program=program)
        log.info("the step session runs %s", program)
        return program
    error = result.stderr.strip()
    if result.returncode == FAULT:
        # The cause is the test's first line; the Incus client can add a line
        # of its own for a command that exits with status 126.
        cause = error.splitlines()[0] if error else "no cause given"
        raise Discard(f"the entry's defender cannot run: {cause}")
    raise Discard(
        f"the test of {ENTRY_DEFENDER} failed with status {result.returncode}: "
        f"{error or program}"
    )


def _end(
    testbed: Testbed,
    record: EpisodeRecord,
    track: str,
    outcome: str,
    cause: str | None,
    step_0: float | None,
    ran: _Steps,
    phase: Callable[[str, float, float], None],
    utc: Callable[[float | None], str | None],
    episode_start: datetime,
) -> tuple[str, str | None]:
    """Export the days from step 0 on, or from `episode_start` when step 0
    never came, read a simulator stop from the exported log, and keep the
    steps that ran, which end with the step in which the simulator stopped,
    and in Track A the reward that they give. Returns the episode's outcome
    and cause."""
    clock = testbed.clock
    result = ran.result
    if outcome == HORIZON_REACHED and result is not None and result.terminal_state:
        # A Track A terminal state ended the episode, which takes the terminal
        # state's own outcome (episodes.md §12, terminated and discarded
        # episodes).
        outcome, cause = TRACK_A_ENDS[result.terminal_state], None
    started = clock.now()
    errors = export.export(
        testbed.incus,
        record,
        wait=lambda s: clock.sleep_until(clock.now() + s),
        start=episode_start if step_0 is None else clock.utc(step_0),
        now=lambda: clock.utc(clock.now()),
    )
    record.set(export=errors)
    phase("export", started, clock.now())
    failed = {name: error for name, error in errors.items() if error}
    if outcome != DISCARDED and failed:
        outcome = DISCARDED
        cause = "the export failed: " + "; ".join(
            f"{n}: {e}" for n, e in failed.items()
        )

    # A simulator stop, from the exported simulator log. A stop that the run
    # did not see is still in the log, so the log is read after every run of
    # the steps. A stop before step 0 fails the deployment, and a stop during
    # the steps ends the episode in the step in which it came, even one that
    # came before a Track A terminal state.
    stop_at: datetime | None = None
    ended_early = None if result is None else result.ended_early
    if outcome != DISCARDED and result is not None and step_0 is not None:
        stop = stops.read_stop(
            record.exported_path("sim.log").read_text(errors="replace")
        )
        step_0_at = clock.utc(step_0)
        last_end = clock.utc(result.steps[-1].end if result.steps else step_0)
        seen = ended_early is not None and result.terminal_state is None
        missed = not seen and stop.cause is not None
        if missed and stop.at is not None and stop.at > last_end:
            # A stop after the last step leaves the episode as it ended.
            missed = False
        if seen or missed:
            record.set(
                simulator_stop={
                    "detected": utc(ended_early) if seen else None,
                    "at": None if stop.at is None else utc_text(stop.at),
                    "cause": stop.cause,
                    "panics": list(stop.panics),
                    "outcome": stop.outcome,
                }
            )
            if missed:
                log.warning("found an unseen simulator stop in its log: %s", stop.cause)
            if (stop.at is not None and stop.at < step_0_at) or (
                missed and stop.at is None
            ):
                outcome = DISCARDED
                cause = f"the simulator stopped before step 0: {stop.cause}"
            else:
                outcome = stop.outcome
                cause = stop.cause or f"{result.end_cause}, and its log holds no cause"
                stop_at = stop.at

    # The steps, which end with the step in which the simulator stopped.
    if result is not None:
        kept = result.steps
        if stop_at is not None:
            kept = cut_at(result.steps, clock.monotonic(stop_at), track)
        for step in kept:
            record.append_step(step.as_line(at=utc))
        record.set(
            steps={
                "run": len(kept),
                "end_notice_step": None
                if result.end_notice_sent is None
                else len(result.steps),
                "end_notice_sent": utc(result.end_notice_sent),
                "session_ended": utc(result.session_ended),
                "ended_early": utc(result.ended_early),
                # Why the steps ended, in words, in either track, for
                # diagnosis; Track A's end cause is track_a's.
                "end_reason": result.end_cause,
                "stop_detection": ran.detection,
                "defender_exit_status": ran.exit_status,
            }
        )
        if track == "A":
            record.set(track_a=_track_a(result, kept, ran.stages, stop_at, utc))
    return outcome, cause


def _track_a(
    result: StepsResult,
    kept: list[Step],
    stages: int | None,
    stop_at: datetime | None,
    utc: Callable[[float | None], str | None],
) -> dict[str, Any]:
    """Track A's fields in the episode record (episode-runner-impl.md §11):
    the first alarm, K, k_n, R_n, Track A's outcome and the end cause. An
    alarm that arrived after a simulator stop no longer counts."""
    step = result.alarm_step
    if step is not None and (step >= len(kept) or kept[step].decision != ALARM):
        step = None
    scored = scoring.track_a(stages, step is not None, result.released_before_alarm)
    if stop_at is not None or (
        result.ended_early is not None and result.terminal_state is None
    ):
        end_cause = SIMULATOR_STOP_ENDED
    elif result.terminal_state is not None:
        end_cause = TRACK_A_ENDS[result.terminal_state]
    else:
        end_cause = HORIZON_REACHED
    return {
        "alarm": None
        if step is None
        else {"step": step, "arrival": utc(result.alarm_arrival)},
        "K": scored.K,
        "k_n": scored.k_n,
        "R_n": scored.R_n,
        "outcome": scored.outcome,
        "end_cause": end_cause,
    }


def _attach_entry_volume(incus: Any, record: EpisodeRecord, directory: Path) -> None:
    """Attach the entry's submitted volume read-only to siem-01 at
    /var/lib/entry straight after the deployment, as the living port's manager
    does in evaluation, and record its version (episodes.md §3.2, modes). A
    volume that cannot be opened or attached is a failure of the testbed."""
    try:
        volume.open_to_the_container(directory, writable=False)
    except OSError as error:
        raise Discard(
            f"the entry's volume {directory} cannot be opened: {error}"
        ) from error
    attached = volume.attach_read_only(incus, directory)
    if not attached.ok:
        raise Discard(
            f"the entry's volume {directory} was not attached: "
            f"{attached.stderr.strip() or attached.returncode}"
        )
    record.set(
        entry_volume={"source": str(directory), "version": volume.version(incus)}
    )


def _settle_record(
    settled: settle.Settled, sim_start: float, utc: Callable[[float | None], str | None]
) -> dict[str, Any]:
    return {
        "start": utc(settled.start),
        "conditions": [
            {"condition": c.number, "held": utc(c.held), "lines": c.lines}
            for c in settled.conditions
        ],
        "step_0": utc(settled.step_0),
        "simulator_start": utc(sim_start),
        "start_state": settled.start_state,
    }


def run_episode(
    testbed: Testbed,
    scenario: Scenario,
    episode_seed: str,
    track: str,
    records_root: Path,
    *,
    settle_timeout_s: float | None = settle.SETTLE_TIMEOUT_S,
    episode_type: str = BENIGN,
    chain: chains.Chain | None = None,
    chosen: dict[str, Any] | None = None,
    dist: Path = chains.DIST,
    suppressed: bool = False,
    entry_volume: Path | None = None,
) -> Outcome:
    """Run one episode from plan to reset, and keep its episode record. An
    adversarial episode runs the attack chain `chain`, and `chosen` says how
    the chain was chosen. A suppressed episode, a replay for Competition Rule
    5, records each decision and carries none out. `entry_volume` is the
    entry's submitted storage volume, a directory, which siem-01 mounts
    read-only at /var/lib/entry from the deployment on, as in evaluation. A
    discarded episode returns its outcome and cause, and the episode runner
    does not run it again."""
    if episode_type not in (BENIGN, ADVERSARIAL):
        raise ValueError(f"no episode type {episode_type!r}")
    if (episode_type == ADVERSARIAL) != (chain is not None):
        raise ValueError(f"a {episode_type} episode with the attack chain {chain}")
    clock = testbed.clock

    def utc(t: float | None) -> str | None:
        return None if t is None else utc_text(clock.utc(t))

    planned = clock.now()
    durations: dict[str, float] = {}

    def phase(name: str, start: float, end: float) -> None:
        durations[name] = round(end - start, 3)
        record.set(durations_s=durations)

    # Plan.
    record = EpisodeRecord(records_root, clock.utc(planned), episode_seed)
    seeds = derive_seeds(episode_seed)
    startup = startup_input(episode_seed, scenario.timings, clock.utc(planned))
    record.write_startup(startup)
    record.set(
        episode_seed=episode_seed,
        seeds=seeds,
        type=episode_type,
        mode="evaluation",
        suppressed=suppressed,
        track=track,
        defender=DEFENDER,
        # The program that the launcher runs, once the step session opens.
        defender_program=None,
        scenario={
            "compression_factor": scenario.compression_factor,
            "horizon_steps": HORIZON,
            "step_s": STEP_S,
            "settle_delay_s": settle.SETTLE_DELAY_S,
            "settle_timeout_s": settle_timeout_s,
            "file_integrity_interval_s": scenario.file_integrity_s,
        },
    )
    attack = _Attack.planned(chain, seeds, dist, chosen)
    if chain is not None:
        record.set(attack=attack.plan)
    state_dir = deploy.sim_state_dir(startup)
    outcome, cause = HORIZON_REACHED, None
    rendered: dict[str, Any] | None = None
    settled: settle.Settled | None = None
    ran = _Steps()
    phase("plan", planned, clock.now())

    try:
        # Deploy.
        log.info("episode %s: deploying", episode_seed[:8])
        made = testbed.up(state_dir, clock)
        record.set(
            deployment={
                "started": utc(made.started),
                "sim_host_started": utc(made.sim_host_started),
                "ended": utc(made.ended),
                "exit_status": made.returncode,
            }
        )
        phase("deploy", made.started, made.ended)
        if made.returncode != 0 or made.sim_host_started is None:
            last = made.tail[-1] if made.tail else ""
            raise Discard(
                f"the deployment failed with exit status {made.returncode}: {last}"
            )
        rendered = testbed.manifest()
        if chain is not None and chains.ADVERSARY not in rendered["nodes"]:
            raise Discard("the deployment holds no adversary host")
        if chain is not None:
            check_binary(testbed.incus, attack.binary)
        record.set(
            profile=rendered.get("profile"),
            images=deploy.images(testbed.incus, rendered),
        )
        if entry_volume is not None:
            _attach_entry_volume(testbed.incus, record, entry_volume)
        monitored = [n for n in SIEM_MONITORED_NODES if n in rendered["nodes"]]

        # Settle.
        log.info("waiting for the settle conditions")
        try:
            settled = settle.wait_to_settle(
                testbed.incus,
                clock,
                monitored,
                made.sim_host_started,
                timeout_s=settle_timeout_s,
            )
        except settle.SettleFailure as failure:
            raise Discard(f"the deployment failed to settle: {failure}") from failure
        sim_start = clock.monotonic(settle.start_state_time(settled.start_state))
        record.set(settle=_settle_record(settled, sim_start, utc))

        # The scan requests (episodes.md §6.2, first occurrences).
        end = settled.step_0 + HORIZON * STEP_S
        first = schedule.first_scans(episode_seed, monitored, scenario.file_integrity_s)
        record.set(first_scans={host: utc(sim_start + t) for host, t in first.items()})
        requests = scans.plan(
            episode_seed,
            monitored,
            settled.agent_ids,
            scenario.file_integrity_s,
            sim_start,
            settled.step_0,
            end,
        )

        # Run the steps.
        _run_the_steps(
            testbed,
            record,
            track,
            settled.step_0,
            scans.events(testbed.incus, clock, requests),
            attack,
            ran,
            utc,
            suppressed=suppressed,
        )
        result = ran.result
        assert result is not None
        phase("settle", made.ended, settled.step_0)
        last_step_end = result.steps[-1].end if result.steps else settled.step_0
        phase("steps", settled.step_0, last_step_end)
        phase("end", last_step_end, clock.now())
        record.set(
            scan_requests=[
                {
                    "host": r.host,
                    "agent_id": r.agent_id,
                    "due": utc(r.due),
                    "sent": utc(r.sent),
                    "ok": r.ok,
                    "output": r.output,
                }
                for r in requests
            ],
        )
    except KeyboardInterrupt:
        attack.record(record, settled.step_0 if settled else None, utc)
        record.set(outcome="interrupted", cause="the operator interrupted the episode")
        raise
    except (Discard, IncusError) as failure:
        outcome, cause = DISCARDED, str(failure)
        log.warning("episode %s discarded: %s", episode_seed[:8], cause)

    attack.record(record, settled.step_0 if settled else None, utc)
    outcome, cause = _end(
        testbed,
        record,
        track,
        outcome,
        cause,
        settled.step_0 if settled else None,
        ran,
        phase,
        utc,
        clock.utc(planned),
    )

    # Reset.
    made = testbed.down(clock)
    try:
        left = deploy.remaining(testbed.incus, rendered or testbed.manifest())
        unchecked = None
    except (OSError, ValueError, KeyError, IncusError) as error:
        left, unchecked = None, f"the reset could not be checked: {error}"
    record.set(reset={"exit_status": made.returncode, "remaining": left})
    phase("reset", made.started, made.ended)
    shutil.rmtree(state_dir, ignore_errors=True)
    record.set(outcome=outcome, cause=cause)
    log.info("episode %s: %s", episode_seed[:8], outcome)
    if unchecked:
        raise ResetFailure(unchecked)
    if left:
        raise ResetFailure(f"the reset left RavensPort nodes behind: {', '.join(left)}")
    return Outcome(outcome, cause, record.path)


@dataclass(frozen=True)
class Fork:
    """The fork of an episode: the living port, the fork's number and the
    fork instant on the living port's clock, which the fork's clock
    continues from."""

    living: fork.LivingPort
    number: int
    at: datetime

    @property
    def episode_seed(self) -> str:
        return fork.episode_seed_of(self.living, self.number)

    @property
    def age_s(self) -> float:
        return fork.age_s(self.living, self.at)


def run_copy_episode(
    testbed: Testbed,
    scenario: Scenario,
    forked: Fork,
    track: str,
    out: Path,
    *,
    settle_timeout_s: float | None = settle.SETTLE_TIMEOUT_S,
    episode_type: str = BENIGN,
    library: Sequence[chains.Chain] = (),
    given: chains.Chain | None = None,
    dist: Path = chains.DIST,
    read_start: Callable[..., fork.ForkStart] = fork.read_start_state,
    mode: str = volume.EVALUATION,
    training_volume: Path | None = None,
    suppressed: bool = False,
) -> Outcome:
    """Run one episode in a fork of a living port, on the deployment that the
    fork holds, and keep its record in `out`. The episode seed derives from
    the living port seed and the fork's number. An adversarial episode runs
    the chain `given`, or one drawn from `library`, once the fork admits it;
    the start state at the fork and step 0 decide the admission. Step 0
    begins once the settle conditions hold, with no settle delay. In training
    mode the training volume is attached read-write to siem-01 before the
    settle conditions, and detached after the steps. A suppressed episode
    records each decision and carries none out."""
    if episode_type not in (BENIGN, ADVERSARIAL):
        raise ValueError(f"no episode type {episode_type!r}")
    if episode_type == BENIGN and given is not None:
        raise ValueError(f"a benign episode with the attack chain {given.id}")
    if mode not in (volume.TRAINING, volume.EVALUATION):
        raise ValueError(f"no mode {mode!r}")
    if (mode == volume.TRAINING) != (training_volume is not None):
        raise ValueError(f"{mode} mode with the training volume {training_volume}")
    clock = testbed.clock

    def utc(t: float | None) -> str | None:
        return None if t is None else utc_text(clock.utc(t))

    planned = clock.now()
    durations: dict[str, float] = {}

    def phase(name: str, start: float, end: float) -> None:
        durations[name] = round(end - start, 3)
        record.set(durations_s=durations)

    # Plan.
    living = forked.living
    episode_seed = forked.episode_seed
    record = CopyRecord(out)
    record.write_startup(living.startup_path.read_text())
    seeds = derive_seeds(episode_seed)
    record.set(
        living_port_seed=living.seed,
        fork=forked.number,
        forked_at=fork.instant_text(forked.at),
        age_s=forked.age_s,
        episode_seed=episode_seed,
        living_port=living.as_json(),
        seeds=seeds,
        type=episode_type,
        mode=mode,
        suppressed=suppressed,
        track=track,
        defender=DEFENDER,
        # The program that the launcher runs, once the step session opens.
        defender_program=None,
        copy={
            "method": COPY_METHOD,
            "start": fork.instant_text(forked.at),
            "runner_started": utc(planned),
        },
        training_volume=None if training_volume is None else str(training_volume),
        training_volume_in=None,
        training_volume_out=None,
        scenario={
            "compression_factor": scenario.compression_factor,
            "horizon_steps": HORIZON,
            "step_s": STEP_S,
            "settle_delay_s": settle.COPY_DELAY_S,
            "settle_timeout_s": settle_timeout_s,
            "file_integrity_interval_s": scenario.file_integrity_s,
            "allowance_s": predicted_events.ALLOWANCE_S,
        },
    )
    outcome, cause = HORIZON_REACHED, None
    settled: settle.Settled | None = None
    attack = _Attack()
    ran = _Steps()
    copy_start = clock.monotonic(forked.at)
    has_volume = False
    phase("plan", planned, clock.now())

    def close_volume() -> None:
        """The volume's version at the episode's end; in training mode the
        volume is then detached, for the manager to carry to the next fork."""
        if not has_volume:
            return
        record.set(training_volume_out=volume.version(testbed.incus))
        if mode == volume.TRAINING:
            detached = volume.detach(testbed.incus)
            record.set(
                training_volume_detached=detached.ok
                or detached.stderr.strip()
                or f"exit status {detached.returncode}"
            )

    try:
        rendered = testbed.manifest()
        if episode_type == ADVERSARIAL and chains.ADVERSARY not in rendered["nodes"]:
            raise Discard("the fork holds no adversary host")
        record.set(
            profile=rendered.get("profile"),
            images=deploy.images(testbed.incus, rendered),
        )
        monitored = [n for n in SIEM_MONITORED_NODES if n in rendered["nodes"]]

        # The entry's volume on siem-01, before the settle conditions
        # (episodes.md §3.2, modes).
        if training_volume is not None:
            try:
                volume.open_to_the_container(training_volume)
            except OSError as error:
                raise Discard(
                    f"the training volume {training_volume} cannot be opened: {error}"
                ) from error
            attached = volume.attach(testbed.incus, training_volume)
            if not attached.ok:
                raise Discard(
                    f"the training volume {training_volume} was not attached: "
                    f"{attached.stderr.strip() or attached.returncode}"
                )
            has_volume = True
        else:
            has_volume = volume.attached(testbed.incus)
        if has_volume:
            record.set(training_volume_in=volume.version(testbed.incus))

        # The start state at the fork, read alike in every episode, so that
        # the records it leaves before step 0 hold no trace of the type.
        log.info("reading the start state at fork %d", forked.number)
        read = clock.now()
        try:
            start = read_start(testbed.incus, living, forked.at, scenario.timings)
        except predicted_events.PredictedEventsError as error:
            raise Discard(
                f"the start state at the fork was not read: {error}"
            ) from error
        except fork.StartStateFault as fault:
            # A fault of the testbed, which discards the episode with its
            # cause (episodes.md §6.4, faults).
            record.set(start_state=fault.record)
            phase("start_state", read, clock.now())
            raise Discard(
                f"the start state at the fork holds a fault: {fault}"
            ) from fault
        record.set(start_state=start.record)
        phase("start_state", read, clock.now())

        # Settle.
        log.info("waiting for the settle conditions")
        try:
            settled = settle.wait_to_settle(
                testbed.incus,
                clock,
                monitored,
                copy_start,
                delay_s=settle.COPY_DELAY_S,
                timeout_s=settle_timeout_s,
                copy=True,
            )
        except settle.SettleFailure as failure:
            raise Discard(f"the fork failed to settle: {failure}") from failure
        sim_start = clock.monotonic(settle.start_state_time(settled.start_state))
        record.set(settle=_settle_record(settled, sim_start, utc))
        phase("settle", copy_start, settled.step_0)

        # The chain, once step 0 is known (episodes.md §10.2, choosing the
        # chain).
        if episode_type == ADVERSARIAL:
            after = settled.step_0 - copy_start
            choice = predicted_events.choose_at_fork(
                library,
                given,
                start.draws,
                episode_seed,
                scenario.timings,
                (after, after),
            )
            if choice.chain is None:
                record.set(attack={"chosen": choice.chosen})
                outcome = NOT_ADMITTED
                cause = choice.chosen["not_admitted"]
                log.warning("fork %d admits no chain: %s", forked.number, cause)
                close_volume()
                record.set(reset=None, outcome=outcome, cause=cause)
                return Outcome(outcome, cause, record.path)
            attack = _Attack.planned(choice.chain, seeds, dist, choice.chosen)
            record.set(attack=attack.plan)
            check_binary(testbed.incus, attack.binary)

        # Run the steps. A fork of the whole virtual machine keeps the living
        # port's scan times, so no scan is requested (episodes.md §6.2, first
        # occurrences).
        _run_the_steps(
            testbed,
            record,
            track,
            settled.step_0,
            [],
            attack,
            ran,
            utc,
            suppressed=suppressed,
            training=mode == volume.TRAINING,
        )
        result = ran.result
        assert result is not None
        last_step_end = result.steps[-1].end if result.steps else settled.step_0
        phase("steps", settled.step_0, last_step_end)
        phase("end", last_step_end, clock.now())
    except KeyboardInterrupt:
        attack.record(record, settled.step_0 if settled else None, utc)
        record.set(outcome="interrupted", cause="the operator interrupted the episode")
        raise
    except (Discard, IncusError) as failure:
        outcome, cause = DISCARDED, str(failure)
        log.warning("episode at fork %d discarded: %s", forked.number, cause)

    attack.record(record, settled.step_0 if settled else None, utc)
    close_volume()
    # The fork's siem-01 holds every day since the living port's start, and
    # the episode's days start at step 0, or at the fork instant when step 0
    # never came.
    outcome, cause = _end(
        testbed,
        record,
        track,
        outcome,
        cause,
        settled.step_0 if settled else None,
        ran,
        phase,
        utc,
        forked.at,
    )
    record.set(reset=None, outcome=outcome, cause=cause)
    log.info("episode at fork %d: %s", forked.number, outcome)
    return Outcome(outcome, cause, record.path)
