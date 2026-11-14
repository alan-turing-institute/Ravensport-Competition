"""One episode, from plan to reset, and the outcome and cause of each episode
that ends early or is discarded (episodes.md §3.1, phases; §12, terminated and
discarded episodes; §13, episode record).

A fake testbed deploys, settles and stops on a fake clock, and the idle
defender answers each decision request 10 milliseconds after it is sent.

Run with: pytest competition/tests -q
"""

from __future__ import annotations

import dataclasses
import hashlib
import inspect
import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from competition.episode_runner import (
    chains,
    episode,
    export,
    fork,
    pacing,
    settle,
    steps,
    predicted_events,
    volume,
)
from competition.episode_runner.attack import AttackRun, StageEvent
from competition.episode_runner.deploy import MakeRun
from competition.episode_runner.incus import Result
from competition.episode_runner.record import utc_text
from competition.episode_runner.seeds import (
    ATTACK_START_STEP,
    ATTACK_WAITS,
    derive_seeds,
)
from competition.episode_runner.startup import startup_input

SEED = "6f1d8c2a9b3e4f5061728394a5b6c7d8e9fa0b1c2d3e4f5061728394a5b6c7d8"
EPOCH = datetime(2026, 11, 4, 1, 0, tzinfo=timezone.utc)
MONITORED = (
    "fms-01",
    "hmi-01",
    "hist-01",
    "hist-dmz-01",
    "atg-relay-01",
    "dmz-bastion-01",
    "corp-it",
    "recon-01",
    "admin-01",
    "fw-ext-dmz",
    "fw-dmz-it",
    "fw-ot-dmz",
)
MANIFEST = {
    "profile": "ci-minimal",
    "nodes": [*MONITORED, "siem-01", "sim-host"],
    "images": {
        **{node: f"ravensport/{node}:latest" for node in MONITORED},
        "siem-01": "ravensport/siem-01-competition:latest",
    },
}
# The deployment of the full topology, which holds the adversary host.
WITH_ADVERSARY = {**MANIFEST, "nodes": [*MANIFEST["nodes"], "adversary"]}
SAMPLE = Path(__file__).resolve().parent / "data" / "chains-sample.json"
CHAIN = chains.find(chains.load(SAMPLE), "it-foothold__disclose-account-inventory")
BINARY = b"\x7fELF the paced binary"
# A short scan interval places some first scans inside every episode.
SCENARIO = episode.Scenario(
    compression_factor=5,
    file_integrity_s=1200,
    timings=predicted_events.competition_timings(),
)
CAVITATION = (
    "fuel_tank: cavitation — pump drawing from an empty tank (volume 0.000000 L "
    "at 1500 L/min outflow); the FMS cavitation guard has been defeated"
)


