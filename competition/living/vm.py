"""The virtual machine (VM) engine of the living port (episodes.md §6.4, how a
fork is made): launch a VM from a chain of disk images in QEMU's copy-on-write
format (qcow2), save a running VM, restore a save into a new VM, stop and
delete.

Every VM has one fixed device set: a user-mode network with one forward for
the Secure Shell (SSH) on 127.0.0.1, a network card with a fixed hardware
address, a QEMU Machine Protocol (QMP) socket, a serial log and a pidfile, and
no share of a host directory. QEMU refuses to migrate a guest that has a
directory share mounted, and a restored VM must not write to the host's files.
A base whose guest expects its cloud-init image, as Lima's do on the Mac, keeps
that image attached read-only for the VM's life, and every restored VM shares
it.

A save pauses the VM, switches it to a new overlay so that the image beneath
stays frozen at the pause, writes the memory and device state to a file in the
platform's format, and continues the VM. A restored VM runs the same command
line with only the disk, the sockets, the forwarded port, the pidfile and the
name changed, on an overlay of the frozen image, and loads the state file
before it continues. Every path is absolute, since qemu-img resolves a relative
backing path against the overlay's directory, and QEMU against its own.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from competition.living.hypervisor import Hypervisor, Tuning
from competition.living.qmp import QMP, QMPError

DISK_ID = "boot-disk"
QEMU_IMG = "qemu-img"
# The save's two formats: QEMU's mapped random-access memory (RAM) over
# several channels, and the plain stream.
MAPPED_RAM = "mapped-ram"
STREAM = "stream"
# The first QEMU whose migrate-incoming takes exit-on-error.
EXIT_ON_ERROR_FROM = (9, 1, 0)
CIDATA_ID = "cidata"
LOG_TAIL_LINES = 20
__all__ = [
    "Engine",
    "Machine",
    "Restored",
    "Saved",
    "Tuning",
    "VMError",
    "command_line",
]


class VMError(Exception):
    """A VM did not start, save, restore or stop as asked."""


@dataclass(frozen=True)
class Machine:
    """One VM: its disk, its run directory and its fixed device set."""

    name: str
    # The top of the VM's chain of qcow2 images.
    disk: Path
    # The directory of the VM's QMP socket, serial log, pidfile and QEMU's log.
    rundir: Path
    ssh_port: int
    # The network card's hardware address, which the guest's network
    # configuration matches.
    mac: str
    cpus: int = 4
    memory_mib: int = 8192
    network: str = "192.168.5.0/24"
    dhcp_start: str = "192.168.5.15"
    # The host threads the VM runs on, such as 0-15, where the platform pins.
    affinity: str | None = None
    # The cloud-init image of a base whose guest expects it, attached read-only
    # for the VM's life, and shared by every restored VM, so that its devices
    # match the original's.
    cidata: Path | None = None

    @property
    def qmp_socket(self) -> Path:
        return self.rundir / "qmp.sock"

    @property
    def serial_log(self) -> Path:
        return self.rundir / "serial.log"

    @property
    def pidfile(self) -> Path:
        return self.rundir / "qemu.pid"

    @property
    def qemu_log(self) -> Path:
        return self.rundir / "qemu.log"

    def copy(self, *, name: str, disk: Path, rundir: Path, ssh_port: int) -> Machine:
        """The machine of a restored VM: this machine with only the disk, the
        run directory (and so the sockets, logs and pidfile), the forwarded
        port and the name changed."""
        return replace(self, name=name, disk=disk, rundir=rundir, ssh_port=ssh_port)

    def to_dict(self) -> dict[str, Any]:
        return {
            k: str(v) if isinstance(v, Path) else v for k, v in asdict(self).items()
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Machine:
        cidata = data.get("cidata")
        return cls(
            **{
                **data,
                "disk": Path(data["disk"]),
                "rundir": Path(data["rundir"]),
                "cidata": Path(cidata) if cidata else None,
            }
        )


@dataclass(frozen=True)
class Features:
    """What the installed QEMU supports for a save and a restore."""

    version: tuple[int, int, int]
    capabilities: frozenset[str]

    @property
    def version_text(self) -> str:
        return ".".join(str(n) for n in self.version)

    @property
    def mapped_ram(self) -> bool:
        return {"mapped-ram", "multifd"} <= self.capabilities

    @property
    def exit_on_error(self) -> bool:
        return self.version >= EXIT_ON_ERROR_FROM


@dataclass(frozen=True)
class Saved:
    """A save of a running VM."""

    # The image frozen at the pause: the backing file of every VM restored from
    # the save, or, in a save by clone, the clone itself.
    frozen: Path
    # The original's new top, on the frozen image, or None in a save by clone,
    # which leaves the original on its top.
    overlay: Path | None
    state: Path
    # The host's clock, in seconds since the epoch, at the stop and the continue.
    stopped_at: float
    resumed_at: float
    pause_s: float
    snapshot_s: float
    # From the migrate command to its return, within migrate_s.
    migrate_command_s: float
    migrate_s: float
    state_bytes: int
    state_allocated_bytes: int
    migration: dict[str, Any]
    # The state file's format, and the QEMU that wrote it.
    format: str = MAPPED_RAM
    qemu_version: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            k: str(v) if isinstance(v, Path) else v for k, v in asdict(self).items()
        }


@dataclass(frozen=True)
class Restored:
    """A VM restored from a save."""

    pid: int
    # From the launch to QEMU's QMP socket, from the socket to the state
    # loaded, and from the launch to the restored VM running.
    launch_s: float
    load_s: float
    launch_to_running_s: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def command_line(
    machine: Machine, hypervisor: Hypervisor, *, incoming: bool = False
) -> list[str]:
    """The QEMU command line of a VM. With incoming, QEMU starts paused and
    waits for `migrate-incoming`."""
    for path in (machine.disk, machine.rundir, machine.cidata):
        if path is None:
            continue
        if "," in str(path):
            raise VMError(f"a path with a comma is not supported: {path}")
        # QEMU resolves a relative path against its own working directory.
        if not path.is_absolute():
            raise VMError(f"a path must be absolute: {path}")
    forward = f"hostfwd=tcp:127.0.0.1:{machine.ssh_port}-:22"
    argv = [
        hypervisor.binary,
        "-name",
        machine.name,
        *hypervisor.args(),
        "-m",
        str(machine.memory_mib),
        "-smp",
        f"{machine.cpus},sockets=1,cores={machine.cpus},threads=1",
        "-drive",
        f"file={machine.disk},format=qcow2,if=none,discard=on,id={DISK_ID}",
        "-device",
        f"virtio-blk-pci,drive={DISK_ID},bootindex=1",
        *cidata_args(machine.cidata),
        "-netdev",
        f"user,id=net0,net={machine.network},dhcpstart={machine.dhcp_start},{forward}",
        "-device",
        f"virtio-net-pci,netdev=net0,mac={machine.mac}",
        "-device",
        "virtio-rng-pci",
        "-display",
        "none",
        "-vga",
        "none",
        "-parallel",
        "none",
        "-chardev",
        f"file,id=serial0,path={machine.serial_log}",
        "-serial",
        "chardev:serial0",
        "-chardev",
        f"socket,id=qmp0,path={machine.qmp_socket},server=on,wait=off",
        "-qmp",
        "chardev:qmp0",
        "-pidfile",
        str(machine.pidfile),
    ]
    if incoming:
        argv += ["-incoming", "defer"]
    return argv


def cidata_args(cidata: Path | None) -> list[str]:
    if cidata is None:
        return []
    return [
        "-drive",
        f"id={CIDATA_ID},if=none,format=raw,readonly=on,file={cidata}",
        "-device",
        f"virtio-blk-pci,drive={CIDATA_ID}",
    ]


class Engine:
    """Launches, saves, restores, stops and deletes VMs on one host."""

    def __init__(
        self,
        hypervisor: Hypervisor,
        *,
        tuning: Tuning | None = None,
        run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        popen: Callable[..., Any] = subprocess.Popen,
        connect: Callable[..., QMP] = QMP,
        wall: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        poll_s: float = 0.02,
        job_poll_s: float = 0.5,
        launch_timeout_s: float = 60.0,
        migrate_timeout_s: float = 900.0,
    ) -> None:
        self.hypervisor = hypervisor
        # The save's format is the platform's, unless a caller sets another.
        self.tuning = tuning or hypervisor.tuning
        self._run = run
        self._popen = popen
        self._connect = connect
        self._wall = wall
        self._monotonic = monotonic
        self._sleep = sleep
        self._poll_s = poll_s
        self._job_poll_s = job_poll_s
        self._launch_timeout_s = launch_timeout_s
        self._migrate_timeout_s = migrate_timeout_s
        self._processes: dict[str, Any] = {}
        # What the installed QEMU supports, read once, since every VM of the
        # engine runs the same binary.
        self._features: Features | None = None

    # Disks

    def image_format(self, image: Path) -> str:
        """The format of a disk image, such as qcow2 or raw, read without
        taking QEMU's lock on it, since a running VM may hold it."""
        result = self._run(
            [QEMU_IMG, "info", "-U", "--output=json", str(image)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise VMError(f"qemu-img info of {image} failed: {result.stderr.strip()}")
        return str(json.loads(result.stdout)["format"])

    def create_overlay(
        self, backing: Path, overlay: Path, backing_format: str | None = None
    ) -> None:
        """A new qcow2 overlay on `backing`, which QEMU then opens read-only.
        Both paths are absolute, since qemu-img resolves a relative backing
        path against the overlay's own directory."""
        if not (backing.is_absolute() and overlay.is_absolute()):
            raise VMError(f"an overlay's paths must be absolute: {backing}, {overlay}")
        if overlay.exists():
            raise VMError(f"{overlay} exists")
        backing_format = backing_format or self.image_format(backing)
        overlay.parent.mkdir(parents=True, exist_ok=True)
        argv = [
            QEMU_IMG,
            "create",
            "-q",
            "-f",
            "qcow2",
            "-b",
            str(backing),
            "-F",
            backing_format,
        ]
        result = self._run(
            [*argv, str(overlay)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise VMError(
                f"qemu-img create of {overlay} failed: {result.stderr.strip()}"
            )

    # Processes

    def launch(self, machine: Machine, *, incoming: bool = False) -> int:
        """Start QEMU for `machine`, and return its process identifier once its
        QMP socket exists."""
        if self.pid(machine) is not None:
            raise VMError(f"{machine.name} is already running")
        machine.rundir.mkdir(parents=True, exist_ok=True)
        for leftover in (machine.qmp_socket, machine.pidfile):
            leftover.unlink(missing_ok=True)
        argv = self.hypervisor.pinned(
            command_line(machine, self.hypervisor, incoming=incoming), machine.affinity
        )
        (machine.rundir / "argv.json").write_text(json.dumps(argv, indent=1) + "\n")
        with open(machine.qemu_log, "ab") as log:
            proc = self._popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
            )
        self._processes[machine.name] = proc
        deadline = self._monotonic() + self._launch_timeout_s
        while not machine.qmp_socket.exists():
            if proc.poll() is not None:
                self._processes.pop(machine.name, None)
                raise VMError(
                    f"QEMU for {machine.name} exited with {proc.returncode}: "
                    f"{log_tail(machine.qemu_log)}"
                )
            if self._monotonic() > deadline:
                self._signal(proc.pid, signal.SIGKILL)
                raise VMError(f"QEMU for {machine.name} opened no QMP socket in time")
            self._sleep(self._poll_s)
        return int(proc.pid)

    def pid(self, machine: Machine) -> int | None:
        """The process identifier of the machine's QEMU, when it runs."""
        proc = self._processes.get(machine.name)
        if proc is not None:
            if proc.poll() is None:
                return int(proc.pid)
            self._processes.pop(machine.name, None)
            return None
        try:
            pid = int(machine.pidfile.read_text().strip())
        except (OSError, ValueError):
            return None
        return pid if runs_qemu(pid, machine.name) else None

    def status(self, machine: Machine) -> str:
        """QEMU's run state of the machine, such as running or paused, or
        stopped when no QEMU runs for it."""
        if self.pid(machine) is None:
            return "stopped"
        try:
            with self._connect(machine.qmp_socket, connect_timeout_s=2.0) as qmp:
                return str(qmp.execute("query-status")["status"])
        except QMPError:
            return "unreachable"

    def top(self, machine: Machine) -> Path:
        """The top image of the running machine's disk."""
        with self._connect(machine.qmp_socket) as qmp:
            return self._top(qmp)

    # Save and restore

    def save(
        self,
        machine: Machine,
        overlay: Path | None,
        state: Path,
        *,
        clone: Path | None = None,
    ) -> Saved:
        """Pause the running machine, freeze its disk, write its state to
        `state`, and continue it. By default the disk freezes by a switch to
        `overlay`, a new layer. With `clone`, the top image is cloned to
        `clone` instead, as it stands once the stop has flushed QEMU's
        writes, and the machine keeps its top with no new layer. The machine
        continues even when the save fails."""
        if (overlay is None) == (clone is None):
            raise VMError("a save takes an overlay or a clone")
        for path in (overlay, clone, state):
            if path is not None and path.exists():
                raise VMError(f"{path} exists")
        state.parent.mkdir(parents=True, exist_ok=True)
        with self._connect(machine.qmp_socket) as qmp:
            status = qmp.execute("query-status")["status"]
            if status != "running":
                raise VMError(f"{machine.name} is {status}, not running")
            frozen = self._top(qmp)
            features = self.features(qmp)
            fmt = self.save_format(features)
            self._tune(qmp, fmt)
            started = self._monotonic()
            stopped_at = self._wall()
            qmp.execute("stop")
            snapshotted = commanded = migrated = started
            info: dict[str, Any] = {}
            failure: Exception | None = None
            try:
                if clone is None:
                    qmp.execute(
                        "blockdev-snapshot-sync",
                        {
                            "device": DISK_ID,
                            "snapshot-file": str(overlay),
                            "format": "qcow2",
                        },
                    )
                else:
                    # The stop drained and flushed the disk, so the clone holds
                    # the top image as the guest left it.
                    self.clone(frozen, clone)
                    frozen = clone
                snapshotted = self._monotonic()
                qmp.execute("migrate", {"uri": f"file:{state}"})
                commanded = self._monotonic()
                info = self._wait_migration(qmp)
                migrated = self._monotonic()
            except (QMPError, VMError) as error:
                failure = error
            try:
                qmp.execute("cont")
            except QMPError as error:
                raise VMError(
                    f"{machine.name} did not continue after the save: {error}"
                ) from (failure or error)
            resumed = self._monotonic()
            resumed_at = self._wall()
        if failure is not None:
            state.unlink(missing_ok=True)
            raise VMError(f"the save of {machine.name} failed: {failure}") from failure
        stat = state.stat()
        ram = info.get("ram") or {}
        return Saved(
            frozen=frozen,
            overlay=overlay,
            state=state,
            stopped_at=stopped_at,
            resumed_at=resumed_at,
            pause_s=resumed - started,
            snapshot_s=snapshotted - started,
            migrate_command_s=commanded - snapshotted,
            migrate_s=migrated - snapshotted,
            state_bytes=stat.st_size,
            state_allocated_bytes=getattr(stat, "st_blocks", 0) * 512,
            migration={
                "status": info.get("status"),
                "total_time_ms": info.get("total-time"),
                "downtime_ms": info.get("downtime"),
                "mbps": ram.get("mbps"),
                "transferred": ram.get("transferred"),
            },
            format=fmt,
            qemu_version=features.version_text,
        )

    def restore(
        self,
        copy: Machine,
        frozen: Path | None,
        state: Path,
        *,
        disk_ready: bool = False,
        fmt: str | None = None,
    ) -> Restored:
        """Start `copy` on a new overlay of `frozen`, or on its own disk
        where `disk_ready`, as a clone made by a save is, load `state` into it
        and continue it. `fmt` is the state file's format, as its save
        recorded it; by default, the format a save would choose now. A VM that
        fails to load is stopped."""
        if not state.is_file():
            raise VMError(f"no state file {state}")
        if disk_ready:
            if not copy.disk.is_file():
                raise VMError(f"no disk {copy.disk} for {copy.name}")
        elif frozen is None:
            raise VMError(f"no image for the overlay of {copy.name}")
        else:
            self.create_overlay(frozen, copy.disk)
        started = self._monotonic()
        pid = self.launch(copy, incoming=True)
        launched = self._monotonic()
        try:
            with self._connect(copy.qmp_socket) as qmp:
                features = self.features(qmp)
                fmt = fmt or self.save_format(features)
                if fmt == MAPPED_RAM and not features.mapped_ram:
                    raise VMError(
                        f"{state} is in mapped RAM, which QEMU "
                        f"{features.version_text} cannot load"
                    )
                self._tune(qmp, fmt)
                incoming: dict[str, Any] = {"uri": f"file:{state}"}
                if features.exit_on_error:
                    # exit-on-error keeps a VM whose load failed alive, so
                    # that query-migrate can say why.
                    incoming["exit-on-error"] = False
                qmp.execute("migrate-incoming", incoming)
                status = self._wait_incoming(qmp)
                loaded = self._monotonic()
                if status != "running":
                    qmp.execute("cont")
                status = qmp.execute("query-status")["status"]
                if status != "running":
                    raise VMError(f"{copy.name} is {status} after the continue")
        except (QMPError, VMError) as error:
            self.stop(copy)
            raise VMError(f"the restore of {copy.name} failed: {error}") from error
        running = self._monotonic()
        return Restored(
            pid=pid,
            launch_s=launched - started,
            load_s=loaded - launched,
            launch_to_running_s=running - started,
        )

    def stop(self, machine: Machine, *, timeout_s: float = 30.0) -> bool:
        """End the machine's QEMU, by `quit` and then by signals. Returns
        whether it was running."""
        pid = self.pid(machine)
        proc = self._processes.pop(machine.name, None)
        if pid is None:
            return False
        try:
            with self._connect(machine.qmp_socket, connect_timeout_s=2.0) as qmp:
                qmp.execute("quit")
        except QMPError:
            pass
        for sig in (None, signal.SIGTERM, signal.SIGKILL):
            if sig is not None:
                self._signal(pid, sig)
            if self._exited(pid, proc, timeout_s if sig is None else 10.0):
                break
        else:
            raise VMError(f"QEMU {pid} for {machine.name} did not end")
        for leftover in (machine.qmp_socket, machine.pidfile):
            leftover.unlink(missing_ok=True)
        return True

    def delete(self, machine: Machine) -> None:
        """Stop the machine, and delete its disk and its run directory."""
        self.stop(machine)
        machine.disk.unlink(missing_ok=True)
        shutil.rmtree(machine.rundir, ignore_errors=True)

    # Clones

    def can_clone(self, directory: Path) -> bool:
        """Whether the file system of `directory` clones a file without
        copying its data, as the Apple File System (APFS) does."""
        probe = directory / f".clone-probe-{os.getpid()}"
        twin = probe.with_name(probe.name + "-clone")
        try:
            probe.write_bytes(b"probe")
            result = self._run(
                clone_argv(probe, twin),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
            )
            return result.returncode == 0 and twin.is_file()
        except OSError:
            return False
        finally:
            probe.unlink(missing_ok=True)
            twin.unlink(missing_ok=True)

    def clone(self, source: Path, target: Path) -> None:
        """A clone of `source` at `target`, which shares its blocks until
        either is written."""
        if target.exists():
            raise VMError(f"{target} exists")
        target.parent.mkdir(parents=True, exist_ok=True)
        result = self._run(
            clone_argv(source, target),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise VMError(f"the clone of {source} failed: {result.stderr.strip()}")

    # The chain of images

    def stream(
        self, machine: Machine, base: Path, *, timeout_s: float = 7200.0
    ) -> dict[str, Any]:
        """Copy into the running machine's top image every cluster that the
        images between it and `base` hold, with `block-stream`, and make `base`
        the top's backing file. The VM runs throughout, and no image below the
        top changes."""
        job = f"stream-{int(self._wall())}"
        with self._connect(machine.qmp_socket) as qmp:
            top = self._top(qmp)
            started = self._monotonic()
            qmp.execute(
                "block-stream", {"job-id": job, "device": DISK_ID, "base": str(base)}
            )
            length = 0
            deadline = started + timeout_s
            while True:
                found = [
                    j for j in qmp.execute("query-block-jobs") if j["device"] == job
                ]
                if not found:
                    break
                length = int(found[0].get("len", length))
                if self._monotonic() > deadline:
                    qmp.execute("block-job-cancel", {"device": job})
                    raise VMError(f"the stream of {machine.name} did not end in time")
                self._sleep(self._job_poll_s)
            ended = self._monotonic()
            for event in qmp.events:
                data = event.get("data", {})
                if data.get("device") != job:
                    continue
                if event["event"] == "BLOCK_JOB_ERROR" or data.get("error"):
                    raise VMError(f"the stream of {machine.name} failed: {data}")
            backing = self._backing(qmp)
        if backing != base:
            raise VMError(f"{top} backs onto {backing} after the stream, not {base}")
        return {
            "top": str(top),
            "base": str(base),
            "stream_s": round(ended - started, 3),
            "bytes": length,
        }

    # Internals

    def _backing(self, qmp: QMP) -> Path | None:
        for block in qmp.execute("query-block"):
            if block.get("device") == DISK_ID and block.get("inserted"):
                image = block["inserted"].get("image", {})
                below = image.get("backing-image", {}).get("filename")
                return Path(below) if below else None
        raise VMError(f"the VM has no {DISK_ID}")

    def _top(self, qmp: QMP) -> Path:
        for block in qmp.execute("query-block"):
            if block.get("device") == DISK_ID and block.get("inserted"):
                return Path(block["inserted"]["file"])
        raise VMError(f"the VM has no {DISK_ID}")

    def features(self, qmp: QMP) -> Features:
        """What the installed QEMU supports, from query-version and
        query-migrate-capabilities."""
        if self._features is None:
            version = qmp.execute("query-version")["qemu"]
            listed = qmp.execute("query-migrate-capabilities")
            self._features = Features(
                version=(
                    int(version["major"]),
                    int(version["minor"]),
                    int(version["micro"]),
                ),
                capabilities=frozenset(c["capability"] for c in listed),
            )
        return self._features

    def save_format(self, features: Features) -> str:
        """The platform's format, or the stream where the installed QEMU
        lacks mapped RAM, as QEMU 8.2 does."""
        if self.tuning.mapped_ram and features.mapped_ram:
            return MAPPED_RAM
        return STREAM

    def _tune(self, qmp: QMP, fmt: str) -> None:
        """The migration's settings for `fmt`, with only the capabilities
        that format needs: none for the stream."""
        parameters: dict[str, Any] = {"max-bandwidth": self.tuning.max_bandwidth}
        if fmt == MAPPED_RAM:
            qmp.execute(
                "migrate-set-capabilities",
                {
                    "capabilities": [
                        {"capability": "mapped-ram", "state": True},
                        {"capability": "multifd", "state": True},
                    ]
                },
            )
            parameters["multifd-channels"] = self.tuning.multifd_channels
        qmp.execute("migrate-set-parameters", parameters)

    def _wait_migration(self, qmp: QMP) -> dict[str, Any]:
        deadline = self._monotonic() + self._migrate_timeout_s
        while True:
            info = qmp.execute("query-migrate")
            status = info.get("status")
            if status == "completed":
                return info
            if status in ("failed", "cancelled"):
                raise VMError(f"the migration {status}: {info.get('error-desc', '')}")
            if self._monotonic() > deadline:
                qmp.execute("migrate_cancel")
                raise VMError("the migration did not complete in time")
            self._sleep(self._poll_s)

    def _wait_incoming(self, qmp: QMP) -> str:
        deadline = self._monotonic() + self._migrate_timeout_s
        while True:
            status = str(qmp.execute("query-status")["status"])
            info = qmp.execute("query-migrate")
            if info.get("status") == "failed":
                raise VMError(f"the state did not load: {info.get('error-desc', '')}")
            if status != "inmigrate":
                return status
            if self._monotonic() > deadline:
                raise VMError("the state did not load in time")
            self._sleep(self._poll_s)

    def _exited(self, pid: int, proc: Any, timeout_s: float) -> bool:
        deadline = self._monotonic() + timeout_s
        while True:
            if proc is not None:
                if proc.poll() is not None:
                    return True
            elif not alive(pid):
                return True
            if self._monotonic() > deadline:
                return False
            self._sleep(0.1)

    @staticmethod
    def _signal(pid: int, sig: int) -> None:
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError):
            pass


def clone_argv(source: Path, target: Path) -> list[str]:
    """The command that clones a file: clonefile on macOS, through `cp -c`,
    and a reflink on Linux."""
    if sys.platform == "darwin":
        return ["cp", "-c", str(source), str(target)]
    return ["cp", "--reflink=always", str(source), str(target)]


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def runs_qemu(pid: int, name: str) -> bool:
    """Whether `pid` is a live QEMU process for the VM `name`, so that a stale
    pidfile never leads to signalling another process."""
    if not alive(pid):
        return False
    cmdline = Path(f"/proc/{pid}/cmdline")
    try:
        if cmdline.exists():
            args = cmdline.read_bytes().split(b"\0")
            return name.encode() in args
        out = subprocess.run(
            ["ps", "-o", "command=", "-p", str(pid)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
        ).stdout
    except OSError:
        return False
    return f"-name {name} " in out + " "


def log_tail(path: Path, lines: int = LOG_TAIL_LINES) -> str:
    try:
        return " | ".join(path.read_text(errors="replace").splitlines()[-lines:])
    except OSError:
        return "(no log)"
