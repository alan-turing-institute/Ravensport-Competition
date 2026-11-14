"""The living port and its forks (the living port contract; episodes.md
§6.3, the living port; §6.4, how a fork is made), with fake QEMU processes and
fake guests, so that no virtual machine is needed.

Run with: pytest competition/tests -q
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
from pathlib import Path

import pytest

from competition.living import port as living
from competition.living.hypervisor import linux_kvm, macos_hvf_x86_64
from competition.living.port import (
    Base,
    Manager,
    PortError,
    RoomError,
    episode_command,
    iso,
    parse_utc,
    snapshot_bytes_expected,
)
from competition.living.snapshots import FORK_SPACING_S, fork_wait_s
from competition.living.ssh import ClockOffset
from competition.living.vm import Engine, Machine, VMError

KVM = linux_kvm(firmware=Path("/usr/local/share/qemu/edk2-x86_64-code.fd"))
INTEL_MAC = macos_hvf_x86_64(firmware=Path("/opt/local/share/qemu/edk2-x86_64-code.fd"))
SEED = "5e" * 32
STARTED_AT = "2026-11-11T20:00:00.000Z"
SETTLED_AT = "2026-11-11T20:01:10.000Z"
CONTRACT = (
    "cd /opt/ravensport && deploy/.venv-$(uname -s)-$(uname -m)/bin/python "
    "-m competition.episode_runner copy "
    "--fork 3 --forked-at 2026-11-11T20:15:00.000Z --track A --type adversarial "
    "--chain c-01 --out /var/lib/competition/episodes/fork-3"
)


class Guests:
    """Stands in for Remote, for the living port and every fork: answers each
    command as the guest would, and records what was asked."""

    def __init__(self, wall=time.time) -> None:
        self.wall = wall
        self.commands: list[tuple[int, str]] = []
        self.streams: list[tuple[int, str]] = []
        self.sent: list[tuple[int, Path, str, tuple]] = []
        self.fetched: list[tuple[int, str]] = []
        self.clock = living.CLOCK_HELD
        self.clocksource = "hpet"
        self.updates = living.UPDATES_HELD
        self.simulator = "started"
        self.offset_s = -2.5
        self.stream_status = 0
        self.identity: dict | None = None
        self.out_exists = False
        # The record.json that each fork's episode writes, by its directory.
        self.records: dict[str, dict] = {}
        # An event that each episode's command waits for, where set.
        self.block: threading.Event | None = None

    def __call__(self, ssh):
        return FakeRemote(self, ssh.port)


class FakeRemote:
    def __init__(self, guests: Guests, port: int) -> None:
        self.guests, self.port = guests, port

    def run(self, command, *, timeout_s=600.0, check=True):
        self.guests.commands.append((self.port, command))
        out = ""
        if command == living.CLOCK_STATE:
            out = self.guests.clock
        elif command == living.CLOCKSOURCE_STATE:
            out = self.guests.clocksource
        elif command == living.UPDATES_STATE:
            out = self.guests.updates
        elif command == f"cat {living.IDENTITY}":
            out = json.dumps(self.guests.identity)
        elif command == living.SIMULATOR_STATUS:
            out = f" * status: {self.guests.simulator}\n"
        elif command == f"cat {living.SETTLED}":
            out = json.dumps({"settled_at": SETTLED_AT, "conditions": {}})
        elif command.startswith("test ! -e"):
            return subprocess.CompletedProcess(
                command, int(self.guests.out_exists), "", ""
            )
        return subprocess.CompletedProcess(command, 0, out, "")

    def stream(self, command, log, *, timeout_s=None):
        self.guests.streams.append((self.port, command))
        if "episode_runner copy" in command and self.guests.block is not None:
            self.guests.block.wait(10.0)
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("streamed\n")
        if "competition.living.guest create" in command:
            seed = command.split("--seed ")[1].split()[0].strip("'")
            self.guests.identity = {
                "living_port_seed": seed,
                "tug_seed": 1234567890,
                "started_at": STARTED_AT,
                "startup": "/var/lib/competition/sim-state/startup.json",
            }
        return self.guests.stream_status

    def wait(self, timeout_s=300.0, poll_s=2.0):
        return 0.5

    def send_tree(self, source, dest, excludes=(), timeout_s=900.0):
        self.guests.sent.append((self.port, source, dest, tuple(excludes)))

    def fetch_tree(self, remote, local_parent, timeout_s=900.0, sudo=False):
        self.guests.fetched.append((self.port, remote, sudo))
        out = local_parent / remote.rsplit("/", 1)[1]
        out.mkdir(parents=True, exist_ok=True)
        if "/training/" in remote:
            (out / "written").write_text(f"by the defender in {remote}")
        else:
            (out / "record.json").write_text(
                json.dumps(self.guests.records.get(remote, {}))
            )

    def clock_offset(self, samples=5, timeout_s=30.0):
        now = self.guests.wall()
        return ClockOffset(self.guests.offset_s, 0.002, now)


class FakeWall:
    """A host clock that advances only when the loop waits."""

    def __init__(self, at: float) -> None:
        self.at = at

    def __call__(self) -> float:
        return self.at

    def wait(self, seconds: float) -> bool:
        self.at += seconds
        return False

    def is_set(self) -> bool:
        return False


@pytest.fixture
def base(short_tmp) -> Base:
    (short_tmp / "base").mkdir()
    disk = short_tmp / "base" / "disk"
    disk.write_bytes(b"base image")
    return Base(disk=disk, mac="52:55:55:a4:9c:41", user="aicd", key=Path("/keys/user"))


@pytest.fixture
def tree(short_tmp) -> Path:
    path = short_tmp / "tree" / "competition" / "living"
    path.mkdir(parents=True)
    (path / "guest.py").write_text("")
    return short_tmp / "tree"


def manager(
    short_tmp,
    host,
    guests,
    wall=time.time,
    free=100 * 10**9,
    cache=0,
    memory=None,
    fold=True,
    clones=False,
    hypervisor=KVM,
) -> Manager:
    engine = Engine(
        hypervisor,
        run=lambda argv, **kw: host.run(argv, **kw),
        popen=lambda argv, **kw: host.popen(argv, **kw),
        wall=wall,
        poll_s=0.001,
        job_poll_s=0.001,
    )
    host.clones = clones
    ports = iter(range(41001, 41100))
    return Manager(
        short_tmp / "ports",
        engine,
        remote=guests,
        wall=wall,
        port=lambda: next(ports),
        disk_free=lambda path: free,
        snapshot_cache_bytes=cache,
        memory_dir=memory,
        auto_fold=fold,
        clones=clones,
    )


def test_create_starts_a_vm_on_an_overlay_holds_its_clock_and_deploys(
    short_tmp, host, base, tree
) -> None:
    guests = Guests()
    record = manager(short_tmp, host, guests).create("lp1", base, tree, seed=SEED)
    root = short_tmp / "ports" / "lp1"
    assert host.creates()[0] == [
        "qemu-img", "create", "-q", "-f", "qcow2", "-b", str(base.disk), "-F", "qcow2",
        str(root / "disk-0.qcow2"),
    ]  # fmt: skip
    launch = " ".join(host.launches[0])
    assert f"file={root}/disk-0.qcow2," in launch and str(base.disk) not in launch
    assert "-name living-lp1 " in launch and "mac=52:55:55:a4:9c:41" in launch
    assert [command for _, command in guests.commands] == [
        (
            'sudo install -d -o "$(id -un)" -g "$(id -gn)" -m 755 /opt/ravensport '
            "/var/lib/competition /var/lib/competition/sim-state "
            "/var/lib/competition/episodes"
        ),
        living.HOLD_CLOCK,
        living.CLOCK_STATE,
        living.HOLD_UPDATES,
        living.UPDATES_STATE,
        living.VENV,
        "cat /var/lib/competition/living-port.json",
        "cat /var/lib/competition/living-port-settled.json",
    ]
    assert guests.sent == [(41001, tree, "/opt/ravensport", living.TREE_EXCLUDES)]
    assert guests.streams == [
        (
            41001,
            living.login(
                "cd /opt/ravensport && deploy/.venv-$(uname -s)-$(uname -m)/bin/python "
                f"-m competition.living.guest create --seed {SEED}"
            ),
        )
    ]
    assert record["status"] == "running" and record["started_at"] == STARTED_AT
    assert record["settled_at"] == SETTLED_AT
    assert json.loads((root / "port.json").read_text())["chain"] == ["disk-0.qcow2"]
    assert base.disk.read_bytes() == b"base image"


def test_the_clock_is_held_as_the_contract_says() -> None:
    """NTP off, and Lima's guest agent disabled and stopped, its unit file
    moved out of /etc/systemd/system, and the unit masked."""
    hold = living.HOLD_CLOCK
    steps = [
        "sudo timedatectl set-ntp false",
        "sudo systemctl disable --now lima-guestagent",
        "sudo mv /etc/systemd/system/lima-guestagent.service /var/lib/competition/",
        "sudo systemctl daemon-reload",
        "sudo systemctl mask lima-guestagent",
        "sudo systemctl reset-failed lima-guestagent",
    ]
    assert [hold.index(step) for step in steps] == sorted(
        hold.index(step) for step in steps
    )
    assert living.CLOCK_HELD == "ntp=no agent=inactive/masked"


def test_a_raw_base_gets_an_overlay_that_names_its_format(
    short_tmp, host, base, tree
) -> None:
    host.formats[str(base.disk)] = "raw"
    manager(short_tmp, host, Guests()).create("lp1", base, tree, seed=SEED)
    assert host.creates()[0][host.creates()[0].index("-F") + 1] == "raw"


def test_a_base_with_a_cloud_init_image_keeps_it_for_every_copy(
    short_tmp, host, base, tree
) -> None:
    cidata = base.disk.parent / "cidata.iso"
    cidata.write_bytes(b"CD001")
    lots = manager(short_tmp, host, Guests())
    lots.create("lp1", Base(**{**base.__dict__, "cidata": cidata}), tree, seed=SEED)
    lots.fork("lp1")
    for argv in host.launches:
        assert f"id=cidata,if=none,format=raw,readonly=on,file={cidata}" in argv


def test_create_refuses_an_existing_port_and_a_tree_without_the_package(
    short_tmp, host, base, tree
) -> None:
    lots = manager(short_tmp, host, Guests())
    with pytest.raises(PortError, match="not a RavensPort tree"):
        lots.create("lp1", base, short_tmp)
    lots.create("lp1", base, tree, seed=SEED)
    with pytest.raises(PortError, match="exists"):
        lots.create("lp1", base, tree, seed=SEED)
    with pytest.raises(PortError, match="lower-case"):
        lots.create("LP 2", base, tree)


def test_a_clock_still_set_by_a_time_source_stops_the_creation(
    short_tmp, host, base, tree
) -> None:
    guests = Guests()
    guests.clock = "ntp=yes agent=active/enabled"
    lots = manager(short_tmp, host, guests)
    with pytest.raises(PortError, match="not held"):
        lots.create("lp1", base, tree, seed=SEED)
    assert guests.streams == []
    assert lots.read("lp1")["status"] == "failed"
    assert host.servers["living-lp1"].names()[-1] == "quit"


def test_an_intel_mac_switches_the_guests_clock_source_before_the_deployment(
    short_tmp, host, base, tree
) -> None:
    """HVF on Intel keeps the guest's TSC counting through a pause, so the
    living port's guest is switched to the HPET, and the switch is checked,
    right after the clock is held and before the tree and the deployment."""
    guests = Guests()
    lots = manager(short_tmp, host, guests, hypervisor=INTEL_MAC)
    record = lots.create("lp1", base, tree, seed=SEED)
    commands = [command for _, command in guests.commands]
    switch = (
        "echo hpet | sudo tee "
        "/sys/devices/system/clocksource/clocksource0/current_clocksource > /dev/null"
    )
    assert commands[1:6] == [
        living.HOLD_CLOCK,
        living.CLOCK_STATE,
        switch,
        living.CLOCKSOURCE_STATE,
        living.HOLD_UPDATES,
    ]
    assert record["clocksource"] == "hpet"
    assert "q35,accel=hvf,hpet=on" in host.launches[0]
    assert guests.sent and guests.streams


def test_a_guest_that_keeps_its_clock_source_stops_the_creation(
    short_tmp, host, base, tree
) -> None:
    guests = Guests()
    guests.clocksource = "tsc"
    lots = manager(short_tmp, host, guests, hypervisor=INTEL_MAC)
    with pytest.raises(PortError, match="the clock source is tsc, not hpet"):
        lots.create("lp1", base, tree, seed=SEED)
    assert guests.sent == [] and guests.streams == []
    assert lots.read("lp1")["status"] == "failed"
    assert host.servers["living-lp1"].names()[-1] == "quit"


def test_linux_keeps_the_guests_clock_source(short_tmp, host, base, tree) -> None:
    guests = Guests()
    record = manager(short_tmp, host, guests).create("lp1", base, tree, seed=SEED)
    assert living.CLOCKSOURCE_STATE not in [command for _, command in guests.commands]
    assert "clocksource" not in record


def test_a_failed_deployment_marks_the_port_failed_and_stops_its_vm(
    short_tmp, host, base, tree
) -> None:
    guests = Guests()
    guests.stream_status = 2
    lots = manager(short_tmp, host, guests)
    with pytest.raises(PortError, match="exited 2"):
        lots.create("lp1", base, tree, seed=SEED)
    assert lots.read("lp1")["status"] == "failed"
    assert (
        lots.engine.status(Machine.from_dict(lots.read("lp1")["machine"])) == "stopped"
    )


def test_a_fork_saves_the_living_port_and_restores_a_copy(
    short_tmp, host, base, tree
) -> None:
    guests = Guests()
    lots = manager(short_tmp, host, guests)
    lots.create("lp1", base, tree, seed=SEED)
    root = short_tmp / "ports" / "lp1"
    fork = lots.fork("lp1")
    living_qemu = host.servers["living-lp1"]
    arguments = dict(living_qemu.commands)
    assert arguments["blockdev-snapshot-sync"]["snapshot-file"] == str(
        root / "disk-1.qcow2"
    )
    assert arguments["migrate"] == {"uri": f"file:{root}/states/fork-1.mig"}
    assert living_qemu.status == "running"
    copy_qemu = host.servers["living-lp1-fork-1"]
    assert (
        dict(copy_qemu.commands)["migrate-incoming"]["uri"]
        == f"file:{root}/states/fork-1.mig"
    )
    assert copy_qemu.status == "running"
    # The fork's overlay lies on the image frozen at the save.
    assert host.creates()[-1][host.creates()[-1].index("-b") + 1] == str(
        root / "disk-0.qcow2"
    )
    assert host.creates()[-1][-1] == str(root / "copies" / "fork-1" / "disk.qcow2")
    assert fork["k"] == 1 and fork["status"] == "running"
    assert fork["forked_at"] == iso(fork["saved"]["stopped_at"] + guests.offset_s)
    assert fork["age_s"] == pytest.approx(
        parse_utc(fork["forked_at"]) - parse_utc(STARTED_AT), abs=1e-3
    )
    assert fork["copy"]["ssh_port"] == 41002 and fork["copy"]["mac"] == base.mac
    assert fork["clock_shift_s"] == 0
    assert not (root / "states" / "fork-1.mig").exists()
    second = lots.fork("lp1")
    assert second["k"] == 2
    assert lots.read("lp1")["chain"] == ["disk-0.qcow2", "disk-1.qcow2", "disk-2.qcow2"]
    assert host.creates()[-1][host.creates()[-1].index("-b") + 1] == str(
        root / "disk-1.qcow2"
    )


def test_a_kept_state_file_stays_for_replay(short_tmp, host, base, tree) -> None:
    lots = manager(short_tmp, host, Guests())
    lots.create("lp1", base, tree, seed=SEED)
    fork = lots.fork("lp1", 4, keep_state=True)
    assert fork["state_kept"] is True
    assert Path(fork["saved"]["state"]).is_file()
    with pytest.raises(PortError, match="fork 4 exists"):
        lots.fork("lp1", 4)


def test_a_fork_of_a_stopped_living_port_is_refused(
    short_tmp, host, base, tree
) -> None:
    lots = manager(short_tmp, host, Guests())
    lots.create("lp1", base, tree, seed=SEED)
    lots.stop("lp1")
    with pytest.raises(PortError, match="is stopped"):
        lots.fork("lp1")


def test_a_save_that_would_leave_less_than_the_disk_reserve_waits(
    short_tmp, host, base, tree
) -> None:
    lots = manager(short_tmp, host, Guests(), free=15 * 10**9)
    lots.create("lp1", base, tree, seed=SEED)
    with pytest.raises(RoomError, match="below the reserve of 10 GB"):
        lots.fork("lp1")
    assert "stop" not in host.servers["living-lp1"].names()


def test_the_episode_command_is_the_contracts() -> None:
    assert (
        episode_command(3, "2026-11-11T20:15:00.000Z", "A", "adversarial", "c-01")
        == CONTRACT
    )
    benign = episode_command(1, "2026-11-11T20:15:00.000Z", "B", "benign")
    assert "--chain" not in benign
    assert "--mode" not in benign
    assert benign.endswith("--type benign --out /var/lib/competition/episodes/fork-1")
    training = episode_command(
        1,
        "2026-11-11T20:15:00.000Z",
        "B",
        "benign",
        training_volume="/var/lib/competition/training/fork-1",
    )
    assert training.endswith(
        "--type benign --mode training --training-volume "
        "/var/lib/competition/training/fork-1 "
        "--out /var/lib/competition/episodes/fork-1"
    )


def test_an_episode_runs_in_its_copy_and_the_copy_is_deleted_after_collection(
    short_tmp, host, base, tree
) -> None:
    guests = Guests()
    lots = manager(short_tmp, host, guests)
    lots.create("lp1", base, tree, seed=SEED)
    fork = lots.fork("lp1")
    episode = lots.episode("lp1", 1, track="A", episode_type="benign")
    root = short_tmp / "ports" / "lp1"
    assert guests.streams[-1] == (
        41002,
        living.login(episode_command(1, fork["forked_at"], "A", "benign")),
    )
    assert guests.fetched == [(41002, "/var/lib/competition/episodes/fork-1", False)]
    assert (root / "episodes" / "fork-1" / "record.json").is_file()
    assert episode["exit"] == 0 and episode["collected"] is True
    assert host.servers["living-lp1-fork-1"].names()[-1] == "quit"
    assert not (root / "copies" / "fork-1").exists()
    recorded = lots.read("lp1")["forks"]["1"]
    assert recorded["status"] == "deleted" and recorded["episode"]["exit"] == 0
    assert host.servers["living-lp1"].status == "running"


def test_a_copy_outside_the_living_ports_copies_is_never_deleted(
    short_tmp, host, base, tree
) -> None:
    lots = manager(short_tmp, host, Guests())
    lots.create("lp1", base, tree, seed=SEED)
    lots.fork("lp1")
    record = lots.read("lp1")
    record["forks"]["1"]["copy"]["rundir"] = str(short_tmp / "elsewhere")
    (short_tmp / "ports" / "lp1" / "port.json").write_text(json.dumps(record))
    with pytest.raises(PortError, match="outside"):
        lots.delete_copy("lp1", 1)


def test_a_stopped_simulator_ends_the_living_ports_forks(
    short_tmp, host, base, tree
) -> None:
    guests = Guests()
    lots = manager(short_tmp, host, guests)
    lots.create("lp1", base, tree, seed=SEED)
    guests.simulator = "crashed"
    assert lots.loop("lp1", track="A", episode_type="benign", forks=1) == []
    assert "stop" not in host.servers["living-lp1"].names()


def test_list_shows_each_living_port_and_its_running_copies(
    short_tmp, host, base, tree
) -> None:
    lots = manager(short_tmp, host, Guests())
    lots.create("lp1", base, tree, seed=SEED)
    lots.fork("lp1")
    (listed,) = lots.list()
    assert listed["name"] == "lp1" and listed["status"] == "running"
    assert listed["copies"] == {"1": {"status": "running", "ssh_port": 41002}}
    assert listed["chain"] == 2 and listed["forks"] == 1


def test_stop_with_delete_removes_the_living_port_but_not_its_base(
    short_tmp, host, base, tree
) -> None:
    lots = manager(short_tmp, host, Guests())
    lots.create("lp1", base, tree, seed=SEED)
    lots.fork("lp1")
    lots.stop("lp1", delete=True)
    assert host.servers["living-lp1"].names()[-1] == "quit"
    assert host.servers["living-lp1-fork-1"].names()[-1] == "quit"
    assert not (short_tmp / "ports" / "lp1").exists()
    assert base.disk.read_bytes() == b"base image"


def test_a_snapshot_is_a_save_with_no_copy_and_keeps_its_state(
    short_tmp, host, base, tree
) -> None:
    lots = manager(short_tmp, host, Guests(), cache=10**9)
    lots.create("lp1", base, tree, seed=SEED)
    kept = lots.snapshot("lp1")
    root = short_tmp / "ports" / "lp1"
    assert kept["n"] == 1 and kept["fork"] is None
    assert kept["state"] == str(root / "snapshots" / "snapshot-1.mig")
    assert Path(kept["state"]).is_file()
    assert kept["frozen"] == str(root / "disk-0.qcow2")
    assert len(host.launches) == 1
    record = lots.read("lp1")
    assert record["chain"] == ["disk-0.qcow2", "disk-1.qcow2"]
    assert record["forks"] == {}


def test_the_oldest_snapshots_beyond_the_cache_are_pruned(
    short_tmp, host, base, tree
) -> None:
    lots = manager(short_tmp, host, Guests(), cache=10**9)
    lots.create("lp1", base, tree, seed=SEED)
    size = lots.snapshot("lp1")["state_bytes"]
    # Room for two snapshots from now on.
    lots._snapshot_cache_bytes = 2 * size
    lots.snapshot("lp1")
    lots.snapshot("lp1")
    snapshots = lots.read("lp1")["snapshots"]
    assert [bool(s.get("pruned_at")) for s in snapshots] == [True, False, False]
    assert [Path(s["state"]).exists() for s in snapshots] == [False, True, True]


def test_the_next_snapshot_follows_the_latest_on_the_schedule() -> None:
    assert living.next_snapshot_age({"snapshots": []}) == 900.0
    assert living.next_snapshot_age({"snapshots": [{"age_s": 900.4}]}) == 1800.0
    assert living.next_snapshot_age({"snapshots": [{"age_s": 3600.2}]}) == 5400.0


def test_the_guests_commands_run_in_a_login_shell() -> None:
    """The guest's profile puts cargo on the PATH, and an SSH command's own
    shell reads no profile."""
    command = episode_command(1, "2026-11-11T20:15:00.000Z", "A", "benign")
    assert living.login(command) == f"bash -lc '{command}'"


def test_the_first_fork_falls_a_drawn_wait_after_the_living_port_settled(
    short_tmp, host, base, tree
) -> None:
    lots = manager(short_tmp, host, Guests())
    lots.create("lp1", base, tree, seed=SEED)
    due, drawn = lots.fork_due(lots.read("lp1"))
    assert due == pytest.approx(parse_utc(SETTLED_AT) + fork_wait_s(SEED, 1))
    assert drawn["after"] == "the settle period" and drawn["after_at"] == SETTLED_AT
    assert 0.0 <= drawn["wait_s"] <= 360.0


def test_the_loop_forks_on_the_drawn_schedule_of_the_living_ports_clock(
    short_tmp, host, base, tree
) -> None:
    """Fork 1 falls one drawn wait after the settle period, and fork 2 the
    spacing and one drawn wait after fork 1's due instant, whatever the end of
    fork 1's episode, so that the instants depend on the seed alone."""
    wall = FakeWall(parse_utc(SETTLED_AT) + 12.5)
    lots = manager(short_tmp, host, Guests(wall), wall=wall)
    lots.create("lp1", base, tree, seed=SEED)
    made = lots.loop("lp1", track="A", episode_type="benign", forks=2, stop=wall)
    assert made == [1, 2]
    first, second = (lots.read("lp1")["forks"][k] for k in ("1", "2"))
    due_1 = parse_utc(SETTLED_AT) + fork_wait_s(SEED, 1)
    due_2 = due_1 + FORK_SPACING_S + fork_wait_s(SEED, 2)
    assert first["drawn"]["due_at"] == iso(due_1)
    assert second["drawn"]["after_at"] == first["drawn"]["due_at"]
    assert second["drawn"]["due_at"] == iso(due_2)
    for fork, k in ((first, 1), (second, 2)):
        assert fork["drawn"]["wait_s"] == pytest.approx(fork_wait_s(SEED, k), abs=1e-3)
        # The actual instant, within the loop's recheck of the due instant.
        assert 0.0 <= fork["drawn"]["late_s"] < 61.0
        assert fork["status"] == "deleted"
    assert FORK_SPACING_S == 840.0