@pytest.fixture(autouse=True)
def start_up_directories(tmp_path_factory, monkeypatch) -> None:
    """Keeps each start-up directory that an episode makes inside pytest's
    temporary directory, including the directory that an interrupted episode
    leaves behind."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path_factory.mktemp("state")))


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    def sleep_until(self, t: float, wake=None) -> None:
        if wake is not None and wake.at < t:
            t = wake.at
        if t > 100_000:
            raise AssertionError("the episode never ends")
        self.t = max(self.t, t)

    def utc(self, t: float) -> datetime:
        return EPOCH + timedelta(seconds=t)

    def monotonic(self, at: datetime) -> float:
        return (at - EPOCH).total_seconds()


class IdleDefender:
    """The idle defender's side of the step session, or, with alarm_at, a test
    defender that raises the Track A alarm `delay` seconds into that step."""

    def __init__(
        self,
        clock: FakeClock,
        track: str,
        alarm_at: int | None = None,
        delay: float = 0.01,
    ) -> None:
        self.clock, self.track = clock, track
        self.alarm_at, self.delay = alarm_at, delay
        self.sent: list[dict] = []
        self.due: list[tuple[float, str]] = []
        # When the session opened, and when its standard input closed.
        self.opened_at = clock.now()
        self.closed_at: float | None = None

    def send(self, line: str) -> None:
        message = json.loads(line)
        self.sent.append(message)
        if message["ended"]:
            return
        if message["step"] == self.alarm_at:
            self.due.append((self.clock.now() + self.delay, steps.ALARM))
        else:
            self.due.append((self.clock.now() + 0.01, steps.NO_DECISION[self.track]))
        self.due.sort()

    def receive(self, timeout_s: float, wake=None) -> str | None:
        deadline = self.clock.now() + timeout_s
        if wake is not None and wake.at <= deadline:
            if not self.due or wake.at < self.due[0][0]:
                self.clock.sleep_until(wake.at)
                return None
        if self.due and self.due[0][0] <= deadline:
            at, decision = self.due.pop(0)
            self.clock.sleep_until(at)
            return json.dumps({"decision": decision}) + "\n"
        self.clock.sleep_until(deadline)
        return None

    def arrived(self) -> list[str]:
        lines = []
        while self.due and self.due[0][0] <= self.clock.now():
            lines.append(json.dumps({"decision": self.due.pop(0)[1]}) + "\n")
        return lines

    def close(self) -> int:
        if self.closed_at is None:
            self.closed_at = self.clock.now()
        return 0


class FakeWake:
    """The watch on the simulator's process, which fires at a time on the fake
    clock."""

    def __init__(self, clock: FakeClock, at: float) -> None:
        self.clock, self.at = clock, at

    def fileno(self) -> int:
        raise AssertionError("the fake clock waits for the stop without a file")

    def fired(self) -> bool:
        return self.clock.now() >= self.at

    def close(self) -> None:
        pass


class FakeAttack:
    """The attack chain's session, whose binary completes each stage half a
    second after the step schedule releases it."""

    def __init__(self, clock: FakeClock, command, node, waits_s, stderr_path) -> None:
        self.clock, self.command, self.node = clock, command, node
        self.waits_s, self.stderr_path = list(waits_s), stderr_path
        self.run = AttackRun()
        self.stopped_at: float | None = None
        # Why the session failed to start, as the real session gives it.
        self.failure: str | None = None
        self.fails = False

    @property
    def total(self) -> int:
        return len(self.waits_s)

    @property
    def released(self) -> int:
        return len(self.run.releases)

    def start(self) -> None:
        if self.fails:
            self.failure = (
                "the attack chain's session gave no process identifier: its first "
                "line was 'Error: Instance not found'"
            )
            self.run.problems.append(self.failure)
            return
        self.run.started = self.clock.now()
        self.run.pid = 4242
        self.stderr_path.write_text("[*] scan-it-segment\n")

    def next_stage(self) -> tuple[int, float] | None:
        n = len(self.run.releases)
        if self.run.started is None or self.stopped_at is not None or n >= self.total:
            return None
        wait_start = self.run.started if n == 0 else self.run.releases[-1] + 0.5
        if self.clock.now() < wait_start:
            return None
        return n + 1, wait_start + self.waits_s[n]

    def release(self, number: int) -> bool:
        assert number == len(self.run.releases) + 1
        at = self.clock.now()
        stage = CHAIN.stages[number - 1].name
        self.run.releases.append(at)
        self.run.events.append(StageEvent("begin", number, stage, at + 0.01))
        self.run.events.append(StageEvent("end", number, stage, at + 0.5, "completed"))
        return True

    def stop(self) -> AttackRun:
        self.stopped_at = self.clock.now()
        # The events after the stop never came.
        self.run.events = [e for e in self.run.events if e.at <= self.stopped_at]
        if self.run.started is not None:
            ends = [e for e in self.run.events if e.kind == "end"]
            if len(ends) == self.total:
                # The binary exited by itself after its last stage.
                self.run.exit_status = 0
            else:
                # The binary, still waiting for `begin`, exits once its
                # standard input closes.
                self.run.stop, self.run.exit_status = "standard input closed", 3
        self.run.ended = self.clock.now()
        return self.run


class FakeTestbed:
    """A deployment that settles about a minute after sim-host starts, and
    whose simulator stops at stop_at, logging the cause stop_cause at
    logged_stop_at, or at stop_at when logged_stop_at is None. With
    watched False, no watch on the simulator's process exists. With
    watch_sees_stop False, the watch misses the stop; with status_sees_stop
    False, OpenRC reports the simulator started after it stops; with
    process_visible True, the host sees whether the simulator's process runs."""

    def __init__(self, clock: FakeClock, track: str = "A") -> None:
        self.clock, self.track = clock, track
        self.up_status = 0
        self.sim_host_starts = True
        self.stop_at: float | None = None
        self.logged_stop_at: float | None = None
        self.stop_cause = CAVITATION
        self.watched = True
        self.watch_sees_stop = True
        self.status_sees_stop = True
        self.process_visible = False
        self.failing_pull = ""
        # The dated files that the export listed, below /var/ossec/logs and
        # without their extension.
        self.listed: list[str] = []
        self.left_after_down: list[str] = []
        self.sim_host_start = 0.0
        self.running = False
        self.state_dir: Path | None = None
        self.startup = ""
        self.scan_requests: list[tuple[float, str]] = []
        self.defenders: list[IdleDefender] = []
        self.downs = 0
        self.manifest = MANIFEST
        self.attacks: list[FakeAttack] = []
        # The devices of siem-01, the version digest of its volume before and
        # after the steps, and each other command of Incus with its time.
        self.devices: dict[str, dict] = {}
        self.versions = ("a" * 64, "a" * 64)
        self.attach_fails = False
        self.commands: list[tuple[float, tuple[str, ...]]] = []
        # The step at which the test defender raises the alarm, and the times
        # of each `siem-read --start`.
        self.alarm_at: int | None = None
        self.alarm_delay = 0.01
        # Whether the attack chain's session fails to start.
        self.attack_fails = False
        self.reads_started: list[float] = []
        self.reads_start_fails = False
        # Step 0's instant as each siem-read --start received it, and how long
        # the test of the defender and siem-read --start each take.
        self.reads_step_0: list[str] = []
        self.call_s = 0.0
        # The launcher's line on standard error, and the step session's exit
        # status.
        # The test of the entry's defender, as siem-01 answers it.
        self.defender_test = Result(0, "/usr/local/bin/idle-defender\n", "")
        self.defender_tested_at: list[float] = []
        # When a read of the simulator's log finds its stop line, the stop's
        # time unless given; and the times of the reads during the episode.
        self.stop_written_at: float | None = None
        self.log_reads: list[float] = []
        # The monitored hosts whose Wazuh agent is kept down, and the services
        # kept down, as (node, unit).
        self.agents_down: set[str] = set()
        self.services_down: set[tuple[str, str]] = set()
        # Whether the adversary host holds the chain's binary as an executable
        # file, and each path that the episode runner checked.
        self.binary_present = True
        self.binary_checks: list[str] = []

    def testbed(self) -> episode.Testbed:
        return episode.Testbed(
            incus=self,
            clock=self.clock,
            up=self.up,
            down=self.down,
            manifest=lambda: self.manifest,
            session=self.session,
            watch=self.watch,
            alive=self.alive,
            attack=self.attack,
        )

    def attack(self, incus, clock, command, node, waits_s, stderr_path) -> FakeAttack:
        attack = FakeAttack(clock, command, node, waits_s, stderr_path)
        attack.fails = self.attack_fails
        self.attacks.append(attack)
        return attack

    def watch(self, incus) -> FakeWake | None:
        if not self.watched:
            return None
        missed = self.stop_at is None or not self.watch_sees_stop
        return FakeWake(self.clock, float("inf") if missed else self.stop_at)

    def alive(self, incus) -> bool | None:
        return not self.stopped() if self.process_visible else None

    # The deployment's make targets.
    def up(self, state_dir: Path, clock: FakeClock) -> MakeRun:
        self.state_dir = state_dir
        self.startup = (state_dir / "startup.json").read_text()
        run = MakeRun("up", started=clock.now())
        clock.t += 30.0
        if self.sim_host_starts:
            run.sim_host_started = self.sim_host_start = clock.now()
        clock.t += 1.0
        run.ended, run.returncode = clock.now(), self.up_status
        run.tail = ["Done."] if self.up_status == 0 else ["ERROR: sim-host failed"]
        self.running = self.up_status == 0
        return run

    def down(self, clock: FakeClock) -> MakeRun:
        run = MakeRun("down", started=clock.now())
        clock.t += 10.0
        run.ended = clock.now()
        self.running = False
        self.downs += 1
        return run

    # Incus.
    def instances(self) -> list[dict]:
        names = list(self.manifest["nodes"]) if self.running else self.left_after_down
        return [
            {"name": n, "config": {"volatile.base_image": f"fp-{n}"}} for n in names
        ]

    def step_session(self, node: str, *argv: str) -> list[str]:
        return ["incus", "exec", node, "--", *argv]

    def attack_session(self, node: str, binary: str) -> list[str]:
        return ["incus", "exec", node, "--", "paced", binary]

    def session(self, argv: list[str]) -> IdleDefender:
        # The launcher, with the program that the episode runner's test found
        # (episode-runner-impl.md §1, competition images).
        tested = self.defender_test.stdout.strip()
        assert argv == [
            "incus",
            "exec",
            "siem-01",
            "--",
            "/usr/local/bin/defender",
            tested,
        ]
        defender = IdleDefender(self.clock, self.track, self.alarm_at, self.alarm_delay)
        self.defenders.append(defender)
        return defender

    def stopped(self) -> bool:
        return self.stop_at is not None and self.clock.now() >= self.stop_at

    def log_time(self, t: float) -> str:
        return utc_text(self.clock.utc(t))

    def exec(self, node: str, *argv: str, timeout_s: float = 60.0) -> Result:
        since = self.clock.now() - self.sim_host_start
        if node == "adversary" and argv[:3] == ("sh", "-c", episode.BINARY_CHECK):
            self.binary_checks.append(argv[3])
            return Result(0, "present\n" if self.binary_present else "absent\n", "")
        if argv == ("rc-service", "sim", "status"):
            crashed = self.stopped() and self.status_sees_stop
            return Result(0, f" * status: {'crashed' if crashed else 'started'}\n", "")
        if argv == ("cat", settle.SIM_LOG):
            self.log_reads.append(self.clock.now())
            return Result(0, self.sim_log(until=self.clock.now()), "")
        if argv[:1] == ("grep",) and argv[-1] == settle.SIM_LOG:
            return Result(0, self.start_state() + "\n", "")
        if argv == (settle.AGENT_CONTROL, "-l"):
            lines = [
                f"   ID: {n:03d}, Name: {host}, IP: any, "
                + ("Never connected" if host in self.agents_down else "Active")
                for n, host in enumerate(MONITORED, start=1)
            ]
            return Result(0, "\n".join(lines) + "\n", "")
        if argv[:3] == (settle.AGENT_CONTROL, "-r", "-u"):
            self.scan_requests.append((self.clock.now(), argv[3]))
            return Result(0, f"Restarting Syscheck/Rootcheck on agent: {argv[3]}\n", "")
        if argv[:1] == ("grep",) and argv[-1] == settle.OSSEC_LOG:
            # An agent kept down never restarts or scans.
            lines = [(25.0, settle.RELOAD), (30.0, settle.SCAN_ENDED)]
            if node in self.agents_down:
                lines = []
            out = "".join(
                f"{n}:2026/11/04 01:00:00 {text}\n"
                for n, (at, text) in enumerate(lines, start=1)
                if since >= at
            )
            return Result(0 if out else 1, out, "")
        if (argv[:1] == ("grep",) and argv[-1] == settle.ARCHIVES) or (
            argv[:3] == ("sh", "-c", settle.LATEST)
        ):
            out = "".join(
                json.dumps({"timestamp": "t", "agent": {"name": host}}) + "\n"
                for host in MONITORED
                if host not in self.agents_down
            )
            return Result(0, out, "")
        if argv[:2] == ("systemctl", "is-active") and argv[2:] != (
            settle.SURICATA_UNIT,
        ):
            states = [
                "inactive" if (node, unit) in self.services_down else "active"
                for unit in argv[2:]
            ]
            out = "".join(f"{s}\n" for s in states)
            return Result(0 if set(states) == {"active"} else 3, out, "")
        if argv == ("rc-service", "haproxy", "status"):
            return Result(0, " * status: started\n", "")
        if argv == ("systemctl", "is-active", settle.SURICATA_UNIT):
            return Result(0, "active\n", "")
        if argv[:3] == ("sh", "-c", export.LIST):
            # The export's list of the manager's dated files of the episode's
            # days, each of which exists as the manager wrote it.
            self.listed = list(argv[5:])
            return Result(0, "".join(f"{name}.json\n" for name in argv[5:]), "")
        if argv[:3] == ("sh", "-c", volume.DIGEST):
            if not {volume.TRAINING_DEVICE, volume.EVALUATION_DEVICE} & set(
                self.devices
            ):
                return Result(3, "", f"sh: cd: can't cd to {argv[-1]}")
            return Result(0, f"{self.versions[bool(self.defenders)]}  -\n", "")
        if argv[:1] == ("grep",) and argv[-1] == settle.SURICATA_LOG:
            return Result(0, "Notice: threads: Threads created   Engine started.\n", "")
        if node == settle.SIEM and argv[:3] == (
            "/usr/local/bin/siem-read",
            "--start",
            "--step-0",
        ):
            assert argv[4:] == ("--step-s", "5")
            # The step session has opened, and no decision request has gone
            # out, when the query interface's start runs.
            assert self.defenders and not any(d.sent for d in self.defenders)
            self.reads_step_0.append(argv[3])
            self.reads_started.append(self.clock.now())
            self.clock.t += self.call_s
            if self.reads_start_fails:
                return Result(1, "", "siem-read: /var/ossec/logs/alerts: not found")
            return Result(0, "", "")
        if node == settle.SIEM and argv == (
            "sh",
            "-c",
            episode.DEFENDER_TEST,
            "sh",
            "/var/lib/entry/defender",
            "/usr/local/bin/idle-defender",
        ):
            # The test of the entry's defender comes before the step session.
            assert not self.defenders
            self.defender_tested_at.append(self.clock.now())
            self.clock.t += self.call_s
            return self.defender_test
        raise AssertionError(f"an unexpected call on {node}: {argv}")

    def sim_log(self, until: float | None = None) -> str:
        """The simulator's log, with its line "sim shutting down" once the
        simulator has stopped and the line is in the log, by the time until
        when it is given."""
        lines = [self.start_state()]
        written = self.stop_at if self.stop_written_at is None else self.stop_written_at
        if self.stop_at is not None and (until is None or until >= written):
            logged = self.logged_stop_at
            lines.append(
                f"{self.log_time(self.stop_at if logged is None else logged)} "
                f"ERROR sim::orchestrator: sim shutting down cause={self.stop_cause}"
            )
        return "\n".join(lines) + "\n"

    def start_state(self) -> str:
        return (
            f"{self.log_time(self.sim_host_start + 0.9)}  INFO start_state: "
            'start_state sim_tug_arrivals=1 delivery_state="no order placed"'
        )

    def query(self, path: str, timeout_s: float = 60.0) -> dict:
        assert path == f"/1.0/instances/{settle.SIEM}"
        return {"devices": dict(self.devices)}

    def command(self, *args: str, timeout_s: float = 60.0) -> Result:
        self.commands.append((self.clock.now(), args))
        if args[:4] == ("config", "device", "add", settle.SIEM):
            if self.attach_fails:
                return Result(1, "", "Error: Failed to add device")
            self.devices[args[4]] = dict(a.split("=", 1) for a in args[6:])
            return Result(0, "", "")
        if args == ("config", "device", "remove", settle.SIEM, volume.TRAINING_DEVICE):
            del self.devices[volume.TRAINING_DEVICE]
            return Result(0, "", "")
        raise AssertionError(f"an unexpected command: {args}")

    def pull(
        self, node: str, path: str, dest: Path, timeout_s: float = 300.0
    ) -> Result:
        if path == self.failing_pull:
            return Result(1, "", f"Error: open {path}: no such file or directory")
        if path == settle.SIM_LOG:
            dest.write_text(self.sim_log())
        else:
            dest.write_text("{}\n")
        return Result(0, "", "")


def run(fake: FakeTestbed, tmp_path: Path, **options) -> episode.Outcome:
    return episode.run_episode(
        fake.testbed(), SCENARIO, SEED, fake.track, tmp_path, **options
    )


def adversarial(fake: FakeTestbed, tmp_path: Path, **options) -> episode.Outcome:
    """An adversarial episode of CHAIN, whose binary sits in a directory of its
    own beside the episode records."""
    dist = tmp_path / "dist"
    dist.mkdir(exist_ok=True)
    (dist / CHAIN.id).write_bytes(BINARY)
    fake.manifest = WITH_ADVERSARY
    return run(
        fake,
        tmp_path / "records",
        episode_type=episode.ADVERSARIAL,
        chain=CHAIN,
        chosen={"by": "the command line", "library": [CHAIN.id]},
        dist=dist,
        **options,
    )


def episode_json(outcome: episode.Outcome) -> dict:
    return json.loads((outcome.record / "episode.json").read_text())


