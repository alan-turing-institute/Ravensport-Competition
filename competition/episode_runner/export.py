"""The export (episodes.md §3.1, phases; §13, episode record;
episode-runner-impl.md §10, export).

The episode runner copies the evidence from the RavensPort nodes into the
episode record through Incus. Each copied file keeps the timestamps of its own
host. A file that cannot be copied fails the export, which discards the episode
(episodes.md §12, terminated and discarded episodes).

The manager of the security monitoring writes each day's alerts and archives
into a dated file, such as alerts/2026/Nov/ossec-alerts-04.json below
/var/ossec/logs, links alerts.json and archives.json to the current day's
files, and compresses the files of earlier days (siem.md §7, retention). The
manager names each file by its day in Coordinated Universal Time (UTC),
siem-01's time zone. The export copies the dated files of each day from step 0
to the export, each whole from its first byte, under its path below
/var/ossec/logs, so that the byte ranges in the query interface's record of
reads refer to the exported files (episode-runner-impl.md §12, query
interface). A file that the manager has compressed stays compressed; while the
manager is compressing a day's file, the file as written is copied. In a fork
of a living port, siem-01 holds the dated files of every day since the living
port's start, of which the export copies the episode's days and each earlier
dated file that a read touched: a read with a start before step 0 reads the
security monitoring's history (episodes.md §8, query interface). A touched
file that siem-01 no longer holds fails the export. The export copies the
record of reads before the dated files, so that each byte range of a read lies
within the files copied after it, and then the simulator's log.

The manager writes its logs through a buffer of the C library, and writes the
buffer out to the files about once a second. Copying the logs at once after the
end notice could miss the lines of the manager's last second. The export
therefore waits two seconds before it copies the manager's logs, so that every
line written before the end notice is in the files. A copied log can still end
inside a line that the manager is writing. That line belongs to an event
written after the end notice, and the export leaves it out of the file as
written.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Sequence
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol

from competition.episode_runner.incus import Result
from competition.episode_runner.record import EpisodeRecord
from competition.episode_runner.settle import SIEM, SIM_HOST, SIM_LOG

FLUSH_WAIT_S = 2.0

log = logging.getLogger(__name__)

LOGS = "/var/ossec/logs"
# The directory of each output file's dated files below LOGS, and the prefix
# of their names.
MANAGER_LOGS = (
    ("alerts", "ossec-alerts-"),
    ("archives", "ossec-archive-"),
)
# The query interface's record of reads (episode-runner-impl.md §12).
READS = "/var/lib/siem-read/reads.jsonl"
# The manager's directory of each month, such as 2026/Nov, named by the month's
# abbreviation in English whatever the locale.
MONTHS = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)
# A dated file as the record of reads names it, by its path below LOGS.
_DATED = re.compile(
    r"\A(?P<directory>alerts|archives)/\d{4}/(?:"
    + "|".join(MONTHS)
    + r")/(?P<prefix>ossec-alerts-|ossec-archive-)\d{2}\.json\Z"
)
# The arguments are the log directory and each dated file, below it and
# without its extension. The script lists the file as written where it exists,
# and its compressed file otherwise.
LIST = """
cd "$1" || exit 1
shift
for name in "$@"; do
    if [ -f "$name.json" ]; then
        echo "$name.json"
    elif [ -f "$name.json.gz" ]; then
        echo "$name.json.gz"
    fi
done
"""


def days_spanned(start: datetime, end: datetime) -> list[date]:
    """Each day in UTC from the day of `start` to the day of `end`, oldest
    first."""
    first = start.astimezone(timezone.utc).date()
    last = end.astimezone(timezone.utc).date()
    return [first + timedelta(days=n) for n in range((last - first).days + 1)]


def dated_files(prefix: str, days: Sequence[date]) -> list[str]:
    """The manager's dated file of each day, within its log directory and
    without its extension, such as 2026/Nov/ossec-alerts-04."""
    return [f"{d.year}/{MONTHS[d.month - 1]}/{prefix}{d.day:02d}" for d in days]


def touched_files(reads: Path) -> list[str]:
    """Each dated file that a read in the record of reads touched, within
    the manager's log directory and without its extension, such as
    archives/2026/Nov/ossec-archive-02, in the order first touched. A name
    that is no dated file of the manager's is left out, since siem-01 runs
    the entry's software."""
    try:
        text = reads.read_text()
    except (OSError, UnicodeDecodeError):
        return []
    prefixes = dict(MANAGER_LOGS)
    names: list[str] = []
    for line in text.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        ranges = entry.get("ranges") if isinstance(entry, dict) else None
        for r in ranges if isinstance(ranges, list) else []:
            name = r.get("file") if isinstance(r, dict) else None
            found = _DATED.match(name) if isinstance(name, str) else None
            if found is None or prefixes[found["directory"]] != found["prefix"]:
                log.warning("the record of reads names no dated file: %r", name)
                continue
            stem = name[: -len(".json")]
            if stem not in names:
                names.append(stem)
    return names


