"""The query interface's program, siem-read (episodes.md §8, query interface;
episode-runner-impl.md §12, query interface; episodes-tests.md §6).

The tests lay out the Wazuh manager's dated files in a temporary directory,
with alerts.json and archives.json as second names, hard links, for the
current day's files, and run the program's main function against it.

Run with: pytest competition/tests -q
"""

from __future__ import annotations

import gzip
import importlib.machinery
import importlib.util
import io
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

SIEM_READ = Path(__file__).resolve().parents[1] / "siem_read" / "siem-read"
_loader = importlib.machinery.SourceFileLoader("siem_read_program", str(SIEM_READ))
_spec = importlib.util.spec_from_loader(_loader.name, _loader)
siem_read = importlib.util.module_from_spec(_spec)
_loader.exec_module(siem_read)

STEP_0 = 1_762_977_600.0  # 2025-11-12T20:00:00Z on the clock of siem-01


def record(host: str, location: str, n: int) -> bytes:
    """One record as the manager writes it, in compact JSON."""
    return (
        json.dumps(
            {
                "timestamp": f"t{n}",
                "agent": {"name": host},
                "location": location,
                "n": n,
            },
            separators=(",", ":"),
        ).encode()
        + b"\n"
    )


class Logs:
    """The manager's logs below a temporary /var/ossec/logs."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def dated(self, output: str, day: int) -> Path:
        directory, _, prefix = siem_read.OUTPUTS[output]
        return self.root / directory / "2025" / "Nov" / f"{prefix}{day:02d}.json"

    def current(self, output: str) -> Path:
        directory, current, _ = siem_read.OUTPUTS[output]
        return self.root / directory / current

    def begin_day(self, day: int) -> None:
        """The manager moves each output file's second name to the day's new
        dated file."""
        for output in siem_read.OUTPUTS:
            path = self.dated(output, day)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
            current = self.current(output)
            if current.exists():
                current.unlink()
            os.link(path, current)

    def write(self, output: str, data: bytes) -> None:
        with open(self.current(output), "ab") as f:
            f.write(data)

    def compress(self, output: str, day: int) -> None:
        """The manager's monitord compresses a previous day's dated file."""
        path = self.dated(output, day)
        Path(f"{path}.gz").write_bytes(gzip.compress(path.read_bytes()))
        path.unlink()


@pytest.fixture
def logs(tmp_path: Path) -> Logs:
    logs = Logs(tmp_path / "logs")
    logs.begin_day(12)
    return logs


def run(logs: Logs, state: Path, *argv: str, now: float = STEP_0) -> tuple[int, bytes]:
    out = io.BytesIO()
    status = siem_read.main(
        list(argv), logs=logs.root, state=state, now=lambda: now, stdout=out
    )
    return status, out.getvalue()


def reads(state: Path) -> list[dict]:
    return [
        json.loads(line) for line in (state / "reads.jsonl").read_text().splitlines()
    ]


FMS_AUTH = ("fms-01", "/var/log/auth.log")
FMS_AUDIT = ("fms-01", "/var/log/audit/audit.log")
HMI_AUTH = ("hmi-01", "/var/log/auth.log")


def test_siem_read_writes_the_matching_records_as_the_file_holds_them(
    logs: Logs, tmp_path: Path
) -> None:
    """episodes-tests.md, siem_read_writes_the_matching_records_as_the_file_holds_them."""
    state = tmp_path / "state"
    assert run(logs, state, "--start")[0] == 0
    written = [
        record(*FMS_AUTH, 1),
        record(*HMI_AUTH, 2),
        record(*FMS_AUDIT, 3),
        record(*FMS_AUTH, 4),
    ]
    # A record with keys in another order and spacing stays byte for byte.
    odd = b'{ "location": "/var/log/auth.log", "agent": {"name": "fms-01"}, "n": 5 }\n'
    logs.write("archives", b"".join(written) + odd)
    assert run(
        logs, state, "archives", "--host", "fms-01", "--input", "/var/log/auth.log"
    ) == (
        0,
        written[0] + written[3] + odd,
    )
    assert run(logs, state, "archives") == (0, b"".join(written) + odd)


def test_a_request_selects_by_file_host_and_input(logs: Logs, tmp_path: Path) -> None:
    """A request for one output file, one originating host and one input
    returns only the records that match all three: a record of another host,
    of another input or in the other output file stays out. A request that
    names fewer selects by those that it names (episodes-tests.md,
    a_request_selects_by_file_host_and_input; episodes.md §8, query
    interface)."""
    state = tmp_path / "state"
    assert run(logs, state, "--start")[0] == 0
    archives = [
        record(*FMS_AUTH, 1),
        record(*HMI_AUTH, 2),
        record(*FMS_AUDIT, 3),
        record(*FMS_AUTH, 4),
    ]
    alerts = [record(*HMI_AUTH, 5), record(*FMS_AUTH, 6), record(*FMS_AUDIT, 7)]
    logs.write("archives", b"".join(archives))
    logs.write("alerts", b"".join(alerts))
    host, given = ("--host", "fms-01"), ("--input", "/var/log/auth.log")
    assert run(logs, state, "archives", *host, *given) == (
        0,
        archives[0] + archives[3],
    )
    assert run(logs, state, "alerts", *host, *given) == (0, alerts[1])
    assert run(logs, state, "archives", *host) == (
        0,
        archives[0] + archives[2] + archives[3],
    )
    assert run(logs, state, "archives", *given) == (
        0,
        archives[0] + archives[1] + archives[3],
    )
    assert run(logs, state, "alerts", "--host", "hmi-01", *given) == (0, alerts[0])
    assert run(logs, state, "alerts", "--host", "hmi-01", "--input", FMS_AUDIT[1]) == (
        0,
        b"",
    )


@pytest.mark.parametrize(
    "argv",
    [
        ["alerts.log"],
        ["archive"],
        [],
        ["alerts", "--since", "0"],
        ["alerts", "--step-s", "5"],
        ["--start", "alerts"],
        ["--start", "--step-s", "0"],
    ],
)
def test_a_request_the_program_cannot_serve_exits_with_status_2(
    logs: Logs, tmp_path: Path, argv: list[str], capsys
) -> None:
    state = tmp_path / "state"
    run(logs, state, "--start")
    with pytest.raises(SystemExit) as stopped:
        run(logs, state, *argv)
    assert stopped.value.code == 2
    assert capsys.readouterr().err


def test_a_read_before_the_start_exits_with_status_2(
    logs: Logs, tmp_path: Path, capsys
) -> None:
    """episodes-tests.md, siem_read_starts_where_step_0_found_each_file."""
    state = tmp_path / "state"
    logs.write("alerts", record(*FMS_AUTH, 1))
    assert run(logs, state, "alerts") == (2, b"")
    assert "--start has not run" in capsys.readouterr().err
    assert not state.exists()


def test_siem_read_starts_where_step_0_found_each_file(
    logs: Logs, tmp_path: Path
) -> None:
    """The first read returns only records written after the offsets that
    --start recorded. A record that the manager was writing at step 0 comes
    whole in the first read."""
    state = tmp_path / "state"
    before = record(*FMS_AUTH, 1) + record(*HMI_AUTH, 2)
    partial = record(*FMS_AUTH, 3)
    logs.write("alerts", before + partial[:10])
    logs.write("archives", before)
    assert run(logs, state, "--start", "--step-s", "5") == (0, b"")
    started = json.loads((state / "state.json").read_text())
    assert started["step_0"] == STEP_0 and started["step_s"] == 5.0
    assert started["start"]["alerts"] == {
        "file": "alerts/2025/Nov/ossec-alerts-12.json",
        "size": len(before) + 10,
        "offset": len(before),
    }
    assert started["start"]["archives"]["offset"] == len(before)
    logs.write("alerts", partial[10:] + record(*FMS_AUTH, 4))
    assert run(logs, state, "alerts", now=STEP_0 + 3.0) == (
        0,
        partial + record(*FMS_AUTH, 4),
    )
    assert run(logs, state, "archives", now=STEP_0 + 3.0) == (0, b"")
    # A further read returns nothing new, and a new request starts at step 0.
    assert run(logs, state, "alerts", now=STEP_0 + 7.0) == (0, b"")
    assert run(logs, state, "alerts", "--host", "fms-01", now=STEP_0 + 7.0) == (
        0,
        partial + record(*FMS_AUTH, 4),
    )


def lines_of(data: bytes) -> list[bytes]:
    """The records of a read's output, one line each."""
    return [line + b"\n" for line in data.split(b"\n")[:-1]]


