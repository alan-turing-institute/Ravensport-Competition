#!/usr/bin/env python3
"""Offline replay of exported episodes through a Track A defender, for
development alone. Scoring runs every entry live in its episodes, and never
against a recording (episodes.md §3.2, modes).

    replay.py --entry DIR [--training] [--volume DIR] [--jobs N] [--out FILE]
              EPISODE...

Each EPISODE is an episode record: the episode's file, record.json in a fork
or episode.json at fork 0, steps.jsonl, and the exported dated files of the
security monitoring (episodes.md §13, episode record). The exported files lie
in export/ in a fork and beside the episode's file at fork 0. The replay takes
either the dated files below alerts/ and archives/, as the export writes them
(episode-runner-impl.md §10, export), or the single files alerts.json and
archives.json, compressed or not, of the exports made before the query
interface existed.

The replay runs the defender's program, `defender` in the entry's storage
volume, once for each episode, and sends it the decision requests of the
episode's steps. Before each decision request, the replay copies into a
directory of its own the records that the Wazuh manager had written by the
time of the request, and the defender's reads run the query interface's own
program against that directory at that time (replay_siem_read.py). The
defender finds the program through the environment variable SIEM_READ. A
record's `timestamp` gives the time at which the manager processed it, on the
clock of siem-01, which the step times share. The manager writes its records
out about once a second (episode-runner-impl.md §10, export), so the replay
serves a record only once WRITE_DELAY_S has passed since its timestamp.

A read with a start before step 0 reads the security monitoring's history
(episodes.md §8, query interface), and the replay serves the history that the
export holds: the dated files of the days from step 0's, each whole, and each
earlier dated file that a recorded read touched. A read whose start lies on a
day whose dated file the export lacks therefore starts at the first record of
the next day that the export holds, and omits the records of the days that
the export lacks.

The replay is sequential. The defender reads a decision request at the later
of the request's time and its answer to the one before, and the wall-clock
time that it takes to answer passes on the episode's clock. A defender that
is late answers the newest decision request waiting, as protocol.py does. A
decision counts for the step during which it arrives, and only the first
decision of a step counts (episodes.md §7, step protocol). Track A's
decisions are no action and the alarm. Any other line, `{"decision":
"continue"}` among them, is an invalid line, which the replay counts and
drops (episode-runner-impl.md §6, step messages).

The step messages follow the mode. In evaluation mode no step message carries
a reward: `previous_reward` is null in every decision request and in the end
notice. In training mode each decision request after step 0 carries the
previous step's reward, 0 in Track A, and the end notice carries R_n.

An episode record whose outcome scoring omits is reported unscored, and no
defender runs for it: a discarded episode, such as an adversarial episode
whose chain's binary the adversary host lacks, which ends before step 0; a
fork that admits no chain; and an interrupted episode (episodes.md §12,
terminated and discarded episodes).

The defender's alarm ends the episode. k_n is the number of stages released
before the alarm's arrival, and R_n comes from the episode runner's own
scoring (competition/episode_runner/scoring.py). An episode without an alarm
ends at the instant at which the episode runner would release the attack
chain's last stage (episodes.md §11, horizon and termination), at a simulator
stop that the record shows, or at the horizon. An export made before the
Track A alarm existed holds the records of the last stage, which the replay
never serves, since the episode ends before them.

In evaluation mode the replay assembles a new entry volume for each episode,
read-only, from the baseline's directory and protocol.py. In training mode
(--training) the volume is read-write, and with --volume the episodes run one
after another on one volume, as the episodes of a training lineage do.

The replay checks each read in the record of reads against the entry's
telemetry specification, telemetry.json (Competition Rule 3), and reports
each read that it does not declare.
"""

from __future__ import annotations