class Incus(Protocol):
    def exec(self, node: str, *argv: str, timeout_s: float = 60.0) -> Result: ...

    def pull(
        self, node: str, path: str, dest: Path, timeout_s: float = 300.0
    ) -> Result: ...


def _error(result: Result) -> str | None:
    return (
        None
        if result.ok
        else result.stderr.strip() or f"exit status {result.returncode}"
    )


def keep_whole_lines(path: Path) -> None:
    """Cut the file after its last newline, leaving out a line that the
    manager is still writing."""
    data = path.read_bytes()
    whole = data[: data.rfind(b"\n") + 1]
    if len(whole) < len(data):
        log.info(
            "left out the last %d bytes of %s, a line still being written",
            len(data) - len(whole),
            path.name,
        )
        path.write_bytes(whole)


def export(
    incus: Incus,
    record: EpisodeRecord,
    *,
    wait: Callable[[float], None],
    start: datetime,
    now: Callable[[], datetime],
) -> dict[str, str | None]:
    """Copy each exported file into the episode record, after waiting
    FLUSH_WAIT_S seconds through `wait`. The manager's dated files are those
    of the days from `start`, step 0, to the export, at `now()` once the wait
    is over, and each earlier dated file that a read touched. The result
    holds, for each file, the error from copying it, or None when the file
    was copied."""
    errors: dict[str, str | None] = {}
    wait(FLUSH_WAIT_S)
    # The record of reads first: a defender's process can outlive the end
    # notice and read again, and the dated files only grow, so every byte
    # range that the copied record holds lies within the files copied after
    # it, and every read replays (episodes.md §8, query interface).
    result = incus.pull(SIEM, READS, record.exported_path("reads.jsonl"))
    errors["reads.jsonl"] = _error(result)
    days = days_spanned(start, now())
    names = [
        f"{directory}/{name}"
        for directory, prefix in MANAGER_LOGS
        for name in dated_files(prefix, days)
    ]
    touched = (
        []
        if errors["reads.jsonl"]
        else touched_files(record.exported_path("reads.jsonl"))
    )
    earlier = [name for name in touched if name not in names]
    names += earlier
    listed = incus.exec(SIEM, "sh", "-c", LIST, "sh", LOGS, *names, timeout_s=60.0)
    if not listed.ok:
        errors["dated files"] = _error(listed)
    else:
        # Only the names asked for, since siem-01 runs the entry's software.
        expected = {f"{n}{suffix}" for n in names for suffix in (".json", ".json.gz")}
        found = [name for name in listed.stdout.split() if name in expected]
        for directory, _ in MANAGER_LOGS:
            if not any(name.startswith(f"{directory}/") for name in found):
                errors[directory] = f"no dated file from {days[0]} to {days[-1]}"
        # A read touched the file, so its replay needs it.
        for name in earlier:
            if f"{name}.json" not in found and f"{name}.json.gz" not in found:
                errors[f"{name}.json"] = (
                    "a read touched it, and siem-01 no longer holds it"
                )
        for name in found:
            dest = record.exported_path(name)
            result = incus.pull(SIEM, f"{LOGS}/{name}", dest, timeout_s=300.0)
            errors[name] = _error(result)
            if result.ok and name.endswith(".json"):
                keep_whole_lines(dest)
    result = incus.pull(SIM_HOST, SIM_LOG, record.exported_path("sim.log"))
    errors["sim.log"] = _error(result)
    return errors
