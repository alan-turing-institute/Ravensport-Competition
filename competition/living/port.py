"""The living port and its forks (the living port contract; episodes.md
§6.3, the living port; §6.4, how a fork is made).

A living port is one virtual machine (VM), started from a base disk through an
overlay of its own, so that the base is only ever a read-only backing file.
The manager:

- creates it: boots the VM, holds its clock, puts the tree at /opt/ravensport
  with its virtual environment, and runs guest.py inside it, which writes the
  start-up input, deploys with the competition's additional override file and
  writes the identity file;
- forks it: saves the running VM and restores the fork from the save;
- runs one episode in a fork by the contract's command, copies the episode's
  outputs to the host, adds to the episode record the fork wait, the pause and
  the clocks' offsets, and deletes the fork; a discarded episode is not run
  again, and the living port continues with its next fork;
- keeps snapshots for replay: saves from which no fork is restored, within a
  cache, and restores a kept snapshot on a virtual machine of its own;
- folds each frozen image into the living port's top once nothing needs it,
  or, where the file system clones files, clones the top at each save and
  adds no layer;
- loops: forks on the drawn schedule of the living port's own clock, runs
  one episode at a time, and keeps the snapshots on their schedule;
- lists living ports, and stops them.

Each living port keeps its records in ROOT/NAME/port.json, beside its overlays
(disk-N.qcow2), its run directory, its saved states and snapshots, the disks
and run directories of its forks and replays, and the episodes' outputs.
Nothing outside ROOT/NAME is ever written or deleted.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import re
import secrets
import shlex
import shutil
import socket
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from competition.living.snapshots import (
    FORK_SPACING_S,
    fork_wait_s,
    kept_snapshots,
    snapshot_ages_s,
)
from competition.living.ssh import ClockOffset, Remote, Ssh, SSHError
from competition.living.vm import Engine, Machine, Saved, VMError

GUEST_TREE = "/opt/ravensport"
GUEST_STATE = "/var/lib/competition"
IDENTITY = f"{GUEST_STATE}/living-port.json"
SETTLED = f"{GUEST_STATE}/living-port-settled.json"
# The entry's submitted storage volume, in the VM, attached read-only to
# siem-01 in evaluation; and the training volume of each fork, which the
# episode runner's `copy` command attaches read-write to siem-01.
GUEST_VOLUME = f"{GUEST_STATE}/volume"
GUEST_TRAINING = f"{GUEST_STATE}/training"
# Where siem-01 mounts either volume, the one path that the launcher and the
# episode runner's test of the entry's defender read (episodes.md §3.2, modes).
VOLUME_MOUNT = "/var/lib/entry"
# The backing file of the living port's first image, outside the living port.
BASE = "base"
EPISODES = f"{GUEST_STATE}/episodes"
# The virtual environment that `make install` creates in deploy/, whose name
# carries the platform; the guest's shell expands it.
PYTHON = "deploy/.venv-$(uname -s)-$(uname -m)/bin/python"
# What copying the tree into the VM leaves out: build outputs, virtual
# environments and version control, which the VM builds or needs not.
TREE_EXCLUDES = (
    "./.git",
    "*/target",
    "./deploy/.venv*",
    "./deploy/build",
    "*/__pycache__",
)
# The contract's hold of the clock: NTP off, and Lima's guest agent stopped
# for good. Lima installs the agent's unit file in /etc/systemd/system, where
# `systemctl mask` cannot place its link, so the agent is disabled and stopped,
# its unit file moved out to the competition's directory, the unit masked, and
# a failure of the agent at the boot reset.
AGENT_UNIT = "/etc/systemd/system/lima-guestagent.service"
HOLD_CLOCK = (
    "sudo timedatectl set-ntp false"
    " && { sudo systemctl disable --now lima-guestagent 2>/dev/null || true; }"
    f" && {{ [ -L {AGENT_UNIT} ] || [ ! -e {AGENT_UNIT} ]"
    f" || sudo mv {AGENT_UNIT} {GUEST_STATE}/; }}"
    " && sudo systemctl daemon-reload && sudo systemctl mask lima-guestagent"
    # A guest agent that cannot reach its host, as under QEMU on the Mac, has
    # failed at the boot, and stays failed until reset.
    " && { sudo systemctl reset-failed lima-guestagent 2>/dev/null || true; }"
)
CLOCK_STATE = (
    "printf 'ntp=%s agent=%s/%s\\n' \"$(timedatectl show -p NTP --value)\" "
    '"$(systemctl is-active lima-guestagent)" "$(systemctl is-enabled lima-guestagent)"'
)
CLOCK_HELD = "ntp=no agent=inactive/masked"
# The guest's clock source, where the platform names one (Hypervisor's
# guest_clocksource): on an Intel Mac, HVF keeps the guest's TSC counting
# through a pause, and the HPET stands still with the VM. The switch at run
# time lasts the living port's life, since it never boots again, and passes
# to every fork, which never boots.
CLOCKSOURCE = "/sys/devices/system/clocksource/clocksource0/current_clocksource"
CLOCKSOURCE_STATE = f"cat {CLOCKSOURCE}"
# A living port runs for days and must not update itself: in the twin VMs,
# unattended upgrades ran 13 minutes after the boot, and needrestart then
# restarted incus.service four times in 6 minutes. The Advanced Package Tool's
# (APT's) daily timers and unattended upgrades are stopped and masked.
UPDATE_UNITS = (
    "apt-daily.timer",
    "apt-daily-upgrade.timer",
    "unattended-upgrades.service",
)
HOLD_UPDATES = (
    f"{{ sudo systemctl disable --now {' '.join(UPDATE_UNITS)} 2>/dev/null || true; }}"
    f" && sudo systemctl mask {' '.join(UPDATE_UNITS)}"
)
UPDATES_STATE = (
    f"for unit in {' '.join(UPDATE_UNITS)}; do printf '%s=%s/%s\\n' \"$unit\" "
    '"$(systemctl is-active "$unit")" "$(systemctl is-enabled "$unit")"; done'
)
UPDATES_HELD = "\n".join(f"{unit}=inactive/masked" for unit in UPDATE_UNITS)
GUEST_DIRS = (GUEST_TREE, GUEST_STATE, f"{GUEST_STATE}/sim-state", EPISODES)
VENV = f"cd {GUEST_TREE} && make -C deploy install"
SIMULATOR_STATUS = "incus exec sim-host -- rc-service sim status"
GIB = 1024**3
MIB = 1024**2
# Host memory a fork needs beyond its guest's, for QEMU itself.
MEMORY_MARGIN = GIB
EPISODE_TIMEOUT_S = 3 * 3600.0
_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")

log = logging.getLogger(__name__)


class PortError(Exception):
    """A living port or a fork could not be made, run or found."""


class RoomError(PortError):
    """The host has too little disk or memory for a save or a fork now."""


@dataclass(frozen=True)
class Base:
    """The base disk a living port starts from, and how to reach its guest."""

    disk: Path
    # The network card's hardware address, which the base's network
    # configuration matches.
    mac: str
    user: str
    key: Path
    cpus: int = 4
    memory_mib: int = 8192
    # The cloud-init image of a base whose guest expects it for the VM's life.
    cidata: Path | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            k: str(v) if isinstance(v, Path) else v for k, v in asdict(self).items()
        }


def iso(epoch_s: float) -> str:
    """A time in Coordinated Universal Time (UTC), to the millisecond."""
    moment = datetime.fromtimestamp(epoch_s, timezone.utc)
    return f"{moment:%Y-%m-%dT%H:%M:%S}.{moment.microsecond // 1000:03d}Z"


def parse_utc(text: str) -> float:
    """Seconds since the epoch of a time such as 2026-11-11T20:00:00.000Z."""
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text).timestamp()


def login(command: str) -> str:
    """A command run in a login shell of the VM's default user, whose profile
    sets the user's PATH: for fork 0 the episode runner's run command builds
    the helper that repeats the tugs' draws (competition/predicted_events)
    with cargo, which the profile puts on the PATH, and an SSH command's own
    shell reads no profile."""
    return "bash -lc " + shlex.quote(command)


def set_clocksource(source: str) -> str:
    """The command that switches the guest's clock source at run time."""
    return f"echo {shlex.quote(source)} | sudo tee {CLOCKSOURCE} > /dev/null"


