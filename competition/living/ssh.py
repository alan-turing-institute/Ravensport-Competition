"""Commands, files and clock readings in a living port's virtual machine (VM),
or in a fork's, over the Secure Shell (SSH) forward on 127.0.0.1.

Every session runs with its standard input closed, except the two whose input
is piped on purpose: the transfer of a tree into the VM, and the clock reading,
which asks the guest for its time once for each line it sends.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class SSHError(Exception):
    """A command in the VM failed, or the VM did not answer."""


@dataclass(frozen=True)
class Ssh:
    """How to reach one VM."""

    port: int
    user: str
    key: Path
    host: str = "127.0.0.1"
    connect_timeout_s: int = 10

    def argv(self, command: str | None = None) -> list[str]:
        options = (
            "IdentitiesOnly=yes",
            "BatchMode=yes",
            "StrictHostKeyChecking=no",
            "UserKnownHostsFile=/dev/null",
            "LogLevel=ERROR",
            f"ConnectTimeout={self.connect_timeout_s}",
            "ServerAliveInterval=15",
            "ServerAliveCountMax=8",
        )
        argv = ["ssh", "-i", str(self.key), "-p", str(self.port)]
        for option in options:
            argv += ["-o", option]
        argv.append(f"{self.user}@{self.host}")
        if command is not None:
            argv.append(command)
        return argv


@dataclass(frozen=True)
class ClockOffset:
    """The guest's clock less the host's, from the reading with the shortest
    round trip."""

    offset_s: float
    round_trip_s: float
    # The host's clock at the middle of that reading.
    host_s: float


class Remote:
    """Runs commands in one VM."""

    def __init__(
        self,
        ssh: Ssh,
        *,
        run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        popen: Callable[..., Any] = subprocess.Popen,
        wall: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.ssh = ssh
        self._run = run
        self._popen = popen
        self._wall = wall
        self._monotonic = monotonic
        self._sleep = sleep

    def run(
        self, command: str, *, timeout_s: float | None = 600.0, check: bool = True
    ) -> subprocess.CompletedProcess:
        """Run a command and capture its output."""
        try:
            result = self._run(
                self.ssh.argv(command),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=timeout_s,
            )
        except subprocess.TimeoutExpired as error:
            raise SSHError(f"timed out after {timeout_s} s: {command}") from error
        if check and result.returncode != 0:
            detail = (result.stderr or result.stdout).strip().splitlines()[-5:]
            raise SSHError(
                f"exit {result.returncode} from {command!r} on port {self.ssh.port}: "
                + " | ".join(detail)
            )
        return result

    def stream(self, command: str, log: Path, *, timeout_s: float | None = None) -> int:
        """Run a command with its output appended to `log`, and return its exit
        status, or -1 when it timed out."""
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "ab") as out:
            try:
                return self._run(
                    self.ssh.argv(command),
                    stdin=subprocess.DEVNULL,
                    stdout=out,
                    stderr=subprocess.STDOUT,
                    timeout=timeout_s,
                ).returncode
            except subprocess.TimeoutExpired:
                return -1

    def wait(self, timeout_s: float = 300.0, poll_s: float = 2.0) -> float:
        """Wait until the VM answers, and return the seconds waited."""
        started = self._monotonic()
        while True:
            try:
                result = self._run(
                    self.ssh.argv("true"),
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    timeout=self.ssh.connect_timeout_s + 10,
                )
                if result.returncode == 0:
                    return self._monotonic() - started
            except subprocess.TimeoutExpired:
                pass
            if self._monotonic() - started > timeout_s:
                raise SSHError(
                    f"no answer on port {self.ssh.port} within {timeout_s} s"
                )
            self._sleep(poll_s)

    def send_tree(
        self,
        source: Path,
        dest: str,
        excludes: Sequence[str] = (),
        timeout_s: float = 900.0,
    ) -> None:
        """Copy the directory `source` into the existing directory `dest` in
        the VM, through tar."""
        pack = ["tar", "-C", str(source), "-cf", "-"]
        for pattern in excludes:
            pack += ["--exclude", pattern]
        pack.append(".")
        env = {**os.environ, "COPYFILE_DISABLE": "1"}
        sender = self._popen(
            pack,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        try:
            unpack = self._run(
                self.ssh.argv(f"tar -C {dest} --no-same-owner -xf -"),
                stdin=sender.stdout,
                capture_output=True,
                text=True,
                timeout=timeout_s,
            )
        finally:
            sender.stdout.close()
            sent = sender.wait()
        if sent != 0 or unpack.returncode != 0:
            raise SSHError(
                f"copying {source} to {dest} failed: tar {sent}, ssh {unpack.returncode}: "
                f"{unpack.stderr.strip()}"
            )

    def fetch_tree(
        self,
        remote: str,
        local_parent: Path,
        timeout_s: float = 900.0,
        sudo: bool = False,
    ) -> None:
        """Copy the directory `remote` from the VM into `local_parent`, under
        its own name. With sudo, the guest reads every file, whoever owns it,
        such as files that a container wrote to a volume."""
        parent, name = remote.rstrip("/").rsplit("/", 1)
        local_parent.mkdir(parents=True, exist_ok=True)
        sender = self._popen(
            self.ssh.argv(
                f"{'sudo ' if sudo else ''}tar -C {parent or '/'} -cf - {name}"
            ),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            unpack = self._run(
                ["tar", "-C", str(local_parent), "-xf", "-"],
                stdin=sender.stdout,
                capture_output=True,
                text=True,
                timeout=timeout_s,
            )
        finally:
            sender.stdout.close()
            sent = sender.wait()
        if sent != 0 or unpack.returncode != 0:
            raise SSHError(
                f"copying {remote} from port {self.ssh.port} failed: ssh {sent}, "
                f"tar {unpack.returncode}: {unpack.stderr.strip()}"
            )

    def clock_offset(self, samples: int = 5, timeout_s: float = 30.0) -> ClockOffset:
        """The guest's clock less the host's. One session reads the guest's
        clock once for each line sent, and the reading with the shortest round
        trip gives the offset, against the middle of that round trip."""
        proc = self._popen(
            self.ssh.argv("while read -r _; do date -u +%s.%N; done"),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        timer = threading.Timer(timeout_s, proc.kill)
        timer.start()
        best: ClockOffset | None = None
        try:
            for _ in range(samples + 1):
                sent = self._wall()
                proc.stdin.write("\n")
                proc.stdin.flush()
                line = proc.stdout.readline()
                received = self._wall()
                if not line:
                    raise SSHError(f"no clock reading on port {self.ssh.port}")
                middle = (sent + received) / 2
                reading = ClockOffset(float(line) - middle, received - sent, middle)
                # The first reading waits for the session to open.
                if best is None or reading.round_trip_s < best.round_trip_s:
                    best = reading
        finally:
            timer.cancel()
            try:
                proc.stdin.close()
            except OSError:
                pass
            proc.wait()
        assert best is not None
        return best
