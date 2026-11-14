"""Fakes for the living port's tests, so that no virtual machine is needed: a
QEMU Machine Protocol (QMP) server on a UNIX socket that answers as QEMU does,
a stand-in for subprocess.Popen that starts one such server for each launch,
and a stand-in for subprocess.run that creates the overlays qemu-img would."""

from __future__ import annotations

import json
import socket
import subprocess
import threading
from pathlib import Path
from typing import Any

GREETING = {
    "QMP": {
        "version": {"qemu": {"major": 11, "minor": 1, "micro": 2}},
        "capabilities": [],
    }
}

# The migration capabilities that QEMU 11.1 lists, of those that matter here,
# and those that Ubuntu's QEMU 8.2 lists, which lack mapped RAM.
QEMU_11_1 = ((11, 1, 2), frozenset({"xbzrle", "multifd", "mapped-ram", "postcopy-ram"}))
QEMU_8_2 = ((8, 2, 2), frozenset({"xbzrle", "multifd", "postcopy-ram"}))


class FakeQEMU:
    """A QMP server for one virtual machine. It records every command, keeps a
    run state as QEMU does, writes the overlay and the state file that a save
    creates, and completes a migration after a few queries."""

    def __init__(
        self,
        path: Path,
        *,
        status: str = "running",
        top: str = "",
        features: tuple[tuple[int, int, int], frozenset[str]] = QEMU_11_1,
    ) -> None:
        self.path = Path(path)
        self.status = status
        self.top = top
        self.version, self.capabilities = features
        self.commands: list[tuple[str, dict[str, Any]]] = []
        # Error replies to give, by command.
        self.errors: dict[str, dict[str, str]] = {}
        # Events sent before the reply to the named command.
        self.events: dict[str, list[dict[str, Any]]] = {}
        self.migration = "none"
        self.migration_end = "completed"
        self.polls = 2
        self._left = 0
        # Each image's backing file, as the chain stands.
        self.backing: dict[str, str] = {}
        # A stream job, with the queries left before it ends.
        self.job: dict[str, Any] | None = None
        self.stream_error = ""
        # Events sent once, before the next reply.
        self.pending: list[dict[str, Any]] = []
        self.quit = threading.Event()
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(str(self.path))
        self._server.listen(4)
        self._server.settimeout(0.2)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def names(self) -> list[str]:
        return [name for name, _ in self.commands if name != "qmp_capabilities"]

    def close(self) -> None:
        self.quit.set()
        self._server.close()
        self.path.unlink(missing_ok=True)

    def _serve(self) -> None:
        while not self.quit.is_set():
            try:
                conn, _ = self._server.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with conn:
                self._session(conn)

    def _session(self, conn: socket.socket) -> None:
        conn.sendall(json.dumps(GREETING).encode() + b"\r\n")
        buffer = b""
        while True:
            try:
                chunk = conn.recv(65536)
            except OSError:
                return
            if not chunk:
                return
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                message = json.loads(line)
                name = message["execute"]
                reply = self._reply(message)
                for event in [*self.events.get(name, []), *self.pending]:
                    conn.sendall(json.dumps(event).encode() + b"\r\n")
                self.pending.clear()
                conn.sendall(json.dumps(reply).encode() + b"\r\n")
                if name == "quit" and name not in self.errors:
                    self.close()
                    return

    def _refusal(self, name: str, arguments: dict[str, Any]) -> dict[str, str] | None:
        """What a QEMU of this version refuses, as it words it."""
        if name == "migrate-set-capabilities":
            for entry in arguments["capabilities"]:
                if entry["capability"] not in self.capabilities:
                    return {
                        "class": "GenericError",
                        "desc": "Parameter 'capability' does not accept value "
                        f"'{entry['capability']}'",
                    }
        if name == "migrate-incoming" and "exit-on-error" in arguments:
            if self.version < (9, 1, 0):
                return {
                    "class": "GenericError",
                    "desc": "Parameter 'exit-on-error' is unexpected",
                }
        return None

    def _query_version(self, arguments: dict[str, Any]) -> dict[str, Any]:
        major, minor, micro = self.version
        return {"qemu": {"major": major, "minor": minor, "micro": micro}, "package": ""}

    def _query_migrate_capabilities(
        self, arguments: dict[str, Any]
    ) -> list[dict[str, Any]]:
        return [{"capability": c, "state": False} for c in sorted(self.capabilities)]

    def _reply(self, message: dict[str, Any]) -> dict[str, Any]:
        name, arguments = message["execute"], message.get("arguments", {})
        self.commands.append((name, arguments))
        refused = self._refusal(name, arguments)
        if refused:
            return {"error": refused, "id": message.get("id")}
        if name in self.errors:
            return {"error": self.errors[name], "id": message.get("id")}
        handler = getattr(self, "_" + name.replace("-", "_"), lambda arguments: {})
        return {"return": handler(arguments), "id": message.get("id")}

    def _query_status(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return {"status": self.status, "running": self.status == "running"}

    def _stop(self, arguments: dict[str, Any]) -> dict[str, Any]:
        self.status = "paused"
        return {}

    def _cont(self, arguments: dict[str, Any]) -> dict[str, Any]:
        self.status = "running"
        return {}

    def _query_block(self, arguments: dict[str, Any]) -> list[dict[str, Any]]:
        image: dict[str, Any] = {"filename": self.top}
        if self.top in self.backing:
            image["backing-image"] = {"filename": self.backing[self.top]}
        return [
            {"device": "cdrom0"},
            {"device": "boot-disk", "inserted": {"file": self.top, "image": image}},
        ]

    def _blockdev_snapshot_sync(self, arguments: dict[str, Any]) -> dict[str, Any]:
        Path(arguments["snapshot-file"]).write_bytes(b"QFI\xfb")
        self.backing[arguments["snapshot-file"]] = self.top
        self.top = arguments["snapshot-file"]
        return {}

    def _block_stream(self, arguments: dict[str, Any]) -> dict[str, Any]:
        self.job = {**arguments, "left": self.polls}
        return {}

    def _query_block_jobs(self, arguments: dict[str, Any]) -> list[dict[str, Any]]:
        if self.job is None:
            return []
        self.job["left"] -= 1
        if self.job["left"] > 0:
            return [{"device": self.job["job-id"], "type": "stream", "len": 1 << 30}]
        if not self.stream_error:
            self.backing[self.top] = self.job["base"]
        data = {"device": self.job["job-id"], "type": "stream", "len": 1 << 30}
        if self.stream_error:
            data["error"] = self.stream_error
        self.pending.append({"event": "BLOCK_JOB_COMPLETED", "data": data})
        self.job = None
        return []

    def _migrate(self, arguments: dict[str, Any]) -> dict[str, Any]:
        Path(arguments["uri"].removeprefix("file:")).write_bytes(b"\0" * 8192)
        self.migration, self._left = "active", self.polls
        return {}

    def _migrate_incoming(self, arguments: dict[str, Any]) -> dict[str, Any]:
        self.migration, self._left = "active", self.polls
        return {}

    def _query_migrate(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if self.migration == "active":
            self._left -= 1
            if self._left <= 0:
                self.migration = self.migration_end
                if self.status == "inmigrate" and self.migration == "completed":
                    self.status = "paused"
                elif self.status == "paused" and self.migration == "completed":
                    self.status = "postmigrate"
        info: dict[str, Any] = {"status": self.migration}
        if self.migration == "completed":
            info.update(
                {
                    "total-time": 4660,
                    "downtime": 4650,
                    "ram": {"mbps": 27455.0, "transferred": 8 << 30},
                }
            )
        if self.migration == "failed":
            info["error-desc"] = "no space left on device"
        return info

    def _migrate_cancel(self, arguments: dict[str, Any]) -> dict[str, Any]:
        self.migration = "cancelled"
        return {}


class FakeProcess:
    """A QEMU process: its QMP server, until it quits."""

    def __init__(
        self, pid: int, server: FakeQEMU | None, exit_at_start: int | None = None
    ) -> None:
        self.pid = pid
        self.server = server
        self.returncode = exit_at_start

    def poll(self) -> int | None:
        if (
            self.returncode is None
            and self.server is not None
            and self.server.quit.is_set()
        ):
            self.returncode = 0
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        return self.poll() or 0


class FakeHost:
    """Stands in for subprocess.Popen and subprocess.run on the physical host:
    each launch of QEMU starts a FakeQEMU on the socket that its command line
    names, and each qemu-img create makes an empty overlay."""

    def __init__(self) -> None:
        self.launches: list[list[str]] = []
        self.runs: list[list[str]] = []
        self.servers: dict[str, FakeQEMU] = {}
        self.processes: list[FakeProcess] = []
        # A launch that exits at once, with this status.
        self.fail_launch: int | None = None
        # The format qemu-img info reports, by path, qcow2 by default.
        self.formats: dict[str, str] = {}
        # Whether the file system clones files.
        self.clones = False
        # The QEMU that each launch runs.
        self.features = QEMU_11_1
        # The QEMU processes that had quit when each launch came.
        self.quit_at_launch: dict[str, list[str]] = {}
        self._next_pid = 4000

    def popen(self, argv: list[str], **kwargs: Any) -> FakeProcess:
        self.launches.append(list(argv))
        assert kwargs["stdin"] is subprocess.DEVNULL
        self._next_pid += 1
        if self.fail_launch is not None:
            kwargs["stdout"].write(
                b"qemu-system-x86_64: Could not set up host forwarding rule\n"
            )
            return FakeProcess(self._next_pid, None, self.fail_launch)
        name = argv[argv.index("-name") + 1]
        self.quit_at_launch[name] = [
            n for n, server in self.servers.items() if server.quit.is_set()
        ]
        chardev = next(a for a in argv if a.startswith("socket,id=qmp0,path="))
        path = chardev.split("path=", 1)[1].split(",", 1)[0]
        drive = next(a for a in argv if a.startswith("file=") and "id=boot-disk" in a)
        top = drive.removeprefix("file=").split(",", 1)[0]
        status = "inmigrate" if "-incoming" in argv else "running"
        server = FakeQEMU(Path(path), status=status, top=top, features=self.features)
        self.servers[name] = server
        process = FakeProcess(self._next_pid, server)
        self.processes.append(process)
        return process

    def run(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        self.runs.append(list(argv))
        assert kwargs["stdin"] is subprocess.DEVNULL
        if argv[:2] == ["qemu-img", "create"]:
            Path(argv[-1]).write_bytes(b"QFI\xfb")
        if argv[:2] == ["qemu-img", "info"]:
            found = json.dumps({"format": self.formats.get(argv[-1], "qcow2")})
            return subprocess.CompletedProcess(argv, 0, found, "")
        if argv[0] == "cp":
            # A clone, as clonefile or a reflink makes one, where the file
            # system clones files.
            if not self.clones:
                return subprocess.CompletedProcess(argv, 1, "", "cp: clone failed")
            Path(argv[-1]).write_bytes(Path(argv[-2]).read_bytes())
        return subprocess.CompletedProcess(argv, 0, "", "")

    def clone_runs(self) -> list[list[str]]:
        """Each clone, in order."""
        return [argv for argv in self.runs if argv[0] == "cp"]

    def creates(self) -> list[list[str]]:
        """Each qemu-img create, in order."""
        return [argv for argv in self.runs if argv[:2] == ["qemu-img", "create"]]

    def close(self) -> None:
        for server in self.servers.values():
            server.close()