def test_the_episode_record_gains_the_fork_wait_the_pause_and_the_clock_offsets(
    short_tmp, host, base, tree
) -> None:
    """Only the manager holds the fork wait, the pause and the clocks'
    offsets, so it writes them into the record that the fork wrote, once the
    outputs are on the host (episodes.md §13, episode record)."""
    wall = FakeWall(parse_utc(SETTLED_AT) + 12.5)
    guests = Guests(wall)
    guests.records = {"/var/lib/competition/episodes/fork-1": {"fork": 1}}
    lots = manager(short_tmp, host, guests, wall=wall)
    lots.create("lp1", base, tree, seed=SEED)
    lots.loop("lp1", track="A", episode_type="benign", forks=1, stop=wall)
    fork = lots.read("lp1")["forks"]["1"]
    record = json.loads(
        (
            short_tmp / "ports" / "lp1" / "episodes" / "fork-1" / "record.json"
        ).read_text()
    )
    assert record["fork"] == 1
    assert record["fork_wait"] == fork["drawn"]
    assert record["fork_wait"]["wait_s"] == pytest.approx(
        fork_wait_s(SEED, 1), abs=1e-3
    )
    assert record["living_port_pause_s"] == fork["saved"]["pause_s"]
    assert record["clock_offsets"] == {
        "living_port_before_the_save": fork["clock_before"],
        "living_port_after_the_save": fork["clock_after"],
        "living_port_shift_s": fork["clock_shift_s"],
        "copy": fork["episode"]["copy_clock"],
    }
    assert fork["episode"]["copy_clock"]["offset_s"] == guests.offset_s
    assert fork["episode"]["recorded"] is True