def test_the_first_read_starts_at_step_0(logs: Logs, tmp_path: Path) -> None:
    """episodes-tests.md, the_first_read_starts_at_step_0: every record that
    the first read of a request that names no start returns was written at or
    after step 0.
    Records written wholly before --start, which runs 0.35 s into step 0,
    stay out of the first read of an alerts request with a host and of an
    archives request. A record half written at --start, completed after it,
    comes whole. A request first made at step 3 starts at step 0's offsets,
    not at the position of another request of the same output
    (episode-runner-impl.md §12, query interface)."""
    state = tmp_path / "state"
    earlier = {
        "alerts": [record(*FMS_AUTH, 1), record(*HMI_AUTH, 2)],
        "archives": [record(*FMS_AUTH, 3), record(*FMS_AUDIT, 4)],
    }
    halves = {"alerts": record(*FMS_AUTH, 5), "archives": record(*FMS_AUTH, 6)}
    for output, records in earlier.items():
        logs.write(output, b"".join(records) + halves[output][:12])
    given = siem_read.utc_text(STEP_0)
    assert run(logs, state, "--start", "--step-0", given, now=STEP_0 + 0.35) == (
        0,
        b"",
    )
    later = {
        "alerts": [record(*FMS_AUTH, 7), record(*HMI_AUTH, 8)],
        "archives": [record(*HMI_AUTH, 9), record(*FMS_AUTH, 10)],
    }
    for output, records in later.items():
        logs.write(output, halves[output][12:] + b"".join(records))

    alerts = run(logs, state, "alerts", "--host", "fms-01", now=STEP_0 + 1.0)
    assert alerts == (0, halves["alerts"] + later["alerts"][0])
    archives = run(logs, state, "archives", now=STEP_0 + 1.0)
    assert archives == (0, halves["archives"] + b"".join(later["archives"]))
    for output, (_, returned) in (("alerts", alerts), ("archives", archives)):
        assert not set(lines_of(returned)) & set(earlier[output])
    assert [entry["step"] for entry in reads(state)] == [0, 0]

    # The archives request has read to the end of the file; a request of the
    # same output first made at step 3 still starts at step 0's offsets.
    assert run(logs, state, "archives", "--host", "fms-01", now=STEP_0 + 16.0) == (
        0,
        halves["archives"] + later["archives"][1],
    )
    started = json.loads((state / "state.json").read_text())["start"]["archives"]
    assert reads(state)[-1]["ranges"][0]["start"] == started["offset"]
    assert started["offset"] == len(b"".join(earlier["archives"]))


