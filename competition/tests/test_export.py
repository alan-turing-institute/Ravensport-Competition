"""The export (episodes.md §3.1, phases; §13, episode record;
episode-runner-impl.md §10, export; episodes-tests.md,
the_export_omits_an_incomplete_line and the_export_keeps_each_dated_file_whole).

A fake Incus holds the files of siem-01 and sim-host below a local directory:
the export's list of dated files runs with sh on the local copy of the
manager's logs, and a pull copies the local file.

Run with: pytest competition/tests -q
"""

from __future__ import annotations

import gzip
import importlib.machinery
import importlib.util
import io
import json
import os
import shutil
import subprocess
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from competition.episode_runner import export
from competition.episode_runner.incus import Result
from competition.episode_runner.record import CopyRecord, EpisodeRecord

SEED = "000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f"
STEP_0 = datetime(2026, 11, 4, 1, 30, 5, tzinfo=timezone.utc)
# An episode whose step 0 comes before midnight, which it crosses into
# 5 November.
ACROSS_MIDNIGHT = datetime(2026, 11, 4, 23, 55, tzinfo=timezone.utc)
SIM_LOG = b"2026-11-04T01:28:00Z INFO start_state\n"


def no_wait(seconds: float) -> None:
    """Waits no time, for the tests that do not look at the export's wait."""


class FakeIncus:
    """The files of each node below root/<node>."""

    def __init__(self, root: Path, failing: str = "", listing: str | None = None):
        self.root, self.failing, self.listing = root, failing, listing
        self.events: list[str] = []

    def exec(self, node: str, *argv: str, timeout_s: float = 60.0) -> Result:
        assert (node, argv[:3], argv[4]) == (
            "siem-01",
            ("sh", "-c", export.LIST),
            export.LOGS,
        )
        self.events.append("list " + " ".join(argv[5:]))
        if self.listing is not None:
            return Result(0, self.listing, "")
        local = self.root / node / argv[4].lstrip("/")
        out = subprocess.run(
            ["sh", "-c", argv[2], "sh", str(local), *argv[5:]],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
        )
        return Result(out.returncode, out.stdout, out.stderr)

    def pull(
        self, node: str, path: str, dest: Path, timeout_s: float = 300.0
    ) -> Result:
        self.events.append(f"pull {node}{path}")
        source = self.root / node / path.lstrip("/")
        if path == self.failing or not source.is_file():
            return Result(1, "", f"Error: open {path}: no such file or directory")
        shutil.copyfile(source, dest)
        return Result(0, "", "")

    def wait(self, seconds: float) -> None:
        self.events.append(f"wait {seconds:g}")


class Siem:
    """siem-01's manager logs and record of reads, and sim-host's log, below
    root."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.logs = root / "siem-01" / export.LOGS.lstrip("/")
        self.reads = root / "siem-01" / export.READS.lstrip("/")
        self.reads.parent.mkdir(parents=True)
        self.reads.write_bytes(b'{"step": 0}\n')
        sim_log = root / "sim-host" / "var" / "log" / "sim.log"
        sim_log.parent.mkdir(parents=True)
        sim_log.write_bytes(SIM_LOG)

    def dated(self, name: str, content: bytes, current: bool = False) -> Path:
        """A dated file by its path below the logs; as the current file, also
        under its output's second name."""
        path = self.logs / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if name.endswith(".gz"):
            path.write_bytes(gzip.compress(content))
        else:
            path.write_bytes(content)
        if current:
            output = name.split("/")[0]
            second = self.logs / output / f"{output}.json"
            second.unlink(missing_ok=True)
            os.link(path, second)
        return path


def run_export(
    incus: FakeIncus,
    record: EpisodeRecord,
    step_0: datetime = STEP_0,
    minutes: float = 20.0,
    wait=no_wait,
) -> dict[str, str | None]:
    """The export of an episode whose step 0 came at step_0, `minutes`
    before the export."""
    return export.export(
        incus,
        record,
        wait=wait,
        start=step_0,
        now=lambda: step_0 + timedelta(minutes=minutes),
    )


@pytest.fixture
def siem(tmp_path: Path) -> Siem:
    return Siem(tmp_path / "nodes")