def test_a_discarded_episode_is_recorded_and_the_next_fork_runs(
    short_tmp, host, base, tree
) -> None:
    """A fork that discards its episode keeps the record that states the
    cause, no fork runs again, and the living port continues with its next
    fork (episodes.md §12, terminated and discarded episodes)."""
    wall = FakeWall(parse_utc(SETTLED_AT) + 12.5)
    guests = Guests(wall)
    cause = (
        "the fork failed to settle: condition 1 still failed at the settle "
        "timeout of 120 s"
    )
    guests.records = {
        "/var/lib/competition/episodes/fork-1": {
            "outcome": "discarded",
            "cause": cause,
        },
        "/var/lib/competition/episodes/fork-2": {"outcome": "horizon", "cause": None},
    }
    lots = manager(short_tmp, host, guests, wall=wall)
    lots.create("lp1", base, tree, seed=SEED)
    made = lots.loop("lp1", track="A", episode_type="benign", forks=2, stop=wall)
    assert made == [1, 2]
    forks = lots.read("lp1")["forks"]
    first, second = forks["1"]["episode"], forks["2"]["episode"]
    assert (first["outcome"], first["cause"]) == ("discarded", cause)
    assert (second["outcome"], second["cause"]) == ("horizon", None)
    assert forks["1"]["status"] == forks["2"]["status"] == "deleted"
    runs = [c for _, c in guests.streams if "competition.episode_runner copy" in c]
    assert [c.split("--fork ")[1].split()[0] for c in runs] == ["1", "2"]
    record = json.loads(
        (
            short_tmp / "ports" / "lp1" / "episodes" / "fork-1" / "record.json"
        ).read_text()
    )
    assert (record["outcome"], record["cause"]) == ("discarded", cause)


