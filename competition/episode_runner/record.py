"""The episode record (episodes.md §13, episode record).

The episode record is one directory per episode on the episode runner's host,
which no RavensPort node can reach. Each file keeps the timestamps of its own
source. The episode runner writes episode.json, steps.jsonl and startup.json.
The export copies from siem-01 the security monitoring's dated files, under
their paths below /var/ossec/logs in alerts/ and archives/, and the query
interface's record of reads, reads.jsonl, and from sim-host the simulator's
log, sim.log. In an adversarial episode the attack chain's standard error, its
narration and command trace, goes to attack-chain.log.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

FILES = (
    "episode.json",
    "steps.jsonl",
    "startup.json",
    "alerts",
    "archives",
    "reads.jsonl",
    "sim.log",
)
EXPORTED = ("reads.jsonl", "sim.log")
# The directories of the security monitoring's dated files, whose paths below
# them the export keeps.
DATED = ("alerts", "archives")
# The further file of an adversarial episode's record.
ATTACK_LOG = "attack-chain.log"


def directory_name(start: datetime, episode_seed_hex: str) -> str:
    """The episode's start in Coordinated Universal Time (UTC) and the first 8
    hexadecimal digits of the episode seed, such as 20261104T013005Z-00010203."""
    return f"{start.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}-{episode_seed_hex[:8]}"


def utc_text(at: datetime) -> str:
    """A time of the episode record, in UTC to the microsecond, such as
    2026-11-04T01:30:05.250000Z."""
    return at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class EpisodeRecord:
    """The directory of one episode record."""

    episode_file = "episode.json"
    # The directory of the exported files, relative to the record's.
    export_dir = ""

    def __init__(self, root: Path, start: datetime, episode_seed_hex: str) -> None:
        self.path = Path(root) / directory_name(start, episode_seed_hex)
        self.path.mkdir(parents=True, exist_ok=False)
        self._episode: dict[str, Any] = {}

    def set(self, **fields: Any) -> None:
        """Add fields to the episode's file, which is rewritten whole each
        time."""
        self._episode.update(fields)
        self._write_atomically(
            self.episode_file, json.dumps(self._episode, indent=2) + "\n"
        )

    def append_step(self, line: dict[str, Any]) -> None:
        """Add one step's line to steps.jsonl."""
        with open(self.path / "steps.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(line) + "\n")

    def write_startup(self, text: str) -> None:
        """Keep the start-up input in the record, as startup.json (episodes.md
        §4.3, start-up input)."""
        self._write_atomically("startup.json", text)

    def attack_log_path(self) -> Path:
        """The file that receives the attack chain's standard error."""
        return self.path / ATTACK_LOG

    def exported_path(self, name: str) -> Path:
        """The path to which the export copies a file from a RavensPort node:
        an exported file by its name, or a dated file by its path below
        /var/ossec/logs, such as alerts/2026/Nov/ossec-alerts-04.json."""
        parts = name.split("/")
        dated = (
            len(parts) > 1
            and parts[0] in DATED
            and all(part not in ("", ".", "..") for part in parts)
        )
        if name not in EXPORTED and not dated:
            raise ValueError(f"{name} is no exported file of the episode record")
        path = self.path / self.export_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _write_atomically(self, name: str, text: str) -> None:
        fd, tmp = tempfile.mkstemp(dir=self.path, prefix=f".{name}.")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, self.path / name)


class CopyRecord(EpisodeRecord):
    """The record of an episode in a fork of a living port, in the directory
    that the command line names (living-port contract): record.json for the
    episode's fields, steps.jsonl, the living port's startup.json, the attack
    chain's log, and the export in export/. The runner's log, runner.log,
    lies beside them."""

    episode_file = "record.json"
    export_dir = "export"

    def __init__(self, out: Path) -> None:
        self.path = Path(out)
        self.path.mkdir(parents=True, exist_ok=True)
        if (self.path / self.episode_file).exists():
            raise FileExistsError(f"{self.path / self.episode_file} exists")
        (self.path / self.export_dir).mkdir(exist_ok=True)
        self._episode = {}