import argparse
import calendar
import concurrent.futures
import gzip
import json
import math
import os
import re
import shutil
import sys
import tempfile
import time
from bisect import bisect_left, bisect_right
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPOSITORY = HERE.parents[3]
if str(REPOSITORY) not in sys.path:
    sys.path.insert(0, str(REPOSITORY))
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import protocol  # noqa: E402
import replay_siem_read  # noqa: E402

from competition.episode_runner import scoring  # noqa: E402
from competition.episode_runner.steps import (  # noqa: E402
    ProcessSession,
    decision_of,
    decision_request,
    end_notice,
)

STEP_S = 5.0
TRACK = "A"
# Track A's decisions (episode-runner-impl.md §6, step messages).
DECISIONS = frozenset({protocol.NO_ACTION, protocol.ALARM})
# The wall-clock time that the replay waits for one decision message.
ANSWER_TIMEOUT_S = 60.0
# The time that the defender's program has to exit after the end notice.
EXIT_TIMEOUT_S = 10.0
# The longest time from a record's timestamp to its write into the manager's
# file: the manager writes its buffer out about once a second.
WRITE_DELAY_S = 1.0
OUTPUTS = {
    "alerts": ("alerts", "alerts.json", "ossec-alerts-"),
    "archives": ("archives", "archives.json", "ossec-archive-"),
}
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")  # fmt: skip
# The files of an entry's storage volume: the baseline's own files that its
# defender reads as it runs, of which REQUIRED must exist, and the shared
# module, which the volume holds beside them.
VOLUME_FILES = ("defender", "telemetry.json", "model.json")
REQUIRED = ("defender", "telemetry.json")
SHARED = ("protocol.py",)
TIMESTAMP = re.compile(rb'"timestamp"\s*:\s*"([^"]*)"')
# The outcomes of an episode that scoring omits (episodes.md §12, terminated
# and discarded episodes): a discard, such as of an adversarial episode whose
# chain's binary the adversary host lacks, which ends before step 0; a fork
# that admits no chain; and an episode that its operator interrupted. The
# replay scores none of them, and runs no defender for them.
UNSCORED = ("discarded", "not admitted", "interrupted")
# The end of a recorded episode as the replay continues it, by the end cause
# of its record (episode-runner-impl.md §11, Track A's fields in the record).
# A recording that its own defender's alarm ended holds nothing after the
# alarm, so a replay that reaches that end without an alarm has no outcome.
RECORDED_CAUSES = {
    "horizon": None,
    "last_stage": "last_stage",
    "terminal_state": "simulator_stop",
    "simulator_stop": "simulator_stop",
    "alarm": "recorded_alarm",
}


def record_time(text: str) -> float:
    """A time of the episode record, such as 2026-11-12T16:55:44.230162Z."""
    return datetime.fromisoformat(text).timestamp()


_SECONDS: dict[str, int] = {}


def manager_time(line: bytes) -> float | None:
    """The time at which the Wazuh manager wrote a record: the record's first
    `timestamp`, such as 2026-11-12T16:55:45.636+0000, on the clock of
    siem-01."""
    found = TIMESTAMP.search(line)
    if found is None:
        return None
    text = found.group(1).decode("ascii", "replace")
    second = _SECONDS.get(text[:19])
    if second is None:
        try:
            second = calendar.timegm(time.strptime(text[:19], "%Y-%m-%dT%H:%M:%S"))
        except ValueError:
            return None
        _SECONDS[text[:19]] = second
    rest = text[19:]
    k = 1 if rest.startswith(".") else 0
    while k < len(rest) and rest[k].isdigit():
        k += 1
    fraction = float(rest[:k]) if k > 1 else 0.0
    zone = rest[k:].replace(":", "")
    offset = 0
    if zone and zone not in ("Z", "+0000", "-0000"):
        sign = -1 if zone[0] == "-" else 1
        offset = sign * (int(zone[1:3]) * 3600 + int(zone[3:5]) * 60)
    return second + fraction - offset


@dataclass
class StepTimes:
    start: float
    end: float
    request: float