def test_a_fork_due_during_an_episode_is_saved_on_time_and_restored_after(
    short_tmp, host, base, tree
) -> None:
    wall = FakeWall(parse_utc(SETTLED_AT) + 400.0)
    guests = Guests(wall)
    guests.block = threading.Event()
    lots = manager(short_tmp, host, guests, wall=wall)
    lots.create("lp1", base, tree, seed=SEED)
    made: list[int] = []
    loop = threading.Thread(
        target=lambda: made.extend(
            lots.loop(
                "lp1",
                track="A",
                episode_type="benign",
                forks=2,
                stop=wall,
                recheck_s=0.01,
            )
        )
    )
    loop.start()
    try:
        deadline = time.monotonic() + 10
        while not any("copy --fork 1" in c for _, c in guests.streams):
            assert time.monotonic() < deadline
            time.sleep(0.01)
        # The living port's clock reaches fork 2's due instant as episode 1 runs.
        due_2, _ = lots.fork_due(lots.read("lp1"))
        wall.at = due_2 - guests.offset_s
        while lots.read("lp1")["forks"].get("2", {}).get("status") != "saved":
            assert time.monotonic() < deadline
            time.sleep(0.01)
        forks = lots.read("lp1")["forks"]
        assert forks["1"]["status"] == "running"
        assert forks["2"]["drawn"]["late_s"] == pytest.approx(0.0, abs=1e-3)
        assert "living-lp1-fork-2" not in host.servers
    finally:
        guests.block.set()
        loop.join(timeout=10)
    assert made == [1, 2]
    second = lots.read("lp1")["forks"]["2"]
    assert second["status"] == "deleted" and second["episode"]["exit"] == 0
    # Fork 2 started only once fork 1 had gone.
    assert "living-lp1-fork-1" in host.quit_at_launch["living-lp1-fork-2"]


