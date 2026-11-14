"""Calls to Incus.

The episode runner reaches the RavensPort nodes only through Incus, so its
calls send no network traffic to them (episodes.md §6.6, settle period). No
call uses su, which opens a login session that the security monitoring
reports. Every call runs with its standard input closed, except a session that
the episode runner holds through a pipe of its own: the step session, and the
attack chain's session. Incus passes on a standard input that is not a
terminal, so an inherited standard input would let the command read the
episode runner's own input, and a pipe carries only what the episode runner
writes to it.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class IncusError(RuntimeError):
    """A call to Incus that failed."""


@dataclass(frozen=True)
class Result:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class Incus:
    def __init__(
        self,
        run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        program: str = "incus",
    ) -> None:
        self._run = run
        self.program = program

    def exec(self, node: str, *argv: str, timeout_s: float = 60.0) -> Result:
        """Run a command on a node."""
        return self._call(["exec", node, "--", *argv], timeout_s)

    def exec_as(
        self, node: str, uid: int, gid: int, *argv: str, timeout_s: float = 60.0
    ) -> Result:
        """Run a command on a node as the user and group given, from the root
        directory, with no login session."""
        return self._call(
            [
                "exec",
                node,
                "--user",
                str(uid),
                "--group",
                str(gid),
                "--cwd",
                "/",
                "--",
                *argv,
            ],
            timeout_s,
        )

    def command(self, *args: str, timeout_s: float = 60.0) -> Result:
        """Run another command of Incus, such as config device add."""
        return self._call(list(args), timeout_s)

    def pull(
        self, node: str, path: str, dest: Path, timeout_s: float = 300.0
    ) -> Result:
        """Copy a file from a node to dest on the episode runner's host."""
        return self._call(["file", "pull", f"{node}{path}", str(dest)], timeout_s)

    def instances(self, timeout_s: float = 60.0) -> list[dict[str, Any]]:
        """Every instance, as `incus list --format json` describes it."""
        result = self._call(["list", "--format", "json"], timeout_s)
        if not result.ok:
            raise IncusError(f"incus list failed: {result.stderr.strip()}")
        return json.loads(result.stdout)

    def query(self, path: str, timeout_s: float = 60.0) -> dict[str, Any]:
        """An object of the Incus application programming interface (API),
        such as /1.0/instances/sim-host/state."""
        result = self._call(["query", path], timeout_s)
        if not result.ok:
            raise IncusError(f"incus query {path} failed: {result.stderr.strip()}")
        return json.loads(result.stdout)

    def step_session(self, node: str, *argv: str) -> list[str]:
        """The command of the step session, which runs the defender's program,
        through the launcher with its arguments, on a node and keeps its
        standard input open for the step messages (episodes.md §7, step
        protocol)."""
        return [self.program, "exec", node, "--", *argv]

    def attack_session(self, node: str, binary: str) -> list[str]:
        """The command of the attack chain's session, which runs a paced
        attack chain on a node and keeps its standard input open for the
        releases (episodes.md §10.3, pacing interface). A shell prints its
        process identifier and replaces itself with the binary, so the first
        line of standard output is the binary's process identifier."""
        return [
            self.program,
            "exec",
            node,
            "--",
            "sh",
            "-c",
            'echo $$; exec "$0" --paced',
            binary,
        ]

    def _call(self, args: list[str], timeout_s: float) -> Result:
        argv = [self.program, *args]
        try:
            proc = self._run(
                argv,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=timeout_s,
            )
        except subprocess.TimeoutExpired:
            return Result(-1, "", f"{' '.join(argv)} timed out after {timeout_s} s")
        return Result(proc.returncode, proc.stdout, proc.stderr)