def steps_jsonl(outcome: episode.Outcome) -> list[dict]:
    text = (outcome.record / "steps.jsonl").read_text()
    return [json.loads(line) for line in text.splitlines()]


def test_a_benign_episode_runs_from_plan_to_reset(tmp_path: Path) -> None:
    fake = FakeTestbed(FakeClock())
    outcome = run(fake, tmp_path)
    assert (outcome.outcome, outcome.cause) == ("horizon", None)
    record = episode_json(outcome)
    assert record["episode_seed"] == SEED
    assert record["seeds"] == derive_seeds(SEED)
    assert (record["type"], record["mode"], record["suppressed"]) == (
        "benign",
        "evaluation",
        False,
    )
    assert record["track"] == "A"
    assert record["defender"] == "/usr/local/bin/defender"
    # The launcher ran the idle defender, the volume holding no defender.
    assert record["defender_program"] == "/usr/local/bin/idle-defender"
    assert record["track_a"] == {
        "alarm": None,
        "K": None,
        "k_n": None,
        "R_n": 0.0,
        "outcome": "none",
        "end_cause": "horizon",
    }
    # The query interface's start ran at step 0, before the decision request
    # of step 0 (FakeTestbed.exec checks the order).
    step_0 = fake.clock.monotonic(datetime.fromisoformat(record["settle"]["step_0"]))
    assert fake.reads_started == [step_0]
    assert record["query_interface_start"]["exit_status"] == 0
    # A discarded episode is not run again, so no record numbers a run or
    # names a replaced seed.
    assert "run" not in record and "replaced_seeds" not in record
    assert record["scenario"]["compression_factor"] == 5
    assert record["scenario"]["horizon_steps"] == 144
    assert record["scenario"]["settle_timeout_s"] == 120.0
    assert record["profile"] == "ci-minimal"
    assert record["images"]["siem-01"] == {
        "image": "ravensport/siem-01-competition:latest",
        "fingerprint": "fp-siem-01",
    }
    assert [c["condition"] for c in record["settle"]["conditions"]] == [1, 2, 3, 4]
    assert record["steps"]["run"] == 144
    assert record["steps"]["defender_exit_status"] == 0
    # The episode's days start at step 0, on 4 November.
    assert record["export"] == {
        "alerts/2026/Nov/ossec-alerts-04.json": None,
        "archives/2026/Nov/ossec-archive-04.json": None,
        "reads.jsonl": None,
        "sim.log": None,
    }
    assert record["reset"]["remaining"] == []
    assert list(record["durations_s"]) == [
        "plan",
        "deploy",
        "settle",
        "steps",
        "end",
        "export",
        "reset",
    ]
    assert record["durations_s"]["export"] == export.FLUSH_WAIT_S
    assert (record["outcome"], record["cause"]) == ("horizon", None)

    lines = steps_jsonl(outcome)
    assert [line["step"] for line in lines] == list(range(144))
    assert all(line["decision"] == "no action" for line in lines)
    assert all(line["decision_arrival"] is not None for line in lines)
    startup = startup_input(SEED, SCENARIO.timings, EPOCH)
    assert (outcome.record / "startup.json").read_text() == startup
    assert fake.startup == startup
    assert fake.state_dir is not None and not fake.state_dir.exists()
    # Fork 0 runs as in evaluation, so the end notice carries no reward, and
    # the record holds R_n, 0 in a benign episode without an alarm.
    assert fake.defenders[0].sent[-1] == {
        "track": "A",
        "step": 144,
        "previous_reward": None,
        "ended": True,
    }
    assert lines[-1]["reward"] == 0.0
    assert fake.downs == 1


def test_each_scan_inside_the_episode_is_requested_at_its_time(tmp_path: Path) -> None:
    fake = FakeTestbed(FakeClock())
    record = episode_json(run(fake, tmp_path))
    step_0 = datetime.fromisoformat(record["settle"]["step_0"])
    end = step_0 + timedelta(seconds=720)
    inside = sorted(
        (datetime.fromisoformat(at), host)
        for host, at in record["first_scans"].items()
        if step_0 <= datetime.fromisoformat(at) < end
    )
    assert inside, "no first scan falls inside the episode"
    assert [(r["host"], r["sent"]) for r in record["scan_requests"]] == [
        (host, utc_text(at)) for at, host in inside
    ]
    assert all(r["ok"] for r in record["scan_requests"])
    assert [agent for _, agent in fake.scan_requests] == [
        f"{MONITORED.index(host) + 1:03d}" for _, host in inside
    ]


def test_a_failed_deployment_discards_the_episode(tmp_path: Path) -> None:
    fake = FakeTestbed(FakeClock())
    fake.up_status = 2
    fake.sim_host_starts = False
    outcome = run(fake, tmp_path)
    assert outcome.outcome == "discarded"
    assert "exit status 2" in outcome.cause
    assert "ERROR: sim-host failed" in outcome.cause
    assert not (outcome.record / "steps.jsonl").exists()
    assert fake.downs == 1


def test_a_deployment_that_fails_after_starting_sim_host_is_discarded(
    tmp_path: Path,
) -> None:
    fake = FakeTestbed(FakeClock())
    fake.up_status = 1
    outcome = run(fake, tmp_path)
    assert outcome.outcome == "discarded"
    assert "exit status 1" in outcome.cause


# The dock operator's request that gets a response it does not expect: a
# refused request whose refusal carries no code of the FMS's refusals.
UNEXPECTED = "dock_operator: POST /api/jobs/refuel unexpected HTTP 401 Unauthorized: "


def test_an_unexpected_response_terminates_the_episode(tmp_path: Path) -> None:
    """A request of the dock operator that gets an unexpected response stops
    the simulator 200 s after the deployment began, inside the episode: the
    episode ends with the outcome terminated, the response as its cause, and
    its score, Track A's reward of a benign episode without an alarm, which
    the end notice carries (episodes-tests.md,
    an_unexpected_response_terminates_the_episode; episodes.md §12,
    terminated and discarded episodes)."""
    fake = FakeTestbed(FakeClock())
    fake.stop_at, fake.stop_cause = 200.0, UNEXPECTED
    outcome = run(fake, tmp_path)
    assert (outcome.outcome, outcome.cause) == ("terminated", UNEXPECTED)
    record = episode_json(outcome)
    assert (record["outcome"], record["cause"]) == ("terminated", UNEXPECTED)
    stop = record["simulator_stop"]
    assert stop["cause"] == UNEXPECTED
    assert stop["at"] == utc_text(fake.clock.utc(200.0))
    assert record["track_a"] == {
        "alarm": None,
        "K": None,
        "k_n": None,
        "R_n": 0.0,
        "outcome": "none",
        "end_cause": "simulator_stop",
    }
    lines = steps_jsonl(outcome)
    assert len(lines) == record["steps"]["run"] < 144
    notice = fake.defenders[0].sent[-1]
    assert (notice["step"], notice["ended"]) == (len(lines), True)
    assert notice["previous_reward"] is None


def test_a_simulator_stop_ends_the_record_at_its_step(
    tmp_path: Path,
) -> None:
    """The simulator stops in step 12, from 166 s to 171 s, and the watch fires
    only after step 13 has begun. The end notice counts 14 steps, and the
    episode record counts the 13 steps that began before the stop."""
    fake = FakeTestbed(FakeClock())
    fake.stop_at, fake.logged_stop_at = 172.0, 169.0
    outcome = run(fake, tmp_path)
    assert outcome.outcome == "terminated"
    record = episode_json(outcome)
    assert record["steps"]["run"] == 13
    assert record["steps"]["end_notice_step"] == 14
    assert fake.defenders[0].sent[-1]["step"] == 14
    lines = steps_jsonl(outcome)
    assert [line["step"] for line in lines] == list(range(13))
    assert lines[-1]["end"] == utc_text(fake.clock.utc(169.0))


def test_without_a_watch_the_simulator_is_checked_in_each_step(tmp_path: Path) -> None:
    """The check in the middle of step 13 sees the stop at 170 s."""
    fake = FakeTestbed(FakeClock())
    fake.watched = False
    fake.stop_at = 170.0
    outcome = run(fake, tmp_path)
    assert outcome.outcome == "terminated"
    record = episode_json(outcome)
    assert record["steps"]["stop_detection"] == "status check in each step"
    assert record["steps"]["ended_early"] == utc_text(fake.clock.utc(173.5))
    assert record["steps"]["run"] == 13


def test_a_simulator_defect_discards_the_episode(tmp_path: Path) -> None:
    fake = FakeTestbed(FakeClock())
    fake.stop_at = 200.0
    fake.stop_cause = "dock_operator: job channel closed"
    outcome = run(fake, tmp_path)
    assert (outcome.outcome, outcome.cause) == (
        "discarded",
        "dock_operator: job channel closed",
    )


def test_a_simulator_that_logs_no_cause_discards_the_episode(tmp_path: Path) -> None:
    """A simulator that is killed logs no cause, so the episode record keeps
    what the watch on the simulator's process saw."""
    fake = FakeTestbed(FakeClock())
    fake.stop_at = 200.0
    fake.stop_cause = ""
    original = fake.pull

    def pull_without_a_cause(node, path, dest, timeout_s=300.0):
        result = original(node, path, dest, timeout_s)
        if path == settle.SIM_LOG:
            dest.write_text(fake.start_state() + "\n")
        return result

    fake.pull = pull_without_a_cause
    outcome = run(fake, tmp_path)
    assert outcome.outcome == "discarded"
    assert outcome.cause == (
        "the simulator's process exited, and its log holds no cause"
    )


def test_an_interrupted_episode_leaves_the_deployment_up(tmp_path: Path) -> None:
    fake = FakeTestbed(FakeClock())

    def interrupt(argv):
        raise KeyboardInterrupt

    testbed = fake.testbed()
    testbed.session = interrupt
    with pytest.raises(KeyboardInterrupt):
        episode.run_episode(testbed, SCENARIO, SEED, "A", tmp_path)
    (record,) = tmp_path.iterdir()
    assert json.loads((record / "episode.json").read_text())["outcome"] == "interrupted"
    assert fake.downs == 0


def test_a_simulator_stop_before_step_0_discards_the_episode(tmp_path: Path) -> None:
    """A simulator that stops during the settle delay is first seen stopped in
    step 0, and the time in its log places the stop before step 0."""
    fake = FakeTestbed(FakeClock())
    fake.stop_at = 70.0
    outcome = run(fake, tmp_path)
    assert outcome.outcome == "discarded"
    assert outcome.cause.startswith("the simulator stopped before step 0: ")


EARLY_EOF = "fuel_supplier: ATG poll failed after 3 retries: early eof"