def test_the_loop_keeps_the_snapshots_that_fall_due_in_order(
    short_tmp, host, base, tree
) -> None:
    """Fork 1 falls due before the snapshot of 900 s, so the fork comes first,
    and the snapshot follows while fork 1's episode runs."""
    wall = FakeWall(parse_utc(STARTED_AT) + 2000.0)
    lots = manager(short_tmp, host, Guests(wall), wall=wall, cache=10**9)
    lots.create("lp1", base, tree, seed=SEED)
    lots.loop("lp1", track="A", episode_type="benign", forks=1, stop=wall)
    record = lots.read("lp1")
    assert Path(record["forks"]["1"]["saved"]["frozen"]).name == "disk-0.qcow2"
    (kept,) = record["snapshots"]
    assert Path(kept["frozen"]).name == "disk-1.qcow2" and kept["fork"] is None
    assert kept["age_s"] == pytest.approx(1997.5)


def test_the_loop_takes_no_snapshot_that_the_cache_cannot_keep(
    short_tmp, host, base, tree
) -> None:
    """A cache smaller than one state would prune each snapshot at once, so the
    loop takes none and pauses the living port only for its fork."""
    wall = FakeWall(parse_utc(STARTED_AT) + 2000.0)
    lots = manager(short_tmp, host, Guests(wall), wall=wall, cache=1000)
    lots.create("lp1", base, tree, seed=SEED)
    lots.loop("lp1", track="A", episode_type="benign", forks=1, stop=wall)
    record = lots.read("lp1")
    assert list(record["forks"]) == ["1"]
    assert record["snapshots"] == []