@dataclass
class Episode:
    """What the replay needs of one episode record."""

    path: Path
    name: str
    kind: str
    chain: str | None
    K: int | None
    releases: list[float]
    steps: list[StepTimes]
    # The time at which the recorded episode ended: the end of its last step.
    recorded_end: float
    # Why the recorded episode ended early, or None at the horizon.
    recorded_cause: str | None
    # The time at which the query interface recorded where the reads start.
    reads_start: float
    # The dated files of each output file, in order of day: each file's path
    # below /var/ossec/logs and its path in the export.
    sources: dict[str, list[tuple[str, Path]]] = field(default_factory=dict)

    @property
    def t0(self) -> float:
        return self.steps[0].start

    @property
    def last_stage_due(self) -> float | None:
        """The instant at which the episode runner would release the last
        stage, when the record shows it: the release of stage K in an export
        made before the Track A alarm existed."""
        if self.K is not None and len(self.releases) >= self.K:
            return self.releases[self.K - 1]
        return None

    @property
    def end(self) -> float:
        due = self.last_stage_due
        return self.recorded_end if due is None else min(due, self.recorded_end)

    def step_at(self, t: float) -> int:
        """The step during which time t lies."""
        return max(0, bisect_right([s.start for s in self.steps], t) - 1)

    def steps_before(self, t: float) -> int:
        """The number of steps that began before time t."""
        return bisect_left([s.start for s in self.steps], t)


def _dated_name(output: str, day: datetime) -> str:
    directory, _, prefix = OUTPUTS[output]
    return f"{directory}/{day.year}/{MONTHS[day.month - 1]}/{prefix}{day.day:02d}.json"


class Unscored(Exception):
    """An episode record whose outcome scoring omits."""

    def __init__(self, path: Path, record: dict[str, Any]) -> None:
        super().__init__(f"{path.name}: {record.get('outcome')}: {record.get('cause')}")
        chain = ((record.get("attack") or {}).get("chain")) or {}
        self.result = {
            "episode": path.name,
            "type": record.get("type", "benign"),
            "chain": chain.get("id"),
            "K": len(chain.get("stages", [])) if chain else None,
            "alarm": None,
            "k_n": None,
            "R_n": None,
            "outcome": None,
            "end_cause": record.get("outcome"),
            "cause": record.get("cause"),
            "undeclared_reads": [],
            "defender_exit_status": None,
            "problem": None,
        }