def test_reads_count_their_steps_from_the_instant_given_as_step_0(
    logs: Logs, tmp_path: Path
) -> None:
    """--start runs 0.35 s into step 0, and a read made 0.08 s after step 1's
    request is in step 1, since the steps count from the instant that
    --step-0 gives and not from the option's run (episode-runner-impl.md §12,
    query interface)."""
    state = tmp_path / "state"
    given = siem_read.utc_text(STEP_0)
    assert run(logs, state, "--start", "--step-0", given, now=STEP_0 + 0.35) == (
        0,
        b"",
    )
    started = json.loads((state / "state.json").read_text())
    assert (started["step_0"], started["step_0_time"]) == (STEP_0, given)
    assert started["started_time"] == siem_read.utc_text(STEP_0 + 0.35)
    for step in range(3):
        run(logs, state, "alerts", now=STEP_0 + 5.0 * step + 0.08)
    assert [entry["step"] for entry in reads(state)] == [0, 1, 2]


def test_a_step_0_that_is_no_time_in_utc_is_refused(logs: Logs, tmp_path: Path) -> None:
    for wrong in ("yesterday", "2025-11-12T20:00:00"):
        with pytest.raises(SystemExit) as stopped:
            run(logs, tmp_path / "state", "--start", "--step-0", wrong)
        assert stopped.value.code == 2