def test_a_snapshot_is_expected_at_the_largest_state_saved_or_else_the_memory() -> None:
    record = {"machine": {"memory_mib": 8192}, "snapshots": [], "forks": {}}
    assert snapshot_bytes_expected(record) == 8192 * 2**20
    record["forks"]["1"] = {"saved": {"state_allocated_bytes": 7_900_000_000}}
    record["snapshots"].append({"state_bytes": 7_950_000_000})
    assert snapshot_bytes_expected(record) == 7_950_000_000


def test_the_evaluation_volume_is_attached_read_only_at_the_creation(
    short_tmp, host, base, tree
) -> None:
    volume = short_tmp / "submitted"
    volume.mkdir()
    (volume / "policy").write_text("rules")
    guests = Guests()
    record = manager(short_tmp, host, guests).create(
        "lp1", base, tree, seed=SEED, volume=volume
    )
    assert (41001, volume, "/var/lib/competition/volume", ()) in guests.sent
    ((_, command),) = guests.streams
    assert "--volume /var/lib/competition/volume" in command
    assert "--volume-mount" not in command
    assert record["volume"] == {
        "from": str(volume),
        "in_vm": "/var/lib/competition/volume",
        "mount": "/var/lib/entry",
        "readonly": True,
    }


def test_a_living_port_runs_in_evaluation_or_in_training(
    short_tmp, host, base, tree
) -> None:
    volume = short_tmp / "v"
    volume.mkdir()
    with pytest.raises(PortError, match="not both"):
        manager(short_tmp, host, Guests()).create(
            "lp1", base, tree, volume=volume, training_volume=volume
        )


def test_the_training_volume_passes_to_each_copy(short_tmp, host, base, tree) -> None:
    start = short_tmp / "training-start"
    start.mkdir()
    (start / "state").write_text("version 0")
    guests = Guests()
    lots = manager(short_tmp, host, guests)
    lots.create("lp1", base, tree, seed=SEED, training_volume=start)
    root = short_tmp / "ports" / "lp1"
    assert (root / "training" / "v0" / "state").read_text() == "version 0"
    lots.fork("lp1")
    first = lots.episode("lp1", 1, track="A", episode_type="benign")
    assert first["training_in"] == {
        "version": 0,
        "in_copy": "/var/lib/competition/training/fork-1",
    }
    assert first["training_out"] == {"version": 1}
    assert (
        41002,
        root / "training" / "v0",
        "/var/lib/competition/training/fork-1",
        (),
    ) in guests.sent
    # The episode runner's `copy` command attaches the volume to siem-01 and
    # detaches it, in training mode; the manager touches no device.
    commands = [command for port, command in guests.commands if port == 41002]
    assert not any("incus config device" in c for c in commands)
    ((_, command),) = [s for s in guests.streams if s[0] == 41002]
    assert (
        "--mode training --training-volume /var/lib/competition/training/fork-1 "
        "--out /var/lib/competition/episodes/fork-1"
    ) in command
    assert (41002, "/var/lib/competition/training/fork-1", True) in guests.fetched
    assert (root / "training" / "v1" / "written").is_file()
    lots.fork("lp1")
    second = lots.episode("lp1", 2, track="A", episode_type="benign")
    assert (second["training_in"]["version"], second["training_out"]) == (
        1,
        {"version": 2},
    )
    training = lots.read("lp1")["training"]
    assert training["current"] == 2
    assert [(v["v"], v.get("fork")) for v in training["versions"]] == [
        (0, None),
        (1, 1),
        (2, 2),
    ]


def test_an_episode_that_failed_sets_its_training_volume_aside(
    short_tmp, host, base, tree
) -> None:
    start = short_tmp / "training-start"
    start.mkdir()
    guests = Guests()
    lots = manager(short_tmp, host, guests)
    lots.create("lp1", base, tree, seed=SEED, training_volume=start)
    lots.fork("lp1")
    guests.stream_status = 1
    episode = lots.episode("lp1", 1, track="A", episode_type="benign")
    assert episode["training_out"]["version"] is None
    assert Path(episode["training_out"]["set_aside"]).is_dir()
    assert lots.read("lp1")["training"]["current"] == 0


def test_an_out_that_exists_in_the_copy_is_refused(short_tmp, host, base, tree) -> None:
    guests = Guests()
    lots = manager(short_tmp, host, guests)
    lots.create("lp1", base, tree, seed=SEED)
    lots.fork("lp1")
    guests.out_exists = True
    with pytest.raises(PortError, match="must not"):
        lots.run_episode("lp1", 1, track="A", episode_type="benign")
    assert not any("episode_runner copy" in command for _, command in guests.streams)


def test_no_wait_comes_between_a_copy_and_its_episode(
    short_tmp, host, base, tree
) -> None:
    """A fork has no settle delay: the runner checks the four conditions
    itself, so the manager's own steps before the command are only the check
    that the fork answers and that --out does not exist."""
    guests = Guests()
    lots = manager(short_tmp, host, guests)
    lots.create("lp1", base, tree, seed=SEED)
    lots.fork("lp1")
    before = len(guests.commands)
    lots.run_episode("lp1", 1, track="A", episode_type="benign")
    copy_commands = [c for p, c in guests.commands[before:] if p == 41002]
    assert copy_commands == ["test ! -e /var/lib/competition/episodes/fork-1"]