def test_the_export_copies_the_evidence_into_the_episode_record(
    siem: Siem, tmp_path: Path
) -> None:
    alerts = b'{"rule": 1}\n'
    archives = b'{"location": "/var/log/auth.log"}\n{"location": "netstat"}\n'
    siem.dated("alerts/2026/Nov/ossec-alerts-04.json", alerts, current=True)
    siem.dated("archives/2026/Nov/ossec-archive-04.json", archives, current=True)
    record = EpisodeRecord(tmp_path / "records", STEP_0, SEED)
    assert run_export(FakeIncus(siem.root), record) == {
        "alerts/2026/Nov/ossec-alerts-04.json": None,
        "archives/2026/Nov/ossec-archive-04.json": None,
        "reads.jsonl": None,
        "sim.log": None,
    }
    assert (record.path / "alerts/2026/Nov/ossec-alerts-04.json").read_bytes() == alerts
    assert (
        record.path / "archives/2026/Nov/ossec-archive-04.json"
    ).read_bytes() == archives
    assert (record.path / "reads.jsonl").read_bytes() == b'{"step": 0}\n'
    assert (record.path / "sim.log").read_bytes() == SIM_LOG


def test_a_copys_export_lies_in_export(siem: Siem, tmp_path: Path) -> None:
    siem.dated("alerts/2026/Nov/ossec-alerts-04.json", b"{}\n", current=True)
    siem.dated("archives/2026/Nov/ossec-archive-04.json", b"{}\n", current=True)
    record = CopyRecord(tmp_path / "fork-1")
    run_export(FakeIncus(siem.root), record)
    assert (record.path / "export/alerts/2026/Nov/ossec-alerts-04.json").is_file()
    assert (record.path / "export/reads.jsonl").is_file()


@pytest.mark.parametrize("missing", ["/var/log/sim.log", export.READS])
def test_a_file_that_cannot_be_copied_fails_the_export(
    siem: Siem, tmp_path: Path, missing: str
) -> None:
    siem.dated("alerts/2026/Nov/ossec-alerts-04.json", b"{}\n", current=True)
    siem.dated("archives/2026/Nov/ossec-archive-04.json", b"{}\n", current=True)
    record = EpisodeRecord(tmp_path / "records", STEP_0, SEED)
    errors = run_export(FakeIncus(siem.root, failing=missing), record)
    name = "sim.log" if missing.endswith("sim.log") else "reads.jsonl"
    assert errors[name] == f"Error: open {missing}: no such file or directory"
    assert errors["alerts/2026/Nov/ossec-alerts-04.json"] is None


def test_an_output_without_a_dated_file_fails_the_export(
    siem: Siem, tmp_path: Path
) -> None:
    siem.dated("alerts/2026/Nov/ossec-alerts-04.json", b"{}\n", current=True)
    record = EpisodeRecord(tmp_path / "records", STEP_0, SEED)
    errors = run_export(FakeIncus(siem.root), record)
    assert errors["archives"] == "no dated file from 2026-11-04 to 2026-11-04"
    assert errors["alerts/2026/Nov/ossec-alerts-04.json"] is None


def test_the_export_keeps_each_dated_file_whole(siem: Siem, tmp_path: Path) -> None:
    """Across midnight, each day's dated file of each output is copied whole
    from its first byte, under its path below /var/ossec/logs, and the file
    that the manager compressed stays compressed. The manager's checksum
    files are not copied."""
    before = b'{"before": "midnight", "n": 1}\n{"before": "midnight", "n": 2}\n'
    after = b'{"after": "midnight"}\n'
    compressed = siem.dated("alerts/2026/Nov/ossec-alerts-04.json.gz", before)
    (compressed.parent / "ossec-alerts-04.json.sum").write_text("checksum\n")
    siem.dated("alerts/2026/Nov/ossec-alerts-05.json", after, current=True)
    siem.dated("archives/2026/Nov/ossec-archive-04.json", before)
    siem.dated("archives/2026/Nov/ossec-archive-05.json", after, current=True)
    record = EpisodeRecord(tmp_path / "records", STEP_0, SEED)
    errors = run_export(FakeIncus(siem.root), record, step_0=ACROSS_MIDNIGHT)
    assert all(error is None for error in errors.values())
    month = record.path / "alerts" / "2026" / "Nov"
    assert sorted(p.name for p in month.iterdir()) == [
        "ossec-alerts-04.json.gz",
        "ossec-alerts-05.json",
    ]
    assert (month / "ossec-alerts-04.json.gz").read_bytes() == compressed.read_bytes()
    assert gzip.decompress((month / "ossec-alerts-04.json.gz").read_bytes()) == before
    assert (month / "ossec-alerts-05.json").read_bytes() == after
    assert (
        record.path / "archives/2026/Nov/ossec-archive-04.json"
    ).read_bytes() == before


