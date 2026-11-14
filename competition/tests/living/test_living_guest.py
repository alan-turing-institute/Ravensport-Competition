"""The steps of creating a living port inside its virtual machine: the
start-up input, the deployment and the identity file (the living port
contract; episodes.md §4.3, start-up input).

Run with: pytest competition/tests -q
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from competition.episode_runner import startup
from competition.episode_runner.incus import Result
from competition.episode_runner.seeds import SIM_TUG_ARRIVALS, derive_generator_seed
from competition.living import guest

SEED = "5e" * 32
LINE = (
    "2026-11-11T14:36:00.017097Z  INFO start_state: start_state "
    "sim_tug_arrivals=5277460069125982193 initial_volume_l=35000.0"
)
IMAGES = {
    "fms-01": "ravensport/fms-01:latest",
    "sim-host": "ravensport/sim-competition:latest",
    "siem-01": "ravensport/siem-01-competition:latest",
}


class FakeIncus:
    def __init__(
        self, instances=(), aliases=None, simulator="started", line=LINE
    ) -> None:
        self._instances = [{"name": name} for name in instances]
        self.aliases = list(IMAGES.values() if aliases is None else aliases)
        self.simulator = simulator
        self.line = line
        self.execs: list[tuple[str, ...]] = []

    def instances(self):
        return self._instances

    def query(self, path):
        assert path == "/1.0/images/aliases?recursion=1"
        return [{"name": name} for name in self.aliases]

    def exec(self, node, *argv, timeout_s=60.0):
        self.execs.append((node, *argv))
        if argv[0] == "grep":
            return Result(0 if self.line else 1, self.line, "")
        return Result(0, f" * status: {self.simulator}\n", "")


class FakeRun:
    def __init__(self, ntp="no") -> None:
        self.ntp = ntp
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        assert kwargs["stdin"] is subprocess.DEVNULL
        out = f"{self.ntp}\n" if argv[0] == "timedatectl" else ""
        return subprocess.CompletedProcess(argv, 0, out, "")

    def makes(self) -> list[list[str]]:
        return [argv[3:] for argv in self.calls if argv[0] == "make"]


SETTLING = {
    "settled_at": "2026-11-11T14:37:12.345Z",
    "conditions": {"1": "2026-11-11T14:37:00.000Z", "4": "2026-11-11T14:37:12.345Z"},
}


def create(tmp_path, *, incus=None, run=None, build=False, volume=None, settles=None):
    settles = settles if settles is not None else []

    def settle_wait(incus, rendered, line):
        settles.append((rendered, line))
        return SETTLING

    return guest.create(
        SEED,
        build=build,
        volume=volume,
        incus=incus or FakeIncus(),
        run=run or FakeRun(),
        state_dir=tmp_path / "sim-state",
        identity_path=tmp_path / "living-port.json",
        log=tmp_path / "deploy.log",
        manifest=lambda: {"images": IMAGES, "nodes": {"siem-01": {}}},
        sleep=lambda s: None,
        settle_wait=settle_wait,
        settled_path=tmp_path / "living-port-settled.json",
    )


def test_the_identity_file_holds_the_seed_the_tug_seed_and_the_simulators_start() -> (
    None
):
    text = json.dumps({SIM_TUG_ARRIVALS: derive_generator_seed(SEED, SIM_TUG_ARRIVALS)})
    assert guest.identity(SEED, text, LINE) == {
        "living_port_seed": SEED,
        "tug_seed": derive_generator_seed(SEED, SIM_TUG_ARRIVALS),
        "started_at": "2026-11-11T14:36:00.017Z",
        "startup": "/var/lib/competition/sim-state/startup.json",
    }


def test_a_start_up_input_with_another_tug_seed_is_refused() -> None:
    with pytest.raises(guest.CreateError, match="tug seed"):
        guest.identity(SEED, json.dumps({SIM_TUG_ARRIVALS: 1}), LINE)


def test_create_writes_the_input_deploys_with_it_and_writes_the_identity(
    tmp_path,
) -> None:
    run = FakeRun()
    made = create(tmp_path, run=run)
    override = "EXTRA_OVERRIDE=../competition/deploy/override.yaml"
    assert run.makes() == [
        ["render", override],
        ["deploy", override, f"SIM_STATE_DIR={tmp_path / 'sim-state'}"],
    ]
    written = tmp_path / "sim-state" / "startup.json"
    fields = json.loads(written.read_text())
    assert tuple(fields) == startup.FIELDS
    assert fields[SIM_TUG_ARRIVALS] == derive_generator_seed(SEED, SIM_TUG_ARRIVALS)
    assert fields["initial_volume_l"] == startup.INITIAL_VOLUME_L
    assert written.stat().st_mode & 0o777 == 0o644
    assert (tmp_path / "sim-state").stat().st_mode & 0o777 == 0o755
    identity = json.loads((tmp_path / "living-port.json").read_text())
    assert made == {**identity, **SETTLING}
    assert tuple(identity) == (
        "living_port_seed",
        "tug_seed",
        "started_at",
        "startup",
    )
    assert made["tug_seed"] == fields[SIM_TUG_ARRIVALS]
    assert made["started_at"] == "2026-11-11T14:36:00.017Z"


def test_create_with_build_builds_the_images_first(tmp_path) -> None:
    run = FakeRun()
    create(tmp_path, run=run, build=True)
    assert [argv[0] for argv in run.makes()] == ["build", "deploy"]


def test_an_image_not_built_stops_before_the_deployment(tmp_path) -> None:
    run = FakeRun()
    incus = FakeIncus(aliases=["ravensport/fms-01:latest"])
    with pytest.raises(
        guest.CreateError, match="ravensport/siem-01-competition:latest, ravensport/sim"
    ):
        create(tmp_path, incus=incus, run=run)
    assert [argv[0] for argv in run.makes()] == ["render"]


def test_a_vm_that_already_holds_instances_is_refused(tmp_path) -> None:
    run = FakeRun()
    with pytest.raises(guest.CreateError, match="already holds instances: fms-01"):
        create(tmp_path, incus=FakeIncus(instances=["fms-01"]), run=run)
    assert run.makes() == []


def test_a_clock_still_set_by_a_time_source_is_refused(tmp_path) -> None:
    run = FakeRun(ntp="yes")
    with pytest.raises(guest.CreateError, match="hold it before the deployment"):
        create(tmp_path, run=run)
    assert run.makes() == []
    assert not (tmp_path / "sim-state").exists()


def test_a_simulator_that_crashes_before_its_start_fails_the_creation(tmp_path) -> None:
    incus = FakeIncus(simulator="crashed", line="")
    with pytest.raises(guest.CreateError, match="crashed"):
        create(tmp_path, incus=incus)
    assert not (tmp_path / "living-port.json").exists()


def test_the_command_line_reports_a_failure_without_a_trace(
    capsys, monkeypatch
) -> None:
    def fail(seed, **options):
        raise guest.CreateError("images not built")

    monkeypatch.setattr(guest, "create", fail)
    assert guest.main(["create", "--seed", SEED]) == 1
    assert (
        "the living port was not created: images not built" in capsys.readouterr().err
    )


def test_the_living_port_settles_after_its_start_and_the_instant_is_kept(
    tmp_path,
) -> None:
    settles = []
    made = create(tmp_path, settles=settles)
    assert settles == [({"images": IMAGES, "nodes": {"siem-01": {}}}, LINE)]
    assert made["settled_at"] == SETTLING["settled_at"]
    kept = json.loads((tmp_path / "living-port-settled.json").read_text())
    assert kept == SETTLING


def test_the_submitted_volume_is_attached_read_only_to_siem_01_after_the_deployment(
    tmp_path,
) -> None:
    run = FakeRun()
    create(tmp_path, run=run, volume=Path("/var/lib/competition/volume"))
    commands = [argv for argv in run.calls if argv[0] in ("make", "incus")]
    assert commands[-1] == [
        "incus",
        "config",
        "device",
        "add",
        "siem-01",
        "entry-volume",
        "disk",
        "source=/var/lib/competition/volume",
        "path=/var/lib/entry",
        "readonly=true",
    ]
    assert commands[-2][3] == "deploy"


def test_the_volume_mounts_at_var_lib_entry_alone() -> None:
    """The guest takes no mount point: the volume mounts at /var/lib/entry, the
    one path that the launcher and the episode runner's test read
    (episode-runner-impl.md §5, forks)."""
    with pytest.raises(SystemExit):
        guest.main(
            [
                "create",
                "--seed",
                SEED,
                "--volume",
                "/var/lib/competition/volume",
                "--volume-mount",
                "/srv/v",
            ]
        )


class FakeClock:
    """A clock whose times are seconds since the epoch, which records each
    wait instead of waiting."""

    def __init__(self) -> None:
        self.waited_until: list[float] = []

    def now(self) -> float:
        return max(self.waited_until, default=0.0)

    def utc(self, t: float) -> datetime:
        return datetime.fromtimestamp(t, timezone.utc)

    def monotonic(self, at: datetime) -> float:
        return at.timestamp()

    def sleep_until(self, t: float, wake=None) -> None:
        self.waited_until.append(t)


def test_the_living_port_waits_the_settle_delay_after_the_episode_runners_checks(
    monkeypatch,
) -> None:
    """The settle period ends the settle delay after the four conditions hold,
    and the living port's first fork falls one drawn wait after that end
    (episodes.md §6.6, settle period)."""
    from render import SIEM_MONITORED_NODES

    from competition.episode_runner import settle

    calls = []

    def wait_to_settle(incus, clock, monitored, start, **options):
        calls.append((monitored, options))
        held = clock.monotonic(settle.start_state_time(LINE)) + 72.0
        return settle.Settled(
            start=start,
            conditions=[settle.Condition(n, held - 4 + n) for n in (1, 2, 3, 4)],
            step_0=held + options["delay_s"],
            agent_ids={},
            start_state=LINE,
        )

    monkeypatch.setattr(settle, "wait_to_settle", wait_to_settle)
    nodes = {name: {} for name in [*SIEM_MONITORED_NODES, "fw-ext-dmz"]}
    clock = FakeClock()
    found = guest.settled(FakeIncus(), {"nodes": nodes}, LINE, clock=clock)
    ((monitored, options),) = calls
    assert monitored == list(SIEM_MONITORED_NODES)
    assert options["delay_s"] == settle.SETTLE_DELAY_S == 45.0
    assert found["conditions"]["4"] == "2026-11-11T14:37:12.017Z"
    assert found["settled_at"] == "2026-11-11T14:37:57.017Z"
    assert [clock.utc(t) for t in clock.waited_until] == [
        datetime(2026, 11, 11, 14, 37, 57, 17097, tzinfo=timezone.utc)
    ]