@pytest.mark.parametrize(
    ("kept_down", "unmet"),
    [
        ("the Wazuh agent of corp-it", "condition 1, condition 2"),
        ("the tank-gauge relay on atg-relay-01", "condition 4"),
    ],
)
def test_a_deployment_that_never_settles_is_discarded(
    tmp_path: Path, kept_down: str, unmet: str
) -> None:
    """A Wazuh agent kept down never connects, restarts or scans, and with
    the tank-gauge relay on atg-relay-01 kept down, the fuel supplier's polls
    have no path. Either way the settle timeout expires 120 s after sim-host
    starts: the episode is discarded as a failed deployment, its record
    states the conditions that still failed, no step session opens, and the
    reset runs (episodes-tests.md, a_deployment_that_never_settles_is_discarded;
    episodes.md §6.6, settle period; §12, terminated and discarded
    episodes)."""
    fake = FakeTestbed(FakeClock())
    if kept_down.startswith("the Wazuh agent"):
        fake.agents_down = {"corp-it"}
    else:
        fake.services_down = {("atg-relay-01", "haproxy")}
    outcome = run(fake, tmp_path)
    cause = (
        f"the deployment failed to settle: {unmet} still failed at the settle "
        "timeout of 120 s"
    )
    assert (outcome.outcome, outcome.cause) == ("discarded", cause)
    record = episode_json(outcome)
    assert (record["outcome"], record["cause"]) == ("discarded", cause)
    assert record["scenario"]["settle_timeout_s"] == 120.0
    assert "settle" not in record
    assert not fake.defenders and not fake.reads_started
    assert fake.downs == 1


def test_without_a_watch_a_stop_before_step_0_is_read_from_the_log(
    tmp_path: Path,
) -> None:
    """The simulator stops 9 s after sim-host starts, OpenRC still reports it
    started, and no watch can be opened. The log on sim-host holds the stop, so
    the deployment fails before any step runs."""
    fake = FakeTestbed(FakeClock())
    fake.watched, fake.status_sees_stop = False, False
    fake.stop_at, fake.stop_cause = 40.0, EARLY_EOF
    outcome = run(fake, tmp_path)
    assert (outcome.outcome, outcome.cause) == (
        "discarded",
        f"the simulator stopped before step 0: {EARLY_EOF}",
    )
    assert fake.defenders == []


def test_without_a_watch_a_gone_process_ends_the_episode(tmp_path: Path) -> None:
    """OpenRC still reports the simulator started, but the check in the middle
    of step 13 sees that its process has gone."""
    fake = FakeTestbed(FakeClock())
    fake.watched, fake.status_sees_stop, fake.process_visible = False, False, True
    fake.stop_at = 170.0
    outcome = run(fake, tmp_path)
    assert outcome.outcome == "terminated"
    record = episode_json(outcome)
    assert record["steps"]["ended_early"] == utc_text(fake.clock.utc(173.5))
    assert record["steps"]["run"] == 13


def test_a_stop_in_the_steps_that_the_run_missed_is_found_in_the_log(
    tmp_path: Path,
) -> None:
    """The watch misses a stop at 200 s, so the steps run to the horizon. The
    log after the steps holds the stop, and the episode ends in its step."""
    fake = FakeTestbed(FakeClock())
    fake.watch_sees_stop = False
    fake.stop_at = 200.0
    outcome = run(fake, tmp_path)
    assert (outcome.outcome, outcome.cause) == ("terminated", CAVITATION)
    record = episode_json(outcome)
    assert record["simulator_stop"]["detected"] is None
    assert record["simulator_stop"]["at"] == utc_text(fake.clock.utc(200.0))
    lines = steps_jsonl(outcome)
    assert len(lines) == record["steps"]["run"] < 144
    assert lines[-1]["end"] == utc_text(fake.clock.utc(200.0))


def test_a_stop_before_step_0_that_the_run_missed_discards_the_episode(
    tmp_path: Path,
) -> None:
    fake = FakeTestbed(FakeClock())
    fake.watch_sees_stop = False
    fake.stop_at, fake.stop_cause = 70.0, EARLY_EOF
    outcome = run(fake, tmp_path)
    assert (outcome.outcome, outcome.cause) == (
        "discarded",
        f"the simulator stopped before step 0: {EARLY_EOF}",
    )


def test_a_stop_after_the_last_step_leaves_the_episode_as_it_ended(
    tmp_path: Path,
) -> None:
    fake = FakeTestbed(FakeClock())
    fake.watch_sees_stop = False
    fake.stop_at = 10_000.0
    outcome = run(fake, tmp_path)
    assert outcome.outcome == "horizon"
    assert "simulator_stop" not in episode_json(outcome)


def test_a_failed_export_discards_the_episode(tmp_path: Path) -> None:
    fake = FakeTestbed(FakeClock())
    fake.failing_pull = "/var/ossec/logs/archives/2026/Nov/ossec-archive-04.json"
    outcome = run(fake, tmp_path)
    assert outcome.outcome == "discarded"
    assert outcome.cause.startswith(
        "the export failed: archives/2026/Nov/ossec-archive-04.json: "
    )


def test_a_failed_reset_stops_the_episode_runner(tmp_path: Path) -> None:
    fake = FakeTestbed(FakeClock())
    fake.left_after_down = ["siem-01"]
    with pytest.raises(episode.ResetFailure, match="siem-01"):
        run(fake, tmp_path)
    (record,) = tmp_path.iterdir()
    assert json.loads((record / "episode.json").read_text())["reset"]["remaining"] == [
        "siem-01"
    ]


def test_the_scenario_follows_the_competition_override_file(tmp_path: Path) -> None:
    """The compression factor comes from the competition's additional override
    file, and the compressed scan interval is the agents' own frequency, which
    the render pipeline computes from world.yaml at that factor, as it
    computes the simulator's timings."""
    assert episode.Scenario.from_files() == episode.Scenario(
        5, 12 * 3600 // 5, predicted_events.competition_timings()
    )
    override = tmp_path / "override.yaml"
    override.write_text("compression_factor: 1\n")
    assert episode.Scenario.from_files(override=override) == episode.Scenario(
        1, 12 * 3600, predicted_events.competition_timings(override=override)
    )


def test_the_scenario_reads_the_file_that_the_deployment_applies() -> None:
    """The episode runner reads the compression factor from the file that it
    names in EXTRA_OVERRIDE for every deployment, the competition's override
    file."""
    applied = (episode.deploy.DEPLOY / episode.deploy.EXTRA_OVERRIDE).resolve()
    competition = episode.deploy.DEPLOY.parent / "competition" / "deploy"
    assert applied == competition / "override.yaml"
    read = inspect.signature(episode.Scenario.from_files).parameters["override"]
    assert read.default.resolve() == applied


def test_an_adversarial_episode_starts_its_chain_at_the_onset_step(
    tmp_path: Path,
) -> None:
    """A suppressed episode, whose last stage runs (episodes.md §9,
    suppressed episodes for Competition Rule 5)."""
    fake = FakeTestbed(FakeClock())
    outcome = adversarial(fake, tmp_path, suppressed=True)
    assert (outcome.outcome, outcome.cause) == ("horizon", None)
    record = episode_json(outcome)
    assert record["type"] == "adversarial"
    assert record["suppressed"] is True
    assert record["track_a"] == {
        "alarm": None,
        "K": 3,
        "k_n": 3,
        "R_n": -1.0,
        "outcome": 4,
        "end_cause": "horizon",
    }
    seeds = derive_seeds(SEED)
    onset = pacing.onset_step(seeds[ATTACK_START_STEP])
    waits = pacing.waits(seeds[ATTACK_WAITS], len(CHAIN.stages))
    attack = record["attack"]
    assert attack["chain"]["id"] == CHAIN.id
    assert attack["chosen"] == {"by": "the command line", "library": [CHAIN.id]}
    assert attack["sha256"] == hashlib.sha256(BINARY).hexdigest()
    assert (attack["onset_step"], attack["waits_seed"]) == (onset, seeds[ATTACK_WAITS])
    assert attack["waits_s"] == [round(w, 3) for w in waits]
    assert attack["wait_bounds_s"] == {"lower": 5.0, "mean": 5.0, "upper": 20.0}
    step_0 = datetime.fromisoformat(record["settle"]["step_0"])
    assert attack["onset"] == utc_text(step_0 + timedelta(seconds=onset * 5))

    (chain,) = fake.attacks
    assert chain.command == [
        "incus",
        "exec",
        "adversary",
        "--",
        "paced",
        f"/root/{CHAIN.id}",
    ]
    assert chain.waits_s == waits
    assert chain.run.started == fake.clock.monotonic(step_0) + onset * 5
    # The chain stops once the steps have ended and the end notice has gone.
    assert chain.stopped_at >= fake.clock.monotonic(step_0) + 720
    session = attack["session"]
    assert [(e["event"], e["stage_number"]) for e in session["events"]] == [
        (kind, n) for n in (1, 2, 3) for kind in ("begin", "end")
    ]
    assert [
        e["target"]["node"] for e in session["events"] if e["event"] == "begin"
    ] == [
        "vnet-it",
        "corp-it",
        "adversary",
    ]
    assert session["stages_begun"] == 3
    assert session["exit_status"] == 0
    assert attack["early_stop"] is None
    assert (outcome.record / "attack-chain.log").read_text() == "[*] scan-it-segment\n"


def stage_times() -> tuple[float, float, float, float]:
    """The onset and the release of stages 1 and 2 of CHAIN on the fake
    clock, and the time at which stage 3 falls due, each after step 0, with
    FakeAttack's stages of half a second."""
    seeds = derive_seeds(SEED)
    onset = pacing.onset_step(seeds[ATTACK_START_STEP]) * 5.0
    waits = pacing.waits(seeds[ATTACK_WAITS], len(CHAIN.stages))
    first = onset + waits[0]
    second = first + 0.5 + waits[1]
    return onset, first, second, second + 0.5 + waits[2]