def test_a_day_being_compressed_is_copied_as_written(
    siem: Siem, tmp_path: Path
) -> None:
    """While the manager compresses the previous day's file, both the file and
    a partial compressed file exist; the file as written is copied."""
    siem.dated("alerts/2026/Nov/ossec-alerts-04.json", b'{"day": 4}\n')
    (siem.logs / "alerts/2026/Nov/ossec-alerts-04.json.gz").write_bytes(b"\x1f\x8b")
    siem.dated("alerts/2026/Nov/ossec-alerts-05.json", b'{"day": 5}\n', current=True)
    siem.dated("archives/2026/Nov/ossec-archive-05.json", b"", current=True)
    record = EpisodeRecord(tmp_path / "records", STEP_0, SEED)
    run_export(FakeIncus(siem.root), record, step_0=ACROSS_MIDNIGHT)
    month = record.path / "alerts" / "2026" / "Nov"
    assert sorted(p.name for p in month.iterdir()) == [
        "ossec-alerts-04.json",
        "ossec-alerts-05.json",
    ]


def test_the_export_waits_for_the_manager_to_write_out_its_logs(
    siem: Siem, tmp_path: Path
) -> None:
    siem.dated("alerts/2026/Nov/ossec-alerts-04.json", b"{}\n", current=True)
    siem.dated("archives/2026/Nov/ossec-archive-04.json", b"{}\n", current=True)
    record = EpisodeRecord(tmp_path / "records", STEP_0, SEED)
    incus = FakeIncus(siem.root)
    run_export(incus, record, wait=incus.wait)
    # The record of reads comes first, before the dated files
    # (episode-runner-impl.md §10, export).
    assert incus.events == [
        f"wait {export.FLUSH_WAIT_S:g}",
        f"pull siem-01{export.READS}",
        "list alerts/2026/Nov/ossec-alerts-04 archives/2026/Nov/ossec-archive-04",
        "pull siem-01/var/ossec/logs/alerts/2026/Nov/ossec-alerts-04.json",
        "pull siem-01/var/ossec/logs/archives/2026/Nov/ossec-archive-04.json",
        "pull sim-host/var/log/sim.log",
    ]


@pytest.mark.parametrize(
    ("written", "exported"),
    [
        (b'{"a": 1}\n{"b": ', b'{"a": 1}\n'),
        (b'{"a": ', b""),
        (b"", b""),
    ],
)
def test_the_export_omits_an_incomplete_line(
    siem: Siem, tmp_path: Path, written: bytes, exported: bytes
) -> None:
    """A line that the manager is still writing at the export is left out of
    the exported dated file, which ends with its last complete line."""
    siem.dated("alerts/2026/Nov/ossec-alerts-04.json", b"", current=True)
    siem.dated("archives/2026/Nov/ossec-archive-04.json", written, current=True)
    record = EpisodeRecord(tmp_path / "records", STEP_0, SEED)
    errors = run_export(FakeIncus(siem.root), record)
    name = "archives/2026/Nov/ossec-archive-04.json"
    assert (record.path / name).read_bytes() == exported
    assert errors[name] is None


def test_the_days_run_from_step_0_to_the_export() -> None:
    """Each day in Coordinated Universal Time (UTC), oldest first, across the
    end of a month."""
    start = datetime(2026, 10, 30, 22, 0, tzinfo=timezone.utc)
    assert export.days_spanned(start, start + timedelta(hours=27)) == [
        date(2026, 10, 30),
        date(2026, 10, 31),
        date(2026, 11, 1),
    ]
    assert export.days_spanned(STEP_0, STEP_0 + timedelta(minutes=20)) == [
        date(2026, 11, 4)
    ]
    assert export.dated_files(
        "ossec-alerts-", [date(2026, 10, 31), date(2026, 11, 1)]
    ) == ["2026/Oct/ossec-alerts-31", "2026/Nov/ossec-alerts-01"]


def test_the_export_copies_only_the_days_that_the_episode_spans(
    siem: Siem, tmp_path: Path
) -> None:
    """In a fork, siem-01 holds every day since the living port's start. The
    export copies the days from step 0 alone, also across the end of a
    month."""
    for day in (29, 30, 31):
        siem.dated(f"alerts/2026/Oct/ossec-alerts-{day}.json.gz", b'{"oct": 1}\n')
    siem.dated("alerts/2026/Nov/ossec-alerts-01.json", b'{"nov": 1}\n', current=True)
    siem.dated("archives/2026/Nov/ossec-archive-01.json", b"", current=True)
    record = EpisodeRecord(tmp_path / "records", STEP_0, SEED)
    step_0 = datetime(2026, 10, 31, 23, 50, tzinfo=timezone.utc)
    errors = run_export(FakeIncus(siem.root), record, step_0=step_0)
    assert [name for name in errors if name.startswith("alerts/")] == [
        "alerts/2026/Oct/ossec-alerts-31.json.gz",
        "alerts/2026/Nov/ossec-alerts-01.json",
    ]
    assert not (record.path / "alerts/2026/Oct/ossec-alerts-30.json.gz").exists()