def test_a_replay_restores_a_kept_snapshot_into_a_copy(
    short_tmp, host, base, tree
) -> None:
    lots = manager(short_tmp, host, Guests(), cache=10**9)
    lots.create("lp1", base, tree, seed=SEED)
    kept = lots.snapshot("lp1")
    replay = lots.replay("lp1", 1)
    assert replay["replay"] == "1-1" and replay["snapshot"] == 1
    qemu = host.servers["living-lp1-replay-1-1"]
    assert dict(qemu.commands)["migrate-incoming"]["uri"] == f"file:{kept['state']}"
    assert qemu.status == "running"
    assert host.creates()[-1][host.creates()[-1].index("-b") + 1] == kept["frozen"]
    assert Path(kept["state"]).is_file()
    assert lots.list()[0]["copies"]["replay 1-1"]["status"] == "running"
    lots.delete_replay("lp1", "1-1")
    assert qemu.names()[-1] == "quit"
    assert lots.read("lp1")["replays"]["1-1"]["status"] == "deleted"
    with pytest.raises(PortError, match="no snapshot 2"):
        lots.replay("lp1", 2)


def test_a_save_to_memory_restores_from_memory_and_moves_a_kept_state(
    short_tmp, host, base, tree
) -> None:
    memory = short_tmp / "shm"
    memory.mkdir()
    lots = manager(short_tmp, host, Guests(), cache=10**9, memory=memory)
    lots.create("lp1", base, tree, seed=SEED)
    root = short_tmp / "ports" / "lp1"
    fork = lots.fork("lp1")
    qemu = host.servers["living-lp1"]
    written = dict(qemu.commands)["migrate"]["uri"].removeprefix("file:")
    assert written == str(memory / "ravensport-lp1-fork-1.mig")
    copy = host.servers["living-lp1-fork-1"]
    assert dict(copy.commands)["migrate-incoming"]["uri"] == f"file:{written}"
    assert not Path(written).exists() and fork["state_kept"] is False
    kept = lots.snapshot("lp1")
    assert kept["state"] == str(root / "snapshots" / "snapshot-1.mig")
    assert Path(kept["state"]).is_file() and list(memory.iterdir()) == []
    assert kept["state_moved_s"] >= 0.0


def test_a_deleted_copys_frozen_image_is_folded_into_the_top_and_deleted(
    short_tmp, host, base, tree
) -> None:
    lots = manager(short_tmp, host, Guests())
    lots.create("lp1", base, tree, seed=SEED)
    root = short_tmp / "ports" / "lp1"
    lots.fork("lp1")
    assert lots.read("lp1")["chain"] == ["disk-0.qcow2", "disk-1.qcow2"]
    lots.episode("lp1", 1, track="A", episode_type="benign")
    qemu = host.servers["living-lp1"]
    # Nothing needs disk-0 once the fork has gone: the top streams down to the
    # base, which is never written.
    assert dict(qemu.commands)["block-stream"]["base"] == str(base.disk)
    record = lots.read("lp1")
    assert record["chain"] == ["disk-1.qcow2"]
    assert record["backing"]["disk-1.qcow2"] == living.BASE
    assert not (root / "disk-0.qcow2").exists()
    assert record["folds"][0]["folded"] == ["disk-0.qcow2"]
    assert base.disk.read_bytes() == b"base image"
    # The fork's own file was deleted, never folded.
    assert not (root / "copies" / "fork-1").exists()


def test_a_fold_keeps_the_images_that_a_running_copy_or_a_kept_snapshot_reads(
    short_tmp, host, base, tree
) -> None:
    lots = manager(short_tmp, host, Guests(), cache=10**9, fold=False)
    lots.create("lp1", base, tree, seed=SEED)
    root = short_tmp / "ports" / "lp1"
    lots.fork("lp1")  # freezes disk-0, read by fork 1
    lots.snapshot("lp1")  # freezes disk-1, kept
    for k in (2, 3, 4):  # freeze disk-2 to disk-4, whose forks go
        lots.fork("lp1", k)
        lots.delete_copy("lp1", k)
    assert lots.read("lp1")["chain"] == [f"disk-{n}.qcow2" for n in range(6)]
    entry = lots.fold("lp1")
    qemu = host.servers["living-lp1"]
    assert dict(qemu.commands)["block-stream"]["base"] == str(root / "disk-1.qcow2")
    assert entry["folded"] == ["disk-2.qcow2", "disk-3.qcow2", "disk-4.qcow2"]
    record = lots.read("lp1")
    assert record["chain"] == ["disk-0.qcow2", "disk-1.qcow2", "disk-5.qcow2"]
    assert [n for n in range(6) if (root / f"disk-{n}.qcow2").exists()] == [0, 1, 5]
    # With fork 1 and the snapshot gone, the rest folds down to the base.
    lots.delete_copy("lp1", 1)
    lots._snapshot_cache_bytes = 1
    lots._auto_fold = True
    lots.snapshot("lp1")  # prunes both snapshots, then folds
    record = lots.read("lp1")
    assert record["chain"] == ["disk-6.qcow2"]
    assert record["backing"]["disk-6.qcow2"] == living.BASE
    assert [p.name for p in sorted(root.glob("disk-*.qcow2"))] == ["disk-6.qcow2"]


def test_a_fold_with_nothing_to_fold_streams_nothing(
    short_tmp, host, base, tree
) -> None:
    lots = manager(short_tmp, host, Guests())
    lots.create("lp1", base, tree, seed=SEED)
    assert lots.fold("lp1") is None
    lots.fork("lp1")
    assert lots.fold("lp1") is None  # the fork still reads disk-0
    assert "block-stream" not in host.servers["living-lp1"].names()


def test_a_stream_that_fails_raises_and_deletes_nothing(
    short_tmp, host, base, tree
) -> None:
    lots = manager(short_tmp, host, Guests(), fold=False)
    lots.create("lp1", base, tree, seed=SEED)
    lots.fork("lp1")
    lots.delete_copy("lp1", 1)
    host.servers["living-lp1"].stream_error = "Input/output error"
    with pytest.raises(VMError, match="Input/output error"):
        lots.fold("lp1")
    assert (short_tmp / "ports" / "lp1" / "disk-0.qcow2").exists()
    assert lots.read("lp1")["chain"] == ["disk-0.qcow2", "disk-1.qcow2"]