def load_episode(path: Path) -> Episode:
    """The episode record at path. Raises Unscored for an episode whose
    outcome scoring omits, such as a discarded one."""
    path = Path(path)
    for name in ("record.json", "episode.json"):
        if (path / name).is_file():
            record = json.loads((path / name).read_text())
            break
    else:
        raise FileNotFoundError(f"{path} holds neither record.json nor episode.json")
    if record.get("outcome") in UNSCORED:
        raise Unscored(path, record)
    lines = [
        json.loads(x) for x in (path / "steps.jsonl").read_text().splitlines() if x
    ]
    if not lines:
        raise ValueError(f"{path} records no step")
    steps = []
    for line in lines:
        start, end = record_time(line["start"]), record_time(line["end"])
        sent = line.get("request_sent")
        steps.append(StepTimes(start, end, record_time(sent) if sent else start))
    attack = record.get("attack") or {}
    chain = attack.get("chain") or {}
    session = attack.get("session") or {}
    kind = record.get("type", "benign")
    K = len(chain.get("stages", [])) if kind == "adversarial" and chain else None
    releases = sorted(record_time(t) for t in session.get("releases", []))
    track_a = record.get("track_a") or {}
    run = record.get("steps") or {}
    cause = RECORDED_CAUSES.get(track_a.get("end_cause"))
    if cause is None and (
        record.get("outcome") == "terminated" or run.get("ended_early")
    ):
        cause = "simulator_stop"
    started = (record.get("query_interface_start") or {}).get("sent")
    export = path / "export" if (path / "export").is_dir() else path
    day = datetime.fromtimestamp(steps[0].start, UTC)
    sources = {}
    for output, (directory, current, prefix) in OUTPUTS.items():
        found = []
        if (export / directory).is_dir():
            # Each day's dated file once, as siem-read opens it: the file as
            # written where it is present, and its compressed file otherwise.
            by_name: dict[str, tuple[tuple[int, int, int], Path]] = {}
            for file in sorted((export / directory).rglob(f"{prefix}*.json*")):
                name = file.relative_to(export).as_posix().removesuffix(".gz")
                year, month, base = name.split("/")[1:4]
                day_key = (int(year), MONTHS.index(month), int(base[len(prefix) : -5]))
                if name not in by_name or not file.name.endswith(".gz"):
                    by_name[name] = (day_key, file)
            found = [
                (name, file)
                for name, (_, file) in sorted(by_name.items(), key=lambda i: i[1][0])
            ]
        else:
            for candidate in (export / current, export / f"{current}.gz"):
                if candidate.is_file():
                    found = [(_dated_name(output, day), candidate)]
                    break
        if not found:
            raise FileNotFoundError(f"{export} holds no exported {current}")
        sources[output] = found
    return Episode(
        path=path,
        name=path.name,
        kind=kind,
        chain=chain.get("id"),
        K=K,
        releases=releases,
        steps=steps,
        recorded_end=steps[-1].end,
        recorded_cause=cause,
        reads_start=record_time(started) if started else steps[0].request,
        sources=sources,
    )


def _lines(path: Path) -> Iterator[bytes]:
    opener = gzip.open if path.name.endswith(".gz") else open
    with opener(path, "rb") as f:
        yield from f


class Feeder:
    """The records of one output file, written into the replay's copy of the
    Wazuh manager's logs in the order of the exported files, up to a time."""

    def __init__(self, output: str, sources: list[tuple[str, Path]], logs: Path):
        self.output = output
        self.logs = logs
        directory, current, _ = OUTPUTS[output]
        self.current = logs / directory / current
        self._records = self._read(sources)
        self._pending: tuple[str, bytes, float] | None = None
        self._name: str | None = None
        self._file = None
        self._open(sources[0][0])

    def _read(
        self, sources: list[tuple[str, Path]]
    ) -> Iterator[tuple[str, bytes, float]]:
        last = -math.inf
        for name, path in sources:
            for line in _lines(path):
                if not line.endswith(b"\n"):
                    continue
                t = manager_time(line)
                last = last if t is None else t
                yield name, line, last

    def _open(self, name: str) -> None:
        if self._file is not None:
            self._file.close()
        path = self.logs / name
        path.parent.mkdir(parents=True, exist_ok=True)
        # The file stays open across calls of advance, and close closes it.
        self._file = open(path, "ab")  # noqa: SIM115
        self._name = name
        if self.current.exists():
            self.current.unlink()
        os.link(path, self.current)

    def advance(self, until: float) -> int:
        """Write every record that the manager had written by `until`: the
        records in file order up to the first written later. Returns the
        number of records written."""
        written = 0
        while True:
            if self._pending is None:
                self._pending = next(self._records, None)
                if self._pending is None:
                    break
            name, line, t = self._pending
            if t > until:
                break
            if name != self._name:
                self._open(name)
            self._file.write(line)
            self._pending = None
            written += 1
        self._file.flush()
        return written

    def close(self) -> None:
        if self._file is not None:
            self._file.close()


def _timed_lines(sources: list[tuple[str, Path]]) -> Iterator[tuple[bytes, float]]:
    last = -math.inf
    for _, path in sources:
        for line in _lines(path):
            if line.endswith(b"\n"):
                t = manager_time(line)
                last = last if t is None else t
                yield line, last