def test_a_record_still_being_written_waits_for_the_next_read(
    logs: Logs, tmp_path: Path
) -> None:
    state = tmp_path / "state"
    run(logs, state, "--start")
    whole, part = record(*FMS_AUTH, 1), record(*FMS_AUTH, 2)
    logs.write("archives", whole + part[:7])
    assert run(logs, state, "archives") == (0, whole)
    logs.write("archives", part[7:])
    assert run(logs, state, "archives") == (0, part)


@pytest.mark.parametrize("day_change", [False, True], ids=["one day", "a day change"])
def test_a_read_returns_the_records_since_the_previous_read(
    logs: Logs, tmp_path: Path, day_change: bool
) -> None:
    """episodes-tests.md, a_read_returns_the_records_since_the_previous_read:
    two successive reads with one request return disjoint sets of records,
    and together they hold every matching record written in that interval.
    The matching records interleave with records of another host and of
    another input of the same host, and a record half written at the first
    read is completed before the second. The case of a day change moves the
    current file to the next day's dated file between the reads
    (test_siem_read_follows_the_day_change covers that path in full)."""
    state = tmp_path / "state"
    logs.write("alerts", record(*FMS_AUTH, 0))
    logs.write("archives", record(*FMS_AUTH, 0))
    assert run(logs, state, "--start")[0] == 0
    requests = {
        "alerts": ("--host", "fms-01", "--input", "/var/log/auth.log"),
        "archives": (),
    }

    def selected(request: tuple[str, ...], line: bytes) -> bool:
        """Whether the request selects the record: every record without a
        selection, and fms-01's records of /var/log/auth.log with one."""
        if not request:
            return True
        data = json.loads(line)
        return (data["agent"]["name"], data["location"]) == FMS_AUTH

    def interleaved(first: int) -> list[bytes]:
        return [
            record(*FMS_AUTH, first),
            record(*HMI_AUTH, first + 1),
            record(*FMS_AUDIT, first + 2),
            record(*FMS_AUTH, first + 3),
        ]

    written = {output: interleaved(10) for output in requests}
    half = record(*FMS_AUTH, 14)
    for output in requests:
        logs.write(output, b"".join(written[output]) + half[:20])
    first = {
        output: run(logs, state, output, *request, now=STEP_0 + 2.0)
        for output, request in requests.items()
    }
    for output in requests:
        logs.write(output, half[20:])
        written[output].append(half)
    if day_change:
        logs.begin_day(13)
    for output in requests:
        more = interleaved(15)
        logs.write(output, b"".join(more))
        written[output] += more
    second = {
        output: run(logs, state, output, *request, now=STEP_0 + 7.0)
        for output, request in requests.items()
    }

    for output, request in requests.items():
        assert first[output][0] == second[output][0] == 0
        one, two = lines_of(first[output][1]), lines_of(second[output][1])
        assert not set(one) & set(two)
        assert one + two == [r for r in written[output] if selected(request, r)]
        assert half not in one and half in two


@pytest.mark.parametrize("compressed", [False, True])
def test_siem_read_follows_the_day_change(
    logs: Logs, tmp_path: Path, compressed: bool
) -> None:
    """episodes-tests.md, siem_read_follows_the_day_change: the second read
    returns the rest of the previous day's file and then the new file's
    records, with none missed and none repeated."""
    state = tmp_path / "state"
    run(logs, state, "--start")
    first = [record(*FMS_AUTH, n) for n in range(3)]
    logs.write("alerts", first[0])
    assert run(logs, state, "alerts") == (0, first[0])
    logs.write("alerts", first[1] + first[2])
    logs.begin_day(13)
    second = [record(*HMI_AUTH, n) for n in range(3, 5)]
    logs.write("alerts", b"".join(second))
    if compressed:
        logs.compress("alerts", 12)
    assert run(logs, state, "alerts", now=STEP_0 + 12.0) == (
        0,
        first[1] + first[2] + b"".join(second),
    )
    entry = reads(state)[-1]
    assert entry["ranges"] == [
        {
            "file": "alerts/2025/Nov/ossec-alerts-12.json",
            "start": len(first[0]),
            "end": len(b"".join(first)),
        },
        {
            "file": "alerts/2025/Nov/ossec-alerts-13.json",
            "start": 0,
            "end": len(b"".join(second)),
        },
    ]
    logs.write("alerts", record(*HMI_AUTH, 5))
    assert run(logs, state, "alerts", now=STEP_0 + 17.0) == (0, record(*HMI_AUTH, 5))
    assert reads(state)[-1]["ranges"] == [
        {
            "file": "alerts/2025/Nov/ossec-alerts-13.json",
            "start": len(b"".join(second)),
            "end": len(b"".join(second)) + len(record(*HMI_AUTH, 5)),
        }
    ]