def test_with_clones_a_fork_gives_its_copy_a_clone_and_adds_no_layer(
    short_tmp, host, base, tree
) -> None:
    lots = manager(short_tmp, host, Guests(), clones=True)
    lots.create("lp1", base, tree, seed=SEED)
    root = short_tmp / "ports" / "lp1"
    fork = lots.fork("lp1")
    qemu = host.servers["living-lp1"]
    assert "blockdev-snapshot-sync" not in qemu.names()
    names = qemu.names()
    assert names.index("stop") < names.index("migrate") < names.index("cont")
    disk = root / "copies" / "fork-1" / "disk.qcow2"
    assert host.clone_runs()[-1][-2:] == [str(root / "disk-0.qcow2"), str(disk)]
    assert disk.is_file() and fork["disk_by"] == "clone"
    assert f"file={disk}," in " ".join(host.launches[-1])
    # The fork took the clone itself: no overlay was made for it.
    assert all(argv[-1] != str(disk) for argv in host.creates())
    assert lots.read("lp1")["chain"] == ["disk-0.qcow2"]
    lots.episode("lp1", 1, track="A", episode_type="benign")
    assert not disk.exists() and "block-stream" not in qemu.names()


def test_with_clones_a_snapshot_keeps_a_clone_that_a_replay_backs_onto(
    short_tmp, host, base, tree
) -> None:
    lots = manager(short_tmp, host, Guests(), cache=10**9, clones=True)
    lots.create("lp1", base, tree, seed=SEED)
    root = short_tmp / "ports" / "lp1"
    kept = lots.snapshot("lp1")
    assert kept["frozen"] == str(root / "snapshots" / "snapshot-1.qcow2")
    assert Path(kept["frozen"]).is_file()
    lots.replay("lp1", 1)
    overlay = host.creates()[-1]
    assert overlay[overlay.index("-b") + 1] == kept["frozen"]
    assert lots.read("lp1")["chain"] == ["disk-0.qcow2"]


def test_the_clone_probe_finds_whether_the_file_system_clones(short_tmp, host) -> None:
    engine = Engine(KVM, run=lambda argv, **kw: host.run(argv, **kw))
    host.clones = False
    assert engine.can_clone(short_tmp) is False
    host.clones = True
    assert engine.can_clone(short_tmp) is True
    assert list(short_tmp.iterdir()) == []


def test_a_fork_kept_as_a_snapshot_keeps_its_state_for_replay(
    short_tmp, host, base, tree
) -> None:
    lots = manager(short_tmp, host, Guests(), cache=10**9)
    lots.create("lp1", base, tree, seed=SEED)
    fork = lots.fork("lp1", snapshot=True)
    (kept,) = lots.read("lp1")["snapshots"]
    assert kept["fork"] == 1 and kept["taken_at"] == fork["forked_at"]
    assert Path(kept["state"]).is_file()
    assert kept["frozen"] == fork["saved"]["frozen"]
    replay = lots.replay("lp1", 1)
    assert replay["snapshot"] == 1


def test_a_copy_waits_for_its_save_and_a_save_for_nothing(
    short_tmp, host, base, tree
) -> None:
    lots = manager(short_tmp, host, Guests())
    lots.create("lp1", base, tree, seed=SEED)
    saved = lots.save_fork("lp1")
    assert saved["status"] == "saved" and "living-lp1-fork-1" not in host.servers
    # Nothing folds the frozen image of a save that waits to be restored.
    lots.fork("lp1", 2)
    lots.delete_copy("lp1", 2)
    root = short_tmp / "ports" / "lp1"
    assert (root / "disk-0.qcow2").exists()
    restored = lots.restore_fork("lp1", 1)
    assert restored["status"] == "running"
    assert host.servers["living-lp1-fork-1"].status == "running"
    with pytest.raises(PortError, match="no save waiting"):
        lots.restore_fork("lp1", 1)


def test_on_qemu_8_2_forks_and_replays_run_in_the_stream_format(
    short_tmp, host, base, tree
) -> None:
    from living_fakes import QEMU_8_2

    host.features = QEMU_8_2
    lots = manager(short_tmp, host, Guests(), cache=10**9)
    lots.create("lp1", base, tree, seed=SEED)
    fork = lots.fork("lp1")
    assert (fork["saved"]["format"], fork["saved"]["qemu_version"]) == (
        "stream",
        "8.2.2",
    )
    assert (
        "exit-on-error"
        not in dict(host.servers["living-lp1-fork-1"].commands)["migrate-incoming"]
    )
    kept = lots.snapshot("lp1")
    assert kept["saved"]["format"] == "stream"
    lots.replay("lp1", kept["n"])
    assert host.servers["living-lp1-replay-1-1"].status == "running"


def test_the_updates_are_disabled_stopped_and_masked() -> None:
    """A living port runs for days: the APT timers and unattended upgrades,
    which restarted Incus in the twin VMs, never run in it."""
    units = "apt-daily.timer apt-daily-upgrade.timer unattended-upgrades.service"
    assert living.UPDATE_UNITS == tuple(units.split())
    hold = living.HOLD_UPDATES
    assert hold.index(f"sudo systemctl disable --now {units}") < hold.index(
        f"sudo systemctl mask {units}"
    )
    assert living.UPDATES_HELD.splitlines() == [
        f"{unit}=inactive/masked" for unit in units.split()
    ]


def test_the_updates_are_held_before_the_deployment(
    short_tmp, host, base, tree
) -> None:
    guests = Guests()
    record = manager(short_tmp, host, guests).create("lp1", base, tree, seed=SEED)
    commands = [command for _, command in guests.commands]
    assert commands.index(living.HOLD_UPDATES) < commands.index(living.VENV)
    assert record["updates"] == living.UPDATES_HELD
    # The deployment, in the guest's create, comes after the hold.
    assert len(guests.streams) == 1


def test_a_guest_that_would_still_update_itself_stops_the_creation(
    short_tmp, host, base, tree
) -> None:
    guests = Guests()
    guests.updates = (
        "apt-daily.timer=active/enabled\n"
        "apt-daily-upgrade.timer=inactive/masked\n"
        "unattended-upgrades.service=inactive/masked"
    )
    lots = manager(short_tmp, host, guests)
    with pytest.raises(
        PortError, match="the updates are not held: apt-daily.timer=active"
    ):
        lots.create("lp1", base, tree, seed=SEED)
    assert guests.streams == [] and guests.sent == []
    assert lots.read("lp1")["status"] == "failed"
    assert host.servers["living-lp1"].names()[-1] == "quit"