def test_only_the_dated_files_asked_for_are_copied(siem: Siem, tmp_path: Path) -> None:
    """siem-01 runs the entry's software, so a listed name that the export did
    not ask for is never copied."""
    siem.dated("alerts/2026/Nov/ossec-alerts-04.json", b"{}\n", current=True)
    siem.dated("archives/2026/Nov/ossec-archive-04.json", b"{}\n", current=True)
    listing = (
        "../../../etc/shadow\n"
        "alerts/2026/Nov/ossec-alerts-04.json\n"
        "archives/2026/Nov/ossec-archive-04.json\n"
    )
    record = EpisodeRecord(tmp_path / "records", STEP_0, SEED)
    incus = FakeIncus(siem.root, listing=listing)
    errors = run_export(incus, record)
    assert "../../../etc/shadow" not in errors
    assert not any("shadow" in event for event in incus.events)


SIEM_READ = Path(__file__).resolve().parents[1] / "siem_read" / "siem-read"


def _siem_read():
    loader = importlib.machinery.SourceFileLoader("siem_read_program", str(SIEM_READ))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_reads_replay_from_the_episode_record(siem: Siem, tmp_path: Path) -> None:
    """Each read in the exported record of reads, replayed against the
    exported dated files, returns byte for byte the records that siem-read
    returned, across midnight and the compression of the previous day's file
    (episodes-tests.md, reads_replay_from_the_episode_record)."""
    siem_read = _siem_read()
    siem.dated("alerts/2026/Nov/ossec-alerts-04.json", b"", current=True)
    siem.dated(
        "archives/2026/Nov/ossec-archive-04.json", b'{"before": 0}\n', current=True
    )
    state = siem.reads.parent
    state.mkdir(parents=True, exist_ok=True)
    clock = ACROSS_MIDNIGHT.timestamp()
    assert (
        siem_read.main(["--start"], logs=siem.logs, state=state, now=lambda: clock) == 0
    )
    returned = []
    for n in range(6):
        if n == 3:
            siem.dated("archives/2026/Nov/ossec-archive-05.json", b"", current=True)
        with open(siem.logs / "archives" / "archives.json", "ab") as f:
            f.write(b'{"agent": {"name": "fms-01"}, "n": %d}\n{"n": %d}\n' % (n, n))
        out = io.BytesIO()
        status = siem_read.main(
            ["archives", "--host", "fms-01"],
            logs=siem.logs,
            state=state,
            now=lambda: clock + 5.0 * (n + 1),
            stdout=out,
        )
        assert status == 0
        returned.append(out.getvalue())
    gz = siem.logs / "archives/2026/Nov/ossec-archive-04.json"
    Path(f"{gz}.gz").write_bytes(gzip.compress(gz.read_bytes()))
    gz.unlink()
    record = EpisodeRecord(tmp_path / "records", STEP_0, SEED)
    errors = run_export(FakeIncus(siem.root), record, step_0=ACROSS_MIDNIGHT)
    assert all(error is None for error in errors.values())
    reads = [
        json.loads(line)
        for line in (record.path / "reads.jsonl").read_text().splitlines()
    ]
    assert len(reads) == 6
    for entry, got in zip(reads, returned, strict=True):
        data = b""
        for r in entry["ranges"]:
            path = record.path / r["file"]
            content = (
                path.read_bytes()
                if path.exists()
                else gzip.decompress(Path(f"{path}.gz").read_bytes())
            )
            data += content[r["start"] : r["end"]]
        lines = [line + b"\n" for line in data.split(b"\n")[:-1]]
        assert b"".join(line for line in lines if b'"fms-01"' in line) == got
    assert b'"n": 0' in returned[0] and b'"n": 5' in returned[5]


def reads_touching(*files: str) -> bytes:
    """A record of reads whose one read touched each of files."""
    ranges = [{"file": f, "start": 0, "end": 1} for f in files]
    return json.dumps({"step": 0, "ranges": ranges}).encode() + b"\n"