def _matches(record: dict[str, Any], host: str | None, location: str | None) -> bool:
    agent = record.get("agent")
    if host is not None and (not isinstance(agent, dict) or agent.get("name") != host):
        return False
    return location is None or record.get("location") == location


def timed_reads(
    sources: dict[str, list[tuple[str, Path]]],
    start: float,
    times: Iterable[float],
    requests: Sequence[tuple[str, str | None, str | None]],
    write_delay_s: float = WRITE_DELAY_S,
) -> Iterator[list[list[dict[str, Any]]]]:
    """The records that each request, an output file with an optional host
    and input, reads at each of the given times, in increasing order, when
    the reads start at `start`: for each time, a list of each request's
    records, as the replay's query interface serves them to a defender that
    reads at once. Offline training and development read through this
    function in one process, without the defender's program."""
    start -= write_delay_s
    times = (t - write_delay_s for t in times)
    outputs = {output for output, _, _ in requests}
    streams = {o: _timed_lines(sources[o]) for o in outputs}
    pending: dict[str, tuple[bytes, float] | None] = {o: None for o in outputs}

    def until(output: str, t: float) -> list[bytes]:
        taken = []
        while True:
            if pending[output] is None:
                pending[output] = next(streams[output], None)
                if pending[output] is None:
                    return taken
            line, at = pending[output]
            if at > t:
                return taken
            taken.append(line)
            pending[output] = None

    for output in outputs:
        until(output, start)
    for t in times:
        parsed: dict[str, list[dict[str, Any]]] = {}
        for output in outputs:
            records = []
            for line in until(output, t):
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if isinstance(record, dict):
                    records.append(record)
            parsed[output] = records
        yield [
            [r for r in parsed[output] if _matches(r, host, location)]
            for output, host, location in requests
        ]


def step_reads(
    episode: Episode,
    requests: Sequence[tuple[str, str | None, str | None]],
    write_delay_s: float = WRITE_DELAY_S,
) -> Iterator[tuple[int, float, list[list[dict[str, Any]]]]]:
    """For each step of the episode until its end, the step's number, its
    request's time, and the records that each request reads then."""
    steps = [
        (k, s.request) for k, s in enumerate(episode.steps) if s.request < episode.end
    ]
    reads = timed_reads(
        episode.sources,
        episode.reads_start,
        [t for _, t in steps],
        requests,
        write_delay_s,
    )
    for (k, t), records in zip(steps, reads):
        yield k, t, records


def assemble(baseline: Path, volume: Path, read_only: bool) -> Path:
    """The entry's storage volume for a baseline, as /var/lib/entry holds it:
    the baseline's files that its defender reads as it runs, VOLUME_FILES,
    with the shared module beside them. The defender's mode is 0755 and every
    other file's 0644, which the opening of the volume to siem-01's
    unprivileged container keeps executable and readable. A read-only volume
    takes the same modes without write permission. Anything else in the
    baseline's directory, such as its documentation or the files of an
    image, stays out."""
    missing = [n for n in REQUIRED if not (baseline / n).is_file()]
    if missing:
        raise FileNotFoundError(f"{baseline} lacks {', '.join(missing)}")
    volume.mkdir(parents=True, exist_ok=True)
    for name in VOLUME_FILES:
        if (baseline / name).is_file():
            shutil.copyfile(baseline / name, volume / name)
    for name in SHARED:
        shutil.copyfile(HERE / name, volume / name)
    for item in volume.iterdir():
        mode = 0o755 if item.name == "defender" else 0o644
        item.chmod(mode & ~0o222 if read_only else mode)
    volume.chmod(0o555 if read_only else 0o755)
    return volume


def make_writable(path: Path) -> None:
    for root, dirs, files in os.walk(path):
        os.chmod(root, 0o755)
        for name in files:
            os.chmod(os.path.join(root, name), 0o644)