def test_every_read_enters_the_record_of_reads(logs: Logs, tmp_path: Path) -> None:
    """episodes-tests.md, every_read_enters_the_record_of_reads; the step
    comes from the clock of siem-01 and step 0's instant."""
    state = tmp_path / "state"
    run(logs, state, "--start")
    logs.write("archives", record(*FMS_AUTH, 1) + record(*HMI_AUTH, 2))
    run(logs, state, "archives", "--host", "hmi-01", now=STEP_0 + 12.5)
    run(logs, state, "alerts", now=STEP_0 + 4.99)
    first, second = reads(state)
    assert first == {
        "step": 2,
        "time": "2025-11-12T20:00:12.500000Z",
        "request": {
            "output": "archives",
            "host": "hmi-01",
            "input": None,
            "since": None,
        },
        "ranges": [
            {
                "file": "archives/2025/Nov/ossec-archive-12.json",
                "start": 0,
                "end": len(record(*FMS_AUTH, 1) + record(*HMI_AUTH, 2)),
            }
        ],
        "records": 1,
    }
    assert (second["step"], second["records"]) == (0, 0)
    # A new start begins a new record of reads.
    run(logs, state, "--start")
    assert reads(state) == []


def test_reads_replay_from_the_dated_files(logs: Logs, tmp_path: Path) -> None:
    """Each read's byte ranges, applied to the dated files as the export
    keeps them, give the records the read returned (episodes-tests.md,
    reads_replay_from_the_episode_record)."""
    state = tmp_path / "state"
    run(logs, state, "--start")
    returned = []
    for n in range(4):
        logs.write("archives", record(*FMS_AUTH, n) + record(*HMI_AUTH, n))
        if n == 2:
            logs.begin_day(13)
        returned.append(run(logs, state, "archives", "--host", "fms-01")[1])
    logs.compress("archives", 12)
    for entry, got in zip(reads(state), returned, strict=True):
        data = b""
        for r in entry["ranges"]:
            path = logs.root / r["file"]
            content = (
                path.read_bytes()
                if path.exists()
                else gzip.decompress(Path(f"{path}.gz").read_bytes())
            )
            data += content[r["start"] : r["end"]]
        lines = [line + b"\n" for line in data.split(b"\n")[:-1]]
        assert b"".join(line for line in lines if b'"fms-01"' in line) == got


# Reads of the security monitoring's history (episodes.md §8, query interface;
# episode-runner-impl.md §12, query interface).