def test_a_track_a_episode_ends_when_its_last_stage_falls_due(
    tmp_path: Path,
) -> None:
    """episodes-tests.md, a_track_a_episode_ends_at_the_start_of_the_last_stage."""
    fake = FakeTestbed(FakeClock())
    outcome = adversarial(fake, tmp_path)
    assert (outcome.outcome, outcome.cause) == ("last_stage", None)
    record = episode_json(outcome)
    assert record["suppressed"] is False
    step_0 = fake.clock.monotonic(datetime.fromisoformat(record["settle"]["step_0"]))
    _, first, second, last = stage_times()
    (chain,) = fake.attacks
    assert chain.run.releases == [step_0 + first, step_0 + second]
    assert chain.stopped_at == step_0 + last
    assert record["steps"]["ended_early"] == utc_text(fake.clock.utc(step_0 + last))
    assert record["steps"]["run"] == int(last // 5) + 1
    assert record["attack"]["early_stop"] == {
        "stage_number": 3,
        "stage": CHAIN.stages[2].name,
        "cause": "the episode ended before the stage began",
    }
    assert record["track_a"] == {
        "alarm": None,
        "K": 3,
        "k_n": 3,
        "R_n": -1.0,
        "outcome": 4,
        "end_cause": "last_stage",
    }
    assert fake.defenders[0].sent[-1] == {
        "track": "A",
        "step": int(last // 5) + 1,
        "previous_reward": None,
        "ended": True,
    }


def test_a_track_a_alarm_ends_the_episode_at_its_arrival(tmp_path: Path) -> None:
    """The alarm arrives in the first step after stage 2's release, so k_n is
    2 and R_n is -(2 - 1)/(3 - 1); the end notice follows at once with the
    alarm's step plus 1. Fork 0 runs as in evaluation, so the step messages
    carry no reward, and the episode record holds R_n (episodes-tests.md,
    a_track_a_alarm_ends_the_episode_at_its_arrival, the_end_notice_carries_r_n)."""
    _, _, second, _ = stage_times()
    fake = FakeTestbed(FakeClock())
    fake.alarm_at = int(second // 5) + 1
    outcome = adversarial(fake, tmp_path)
    assert (outcome.outcome, outcome.cause) == ("alarm", None)
    record = episode_json(outcome)
    step_0 = fake.clock.monotonic(datetime.fromisoformat(record["settle"]["step_0"]))
    arrival = step_0 + fake.alarm_at * 5 + 0.01
    assert record["track_a"] == {
        "alarm": {"step": fake.alarm_at, "arrival": utc_text(fake.clock.utc(arrival))},
        "K": 3,
        "k_n": 2,
        "R_n": -0.5,
        "outcome": 1,
        "end_cause": "alarm",
    }
    lines = steps_jsonl(outcome)
    assert [line["step"] for line in lines] == list(range(fake.alarm_at + 1))
    assert lines[-1]["decision"] == "alarm"
    assert [line["reward"] for line in lines] == [0.0] * fake.alarm_at + [-0.5]
    notice = fake.defenders[0].sent[-1]
    assert notice == {
        "track": "A",
        "step": fake.alarm_at + 1,
        "previous_reward": None,
        "ended": True,
    }
    assert all(n["previous_reward"] is None for n in fake.defenders[0].sent)
    (chain,) = fake.attacks
    assert len(chain.run.releases) == 2
    assert chain.stopped_at == arrival


@pytest.mark.parametrize(
    ("ending", "R_n", "K"),
    [
        ("the horizon of a benign episode", 0.0, None),
        ("an alarm after stage 1", 0.0, 3),
        ("an alarm after stage 2", -0.5, 3),
        ("the start of the last stage, with no alarm", -1.0, 3),
    ],
)
def test_track_a_reward_is_zero_until_the_end(
    tmp_path: Path, ending: str, R_n: float, K: int | None
) -> None:
    """In Track A every step's reward is 0, and the final step carries R_n,
    the episodic reward of Track A, with K the attack chain's own number of
    stages, 3 for CHAIN. The episode runner computes R_n from ground truth
    that it records itself, the stages of the attack chain that it released
    before the alarm, with no judgement of the operational correctness
    conditions. Fork 0 runs as in evaluation, so the episode record holds the
    rewards, and no step message carries one (episodes-tests.md,
    track_a_reward_is_zero_until_the_end; episodes.md §7, step protocol;
    §10.4, the attack chain's session)."""
    _, first, second, _ = stage_times()
    fake = FakeTestbed(FakeClock())
    if ending.startswith("an alarm after stage 1"):
        fake.alarm_at = int(first // 5) + 1
    elif ending.startswith("an alarm after stage 2"):
        fake.alarm_at = int(second // 5) + 1
    outcome = run(fake, tmp_path) if K is None else adversarial(fake, tmp_path)
    record = episode_json(outcome)
    assert (record["track_a"]["K"], record["track_a"]["R_n"]) == (K, R_n)
    rewards = [line["reward"] for line in steps_jsonl(outcome)]
    assert rewards == [0.0] * (len(rewards) - 1) + [R_n]
    sent = fake.defenders[0].sent
    assert all(n["previous_reward"] is None for n in sent)
    assert sent[-1]["ended"] is True


def test_an_alarm_before_the_first_release_scores_outcome_3(tmp_path: Path) -> None:
    fake = FakeTestbed(FakeClock())
    fake.alarm_at = 1
    outcome = adversarial(fake, tmp_path)
    record = episode_json(outcome)
    assert (record["track_a"]["k_n"], record["track_a"]["outcome"]) == (0, 3)
    assert record["track_a"]["R_n"] == -1.0
    assert record["attack"]["early_stop"]["cause"] == (
        "the episode ended before the onset step"
    )


def test_run_attaches_the_entry_volume_read_only(tmp_path: Path) -> None:
    """At fork 0 the entry's submitted volume, given to the run command, is
    attached read-only to siem-01 at /var/lib/entry straight after the
    deployment, before the settle conditions and the test of the entry's
    defender, and the record keeps its source and version (episodes.md §3.2,
    modes; episodes-tests.md §0.2, a test defender)."""
    directory = tmp_path / "volume"
    directory.mkdir()
    (directory / "defender").write_text("#!/bin/sh\n")
    (directory / "defender").chmod(0o700)
    fake = FakeTestbed(FakeClock())
    fake.defender_test = Result(0, "/var/lib/entry/defender\n", "")
    outcome = run(fake, tmp_path / "records", entry_volume=directory)
    assert (outcome.outcome, outcome.cause) == ("horizon", None)
    assert fake.devices["entry-volume"] == {
        "source": str(directory),
        "path": "/var/lib/entry",
        "readonly": "true",
    }
    ((at, command),) = fake.commands
    assert command[:5] == ("config", "device", "add", "siem-01", "entry-volume")
    assert at < fake.defender_tested_at[0]
    record = episode_json(outcome)
    assert record["entry_volume"] == {
        "source": str(directory),
        "version": "sha256:" + "a" * 64,
    }
    assert record["defender_program"] == "/var/lib/entry/defender"


def test_a_volume_that_cannot_be_attached_discards_the_episode(tmp_path: Path) -> None:
    directory = tmp_path / "volume"
    directory.mkdir()
    fake = FakeTestbed(FakeClock())
    fake.attach_fails = True
    outcome = run(fake, tmp_path / "records", entry_volume=directory)
    assert outcome.outcome == "discarded"
    assert outcome.cause.startswith(f"the entry's volume {directory} was not attached")
    assert fake.defenders == []


def test_the_record_names_the_volumes_defender(tmp_path: Path) -> None:
    """With the entry's defender in its storage volume, the record names
    /var/lib/entry/defender as the program that ran (episodes-tests.md,
    a_volumes_defender_runs_in_place_of_the_idle_defender)."""
    fake = FakeTestbed(FakeClock())
    fake.defender_test = Result(0, "/var/lib/entry/defender\n", "")
    outcome = run(fake, tmp_path)
    assert (outcome.outcome, outcome.cause) == ("horizon", None)
    assert episode_json(outcome)["defender_program"] == "/var/lib/entry/defender"


def test_the_record_names_the_idle_defender_for_a_volume_without_one(
    tmp_path: Path,
) -> None:
    """episodes-tests.md, a_volume_without_a_defender_runs_the_idle_defender."""
    fake = FakeTestbed(FakeClock())
    outcome = run(fake, tmp_path)
    record = episode_json(outcome)
    assert record["defender_program"] == "/usr/local/bin/idle-defender"
    assert all(line["decision"] == "no action" for line in steps_jsonl(outcome))


@pytest.mark.parametrize("cause", ["is not executable", "is a directory"])
def test_a_defender_that_cannot_run_discards_the_episode(
    tmp_path: Path, cause: str
) -> None:
    """A defender in the entry's volume that is not an executable regular
    file, which the episode runner's own test finds before the step session
    opens, discards the episode before step 0 with that cause (episodes-tests.md,
    a_defender_that_cannot_run_discards_the_episode; episodes.md §7, step
    protocol)."""
    fake = FakeTestbed(FakeClock())
    # The Incus client adds its own line for a command that exits with 126.
    fake.defender_test = Result(
        126,
        "",
        f"/var/lib/entry/defender {cause}\nError: Command not executable\n",
    )
    outcome = run(fake, tmp_path)
    expected = f"the entry's defender cannot run: /var/lib/entry/defender {cause}"
    assert (outcome.outcome, outcome.cause) == ("discarded", expected)
    record = episode_json(outcome)
    assert (record["outcome"], record["cause"]) == ("discarded", expected)
    assert record["defender_program"] is None
    # The step session never opened, and nothing ran at step 0.
    assert fake.defenders == []
    assert fake.reads_started == []
    step_0 = fake.clock.monotonic(datetime.fromisoformat(record["settle"]["step_0"]))
    assert fake.defender_tested_at == [step_0]
    assert "steps" not in record and "track_a" not in record


def test_a_failed_test_of_the_defender_discards_the_episode(tmp_path: Path) -> None:
    """A test that gives neither program nor the fault, as when siem-01
    cannot run it, is a failure of the testbed."""
    fake = FakeTestbed(FakeClock())
    fake.defender_test = Result(1, "", "Error: Instance is not running\n")
    outcome = run(fake, tmp_path)
    assert (outcome.outcome, outcome.cause) == (
        "discarded",
        "the test of /var/lib/entry/defender failed with status 1: "
        "Error: Instance is not running",
    )
    assert fake.defenders == []


def test_step_0_waits_for_siem_read_start(tmp_path: Path) -> None:
    """The episode runner opens the step session at step 0, after the test of
    the entry's defender, before it runs siem-read --start, and sends step 0's
    decision request only after the option has exited with status 0
    (episodes-tests.md, step_0_waits_for_siem_read_start; episodes.md §7, step
    protocol)."""
    fake = FakeTestbed(FakeClock())
    outcome = run(fake, tmp_path)
    record = episode_json(outcome)
    step_0 = fake.clock.monotonic(datetime.fromisoformat(record["settle"]["step_0"]))
    (defender,) = fake.defenders
    assert fake.defender_tested_at == [step_0]
    assert defender.opened_at == step_0 == fake.reads_started[0]
    assert defender.sent[0]["step"] == 0
    assert steps_jsonl(outcome)[0]["request_sent"] == utc_text(fake.clock.utc(step_0))


def test_siem_read_start_takes_step_0s_instant_from_the_episode_runner(
    tmp_path: Path,
) -> None:
    """The test of the defender and siem-read --start each take 0.35 s, so
    siem-read runs 0.35 s into step 0 and the decision request of step 0 goes
    out 0.7 s into it. siem-read still takes step 0's own instant, from the
    episode runner's clock, and numbers each read from it
    (episode-runner-impl.md §12, query interface)."""
    fake = FakeTestbed(FakeClock())
    fake.call_s = 0.35
    outcome = run(fake, tmp_path)
    record = episode_json(outcome)
    step_0 = fake.clock.monotonic(datetime.fromisoformat(record["settle"]["step_0"]))
    assert fake.reads_started == [step_0 + 0.35]
    assert fake.reads_step_0 == [utc_text(fake.clock.utc(step_0))]
    assert steps_jsonl(outcome)[0]["request_sent"] == utc_text(
        fake.clock.utc(step_0 + 0.7)
    )


def test_a_failed_siem_read_start_discards_the_episode_with_no_step_message(
    tmp_path: Path,
) -> None:
    """When siem-read --start exits with another status, the episode is
    discarded with that cause, and the defender's standard input closes with
    no step message (episodes-tests.md, step_0_waits_for_siem_read_start)."""
    fake = FakeTestbed(FakeClock())
    fake.reads_start_fails = True
    outcome = run(fake, tmp_path)
    assert outcome.outcome == "discarded"
    assert outcome.cause == (
        "siem-read --start exited with status 1: "
        "siem-read: /var/ossec/logs/alerts: not found"
    )
    (defender,) = fake.defenders
    assert defender.sent == []
    assert defender.closed_at == fake.reads_started[0]
    record = episode_json(outcome)
    assert record["query_interface_start"]["exit_status"] == 1
    assert record["defender_program"] == "/usr/local/bin/idle-defender"
    assert "track_a" not in record


# The two modes of a Track A episode in these tests: fork 0, which runs as in
# evaluation, and a fork in training mode, the one mode whose step messages
# carry rewards (episodes.md §7, step protocol). Step 0 falls 106 s after the
# fake simulator's start at fork 0, and 1 s after the fork instant in a fork.
FORK_0 = "evaluation at fork 0"
TRAINING_FORK = "training in a fork"
MODES = (FORK_0, TRAINING_FORK)
STEP_0 = {FORK_0: 106.0, TRAINING_FORK: 1.0}


def fake_in(mode: str) -> FakeTestbed:
    """The fake testbed of a benign Track A episode in the mode."""
    return (
        FakeTestbed(FakeClock()) if mode == FORK_0 else copy_of_living_port(FakeClock())
    )


def run_in(
    mode: str, fake: FakeTestbed, tmp_path: Path
) -> tuple[episode.Outcome, dict]:
    """Run the episode in the mode, and read its record."""
    if mode == FORK_0:
        outcome = run(fake, tmp_path)
        return outcome, episode_json(outcome)
    outcome = run_copy(
        fake,
        tmp_path,
        mode=volume.TRAINING,
        training_volume=training_directory(tmp_path),
    )
    return outcome, record_json(outcome)


@pytest.mark.parametrize("mode", MODES)
def test_the_end_notice_carries_r_n(tmp_path: Path, mode: str) -> None:
    """The test defender of a benign Track A episode raises a false alarm at
    step 20, so R_n = -1. In training mode every decision request after step
    0 carries a reward of 0, and the end notice carries R_n, equal to the
    episode record's. In evaluation mode, the end notice's previous_reward
    holds null, and the episode record holds R_n (episodes-tests.md,
    the_end_notice_carries_r_n; episodes.md §7, step protocol)."""
    fake = fake_in(mode)
    fake.alarm_at = 20
    outcome, record = run_in(mode, fake, tmp_path)
    assert (outcome.outcome, record["track_a"]["R_n"]) == ("alarm", -1.0)
    assert steps_jsonl(outcome)[-1]["reward"] == -1.0
    sent = fake.defenders[0].sent
    assert (sent[-1]["step"], sent[-1]["ended"]) == (21, True)
    if mode == TRAINING_FORK:
        assert sent[0]["previous_reward"] is None
        assert all(n["previous_reward"] == 0.0 for n in sent[1:-1])
        assert sent[-1]["previous_reward"] == record["track_a"]["R_n"]
    else:
        assert all(n["previous_reward"] is None for n in sent)


@pytest.mark.parametrize("mode", MODES)
def test_a_stop_found_at_the_alarm_ends_the_episode_as_a_simulator_stop(
    tmp_path: Path, mode: str
) -> None:
    """The simulator stopped in step 19, unseen by the watch; at the alarm of
    step 20, one read of the simulator's log finds the stop, so the episode
    ends at the stop, and the record holds an episode with no alarm. The end
    notice follows the alarm within 1 second, and in training mode carries
    the reward of an episode with no alarm (episodes-tests.md,
    a_stop_found_at_the_alarm_ends_the_episode_as_a_simulator_stop)."""
    fake = fake_in(mode)
    fake.alarm_at = 20
    fake.watch_sees_stop = False
    step_0 = STEP_0[mode]
    fake.stop_at = step_0 + 97.5
    outcome, record = run_in(mode, fake, tmp_path)
    assert fake.clock.monotonic(datetime.fromisoformat(record["settle"]["step_0"])) == (
        step_0
    )
    assert (outcome.outcome, outcome.cause) == ("terminated", CAVITATION)
    assert record["track_a"]["alarm"] is None
    assert record["track_a"]["end_cause"] == "simulator_stop"
    assert record["track_a"]["R_n"] == 0.0
    carried = 0.0 if mode == TRAINING_FORK else None
    assert fake.defenders[0].sent[-1]["previous_reward"] == carried
    assert fake.defenders[0].sent[-1]["step"] == 20
    assert record["steps"]["end_reason"] == steps.STOPPED_BEFORE_ALARM
    assert "end_cause" not in record["steps"]
    assert [line["step"] for line in steps_jsonl(outcome)] == list(range(20))
    # The read came at the alarm, within its 0.5 s, before the end notice.
    alarm = step_0 + 20 * 5 + fake.alarm_delay
    assert fake.log_reads[-1] == alarm
    sent = fake.clock.monotonic(
        datetime.fromisoformat(record["steps"]["end_notice_sent"])
    )
    assert sent - alarm < 1.0


@pytest.mark.parametrize("mode", MODES)
def test_an_alarm_after_an_unnoticed_simulator_stop_is_dropped(
    tmp_path: Path, mode: str
) -> None:
    """The stop's line reaches the simulator's log only after the alarm's
    check, so the alarm ends the steps, and in training mode the end notice
    carries the alarm's R_n, -1 for a false alarm. In both modes the episode
    record then holds no alarm, the end cause simulator stop, and the R_n of
    an episode with no alarm (episodes-tests.md,
    an_alarm_after_an_unnoticed_simulator_stop_is_dropped)."""
    fake = fake_in(mode)
    fake.alarm_at = 20
    fake.watch_sees_stop = False
    fake.stop_at = STEP_0[mode] + 97.5
    fake.stop_written_at = 10_000.0
    outcome, record = run_in(mode, fake, tmp_path)
    carried = -1.0 if mode == TRAINING_FORK else None
    assert fake.defenders[0].sent[-1]["previous_reward"] == carried
    assert (outcome.outcome, outcome.cause) == ("terminated", CAVITATION)
    assert record["track_a"]["alarm"] is None
    assert record["track_a"]["end_cause"] == "simulator_stop"
    assert (record["track_a"]["R_n"], record["track_a"]["outcome"]) == (0.0, "none")


def test_the_end_causes_take_the_outcomes_identifiers() -> None:
    """Track A's end causes in the outcomes' identifier form, the alarm's
    and the last stage's the same as their outcomes; no end cause names a
    terminal state of the ground-truth evaluator, which nothing yet
    identifies (episode-runner-impl.md §11, Track A's fields in the
    record)."""
    assert episode.END_CAUSES == ("horizon", "alarm", "last_stage", "simulator_stop")
    assert episode.TRACK_A_ENDS == {
        steps.TRACK_A_ALARM: "alarm",
        steps.LAST_STAGE: "last_stage",
    }
    assert set(episode.TRACK_A_ENDS.values()) <= set(episode.END_CAUSES)
    assert episode.TERMINATED not in episode.TRACK_A_ENDS.values()


def test_a_failed_attack_session_discards_the_episode(
    tmp_path: Path,
) -> None:
    """An attack chain whose session gives no process identifier never ran:
    the episode is discarded with that failure as its cause, and is not scored
    as an episode with no alarm (episodes-tests.md,
    a_failed_attack_session_discards_the_episode; episodes.md §12, terminated
    and discarded episodes)."""
    fake = FakeTestbed(FakeClock())
    fake.attack_fails = True
    outcome = adversarial(fake, tmp_path)
    cause = (
        "the attack chain's session gave no process identifier: its first line "
        "was 'Error: Instance not found'"
    )
    assert (outcome.outcome, outcome.cause) == ("discarded", cause)
    record = episode_json(outcome)
    assert record["attack"]["session"]["problems"] == [cause]
    assert record["attack"]["session"]["releases"] == []


def test_a_benign_false_alarm_ends_the_episode(tmp_path: Path) -> None:
    """episodes-tests.md, a_benign_false_alarm_ends_the_episode."""
    fake = FakeTestbed(FakeClock())
    fake.alarm_at = 20
    outcome = run(fake, tmp_path)
    assert (outcome.outcome, outcome.cause) == ("alarm", None)
    record = episode_json(outcome)
    assert record["steps"]["run"] == 21
    track_a = record["track_a"]
    assert (track_a["alarm"]["step"], track_a["K"], track_a["k_n"]) == (20, None, None)
    assert (track_a["R_n"], track_a["outcome"]) == (-1.0, "false alarm")
    assert track_a["end_cause"] == "alarm"
    assert fake.defenders[0].sent[-1]["previous_reward"] is None


def test_a_track_a_alarm_leaves_the_episode_running(tmp_path: Path) -> None:
    """The alarm is recorded, every stage runs, and R_n comes from the first
    alarm as if it had ended the episode (episodes-tests.md,
    a_track_a_alarm_leaves_the_episode_running,
    only_a_run_marked_as_a_replay_is_suppressed)."""
    _, _, second, _ = stage_times()
    fake = FakeTestbed(FakeClock())
    fake.alarm_at = int(second // 5) + 1
    outcome = adversarial(fake, tmp_path, suppressed=True)
    assert (outcome.outcome, outcome.cause) == ("horizon", None)
    record = episode_json(outcome)
    assert record["steps"]["run"] == 144
    assert record["track_a"]["alarm"]["step"] == fake.alarm_at
    assert (record["track_a"]["k_n"], record["track_a"]["R_n"]) == (2, -0.5)
    assert record["track_a"]["end_cause"] == "horizon"
    (chain,) = fake.attacks
    assert len(chain.run.releases) == 3
    assert fake.defenders[0].sent[-1]["previous_reward"] is None


@pytest.mark.parametrize("present", [True, False])
def test_an_adversarial_episode_needs_its_binary_on_the_adversary_host(
    tmp_path: Path, present: bool
) -> None:
    """Before step 0 the episode runner checks that the adversary host holds
    the chain's binary as an executable file. With the binary, the episode
    runs its chain. Without it, the episode is discarded before any stage,
    with the cause in the record (episodes.md §19, open items; §12,
    terminated and discarded episodes)."""
    fake = FakeTestbed(FakeClock())
    fake.binary_present = present
    outcome = adversarial(fake, tmp_path)
    assert fake.binary_checks == [f"/root/{CHAIN.id}"]
    if present:
        # In Track A the episode ends when the chain's last stage falls due.
        assert (outcome.outcome, outcome.cause) == ("last_stage", None)
        (chain,) = fake.attacks
        assert chain.run.started is not None
        return
    cause = f"the adversary host holds no executable /root/{CHAIN.id}"
    assert (outcome.outcome, outcome.cause) == ("discarded", cause)
    record = episode_json(outcome)
    assert (record["outcome"], record["cause"]) == ("discarded", cause)
    # No attack chain's session, so no stage released, and no step, so the
    # episode is not scored in Track A.
    assert fake.attacks == [] and fake.defenders == []
    assert record["attack"]["session"] is None
    assert "track_a" not in record
    assert fake.downs == 1


def test_a_fork_whose_adversary_host_lacks_the_binary_discards_its_episode(
    tmp_path: Path,
) -> None:
    """At a fork the check follows the chain's choice, before step 0's
    decision request (episodes.md §19, open items; §12, terminated and
    discarded episodes)."""
    fake = copy_of_living_port(FakeClock())
    fake.binary_present = False
    outcome = run_copy(
        fake,
        tmp_path,
        episode_type=episode.ADVERSARIAL,
        library=chains.library(chains.load(SAMPLE)),
        given=CHAIN,
    )
    cause = f"the adversary host holds no executable /root/{CHAIN.id}"
    assert (outcome.outcome, outcome.cause) == ("discarded", cause)
    assert fake.binary_checks == [f"/root/{CHAIN.id}"]
    assert fake.attacks == [] and fake.defenders == []
    record = record_json(outcome)
    assert record["cause"] == cause
    assert "track_a" not in record


def test_a_check_of_the_binary_that_incus_fails_discards_the_episode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A check that Incus fails to run is a failed call to Incus, which
    discards the episode (episodes.md §12, terminated and discarded
    episodes)."""

    class Failing:
        def exec(self, node: str, *argv: str, timeout_s: float = 60.0) -> Result:
            return Result(1, "", "Error: Instance is not running")

    with pytest.raises(episode.IncusError, match="Instance is not running"):
        episode.check_binary(Failing(), f"/root/{CHAIN.id}")


def test_a_benign_episode_with_the_adversary_host_runs_no_chain(
    tmp_path: Path,
) -> None:
    """The adversary host's own image holds every binary, so the episode
    copies nothing onto it (episodes.md §6.5, adversary host)."""
    fake = FakeTestbed(FakeClock())
    fake.manifest = WITH_ADVERSARY
    record = episode_json(run(fake, tmp_path))
    assert record["type"] == "benign"
    assert "attack" not in record
    assert fake.attacks == []


def test_an_episode_that_ends_before_the_onset_step_never_starts_its_chain(
    tmp_path: Path,
) -> None:
    """Step 0 begins at 106 s, and the simulator stops in step 2, before the
    earliest onset step, step 6."""
    fake = FakeTestbed(FakeClock())
    fake.stop_at = 118.0
    outcome = adversarial(fake, tmp_path)
    assert outcome.outcome == "terminated"
    (chain,) = fake.attacks
    assert chain.run.started is None
    assert episode_json(outcome)["attack"]["early_stop"] == {
        "stage_number": 1,
        "stage": CHAIN.stages[0].name,
        "cause": "the episode ended before the onset step",
    }


def test_an_adversarial_episode_needs_an_attack_chain(tmp_path: Path) -> None:
    fake = FakeTestbed(FakeClock())
    with pytest.raises(ValueError):
        run(fake, tmp_path, episode_type=episode.ADVERSARIAL)
    with pytest.raises(ValueError):
        run(fake, tmp_path, chain=CHAIN)
    with pytest.raises(ValueError):
        run(fake, tmp_path, episode_type="training")


# An episode in a fork of a living port (the living-port contract;
# episodes.md §6.4, how a fork is made). The living port started an hour before
# the fork instant, EPOCH, and the fork's runner starts a second after it.

LIVING_SEED = "000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f"
FORK_1_SEED = "e5bfee0bcf01978597b5a6a89108e98698ad609da3fb915c9231c664eb12ac86"
POLLING_FORK = predicted_events.Draws(
    initial_volume_l=30000.0,
    order_pending_s=None,
    first_poll_after_s=150.0,
    delivery_volume_l=18000.0,
    arrivals=((900.0, 1000.0),),
    audit_completed=False,
)
# A fork whose supplier's next poll is unknown, which admits no chain that
# waits for a poll.
NO_POLL_FORK = predicted_events.Draws(
    initial_volume_l=14000.0,
    order_pending_s=2000.0,
    first_poll_after_s=None,
    delivery_volume_l=18000.0,
    arrivals=(),
    audit_completed=False,
)
EXFILTRATE = chains.find(chains.load(SAMPLE), "external-informed__exfiltrate-inventory")


def living_port(tmp_path: Path) -> fork.LivingPort:
    path = tmp_path / "startup.json"
    path.write_text('{"sim_tug_arrivals": 1}\n')
    started = EPOCH - timedelta(hours=1)
    return fork.LivingPort(LIVING_SEED, 1, started, path, {})


def copy_of_living_port(clock: FakeClock) -> FakeTestbed:
    """The living port's deployment, running since an hour before the fork."""
    fake = FakeTestbed(clock)
    fake.sim_host_start = -3600.0
    fake.running = True
    fake.manifest = WITH_ADVERSARY
    clock.t = 1.0
    return fake


def run_copy(
    fake: FakeTestbed,
    tmp_path: Path,
    draws: predicted_events.Draws = POLLING_FORK,
    reads_start: bool = True,
    **options,
) -> episode.Outcome:
    reads: list[tuple] = []

    def read_start(incus, living, at, timings):
        reads.append((living.seed, at, timings))
        return fork.ForkStart(draws, {"volume_l": draws.initial_volume_l})

    dist = tmp_path / "dist"
    dist.mkdir(exist_ok=True)
    for chain in chains.load(SAMPLE):
        (dist / chain.id).write_bytes(BINARY)
    forked = episode.Fork(living_port(tmp_path), 1, EPOCH)
    outcome = episode.run_copy_episode(
        fake.testbed(),
        SCENARIO,
        forked,
        fake.track,
        tmp_path / "fork-1",
        dist=dist,
        read_start=read_start,
        **options,
    )
    assert reads == ([(LIVING_SEED, EPOCH, SCENARIO.timings)] if reads_start else [])
    return outcome


def record_json(outcome: episode.Outcome) -> dict:
    return json.loads((outcome.record / "record.json").read_text())


def test_a_copy_runs_one_episode_with_no_deployment_and_no_reset(
    tmp_path: Path,
) -> None:
    fake = copy_of_living_port(FakeClock())
    outcome = run_copy(fake, tmp_path)
    assert (outcome.outcome, outcome.cause) == ("horizon", None)
    assert outcome.record == tmp_path / "fork-1"
    assert fake.state_dir is None and fake.downs == 0
    record = record_json(outcome)
    assert record["living_port_seed"] == LIVING_SEED
    assert (record["fork"], record["forked_at"]) == (1, "2026-11-04T01:00:00.000Z")
    assert record["age_s"] == 3600.0
    assert record["episode_seed"] == FORK_1_SEED
    assert record["seeds"] == derive_seeds(FORK_1_SEED)
    assert record["copy"]["method"] == "copying the whole virtual machine"
    assert record["start_state"] == {"volume_l": 30000.0}
    assert "deployment" not in record and record["reset"] is None
    assert "first_scans" not in record and "scan_requests" not in record
    assert fake.scan_requests == []


def test_a_copys_step_0_begins_once_the_settle_conditions_hold(tmp_path: Path) -> None:
    """The deployment settled an hour before, so the conditions hold at the
    first check, a second after the fork instant, and step 0 begins then: a
    fork waits no settle delay."""
    outcome = run_copy(copy_of_living_port(FakeClock()), tmp_path)
    record = record_json(outcome)
    settled = record["settle"]
    assert settled["start"] == "2026-11-04T01:00:00.000000Z"
    assert settled["step_0"] == "2026-11-04T01:00:01.000000Z"
    assert settled["simulator_start"] == "2026-11-04T00:00:00.900000Z"
    assert record["scenario"]["settle_delay_s"] == 0.0
    assert record["scenario"]["allowance_s"] == predicted_events.FLOW_RECORD_LAG_S
    assert len(steps_jsonl(outcome)) == steps.HORIZON


def test_a_copys_record_holds_the_export_and_the_living_ports_start_up_input(
    tmp_path: Path,
) -> None:
    fake = copy_of_living_port(FakeClock())
    outcome = run_copy(fake, tmp_path)
    exported = (
        "alerts/2026/Nov/ossec-alerts-04.json",
        "archives/2026/Nov/ossec-archive-04.json",
        "reads.jsonl",
        "sim.log",
    )
    for name in exported:
        assert (outcome.record / "export" / name).is_file()
    # The export copies the dated files of the days from step 0 to the
    # export, 4 November alone.
    assert fake.listed == [
        "alerts/2026/Nov/ossec-alerts-04",
        "archives/2026/Nov/ossec-archive-04",
    ]
    assert (outcome.record / "startup.json").read_text() == '{"sim_tug_arrivals": 1}\n'
    assert record_json(outcome)["export"] == dict.fromkeys(exported)


def test_a_copy_refuses_to_overwrite_a_record(tmp_path: Path) -> None:
    (tmp_path / "fork-1").mkdir()
    (tmp_path / "fork-1" / "record.json").write_text("{}\n")
    with pytest.raises(FileExistsError):
        run_copy(copy_of_living_port(FakeClock()), tmp_path)


def test_a_copy_admits_the_given_chain_once_step_0_is_known(tmp_path: Path) -> None:
    fake = copy_of_living_port(FakeClock())
    outcome = run_copy(
        fake,
        tmp_path,
        episode_type=episode.ADVERSARIAL,
        library=chains.library(chains.load(SAMPLE)),
        given=CHAIN,
    )
    # A Track A episode ends when the chain's last stage falls due.
    assert (outcome.outcome, outcome.cause) == ("last_stage", None)
    attack = record_json(outcome)["attack"]
    assert attack["chain"]["id"] == CHAIN.id
    assert attack["chosen"]["by"] == "the command line"
    assert attack["chosen"]["step_0_after_fork_s"] == [1.0, 1.0]
    assert attack["onset_step"] == pacing.onset_step(
        derive_seeds(FORK_1_SEED)[ATTACK_START_STEP]
    )
    (session,) = fake.attacks
    assert session.run.started == 1.0 + attack["onset_step"] * steps.STEP_S


def test_a_copy_that_rules_out_the_given_chain_runs_no_episode(
    tmp_path: Path,
) -> None:
    fake = copy_of_living_port(FakeClock())
    outcome = run_copy(
        fake,
        tmp_path,
        draws=NO_POLL_FORK,
        episode_type=episode.ADVERSARIAL,
        library=chains.library(chains.load(SAMPLE)),
        given=EXFILTRATE,
    )
    assert outcome.outcome == episode.NOT_ADMITTED
    assert outcome.cause == (
        "intercept-supplier-poll finds no certain poll (the supplier's next poll "
        "is unknown at the fork)"
    )
    record = record_json(outcome)
    assert record["outcome"] == "not admitted"
    assert record["attack"]["chosen"]["not_admitted"] == outcome.cause
    assert fake.attacks == [] and fake.defenders == []
    assert not (outcome.record / "steps.jsonl").exists()


def test_a_copy_draws_its_chain_from_the_chains_the_fork_admits(
    tmp_path: Path,
) -> None:
    library = chains.library(chains.load(SAMPLE))
    outcome = run_copy(
        copy_of_living_port(FakeClock()),
        tmp_path,
        draws=NO_POLL_FORK,
        episode_type=episode.ADVERSARIAL,
        library=library,
    )
    assert (outcome.outcome, outcome.cause) == ("last_stage", None)
    attack = record_json(outcome)["attack"]
    assert attack["chosen"]["by"] == "a uniform draw from the chains the fork admits"
    assert attack["chain"]["id"] in attack["chosen"]["admitted"]
    assert EXFILTRATE.id in attack["chosen"]["excluded"]


def test_a_copy_that_fails_to_settle_is_discarded(tmp_path: Path) -> None:
    fake = copy_of_living_port(FakeClock())
    fake.stop_at = 0.5
    outcome = run_copy(fake, tmp_path, settle_timeout_s=60.0)
    assert outcome.outcome == "discarded"
    assert outcome.cause.startswith("the fork failed to settle:")
    assert record_json(outcome)["export"]["sim.log"] is None


def test_a_copy_whose_start_state_cannot_be_read_is_discarded(tmp_path: Path) -> None:
    def broken(incus, living, at, timings):
        raise predicted_events.PredictedEventsError(
            "the helper that repeats the tugs' draws (competition/predicted_events) "
            "failed: no such file"
        )

    fake = copy_of_living_port(FakeClock())
    forked = episode.Fork(living_port(tmp_path), 1, EPOCH)
    outcome = episode.run_copy_episode(
        fake.testbed(), SCENARIO, forked, "A", tmp_path / "fork-1", read_start=broken
    )
    assert outcome.outcome == "discarded"
    assert outcome.cause == (
        "the start state at the fork was not read: the helper that repeats the "
        "tugs' draws (competition/predicted_events) failed: no such file"
    )


@pytest.mark.parametrize("episode_type", [episode.BENIGN, episode.ADVERSARIAL])
def test_a_fault_in_the_start_state_discards_the_episode(
    tmp_path: Path, episode_type: str
) -> None:
    """The fault is the cause, the record holds what the reads gave, and no
    chain is chosen; the fork is not settled (episodes.md §6.4, faults; §12,
    discarded)."""
    faults = ["more than one fill is under way at the fork"]
    given = {"faults": faults, "volume_l": 12900.0}

    def faulty(incus, living, at, timings):
        raise fork.StartStateFault(faults, given)

    dist = tmp_path / "dist"
    dist.mkdir()
    for chain in chains.load(SAMPLE):
        (dist / chain.id).write_bytes(BINARY)
    fake = copy_of_living_port(FakeClock())
    forked = episode.Fork(living_port(tmp_path), 1, EPOCH)
    outcome = episode.run_copy_episode(
        fake.testbed(),
        SCENARIO,
        forked,
        "A",
        tmp_path / "fork-1",
        read_start=faulty,
        episode_type=episode_type,
        library=chains.load(SAMPLE),
        dist=dist,
    )
    assert outcome.outcome == "discarded"
    assert outcome.cause == (
        "the start state at the fork holds a fault: more than one fill is under "
        "way at the fork"
    )
    record = record_json(outcome)
    assert record["start_state"] == given
    assert "start_state" in record["durations_s"]
    assert "settle" not in record
    assert record.get("attack") in (None, {})


# A fork at which a poll under way holds the road tanker's arrival: the
# poll and the tanker are overdue, and the start state is uncertain.
HELD_TANKER_FORK = dataclasses.replace(
    POLLING_FORK,
    poll_overdue="the supplier's poll due at 2026-11-04T01:00:00.000000Z had not "
    "come by the fork",
    tanker_overdue="the road tanker of the order placed at "
    "2026-11-03T23:48:00.000000Z was due at 2026-11-04T01:00:00.000000Z, and "
    "the simulator's log holds no arrival of it",
)


def test_an_overdue_tanker_excludes_a_poll_chain_and_discards_nothing(
    tmp_path: Path,
) -> None:
    outcome = run_copy(
        copy_of_living_port(FakeClock()),
        tmp_path,
        draws=HELD_TANKER_FORK,
        episode_type=episode.ADVERSARIAL,
        library=chains.library(chains.load(SAMPLE)),
        given=EXFILTRATE,
    )
    assert outcome.outcome == episode.NOT_ADMITTED
    assert outcome.cause == (
        "intercept-supplier-poll needs supplier_poll, and the start state at the "
        f"fork is uncertain: {HELD_TANKER_FORK.poll_overdue}"
    )


def test_an_overdue_tanker_leaves_a_chain_that_needs_neither_admitted(
    tmp_path: Path,
) -> None:
    outcome = run_copy(
        copy_of_living_port(FakeClock()),
        tmp_path,
        draws=HELD_TANKER_FORK,
        episode_type=episode.ADVERSARIAL,
        library=chains.library(chains.load(SAMPLE)),
        given=CHAIN,
    )
    assert (outcome.outcome, outcome.cause) == ("last_stage", None)
    assert record_json(outcome)["attack"]["chain"]["id"] == CHAIN.id


def test_a_benign_copy_runs_no_chain(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        run_copy(copy_of_living_port(FakeClock()), tmp_path, given=CHAIN)


def training_directory(tmp_path: Path) -> Path:
    """The training volume as the manager provides it in the fork."""
    directory = tmp_path / "training" / "fork-1"
    directory.mkdir(parents=True)
    (directory / "state.json").write_text("{}\n")
    return directory


def test_an_evaluation_copy_without_a_volume_records_none(tmp_path: Path) -> None:
    fake = copy_of_living_port(FakeClock())
    record = record_json(run_copy(fake, tmp_path))
    assert (record["mode"], record["suppressed"]) == ("evaluation", False)
    assert record["training_volume"] is None
    assert (record["training_volume_in"], record["training_volume_out"]) == (None, None)
    assert fake.commands == []


def test_an_evaluation_copy_records_the_living_ports_read_only_volume(
    tmp_path: Path,
) -> None:
    fake = copy_of_living_port(FakeClock())
    fake.devices = {volume.EVALUATION_DEVICE: {"readonly": "true"}}
    record = record_json(run_copy(fake, tmp_path))
    assert record["training_volume_in"] == "sha256:" + "a" * 64
    assert record["training_volume_out"] == "sha256:" + "a" * 64
    # The runner leaves the living port's volume attached.
    assert fake.commands == [] and volume.EVALUATION_DEVICE in fake.devices


def test_a_training_copy_attaches_its_volume_before_the_settle_conditions(
    tmp_path: Path,
) -> None:
    fake = copy_of_living_port(FakeClock())
    fake.versions = ("a" * 64, "b" * 64)
    directory = training_directory(tmp_path)
    outcome = run_copy(fake, tmp_path, mode=volume.TRAINING, training_volume=directory)
    assert outcome.outcome == "horizon"
    record = record_json(outcome)
    assert (record["mode"], record["suppressed"]) == ("training", False)
    assert record["training_volume"] == str(directory)
    assert record["training_volume_in"] == "sha256:" + "a" * 64
    assert record["training_volume_out"] == "sha256:" + "b" * 64
    assert record["training_volume_detached"] is True
    (attach_at, attach), (detach_at, detach) = fake.commands
    assert attach == (
        "config",
        "device",
        "add",
        "siem-01",
        "training-volume",
        "disk",
        f"source={directory}",
        "path=/var/lib/entry",
    )
    assert detach == ("config", "device", "remove", "siem-01", "training-volume")
    step_0 = 1.0
    assert attach_at <= step_0 < detach_at
    assert volume.TRAINING_DEVICE not in fake.devices
    # siem-01's users may write the volume.
    assert (directory / "state.json").stat().st_mode & 0o666 == 0o666


def test_a_training_volume_that_cannot_be_attached_discards_the_episode(
    tmp_path: Path,
) -> None:
    fake = copy_of_living_port(FakeClock())
    fake.attach_fails = True
    directory = training_directory(tmp_path)
    outcome = run_copy(
        fake,
        tmp_path,
        reads_start=False,
        mode=volume.TRAINING,
        training_volume=directory,
    )
    assert outcome.outcome == "discarded"
    assert outcome.cause == (
        f"the training volume {directory} was not attached: Error: Failed to add device"
    )
    assert fake.defenders == []
    assert len(fake.commands) == 1


def test_a_training_copy_needs_its_volume_and_an_evaluation_copy_none(
    tmp_path: Path,
) -> None:
    directory = training_directory(tmp_path)
    with pytest.raises(ValueError):
        run_copy(copy_of_living_port(FakeClock()), tmp_path, mode=volume.TRAINING)
    with pytest.raises(ValueError):
        run_copy(copy_of_living_port(FakeClock()), tmp_path, training_volume=directory)