def _launcher(work: Path, logs: Path, state: Path, now: Path) -> Path:
    """The program that the defender runs as siem-read."""
    path = work / "siem-read"
    path.write_text(
        "#!/bin/sh\n"
        f'exec "{sys.executable}" -I "{HERE / "replay_siem_read.py"}" '
        f'--logs "{logs}" --state "{state}" --now-file "{now}" -- "$@"\n'
    )
    path.chmod(0o755)
    return path


def _set_now(now: Path, t: float) -> None:
    temporary = now.with_name(now.name + ".new")
    temporary.write_text(repr(t))
    os.replace(temporary, now)


def _undeclared_reads(volume: Path, reads: Path) -> list[dict[str, Any]]:
    try:
        telemetry = protocol.Telemetry.load(str(volume / "telemetry.json"))
    except (OSError, ValueError, KeyError) as error:
        return [{"telemetry": f"cannot be read: {error}"}]
    found = []
    if reads.is_file():
        for line in reads.read_text().splitlines():
            request = json.loads(line)["request"]
            if not telemetry.allows(
                request["output"], request["host"], request["input"]
            ):
                found.append(request)
    return found


def replay(
    episode: Episode,
    volume: Path,
    work: Path,
    *,
    training: bool = False,
    answer_timeout_s: float = ANSWER_TIMEOUT_S,
    write_delay_s: float = WRITE_DELAY_S,
) -> dict[str, Any]:
    """Replay one episode through the defender in the entry volume, with the
    replay's files in the empty directory `work`, in training mode or in
    evaluation mode, whose step messages carry no reward. Returns the
    result."""
    logs, state, home = work / "logs", work / "state", work / "root"
    for directory in (logs, state, home):
        directory.mkdir(parents=True)
    now = work / "now"
    _set_now(now, episode.reads_start)
    siem_read = replay_siem_read.load_siem_read()
    feeders = [Feeder(o, s, logs) for o, s in episode.sources.items()]
    stderr_path = work / "defender.stderr"
    argv = [
        "sh", "-c", 'cd "$1" && shift && exec "$@" 2>>"$0"', str(stderr_path),
        str(home), "env", "-i", f"PATH={os.environ.get('PATH', '/usr/bin:/bin')}",
        f"HOME={home}", "LANG=C.UTF-8",
        f"SIEM_READ={_launcher(work, logs, state, now)}",
        sys.executable, str(volume / "defender"),
    ]  # fmt: skip
    session = ProcessSession(argv)
    result: dict[str, Any] = {
        "episode": episode.name,
        "type": episode.kind,
        "chain": episode.chain,
        "K": episode.K,
        "releases_s": [round(r - episode.t0, 3) for r in episode.releases],
    }
    latencies: list[float] = []
    decisions: dict[int, tuple[str, float]] = {}
    dropped = invalid = skipped = 0
    alarm: tuple[int, float] | None = None
    problem = None
    try:
        for feeder in feeders:
            feeder.advance(episode.reads_start - write_delay_s)
        # The episode runner gives step 0's instant with --step-0, so that
        # each read's step counts from step 0's boundary.
        siem_read.start(logs, state, STEP_S, episode.reads_start, step_0=episode.t0)
        end = episode.end
        k, previous = 0, -math.inf
        while k < len(episode.steps) and alarm is None:
            read_at = max(episode.steps[k].request, previous)
            if episode.steps[k].start >= end or read_at >= end:
                break
            while (
                k + 1 < len(episode.steps)
                and episode.steps[k + 1].request <= read_at
                and episode.steps[k + 1].start < end
            ):
                k += 1
                skipped += 1
            for feeder in feeders:
                feeder.advance(read_at - write_delay_s)
            _set_now(now, read_at)
            # Track A's reward is 0 at every step before the last, and only
            # training mode returns it.
            reward = 0.0 if training and k > 0 else None
            session.send(decision_request(TRACK, k, reward))
            began = time.monotonic()
            line = session.receive(answer_timeout_s)
            latency = time.monotonic() - began
            if line is None:
                problem = (
                    f"no decision message within {answer_timeout_s:g} s of step {k}"
                )
                break
            latencies.append(latency)
            arrival = read_at + latency
            previous = arrival
            if arrival >= end:
                break
            decision = decision_of(line)
            if decision not in DECISIONS:
                invalid += 1
                k += 1
                continue
            index = episode.step_at(arrival)
            if index in decisions:
                dropped += 1
            else:
                decisions[index] = (decision, arrival)
                if decision == protocol.ALARM:
                    alarm = (index, arrival)
            k += 1
    except (EOFError, BrokenPipeError):
        problem = "the defender's program ended before the episode"
    finally:
        for feeder in feeders:
            feeder.close()
    if alarm is not None:
        last, ended_at, cause = alarm[0], alarm[1], "alarm"
    else:
        ended_at = episode.end
        last = episode.steps_before(ended_at) - 1
        if episode.last_stage_due is not None and ended_at == episode.last_stage_due:
            cause = "last_stage"
        else:
            cause = episode.recorded_cause or "horizon"
    released = None
    if alarm is not None and episode.K is not None:
        released = sum(1 for r in episode.releases if r < alarm[1])
    if cause == "recorded_alarm":
        scored = None
    else:
        scored = scoring.track_a(episode.K, alarm is not None, released)
    try:
        reward = scored.R_n if training and scored is not None else None
        session.send(end_notice(TRACK, last + 1, reward))
    except BrokenPipeError:
        pass
    exit_status = session.close(timeout_s=EXIT_TIMEOUT_S)
    stderr = stderr_path.read_text(errors="replace") if stderr_path.exists() else ""
    reads = 0
    if (state / "reads.jsonl").exists():
        with open(state / "reads.jsonl") as f:
            reads = sum(1 for _ in f)
    result.update(
        {
            "alarm": (
                None
                if alarm is None
                else {"step": alarm[0], "at_s": round(alarm[1] - episode.t0, 3)}
            ),
            "k_n": None if scored is None else scored.k_n,
            "R_n": None if scored is None else scored.R_n,
            "outcome": None if scored is None else scored.outcome,
            "end_cause": cause,
            "steps_run": last + 1,
            "ended_s": round(ended_at - episode.t0, 3),
            "decisions": len(decisions),
            "dropped_decision_messages": dropped,
            "invalid_lines": invalid,
            "skipped_requests": skipped,
            "latency_s": {
                "mean": (
                    round(sum(latencies) / len(latencies), 4) if latencies else None
                ),
                "max": round(max(latencies), 4) if latencies else None,
            },
            "reads": reads,
            "undeclared_reads": _undeclared_reads(volume, state / "reads.jsonl"),
            "defender_exit_status": exit_status,
            "problem": problem,
            "stderr": stderr[-2000:],
        }
    )
    return result