def stamped(host: str, location: str, n: int, at: float) -> bytes:
    """One record as the manager writes it, with its key timestamp first, at
    `at` seconds since the epoch, to the millisecond."""
    stamp = datetime.fromtimestamp(at, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")
    return (
        json.dumps(
            {
                "timestamp": f"{stamp[:-3]}+0000",
                "agent": {"name": host},
                "location": location,
                "n": n,
            },
            separators=(",", ":"),
        ).encode()
        + b"\n"
    )


def since(at: float) -> str:
    return siem_read.utc_text(at)


DAY = 86_400.0


def replayed(root: Path, entry: dict) -> bytes:
    """The records that a read returned, from its byte ranges in the dated
    files below root and its request's host and input, as an export holds
    them."""
    data = b""
    for r in entry["ranges"]:
        path = root / r["file"]
        content = (
            path.read_bytes()
            if path.exists()
            else gzip.decompress(Path(f"{path}.gz").read_bytes())
        )
        data += content[r["start"] : r["end"]]
    request = entry["request"]
    return b"".join(
        line
        for line in lines_of(data)
        if siem_read.matches(line, request["host"], request["input"])
    )


def test_siem_read_starts_at_the_first_record_at_or_after_its_start(
    logs: Logs, tmp_path: Path, capsys
) -> None:
    """episodes-tests.md, siem_read_starts_at_the_first_record_at_or_after_its_start:
    against dated files over three days, the two earlier ones compressed, a
    request with --since starts at the first record whose timestamp is at or
    after its start, searching in file order from the dated file of its
    start's day. A start before the first record starts there, a start on a
    day without a dated file starts at the next one, and a start after the
    read exits with status 2. The record of reads counts the byte ranges in
    the decompressed content and holds the start."""
    state = tmp_path / "state"
    days = {}
    for d in (9, 11):
        logs.begin_day(d)
        days[d] = [stamped(*FMS_AUTH, n, STEP_0 - (12 - d) * DAY + n) for n in range(3)]
        logs.write("archives", b"".join(days[d]))
    logs.begin_day(12)
    days[12] = [stamped(*FMS_AUTH, n, STEP_0 - 10.0 + n) for n in range(2)]
    logs.write("archives", b"".join(days[12]))
    logs.compress("archives", 9)
    logs.compress("archives", 11)
    run(logs, state, "--start")
    history = b"".join(days[9] + days[11] + days[12])

    # Between the first and the second record of 9 November.
    start = since(STEP_0 - 3 * DAY + 0.5)
    status, got = run(logs, state, "archives", "--since", start)
    assert (status, got) == (0, b"".join(days[9][1:] + days[11] + days[12]))
    entry = reads(state)[-1]
    assert entry["request"]["since"] == start
    assert entry["ranges"] == [
        {
            "file": "archives/2025/Nov/ossec-archive-09.json",
            "start": len(days[9][0]),
            "end": len(b"".join(days[9])),
        },
        {
            "file": "archives/2025/Nov/ossec-archive-11.json",
            "start": 0,
            "end": len(b"".join(days[11])),
        },
        {
            "file": "archives/2025/Nov/ossec-archive-12.json",
            "start": 0,
            "end": len(b"".join(days[12])),
        },
    ]
    assert replayed(logs.root, entry) == got
    # Before the first record, and on a day before the first dated file.
    for early in (STEP_0 - 3 * DAY - 1.0, STEP_0 - 40 * DAY):
        assert run(logs, state, "archives", "--since", since(early)) == (0, history)
    # On 10 November, which has no dated file: the first record of the 11th.
    assert run(logs, state, "archives", "--since", since(STEP_0 - 2 * DAY)) == (
        0,
        b"".join(days[11] + days[12]),
    )
    # After every record, before the read: the end of the current day's file.
    assert run(logs, state, "archives", "--since", since(STEP_0 - 1.0)) == (0, b"")
    assert reads(state)[-1]["ranges"] == [
        {
            "file": "archives/2025/Nov/ossec-archive-12.json",
            "start": len(b"".join(days[12])),
            "end": len(b"".join(days[12])),
        }
    ]
    # After the read on the clock of siem-01.
    count = len(reads(state))
    status, got = run(logs, state, "archives", "--since", since(STEP_0 + 60.0))
    assert (status, got) == (2, b"")
    assert "lies after the read" in capsys.readouterr().err
    assert len(reads(state)) == count


def test_each_start_keeps_its_own_position(logs: Logs, tmp_path: Path) -> None:
    """episodes-tests.md, each_start_keeps_its_own_position: requests that
    differ only in their start keep separate positions, so a read with one
    start leaves unchanged what the next read with the other returns, and a
    second read with one start continues where the first stopped. Two
    spellings of one instant name one request."""
    state = tmp_path / "state"
    early = [stamped(*HMI_AUTH, n, STEP_0 - 300.0 + 100.0 * n) for n in range(3)]
    logs.write("alerts", b"".join(early))
    run(logs, state, "--start")
    late = stamped(*HMI_AUTH, 3, STEP_0 + 1.0)
    logs.write("alerts", late)
    one, two = since(STEP_0 - 300.0), since(STEP_0 - 150.0)
    assert run(logs, state, "alerts", "--since", one) == (0, b"".join(early) + late)
    assert run(logs, state, "alerts", "--since", two) == (
        0,
        b"".join(early[2:]) + late,
    )
    assert run(logs, state, "alerts") == (0, late)
    newer = stamped(*HMI_AUTH, 4, STEP_0 + 6.0)
    logs.write("alerts", newer)
    for start in (one, two, None):
        option = () if start is None else ("--since", start)
        assert run(logs, state, "alerts", *option, now=STEP_0 + 7.0) == (0, newer)
    # 19:55:00 named with no fraction and with three digits of one: one
    # request, at its position after the first read.
    assert run(logs, state, "alerts", "--since", "2025-11-12T19:55:00Z") == (0, b"")
    assert run(logs, state, "alerts", "--since", "2025-11-12T19:55:00.000Z") == (
        0,
        b"",
    )
    assert len(json.loads((state / "state.json").read_text())["requests"]) == 3


def test_a_read_with_a_start_before_step_0_returns_the_history(
    logs: Logs, tmp_path: Path
) -> None:
    """episodes-tests.md, a_read_with_a_start_before_step_0_returns_the_history:
    a read with --since an instant before step 0 returns the matching records
    from the first whose timestamp is at or after that instant, those written
    before step 0 among them. A start before the security monitoring's first
    record returns every matching record from the first on. A request without
    --since still starts at step 0."""
    state = tmp_path / "state"
    early = [stamped(*FMS_AUTH, n, STEP_0 - 600.0 + 60.0 * n) for n in range(5)]
    early.insert(3, stamped(*HMI_AUTH, 9, STEP_0 - 400.0))
    logs.write("archives", b"".join(early))
    run(logs, state, "--start")
    late = [stamped(*FMS_AUTH, n, STEP_0 + 2.0 * n) for n in range(5, 7)]
    logs.write("archives", b"".join(late))
    fms = [line for line in early if b'"fms-01"' in line]
    # From the third record of fms-01 on: 20:00:00 less 480 s.
    assert run(
        logs, state, "archives", "--host", "fms-01", "--since", since(STEP_0 - 480.0)
    ) == (0, b"".join(fms[2:] + late))
    assert run(
        logs, state, "archives", "--host", "fms-01", "--since", since(STEP_0 - DAY)
    ) == (0, b"".join(fms + late))
    assert run(logs, state, "archives", "--host", "fms-01") == (0, b"".join(late))
    assert [entry["request"]["since"] for entry in reads(state)] == [
        "2025-11-12T19:52:00.000000Z",
        "2025-11-11T20:00:00.000000Z",
        None,
    ]


def test_a_start_in_a_long_day_is_the_first_record_at_or_after_it(
    logs: Logs, tmp_path: Path
) -> None:
    """In a long dated file, a binary search of the records' timestamps finds
    where to look, and the start is exactly the first record, in file order,
    whose timestamp is at or after the start. A record after it with an
    earlier timestamp, written out of order, is returned with the rest
    (episode-runner-impl.md §12, query interface)."""
    state = tmp_path / "state"
    written = [stamped(*FMS_AUTH, n, STEP_0 - 7200.0 + n) for n in range(5000)]
    # Written after the record of 4000 s, with a timestamp 0.5 s before it.
    written[4001] = stamped(*FMS_AUTH, 4001, STEP_0 - 7200.0 + 3999.5)
    logs.write("archives", b"".join(written))
    run(logs, state, "--start")
    status, got = run(logs, state, "archives", "--since", since(STEP_0 - 3200.0))
    assert (status, got) == (0, b"".join(written[4000:]))
    (r,) = reads(state)[0]["ranges"]
    assert r["start"] == len(b"".join(written[:4000]))


def test_a_start_after_every_record_starts_at_the_end_of_the_current_file(
    logs: Logs, tmp_path: Path
) -> None:
    """With no record at or after its start, a request starts at the end of
    the current day's file, and its later reads return every record written
    since (episode-runner-impl.md §12, query interface)."""
    state = tmp_path / "state"
    logs.write("alerts", stamped(*HMI_AUTH, 0, STEP_0 - 50.0))
    run(logs, state, "--start")
    start = since(STEP_0 - 20.0)
    assert run(logs, state, "alerts", "--since", start) == (0, b"")
    written = stamped(*HMI_AUTH, 1, STEP_0 + 4.0)
    logs.write("alerts", written)
    assert run(logs, state, "alerts", "--since", start, now=STEP_0 + 5.0) == (
        0,
        written,
    )


def test_a_since_read_replays_from_its_ranges_and_request(
    logs: Logs, tmp_path: Path
) -> None:
    """Each read of a request with --since, replayed from its byte ranges in
    the dated files and its request's host and input, gives byte for byte
    the records that it returned, across a day change and the compression of
    the earlier day (episodes-tests.md, reads_replay_from_the_episode_record)."""
    state = tmp_path / "state"
    early = [stamped(*FMS_AUTH, n, STEP_0 - 100.0 + n) for n in range(4)]
    early += [stamped(*HMI_AUTH, n, STEP_0 - 90.0 + n) for n in range(4, 6)]
    logs.write("archives", b"".join(early))
    run(logs, state, "--start")
    returned = []
    for n in range(4):
        logs.write("archives", stamped(*FMS_AUTH, 10 + n, STEP_0 + 5.0 * n))
        if n == 2:
            logs.begin_day(13)
        status, got = run(
            logs,
            state,
            "archives",
            "--host",
            "fms-01",
            "--since",
            since(STEP_0 - 98.0),
            now=STEP_0 + 5.0 * n + 1.0,
        )
        assert status == 0
        returned.append(got)
    logs.compress("archives", 12)
    for entry, got in zip(reads(state), returned, strict=True):
        assert entry["request"]["since"] == since(STEP_0 - 98.0)
        assert replayed(logs.root, entry) == got
    assert b"".join(returned) == b"".join(
        early[2:4] + [stamped(*FMS_AUTH, 10 + n, STEP_0 + 5.0 * n) for n in range(4)]
    )


@pytest.mark.parametrize(
    "bad",
    [
        "yesterday",
        "2025-11-12T19:00:00",
        "2025-11-12T19:00:00+00:00",
        "2025-11-12 19:00:00Z",
        "2025-11-12T19:00Z",
        "2025-11-31T19:00:00Z",
    ],
)
def test_a_time_not_in_the_form_is_refused(
    logs: Logs, tmp_path: Path, bad: str
) -> None:
    """--since and --step-0 take a date and a time of day joined by T, an
    optional fraction of a second and the suffix Z, and a request with
    another form exits with status 2 (episode-runner-impl.md §12, query
    interface)."""
    state = tmp_path / "state"
    run(logs, state, "--start")
    with pytest.raises(SystemExit) as raised:
        run(logs, state, "alerts", "--since", bad)
    assert raised.value.code == 2
    with pytest.raises(SystemExit) as raised:
        run(logs, state, "--start", "--step-0", bad)
    assert raised.value.code == 2


def test_a_time_takes_its_fraction_to_the_microsecond() -> None:
    assert siem_read.utc_instant("2025-11-12T20:00:00Z") == STEP_0 * 1_000_000
    assert siem_read.utc_instant("2025-11-12T20:00:00.5Z") == (STEP_0 + 0.5) * 1e6
    assert siem_read.utc_instant("2025-11-12T20:00:00.0000019Z") == STEP_0 * 1e6 + 1
    with pytest.raises(SystemExit):
        run(Logs(Path("/nonexistent")), Path("/nonexistent"), "--start", "--since", "x")


def test_siem_read_runs_as_a_program() -> None:
    """The competition image installs the file as /usr/local/bin/siem-read
    (episode-runner-impl.md §12, query interface)."""
    assert SIEM_READ.read_text().startswith("#!/usr/bin/env python3\n")
    assert os.access(SIEM_READ, os.X_OK)
    shown = subprocess.run(
        [sys.executable, str(SIEM_READ), "--help"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )
    assert shown.returncode == 0 and "alerts or archives" in shown.stdout
    refused = subprocess.run(
        [sys.executable, str(SIEM_READ), "bogus"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )
    assert refused.returncode == 2 and refused.stderr