def test_the_export_holds_every_dated_file_that_a_read_touched(
    siem: Siem, tmp_path: Path
) -> None:
    """episodes-tests.md, the_export_holds_every_dated_file_that_a_read_touched:
    a read with --since two days before step 0 touched the dated files of
    those days, which the export holds whole and under their own names,
    compressed as siem-01 holds them, beside the files of the episode's days;
    a day that no read touched stays out, and a name that is no dated file of
    the manager's is never copied. The read then replays byte for byte from
    the episode record."""
    siem_read = _siem_read()
    day = timedelta(days=1)

    def stamped(n: int, at: datetime) -> bytes:
        stamp = at.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "+0000"
        return b'{"timestamp":"%s","agent":{"name":"fms-01"},"n":%d}\n' % (
            stamp.encode(),
            n,
        )

    early = {
        d: [stamped(n, STEP_0 - (4 - d) * day + timedelta(seconds=n)) for n in range(3)]
        for d in (1, 2, 3)
    }
    siem.dated("archives/2026/Nov/ossec-archive-01.json.gz", b"".join(early[1]))
    siem.dated("archives/2026/Nov/ossec-archive-02.json.gz", b"".join(early[2]))
    siem.dated("archives/2026/Nov/ossec-archive-03.json", b"".join(early[3]))
    today = stamped(9, STEP_0 - timedelta(seconds=30))
    siem.dated("alerts/2026/Nov/ossec-alerts-04.json", b"", current=True)
    siem.dated("archives/2026/Nov/ossec-archive-04.json", today, current=True)
    state = siem.reads.parent
    clock = STEP_0.timestamp()
    assert (
        siem_read.main(["--start"], logs=siem.logs, state=state, now=lambda: clock) == 0
    )
    since = siem_read.utc_text((STEP_0 - 2 * day + timedelta(seconds=1)).timestamp())
    out = io.BytesIO()
    status = siem_read.main(
        ["archives", "--since", since],
        logs=siem.logs,
        state=state,
        now=lambda: clock + 1.0,
        stdout=out,
    )
    assert status == 0
    assert out.getvalue() == b"".join(early[2][1:] + early[3]) + today
    with open(siem.reads, "ab") as f:
        f.write(reads_touching("archives/2026/Nov/../../../../etc/shadow.json"))
    record = EpisodeRecord(tmp_path / "records", STEP_0, SEED)
    incus = FakeIncus(siem.root)
    errors = run_export(incus, record)
    assert all(error is None for error in errors.values())
    assert [name for name in errors if name.startswith("archives/")] == [
        "archives/2026/Nov/ossec-archive-04.json",
        "archives/2026/Nov/ossec-archive-02.json.gz",
        "archives/2026/Nov/ossec-archive-03.json",
    ]
    for name in ("ossec-archive-02.json.gz", "ossec-archive-03.json"):
        source = siem.logs / "archives/2026/Nov" / name
        exported = record.path / "archives/2026/Nov" / name
        assert exported.read_bytes() == source.read_bytes()
    assert not (record.path / "archives/2026/Nov/ossec-archive-01.json.gz").exists()
    assert not any("shadow" in event for event in incus.events)
    entry = json.loads((record.path / "reads.jsonl").read_text().splitlines()[0])
    data = b""
    for r in entry["ranges"]:
        exported = record.path / r["file"]
        content = (
            exported.read_bytes()
            if exported.exists()
            else gzip.decompress(Path(f"{exported}.gz").read_bytes())
        )
        data += content[r["start"] : r["end"]]
    lines = [line + b"\n" for line in data.split(b"\n")[:-1]]
    replayed = b"".join(line for line in lines if siem_read.matches(line, None, None))
    assert replayed == out.getvalue()


def test_a_touched_file_that_siem_01_no_longer_holds_fails_the_export(
    siem: Siem, tmp_path: Path
) -> None:
    """A read touched a dated file that is gone at the export, so the read
    would not replay, and the export fails."""
    siem.dated("alerts/2026/Nov/ossec-alerts-04.json", b"{}\n", current=True)
    siem.dated("archives/2026/Nov/ossec-archive-04.json", b"{}\n", current=True)
    siem.reads.write_bytes(reads_touching("alerts/2026/Nov/ossec-alerts-01.json"))
    record = EpisodeRecord(tmp_path / "records", STEP_0, SEED)
    errors = run_export(FakeIncus(siem.root), record)
    assert errors["alerts/2026/Nov/ossec-alerts-01.json"] == (
        "a read touched it, and siem-01 no longer holds it"
    )