def replay_fresh(
    baseline: str,
    episode_path: str,
    training: bool,
    scratch: str | None,
    write_delay_s: float = WRITE_DELAY_S,
) -> dict[str, Any]:
    """Replay one episode on a new entry volume assembled from the baseline,
    in a working directory that the replay then deletes. An episode that
    scoring omits gives its unscored result, and runs no defender."""
    try:
        episode = load_episode(Path(episode_path))
    except Unscored as unscored:
        return unscored.result
    work = Path(tempfile.mkdtemp(prefix="replay-", dir=scratch))
    try:
        volume = assemble(Path(baseline), work / "entry", read_only=not training)
        return replay(
            episode,
            volume,
            work / "run",
            training=training,
            write_delay_s=write_delay_s,
        )
    finally:
        make_writable(work)
        shutil.rmtree(work, ignore_errors=True)


def replay_lineage(
    volume: str,
    episode_paths: list[str],
    scratch: str | None,
    write_delay_s: float = WRITE_DELAY_S,
) -> list[dict[str, Any]]:
    """Replay episodes one after another on one read-write entry volume, as a
    training lineage runs them. An episode that scoring omits gives its
    unscored result, and leaves the volume as it was."""
    results = []
    for episode_path in episode_paths:
        try:
            episode = load_episode(Path(episode_path))
        except Unscored as unscored:
            results.append(unscored.result)
            continue
        work = Path(tempfile.mkdtemp(prefix="replay-", dir=scratch))
        try:
            results.append(
                replay(
                    episode,
                    Path(volume),
                    work,
                    training=True,
                    write_delay_s=write_delay_s,
                )
            )
        finally:
            shutil.rmtree(work, ignore_errors=True)
    return results


def summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Mean R_n by episode type and the false-alarm rate of benign episodes."""
    scored = [r for r in results if r.get("R_n") is not None]
    benign = [r for r in scored if r["type"] == "benign"]
    adversarial = [r for r in scored if r["type"] == "adversarial"]

    def mean(values: list[float]) -> float | None:
        return round(sum(values) / len(values), 4) if values else None

    return {
        "episodes": len(results),
        "unscored": len(results) - len(scored),
        "benign": len(benign),
        "false_alarms": sum(1 for r in benign if r["alarm"] is not None),
        "false_alarm_rate": mean([1.0 if r["alarm"] else 0.0 for r in benign]),
        "benign_mean_R_n": mean([r["R_n"] for r in benign]),
        "adversarial": len(adversarial),
        "adversarial_mean_R_n": mean([r["R_n"] for r in adversarial]),
        "outcomes": {
            str(o): sum(1 for r in adversarial if r["outcome"] == o)
            for o in (1, 2, 3, 4)
        },
        "undeclared_reads": sum(len(r["undeclared_reads"]) for r in results),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="replay.py",
        description="Replay exported episodes through a Track A defender, "
        "for development alone.",
    )
    parser.add_argument("episodes", nargs="+", help="episode record directories")
    parser.add_argument("--entry", help="the baseline's directory, holding `defender`")
    parser.add_argument(
        "--training",
        action="store_true",
        help="mount the entry volume read-write, as training mode does",
    )
    parser.add_argument(
        "--volume",
        help="with --training, an assembled entry volume that the episodes "
        "share, one after another, as a training lineage",
    )
    parser.add_argument("--jobs", type=int, default=1, help="episodes run at once")
    parser.add_argument("--scratch", help="directory for the working files")
    parser.add_argument("--out", help="file for one JSON line per episode")
    parser.add_argument(
        "--write-delay",
        type=float,
        default=WRITE_DELAY_S,
        help="seconds from a record's timestamp to its write into the file "
        f"(default {WRITE_DELAY_S:g})",
    )
    args = parser.parse_args(argv)
    if args.volume and not args.training:
        parser.error("--volume goes with --training")
    if not args.entry and not (args.volume and Path(args.volume).is_dir()):
        parser.error("give --entry, or --training with an assembled --volume")
    delay = args.write_delay
    if args.volume:
        if not Path(args.volume).is_dir():
            assemble(Path(args.entry), Path(args.volume), read_only=False)
        results = replay_lineage(args.volume, args.episodes, args.scratch, delay)
    elif args.jobs > 1:
        with concurrent.futures.ProcessPoolExecutor(args.jobs) as pool:
            futures = [
                pool.submit(
                    replay_fresh, args.entry, e, args.training, args.scratch, delay
                )
                for e in args.episodes
            ]
            results = [f.result() for f in futures]
    else:
        results = [
            replay_fresh(args.entry, e, args.training, args.scratch, delay)
            for e in args.episodes
        ]
    lines = [json.dumps(result) + "\n" for result in results]
    if args.out:
        with open(args.out, "w") as out:
            out.writelines(lines)
    else:
        sys.stdout.writelines(lines)
    print(json.dumps(summary(results), indent=2), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
