"""A minimal client of the QEMU Machine Protocol (QMP) over a UNIX socket.

QEMU sends a greeting when a client connects. The client enters command mode
with `qmp_capabilities`, then sends one command at a time and reads lines until
the reply that carries the command's identifier, keeping the asynchronous
events that arrive on the way. The module uses the standard library alone.

    with QMP(path) as qmp:
        qmp.execute("query-status")
"""

from __future__ import annotations

import json
import socket
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

# The longest path of a UNIX socket that both Linux (108 bytes) and macOS (104
# bytes) accept, with the terminating zero byte.
MAX_SOCKET_PATH = 103


class QMPError(Exception):
    """The connection to QEMU failed, closed or timed out."""


class QMPCommandError(QMPError):
    """QEMU answered a command with an error."""

    def __init__(self, command: str, error: dict[str, Any]) -> None:
        self.command = command
        self.error_class = str(error.get("class", ""))
        self.desc = str(error.get("desc", ""))
        super().__init__(f"{command}: {self.error_class}: {self.desc}")


class QMP:
    """One connection to the QMP socket of a running QEMU process."""

    def __init__(
        self,
        path: Path | str,
        *,
        timeout_s: float = 60.0,
        connect_timeout_s: float = 10.0,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if len(str(path).encode()) > MAX_SOCKET_PATH:
            raise QMPError(
                f"the socket path {path} is longer than {MAX_SOCKET_PATH} bytes"
            )
        deadline = monotonic() + connect_timeout_s
        while True:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(timeout_s)
            try:
                sock.connect(str(path))
                break
            except OSError as error:
                sock.close()
                if monotonic() >= deadline:
                    raise QMPError(f"cannot connect to {path}: {error}") from error
                sleep(0.1)
        self._sock = sock
        self._buffer = b""
        self._last_id = 0
        self.events: list[dict[str, Any]] = []
        try:
            greeting = self._read()
            if "QMP" not in greeting:
                raise QMPError(f"{path} sent no QMP greeting: {greeting}")
            self.greeting: dict[str, Any] = greeting["QMP"]
            self.execute("qmp_capabilities")
        except BaseException:
            sock.close()
            raise

    def execute(self, command: str, arguments: dict[str, Any] | None = None) -> Any:
        """Run one command and return the value of its reply."""
        self._last_id += 1
        ident = self._last_id
        message: dict[str, Any] = {"execute": command, "id": ident}
        if arguments:
            message["arguments"] = arguments
        try:
            self._sock.sendall(json.dumps(message).encode() + b"\n")
        except OSError as error:
            raise QMPError(f"{command}: cannot send: {error}") from error
        while True:
            reply = self._read()
            if "event" in reply:
                self.events.append(reply)
                continue
            if reply.get("id") != ident:
                continue
            if "error" in reply:
                raise QMPCommandError(command, reply["error"])
            return reply.get("return")

    def close(self) -> None:
        self._sock.close()

    def __enter__(self) -> QMP:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _read(self) -> dict[str, Any]:
        while True:
            while b"\n" not in self._buffer:
                try:
                    chunk = self._sock.recv(65536)
                except TimeoutError as error:
                    raise QMPError("timed out waiting for QEMU") from error
                except OSError as error:
                    raise QMPError(f"cannot read from QEMU: {error}") from error
                if not chunk:
                    raise QMPError("QEMU closed the connection")
                self._buffer += chunk
            line, self._buffer = self._buffer.split(b"\n", 1)
            if line.strip():
                return json.loads(line)