def free_port() -> int:
    """A Transmission Control Protocol (TCP) port free on 127.0.0.1 now."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def episode_command(
    k: int,
    forked_at: str,
    track: str,
    episode_type: str,
    chain: str | None = None,
    *,
    training_volume: str | None = None,
    extra_override: str | None = None,
) -> str:
    """The contract's command for one episode in fork k. In training,
    `training_volume` is the training volume's directory in the fork, which
    the command attaches read-write to siem-01 at /var/lib/entry.
    `extra_override`, a path in the tree, is the entry's own version of the
    competition's additional override file with which the living port was
    created, so the fork reads the file that its living port was built from
    (episodes.md §3.2, modes; §6.3, the living port)."""
    argv = ["-m", "competition.episode_runner", "copy", "--fork", str(k)]
    argv += ["--forked-at", forked_at, "--track", track, "--type", episode_type]
    if chain:
        argv += ["--chain", chain]
    if training_volume:
        argv += ["--mode", "training", "--training-volume", training_volume]
    if extra_override:
        argv += ["--extra-override", extra_override]
    argv += ["--out", f"{EPISODES}/fork-{k}"]
    return f"cd {GUEST_TREE} && {PYTHON} " + shlex.join(argv)


class Manager:
    """The living ports under one root directory on this host."""

    def __init__(
        self,
        root: Path,
        engine: Engine,
        *,
        remote: Callable[[Ssh], Remote] = Remote,
        wall: Callable[[], float] = time.time,
        port: Callable[[], int] = free_port,
        disk_free: Callable[[Path], int] = lambda path: shutil.disk_usage(path).free,
        min_free_bytes: int = 10 * 10**9,
        snapshot_cache_bytes: int = 0,
        memory_dir: Path | None = None,
        auto_fold: bool = True,
        clones: bool | None = None,
    ) -> None:
        # Absolute, since QEMU and qemu-img resolve relative paths elsewhere.
        self.root = root.expanduser().resolve()
        self.engine = engine
        self._remote = remote
        self._wall = wall
        self._port = port
        self._disk_free = disk_free
        self._min_free_bytes = min_free_bytes
        # The snapshot cache's size: the loop takes snapshots only with one.
        self._snapshot_cache_bytes = snapshot_cache_bytes
        # A file system in memory for the saves, such as /dev/shm, or None for
        # saves straight to the disk.
        self._memory_dir = memory_dir
        # Whether a frozen image is folded as soon as nothing needs it.
        self._auto_fold = auto_fold
        # Whether each save clones the living port's top in place of adding a
        # layer: by default, where the root's file system clones files.
        self._clones = clones

    # Records

    def port_dir(self, name: str) -> Path:
        if not _NAME.fullmatch(name):
            raise PortError(
                f"a living port's name is lower-case letters, digits and hyphens: {name}"
            )
        return self.root / name

    def read(self, name: str) -> dict[str, Any]:
        path = self.port_dir(name) / "port.json"
        if not path.is_file():
            raise PortError(f"no living port {name} under {self.root}")
        return json.loads(path.read_text())

    def _write(self, name: str, record: dict[str, Any]) -> None:
        path = self.port_dir(name) / "port.json"
        part = path.with_name("port.json.part")
        part.write_text(json.dumps(record, indent=1) + "\n")
        part.replace(path)

    @contextlib.contextmanager
    def _locked(self, name: str) -> Iterator[None]:
        with open(self.port_dir(name) / "lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _update(
        self, name: str, change: Callable[[dict[str, Any]], None]
    ) -> dict[str, Any]:
        with self._locked(name):
            record = self.read(name)
            change(record)
            self._write(name, record)
            return record

    @property
    def clones(self) -> bool:
        if self._clones is None:
            self.root.mkdir(parents=True, exist_ok=True)
            self._clones = self.engine.can_clone(self.root)
        return self._clones

    def _guest(self, machine: Machine, record: dict[str, Any]) -> Remote:
        base = record["base"]
        return self._remote(Ssh(machine.ssh_port, base["user"], Path(base["key"])))

    # The living port

    def create(
        self,
        name: str,
        base: Base,
        tree: Path,
        *,
        seed: str | None = None,
        ssh_port: int | None = None,
        affinity: str | None = None,
        build: bool = False,
        volume: Path | None = None,
        training_volume: Path | None = None,
        boot_timeout_s: float = 300.0,
        deploy_timeout_s: float | None = None,
        extra_override: str | None = None,
    ) -> dict[str, Any]:
        """Start a living port from `base`, through an overlay of its own, and
        deploy RavensPort in it from `tree`. In evaluation, `volume` is the
        entry's submitted storage volume, which siem-01 mounts read-only from
        the creation on, so that every fork keeps it. In training,
        `training_volume` starts the living port's lineage of training
        volumes, which pass from fork to fork. `extra_override`, a path in
        the tree, is an entry's own version of the competition's additional
        override file, which names the entry's images (episodes.md §3.2,
        modes; §6.3, the living port)."""
        port_dir = self.port_dir(name)
        if port_dir.exists():
            raise PortError(f"{port_dir} exists")
        if volume and training_volume:
            raise PortError("a living port runs in evaluation or in training, not both")
        for given in (volume, training_volume):
            if given is not None and not given.is_dir():
                raise PortError(f"no volume directory {given}")
        base = replace(base, disk=base.disk.expanduser().resolve())
        if not base.disk.is_file():
            raise PortError(f"no base disk {base.disk}")
        if not (tree / "competition" / "living" / "guest.py").is_file():
            raise PortError(f"{tree} is not a RavensPort tree with competition/living")
        if extra_override is not None and not (tree / extra_override).is_file():
            raise PortError(f"the tree holds no override file {extra_override}")
        seed = (seed or secrets.token_bytes(32).hex()).lower()
        if not re.fullmatch(r"[0-9a-f]{64}", seed):
            raise PortError("a living port seed is 64 hexadecimal digits")
        port_dir.mkdir(parents=True)
        disk = port_dir / "disk-0.qcow2"
        try:
            self.engine.create_overlay(base.disk, disk)
        except VMError:
            shutil.rmtree(port_dir)
            raise
        machine = Machine(
            name=f"living-{name}",
            disk=disk,
            rundir=port_dir / "run",
            ssh_port=ssh_port or self._port(),
            mac=base.mac,
            cpus=base.cpus,
            memory_mib=base.memory_mib,
            affinity=affinity,
            cidata=base.cidata.resolve() if base.cidata else None,
        )
        record: dict[str, Any] = {
            "name": name,
            "seed": seed,
            "base": base.to_dict(),
            "machine": machine.to_dict(),
            "hypervisor": self.engine.hypervisor.name,
            "tree": str(tree),
            "created_at": iso(self._wall()),
            "status": "creating",
            "chain": [disk.name],
            "forks": {},
            "snapshots": [],
        }
        self._write(name, record)
        try:
            self.engine.launch(machine)
            guest = self._guest(machine, record)
            record["boot_s"] = round(guest.wait(boot_timeout_s), 3)
            log.info("%s: answering after %.1f s", name, record["boot_s"])
            dirs = " ".join(GUEST_DIRS)
            guest.run(f'sudo install -d -o "$(id -un)" -g "$(id -gn)" -m 755 {dirs}')
            guest.run(HOLD_CLOCK)
            record["clock"] = guest.run(CLOCK_STATE).stdout.strip()
            if record["clock"] != CLOCK_HELD:
                raise PortError(f"the clock is not held: {record['clock']}")
            source = self.engine.hypervisor.guest_clocksource
            if source:
                guest.run(set_clocksource(source))
                record["clocksource"] = guest.run(CLOCKSOURCE_STATE).stdout.strip()
                if record["clocksource"] != source:
                    raise PortError(
                        f"the clock source is {record['clocksource']}, not {source}"
                    )
            guest.run(HOLD_UPDATES)
            record["updates"] = guest.run(UPDATES_STATE).stdout.strip()
            if record["updates"] != UPDATES_HELD:
                raise PortError(f"the updates are not held: {record['updates']}")
            guest.send_tree(tree, GUEST_TREE, TREE_EXCLUDES)
            guest.run(VENV, timeout_s=1800.0)
            command = f"cd {GUEST_TREE} && {PYTHON} -m competition.living.guest create --seed {seed}"
            if build:
                command += " --build"
            if extra_override is not None:
                command += f" --extra-override {shlex.quote(extra_override)}"
                record["extra_override"] = extra_override
            if volume:
                guest.run(f"mkdir -p {GUEST_VOLUME}")
                guest.send_tree(volume, GUEST_VOLUME)
                guest.run(f"chmod -R a+rX {GUEST_VOLUME}")
                command += f" --volume {GUEST_VOLUME}"
                record["volume"] = {
                    "from": str(volume),
                    "in_vm": GUEST_VOLUME,
                    "mount": VOLUME_MOUNT,
                    "readonly": True,
                }
            log.info("%s: tree and virtual environment in place; deploying", name)
            status = guest.stream(
                login(command),
                port_dir / "create.log",
                timeout_s=deploy_timeout_s or (7200.0 if build else 1800.0),
            )
            if status != 0:
                raise PortError(
                    f"the deployment in the VM exited {status}; see {port_dir / 'create.log'}"
                )
            made = json.loads(guest.run(f"cat {IDENTITY}").stdout)
            if made.get("living_port_seed") != seed:
                raise PortError("the identity file holds another living port seed")
            settling = json.loads(guest.run(f"cat {SETTLED}").stdout)
            record.update(
                identity=made,
                started_at=made["started_at"],
                settled_at=settling["settled_at"],
                settling=settling,
                status="running",
            )
            if training_volume:
                shutil.copytree(training_volume, port_dir / "training" / "v0")
                record["training"] = {
                    "mount": VOLUME_MOUNT,
                    "current": 0,
                    "versions": [
                        {"v": 0, "from": str(training_volume), "at": iso(self._wall())}
                    ],
                }
        except (PortError, SSHError, VMError) as error:
            record.update(status="failed", error=str(error))
            self._write(name, record)
            self.engine.stop(machine)
            raise
        self._write(name, record)
        log.info("%s: running; the simulator started at %s", name, record["started_at"])
        return record

    def simulator_running(self, name: str) -> bool:
        record = self.read(name)
        guest = self._guest(Machine.from_dict(record["machine"]), record)
        result = guest.run(SIMULATOR_STATUS, check=False, timeout_s=60.0)
        return "status: started" in result.stdout

    def clock_offset(self, name: str) -> ClockOffset:
        """The living port's clock less the host's."""
        record = self.read(name)
        return self._guest(Machine.from_dict(record["machine"]), record).clock_offset()

    # Forks

    def check_room(self, record: dict[str, Any], copy: bool = True) -> None:
        """Raise RoomError when a save, and the fork restored from it, would
        leave the host less disk than its reserve or more memory than it has."""
        need = int(record["machine"]["memory_mib"]) * MIB
        free = self._disk_free(self.port_dir(record["name"]))
        if free - need < self._min_free_bytes:
            raise RoomError(
                f"a save needs about {need / 1e9:.1f} GB and would leave "
                f"{(free - need) / 1e9:.1f} GB, below the reserve of {self._min_free_bytes / 1e9:.0f} GB"
            )
        available = self.engine.hypervisor.available_memory_bytes()
        if self._memory_dir is not None:
            room = shutil.disk_usage(self._memory_dir).free
            if room < need + MEMORY_MARGIN:
                raise RoomError(
                    f"a save in {self._memory_dir} needs {(need + MEMORY_MARGIN) / GIB:.1f}"
                    f" GiB; {room / GIB:.1f} GiB free there"
                )
            # The state file in memory and the fork both take the host's memory.
            need += need if copy else 0
        if copy and available is not None and available < need + MEMORY_MARGIN:
            raise RoomError(
                f"a fork needs {(need + MEMORY_MARGIN) / GIB:.1f} GiB; {available / GIB:.1f} GiB available"
            )

    def _save(
        self,
        name: str,
        record: dict[str, Any],
        state: Path,
        clone: Path | None = None,
    ) -> tuple[Any, str, float, ClockOffset, Path]:
        """Save the running living port. Where the file system clones files,
        the save clones the living port's top to `clone`; elsewhere it adds a
        new overlay, which lengthens the chain until a fold. `state` is where
        a kept state file belongs on the disk. With a memory directory, the
        save writes there, and the caller moves the file to `state` or deletes
        it. Returns the save, the instant of the pause on the living port's
        clock, the living port's age then, the offset of its clock just before
        the save, and the state file's path. The caller holds the lock."""
        machine = Machine.from_dict(record["machine"])
        status = self.engine.status(machine)
        if status != "running":
            raise PortError(f"the living port {name} is {status}")
        guest = self._guest(machine, record)
        before = guest.clock_offset()
        layers = sorted(self._layer_numbers(name)) or [len(record["chain"]) - 1]
        overlay = self.port_dir(name) / f"disk-{layers[-1] + 1}.qcow2"
        written = state
        if self._memory_dir is not None:
            written = self._memory_dir / f"ravensport-{name}-{state.name}"
        top = record["chain"][-1]
        if clone is not None:
            saved = self.engine.save(machine, None, written, clone=clone)
            taken_at = iso(saved.stopped_at + before.offset_s)
            age_s = round(parse_utc(taken_at) - parse_utc(record["started_at"]), 3)
            return saved, taken_at, age_s, before, written
        try:
            saved = self.engine.save(machine, overlay, written)
        finally:
            # A save that failed after the switch to the overlay still
            # lengthens the living port's chain.
            if overlay.exists() and overlay.name not in record["chain"]:
                record["chain"].append(overlay.name)
                record.setdefault("backing", {})[overlay.name] = top
                self._write(name, record)
        # The instant and the age to the millisecond, as the runner reads them
        # from --forked-at and the identity file.
        taken_at = iso(saved.stopped_at + before.offset_s)
        age_s = round(parse_utc(taken_at) - parse_utc(record["started_at"]), 3)
        return saved, taken_at, age_s, before, written

    def _layer_numbers(self, name: str) -> list[int]:
        """The numbers of the living port's images on the disk, so that a new
        overlay never takes the name of an image that a fork or a replay still
        reads."""
        found = []
        for path in self.port_dir(name).glob("disk-*.qcow2"):
            number = path.name[len("disk-") : -len(".qcow2")]
            if number.isdigit():
                found.append(int(number))
        return found

    def _move_state(self, written: Path, state: Path) -> float:
        """Move a state file from memory to the disk, as the living port runs,
        and return the seconds the move took."""
        if written == state:
            return 0.0
        started = time.monotonic()
        state.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(written), str(state))
        return round(time.monotonic() - started, 3)

    def fork(
        self,
        name: str,
        k: int | None = None,
        *,
        keep_state: bool = False,
        snapshot: bool = False,
        drawn: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Save the living port and restore the fork from the save. The fork
        instant is the living port's clock at the pause, from the offset of its
        clock just before the save. With snapshot, the save is also kept as a
        snapshot for replay."""
        saved = self.save_fork(
            name, k, keep_state=keep_state, snapshot=snapshot, drawn=drawn
        )
        return self.restore_fork(name, saved["k"])

    def save_fork(
        self,
        name: str,
        k: int | None = None,
        *,
        keep_state: bool = False,
        snapshot: bool = False,
        drawn: dict[str, Any] | None = None,
        copy_now: bool = True,
    ) -> dict[str, Any]:
        """The save of fork k, from which restore_fork starts the fork at once,
        or once the previous fork is done. The fork's clock holds from the
        fork instant either way. A save whose fork waits needs room for its
        state file alone now."""
        port_dir = self.port_dir(name)
        with self._locked(name):
            record = self.read(name)
            k = k if k is not None else next_fork(record)
            if k < 1 or str(k) in record["forks"]:
                raise PortError(f"fork {k} exists or is not a fork number")
            self.check_room(record, copy=copy_now)
            state = port_dir / "states" / f"fork-{k}.mig"
            disk = port_dir / "copies" / f"fork-{k}" / "disk.qcow2"
            log.info("%s: saving for fork %d", name, k)
            saved, forked_at, age_s, before, written = self._save(
                name, record, state, clone=disk if self.clones else None
            )
            fork: dict[str, Any] = {
                "k": k,
                "forked_at": forked_at,
                "age_s": age_s,
                "saved": saved.to_dict(),
                "clock_before": asdict(before),
                "status": "saved",
                "disk_by": "clone" if self.clones else "layer",
                "pending_state": str(written),
                "keep_state": keep_state or snapshot,
                "snapshot": snapshot,
            }
            if drawn:
                late = parse_utc(forked_at) - parse_utc(drawn["due_at"])
                fork["drawn"] = {**drawn, "late_s": round(late, 3)}
            after = self._guest(
                Machine.from_dict(record["machine"]), record
            ).clock_offset()
            fork["clock_after"] = asdict(after)
            # With the clock held, the living port's clock falls behind the
            # host's by the pause, and keeps that offset.
            fork["clock_shift_s"] = round(after.offset_s - before.offset_s, 3)
            record["forks"][str(k)] = fork
            self._write(name, record)
        log.info("%s: fork %d at %s, paused %.2f s", name, k, forked_at, saved.pause_s)
        return fork

    def restore_fork(self, name: str, k: int) -> dict[str, Any]:
        """Start fork k from its save."""
        port_dir = self.port_dir(name)
        with self._locked(name):
            record = self.read(name)
            fork = record["forks"].get(str(k))
            if not fork or fork.get("status") != "saved":
                raise PortError(
                    f"fork {k} of {name} has no save waiting to be restored"
                )
            written = Path(fork["pending_state"])
            machine = Machine.from_dict(record["machine"])
            copy = machine.copy(
                name=f"{machine.name}-fork-{k}",
                disk=port_dir / "copies" / f"fork-{k}" / "disk.qcow2",
                rundir=port_dir / "copies" / f"fork-{k}",
                ssh_port=self._port(),
            )
            fork["copy"] = copy.to_dict()
            keep_state = bool(fork.get("keep_state"))
            try:
                restored = self.engine.restore(
                    copy,
                    Path(fork["saved"]["frozen"]),
                    written,
                    disk_ready=fork.get("disk_by") == "clone",
                    fmt=fork["saved"].get("format"),
                )
            except VMError as error:
                fork.update(status="restore failed", error=str(error))
                self._write(name, record)
                raise
            finally:
                if not keep_state:
                    written.unlink(missing_ok=True)
            fork.pop("pending_state", None)
            fork.update(
                restored=restored.to_dict(),
                restored_at=iso(self._wall()),
                status="running",
                state_kept=keep_state,
            )
            if keep_state:
                state = port_dir / "states" / f"fork-{k}.mig"
                fork["state_moved_s"] = self._move_state(written, state)
                fork["saved"]["state"] = str(state)
                if fork.get("snapshot"):
                    saved = Saved(
                        **{**fork["saved"], "frozen": Path(fork["saved"]["frozen"])}
                    )
                    self._keep_snapshot(
                        record, saved, state, fork["forked_at"], fork["age_s"], fork=k
                    )
            self._write(name, record)
        log.info(
            "%s: fork %d running %.2f s after its launch, on port %d",
            name,
            k,
            restored.launch_to_running_s,
            copy.ssh_port,
        )
        return fork

    def snapshot(self, name: str) -> dict[str, Any]:
        """A snapshot for replay: a save of the living port from which no fork
        is restored, whose state file and frozen image are kept (episodes.md
        §6.3, the living port). The oldest snapshots beyond the cache are
        pruned."""
        with self._locked(name):
            record = self.read(name)
            self.check_room(record, copy=False)
            n = len(record["snapshots"]) + 1
            state = self.port_dir(name) / "snapshots" / f"snapshot-{n}.mig"
            clone = state.with_suffix(".qcow2") if self.clones else None
            saved, taken_at, age_s, _, written = self._save(
                name, record, state, clone=clone
            )
            moved_s = self._move_state(written, state)
            kept = self._keep_snapshot(record, saved, state, taken_at, age_s)
            kept["state_moved_s"] = moved_s
            self._write(name, record)
        log.info(
            "%s: snapshot %d at %s, paused %.2f s", name, n, taken_at, saved.pause_s
        )
        return kept

    def _keep_snapshot(
        self,
        record: dict[str, Any],
        saved: Any,
        state: Path,
        taken_at: str,
        age_s: float,
        fork: int | None = None,
    ) -> dict[str, Any]:
        kept = {
            "n": len(record["snapshots"]) + 1,
            "taken_at": taken_at,
            "age_s": age_s,
            "state": str(state),
            "frozen": str(saved.frozen),
            "state_bytes": saved.state_allocated_bytes,
            "pause_s": saved.pause_s,
            "fork": fork,
            "saved": saved.to_dict(),
        }
        record["snapshots"].append(kept)
        live = [s for s in record["snapshots"] if not s.get("pruned_at")]
        keep = kept_snapshots(
            [s["state_bytes"] for s in live], self._snapshot_cache_bytes
        )
        for old in live[: len(live) - keep]:
            Path(old["state"]).unlink(missing_ok=True)
            if Path(old["frozen"]).parent.name == "snapshots":
                # A snapshot's own clone goes with it.
                Path(old["frozen"]).unlink(missing_ok=True)
            old["pruned_at"] = iso(self._wall())
        if self._auto_fold:
            self._fold(record)
        return kept

    # Replays and the chain of images

    def replay(self, name: str, n: int) -> dict[str, Any]:
        """Restore kept snapshot n on a virtual machine of its own, which
        continues from the snapshot's instant on its own clock."""
        port_dir = self.port_dir(name)
        with self._locked(name):
            record = self.read(name)
            if not 1 <= n <= len(record["snapshots"]):
                raise PortError(f"no snapshot {n} of {name}")
            kept = record["snapshots"][n - 1]
            state = Path(kept["state"])
            if kept.get("pruned_at") or not state.is_file():
                raise PortError(f"snapshot {n} of {name} is no longer kept")
            self.check_room(record)
            replays = record.setdefault("replays", {})
            replay_id = (
                f"{n}-{1 + sum(1 for r in replays.values() if r['snapshot'] == n)}"
            )
            machine = Machine.from_dict(record["machine"])
            copy = machine.copy(
                name=f"{machine.name}-replay-{replay_id}",
                disk=port_dir / "copies" / f"replay-{replay_id}" / "disk.qcow2",
                rundir=port_dir / "copies" / f"replay-{replay_id}",
                ssh_port=self._port(),
            )
            restored = self.engine.restore(
                copy,
                Path(kept["frozen"]),
                state,
                fmt=(kept.get("saved") or {}).get("format"),
            )
            replays[replay_id] = {
                "snapshot": n,
                "taken_at": kept["taken_at"],
                "frozen": kept["frozen"],
                "copy": copy.to_dict(),
                "restored": restored.to_dict(),
                "status": "running",
                "at": iso(self._wall()),
            }
            self._write(name, record)
        log.info(
            "%s: replay %s of snapshot %d running on port %d",
            name,
            replay_id,
            n,
            copy.ssh_port,
        )
        return {"replay": replay_id, **replays[replay_id]}

    def delete_replay(self, name: str, replay_id: str) -> None:
        """Stop a replay's virtual machine, and delete its overlay and run
        directory."""
        record = self.read(name)
        replay = record.get("replays", {}).get(replay_id)
        if not replay:
            raise PortError(f"no replay {replay_id} of {name}")
        copy = Machine.from_dict(replay["copy"])
        if self.port_dir(name) / "copies" not in copy.rundir.parents:
            raise PortError(
                f"{copy.rundir} is outside the living port's directory copies/"
            )
        self.engine.delete(copy)

        def note(record: dict[str, Any]) -> None:
            record["replays"][replay_id].update(
                status="deleted", deleted_at=iso(self._wall())
            )
            if self._auto_fold:
                self._fold(record)

        self._update(name, note)

    def fold(self, name: str) -> dict[str, Any] | None:
        """Fold now every frozen image that nothing needs any more."""
        with self._locked(name):
            record = self.read(name)
            entry = self._fold(record)
            self._write(name, record)
        return entry

    def _fold(self, record: dict[str, Any]) -> dict[str, Any] | None:
        """Fold into the living port's top every frozen image of its chain
        that no running fork or replay and no kept snapshot needs, and delete
        each. A frozen image holds only the living port's own writes, since
        each fork and replay writes to its own file, so the fold streams the
        top down to the highest image still needed, or to the base where none
        is, while the VM runs. The files that a fork or a replay writes are
        only ever deleted. The caller holds the lock."""
        chain = record["chain"]
        needed = needed_by_others(record)
        frozen = chain[:-1]
        kept = [image for image in frozen if image in needed]
        if kept != frozen[: len(kept)]:
            raise PortError(
                f"the images still needed are not the chain's bottom: {kept}"
            )
        folded = frozen[len(kept) :]
        if not folded:
            return None
        port_dir = self.port_dir(record["name"])
        base = port_dir / kept[-1] if kept else Path(record["base"]["disk"])
        streamed = self.engine.stream(Machine.from_dict(record["machine"]), base)
        top = chain[-1]
        record["chain"] = [*kept, top]
        record.setdefault("backing", {})[top] = kept[-1] if kept else BASE
        for image in folded:
            (port_dir / image).unlink(missing_ok=True)
        entry = {**streamed, "folded": folded, "at": iso(self._wall())}
        record.setdefault("folds", []).append(entry)
        log.info(
            "%s: folded %s into %s; the chain is %s",
            record["name"],
            ", ".join(folded),
            top,
            record["chain"],
        )
        return entry

    def run_episode(
        self,
        name: str,
        k: int,
        *,
        track: str,
        episode_type: str,
        chain: str | None = None,
        timeout_s: float = EPISODE_TIMEOUT_S,
    ) -> dict[str, Any]:
        """Run one episode in fork k by the contract's command, and copy its
        outputs to ROOT/NAME/episodes/fork-K. The command starts as soon as
        the fork answers: a fork has no settle delay, and the runner checks
        the four settle conditions itself. In training, the lineage's latest
        training volume goes into the fork first, the command runs in
        training mode and attaches it read-write to siem-01, and the volume
        comes back with the outputs as the lineage's next version. Once the
        outputs are on the host, the manager adds to the episode record what
        only it holds: the fork wait, the pause and the clocks' offsets. An
        episode that the fork discarded keeps its record and its cause, and
        is not run again (episodes.md §12, terminated and discarded
        episodes)."""
        record = self.read(name)
        fork = record["forks"].get(str(k))
        if not fork or "copy" not in fork or fork.get("status") == "deleted":
            raise PortError(f"fork {k} of {name} is not restored, or is deleted")
        copy = Machine.from_dict(fork["copy"])
        guest = self._guest(copy, record)
        guest.wait(120.0)
        out = f"{EPISODES}/fork-{k}"
        if guest.run(f"test ! -e {out}", check=False).returncode != 0:
            raise PortError(
                f"{out} exists in fork {k}; the contract's --out must not, so "
                "remove it, or run the episode in a fresh fork"
            )
        # The fork's clock holds from the fork instant, so its offset from the
        # host's clock stays as it is read now, at the episode's start.
        episode: dict[str, Any] = {"copy_clock": self._clock_of(guest)}
        training = record.get("training")
        in_training: dict[str, Any] = {}
        if training:
            episode["training_in"] = self._training_in(name, training, guest, k)
            in_training = {"training_volume": episode["training_in"]["in_copy"]}
        command = episode_command(
            k,
            fork["forked_at"],
            track,
            episode_type,
            chain,
            extra_override=record.get("extra_override"),
            **in_training,
        )
        episodes = self.port_dir(name) / "episodes"
        started = self._wall()
        log.info("%s: episode of fork %d started", name, k)
        status = guest.stream(
            login(command), episodes / f"fork-{k}.session.log", timeout_s=timeout_s
        )
        ended = self._wall()
        episode.update(
            command=command,
            exit=status,
            started_at=iso(started),
            ended_at=iso(ended),
            out=str(episodes / f"fork-{k}"),
        )
        episode.update(self._living_port_time(name, ended))
        try:
            guest.fetch_tree(f"{EPISODES}/fork-{k}", episodes)
            episode["collected"] = True
        except SSHError as error:
            episode.update(collected=False, collect_error=str(error))
        if training:
            episode["training_out"] = self._training_out(name, guest, k, status == 0)
        if episode["collected"]:
            episode.update(
                complete_record(episodes / f"fork-{k}", fork, episode["copy_clock"])
            )
        log.info(
            "%s: episode of fork %d exited %d; outputs collected: %s; outcome: %s%s",
            name,
            k,
            status,
            episode["collected"],
            episode.get("outcome"),
            f" ({episode['cause']})" if episode.get("cause") else "",
        )

        def note(record: dict[str, Any]) -> None:
            record["forks"][str(k)]["episode"] = episode
            out = episode.get("training_out") or {}
            if out.get("version") is not None:
                record["training"]["current"] = out["version"]
                record["training"]["versions"].append(
                    {"v": out["version"], "fork": k, "at": iso(self._wall())}
                )

        self._update(name, note)
        return episode

    def _living_port_time(self, name: str, host_s: float) -> dict[str, Any]:
        """A host time on the living port's clock, from the offset read now."""
        try:
            offset = self.clock_offset(name).offset_s
        except SSHError as error:
            return {"living_port_clock_error": str(error)}
        return {"ended_on_living_port": iso(host_s + offset)}

    @staticmethod
    def _clock_of(guest: Remote) -> dict[str, Any]:
        """A guest's clock less the host's, or why it could not be read."""
        try:
            return asdict(guest.clock_offset())
        except SSHError as error:
            return {"error": str(error)}

    def _training_in(
        self, name: str, training: dict[str, Any], guest: Remote, k: int
    ) -> dict[str, Any]:
        """The lineage's latest training volume, imported into the fork, where
        the episode runner's `copy` command attaches it read-write to siem-01
        (episode-runner-impl.md §5, forks)."""
        version = int(training["current"])
        target = f"{GUEST_TRAINING}/fork-{k}"
        guest.run(f"mkdir -p {target}")
        guest.send_tree(self.port_dir(name) / "training" / f"v{version}", target)
        log.info("%s: training volume v%d in fork %d", name, version, k)
        return {"version": version, "in_copy": target}

    def _training_out(
        self, name: str, guest: Remote, k: int, adopt: bool
    ) -> dict[str, Any]:
        """The training volume back from fork k, which the episode runner's
        `copy` command detached from siem-01 at the episode's end. An
        episode that ran to an end gives the lineage's next version; any other
        keeps the volume aside."""
        target = f"{GUEST_TRAINING}/fork-{k}"
        lineage = self.port_dir(name) / "training"
        incoming = lineage / "incoming"
        shutil.rmtree(incoming / f"fork-{k}", ignore_errors=True)
        try:
            guest.fetch_tree(target, incoming, sudo=True)
        except SSHError as error:
            return {"version": None, "error": str(error)}
        if not adopt:
            kept = lineage / "set-aside" / f"fork-{k}"
            kept.parent.mkdir(parents=True, exist_ok=True)
            (incoming / f"fork-{k}").replace(kept)
            return {"version": None, "set_aside": str(kept)}
        version = 1 + max(
            int(path.name[1:]) for path in lineage.glob("v*") if path.name[1:].isdigit()
        )
        (incoming / f"fork-{k}").replace(lineage / f"v{version}")
        log.info("%s: training volume v%d back from fork %d", name, version, k)
        return {"version": version}

    def delete_copy(self, name: str, k: int) -> None:
        """Stop fork k, and delete its overlay and run directory."""
        record = self.read(name)
        fork = record["forks"].get(str(k))
        if not fork or "copy" not in fork:
            raise PortError(f"fork {k} of {name} is not restored")
        copy = Machine.from_dict(fork["copy"])
        if self.port_dir(name) / "copies" not in copy.rundir.parents:
            raise PortError(
                f"{copy.rundir} is outside the living port's directory copies/"
            )
        self.engine.delete(copy)

        def note(record: dict[str, Any]) -> None:
            record["forks"][str(k)].update(
                status="deleted", deleted_at=iso(self._wall())
            )
            if self._auto_fold:
                self._fold(record)

        self._update(name, note)
        log.info("%s: fork %d deleted", name, k)

    def episode(
        self, name: str, k: int, *, keep_copy: bool = False, **episode: Any
    ) -> dict[str, Any]:
        """One episode in fork k, then the fork's deletion."""
        try:
            return self.run_episode(name, k, **episode)
        finally:
            if not keep_copy:
                self.delete_copy(name, k)

    def fork_due(self, record: dict[str, Any]) -> tuple[float, dict[str, Any]]:
        """The instant, on the living port's clock, at which the next fork
        falls, and how it was drawn. Fork 1 falls one drawn wait after the
        living port's settle period, and fork k+1 the spacing, the horizon and
        the settle timeout, and one drawn wait after fork k's due instant, so
        that every fork instant depends on the living port seed alone. A fork
        made by hand, with no due instant, counts from its own instant."""
        k = next_fork(record)
        wait_s = fork_wait_s(record["seed"], k)
        if k == 1:
            after = record.get("settled_at") or record["started_at"]
            reference = "the settle period" if record.get("settled_at") else "the start"
            due = parse_utc(after) + wait_s
        else:
            previous = record["forks"].get(str(k - 1)) or {}
            after = (previous.get("drawn") or {}).get("due_at") or previous.get(
                "forked_at", record["started_at"]
            )
            reference = f"fork {k - 1}"
            due = parse_utc(after) + FORK_SPACING_S + wait_s
        return due, {
            "wait_s": round(wait_s, 3),
            "after": reference,
            "after_at": after,
            "due_at": iso(due),
        }

    def loop(
        self,
        name: str,
        *,
        track: str,
        episode_type: str,
        chain: str | None = None,
        forks: int | None = None,
        timeout_s: float = EPISODE_TIMEOUT_S,
        stop: threading.Event | None = None,
        recheck_s: float = 60.0,
    ) -> list[int]:
        """Fork on the drawn schedule, on the living port's own clock, and run
        one episode at a time. A fork due while the previous fork still runs
        its episode, or exports it, is saved at its due instant all the same,
        and restored once the previous one is done; the fork's clock holds
        from the fork instant either way. With a snapshot cache
        that can keep at least one state, the loop also keeps the snapshots
        for replay on their schedule. A
        living port whose simulator stops makes no further forks, and the
        loop ends once its saved forks have run."""
        stop = stop or threading.Event()
        started = parse_utc(self.read(name)["started_at"])
        made: list[int] = []
        worker: threading.Thread | None = None
        forking = True
        told_cache = False
        while not stop.is_set():
            record = self.read(name)
            running = worker is not None and worker.is_alive()
            pending = [k for k in made if record["forks"][str(k)]["status"] == "saved"]
            if pending and not running:
                k = pending[0]
                try:
                    self.restore_fork(name, k)
                except (PortError, VMError) as error:
                    log.error("%s: fork %d did not start: %s", name, k, error)
                    continue
                worker = threading.Thread(
                    target=self._episode_quietly,
                    args=(name, k),
                    kwargs={
                        "track": track,
                        "episode_type": episode_type,
                        "chain": chain,
                        "timeout_s": timeout_s,
                    },
                    name=f"{name}-fork-{k}",
                )
                worker.start()
                continue
            if forks is not None and len(made) >= forks:
                forking = False
            if forking and not self.simulator_running(name):
                log.warning("%s: the simulator has stopped; no further forks", name)
                forking = False
            if not forking and not running and not pending:
                break
            fork_at, drawn = float("inf"), {}
            if forking:
                fork_at, drawn = self.fork_due(record)
            snapshot_at = float("inf")
            if self._snapshot_cache_bytes > 0:
                expected = snapshot_bytes_expected(record)
                if expected <= self._snapshot_cache_bytes:
                    snapshot_at = started + next_snapshot_age(record)
                elif not told_cache:
                    # A snapshot that the cache cannot keep would pause the
                    # living port for nothing, since pruning deletes it at once.
                    log.warning(
                        "%s: no snapshots: the cache of %.1f GB cannot keep one of about %.1f GB",
                        name,
                        self._snapshot_cache_bytes / 1e9,
                        expected / 1e9,
                    )
                    told_cache = True
            due = min(fork_at, snapshot_at)
            wait_s = due - self.clock_offset(name).offset_s - self._wall()
            if wait_s > 0:
                if running:
                    worker.join(min(wait_s, recheck_s))
                else:
                    stop.wait(min(wait_s, recheck_s))
                continue
            if snapshot_at <= fork_at:
                try:
                    self.snapshot(name)
                except RoomError as error:
                    log.warning("%s: the snapshot waits: %s", name, error)
                    stop.wait(30.0)
                except (PortError, SSHError, VMError) as error:
                    log.error(
                        "%s: a snapshot failed, and the loop stops: %s", name, error
                    )
                    break
                continue
            k = next_fork(record)
            try:
                self.save_fork(name, k, drawn=drawn, copy_now=not running)
            except RoomError as error:
                log.warning("%s: fork %d waits: %s", name, k, error)
                stop.wait(30.0)
                continue
            except (PortError, SSHError, VMError) as error:
                log.error("%s: fork %d failed, and the loop stops: %s", name, k, error)
                break
            made.append(k)
        if worker is not None:
            worker.join()
        return made

    def _episode_quietly(self, name: str, k: int, **episode: Any) -> None:
        try:
            self.episode(name, k, **episode)
        except (PortError, SSHError, VMError) as error:
            log.error("%s: the episode of fork %d failed: %s", name, k, error)
            failed = {"error": str(error), **self._living_port_time(name, self._wall())}

            def note(record: dict[str, Any]) -> None:
                record["forks"][str(k)].setdefault("episode", failed)

            self._update(name, note)

    # Every living port

    def list(self) -> list[dict[str, Any]]:
        """Each living port under the root, with its state, its forks and its
        replays."""
        found = []
        for path in sorted(self.root.glob("*/port.json")):
            record = json.loads(path.read_text())
            machine = Machine.from_dict(record["machine"])
            copies = {}
            running = [(k, fork) for k, fork in record["forks"].items()]
            running += [
                (f"replay {r}", replay)
                for r, replay in record.get("replays", {}).items()
            ]
            for k, fork in running:
                if "copy" in fork and fork.get("status") != "deleted":
                    copy = Machine.from_dict(fork["copy"])
                    copies[k] = {
                        "status": self.engine.status(copy),
                        "ssh_port": copy.ssh_port,
                    }
            found.append(
                {
                    "name": record["name"],
                    "status": self.engine.status(machine),
                    "recorded": record["status"],
                    "ssh_port": machine.ssh_port,
                    "started_at": record.get("started_at"),
                    "forks": len(record["forks"]),
                    "chain": len(record["chain"]),
                    "copies": copies,
                }
            )
        return found

    def stop(self, name: str, *, delete: bool = False) -> None:
        """Stop the living port, its forks and its replays. With delete, remove
        its directory: overlays, states, forks, replays and records."""
        record = self.read(name)
        users = [*record["forks"].values(), *record.get("replays", {}).values()]
        for fork in users:
            if "copy" in fork and fork.get("status") != "deleted":
                self.engine.stop(Machine.from_dict(fork["copy"]))
        self.engine.stop(Machine.from_dict(record["machine"]))
        self._update(
            name,
            lambda record: record.update(
                status="stopped", stopped_at=iso(self._wall())
            ),
        )
        if delete:
            port_dir = self.port_dir(name)
            if (
                Path(record["base"]["disk"])
                .resolve()
                .is_relative_to(port_dir.resolve())
            ):
                raise PortError("the base disk lies inside the living port's directory")
            shutil.rmtree(port_dir)
        log.info("%s: stopped%s", name, " and deleted" if delete else "")


def next_fork(record: dict[str, Any]) -> int:
    return max((int(k) for k in record["forks"]), default=0) + 1


def complete_record(
    out: Path, fork: dict[str, Any], copy_clock: dict[str, Any]
) -> dict[str, Any]:
    """Add to the episode record that a fork wrote, out/record.json, what only
    the manager holds (episodes.md §13, episode record): the fork wait that it
    drew, the pause of the living port's save, and the offsets from the host's
    clock of the living port's clock, before and after the save, and of the
    fork's. Each offset is the guest's clock less the host's. Returns the
    episode's outcome and cause as the record states them."""
    path = out / "record.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        return {"recorded": False, "record_error": str(error)}
    data.update(
        fork_wait=fork.get("drawn"),
        living_port_pause_s=(fork.get("saved") or {}).get("pause_s"),
        clock_offsets={
            "living_port_before_the_save": fork.get("clock_before"),
            "living_port_after_the_save": fork.get("clock_after"),
            "living_port_shift_s": fork.get("clock_shift_s"),
            "copy": copy_clock,
        },
    )
    part = path.with_name(path.name + ".part")
    part.write_text(json.dumps(data, indent=2) + "\n")
    part.replace(path)
    return {
        "recorded": True,
        "outcome": data.get("outcome"),
        "cause": data.get("cause"),
    }


def needed_by_others(record: dict[str, Any]) -> set[str]:
    """The images that something other than the living port's own top reads:
    each running fork and replay, and each kept snapshot, with every image
    beneath each in its chain of backing files."""
    backing = record.get("backing", {})

    def ancestry(image: str) -> set[str]:
        found = set()
        while image and image != BASE and image not in found:
            found.add(image)
            image = backing.get(image, "")
        return found

    needed: set[str] = set()
    users = [*record["forks"].values(), *record.get("replays", {}).values()]
    for user in users:
        # A save that waits to be restored needs its frozen image as a running
        # fork does.
        if user.get("status") in ("deleted", "restore failed"):
            continue
        frozen = user.get("frozen") or user.get("saved", {}).get("frozen", "")
        if frozen:
            needed |= ancestry(Path(frozen).name)
    for kept in record["snapshots"]:
        if not kept.get("pruned_at"):
            needed |= ancestry(Path(kept["frozen"]).name)
    return needed


def snapshot_bytes_expected(record: dict[str, Any]) -> int:
    """The likely size of the next snapshot's state file: the largest state
    saved so far, a snapshot's or a fork's, or else the guest's memory, which
    bounds a state from above before any save."""
    sizes = [int(s.get("state_bytes") or 0) for s in record["snapshots"]]
    sizes += [
        int(f.get("saved", {}).get("state_allocated_bytes") or 0)
        for f in record["forks"].values()
    ]
    known = [size for size in sizes if size > 0]
    return max(known) if known else int(record["machine"]["memory_mib"]) * MIB


def next_snapshot_age(record: dict[str, Any]) -> float:
    """The age of the living port at which its next snapshot falls: the first
    of the schedule after the latest snapshot kept."""
    last = max((s["age_s"] for s in record["snapshots"]), default=0.0)
    return next(age for age in snapshot_ages_s(last + 30 * 86400.0) if age > last + 1.0)
