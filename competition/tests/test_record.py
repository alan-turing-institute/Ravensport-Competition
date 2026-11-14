"""The episode record (episodes.md §13, episode record).

One directory per episode, named by the episode's start in Coordinated
Universal Time (UTC) and the first 8 hexadecimal digits of the episode seed,
holding six files.

Run with: pytest competition/tests -q
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from competition.episode_runner import record

SEED = "000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f"
START = datetime(2026, 11, 4, 1, 30, 5, 250000, tzinfo=timezone.utc)


def test_the_directory_names_the_start_and_the_seed(tmp_path: Path) -> None:
    episode = record.EpisodeRecord(tmp_path, START, SEED)
    assert episode.path == tmp_path / "20261104T013005Z-00010203"
    assert episode.path.is_dir()


def test_an_existing_record_is_never_overwritten(tmp_path: Path) -> None:
    record.EpisodeRecord(tmp_path, START, SEED)
    with pytest.raises(FileExistsError):
        record.EpisodeRecord(tmp_path, START, SEED)


def test_episode_json_holds_every_field_set(tmp_path: Path) -> None:
    episode = record.EpisodeRecord(tmp_path, START, SEED)
    episode.set(episode_seed=SEED, type="benign", mode="evaluation", track="A")
    episode.set(settle_period_start="2026-11-04T01:20:00Z")
    assert json.loads((episode.path / "episode.json").read_text()) == {
        "episode_seed": SEED,
        "type": "benign",
        "mode": "evaluation",
        "track": "A",
        "settle_period_start": "2026-11-04T01:20:00Z",
    }


def test_steps_jsonl_holds_one_line_per_step(tmp_path: Path) -> None:
    episode = record.EpisodeRecord(tmp_path, START, SEED)
    for step in range(3):
        episode.append_step(
            {"step": step, "decision": "no action", "dropped_decision_messages": 0}
        )
    lines = (episode.path / "steps.jsonl").read_text().splitlines()
    assert [json.loads(line)["step"] for line in lines] == [0, 1, 2]


DATED_FILES = (
    "alerts/2026/Nov/ossec-alerts-04.json",
    "archives/2026/Oct/ossec-archive-31.json.gz",
)


def test_the_record_holds_its_files(tmp_path: Path) -> None:
    """The episode runner's three files, the dated files under their paths
    below /var/ossec/logs, the record of reads and the simulator's log."""
    episode = record.EpisodeRecord(tmp_path, START, SEED)
    episode.set(episode_seed=SEED)
    episode.append_step({"step": 0})
    episode.write_startup('{"sim_tug_arrivals": 1, "initial_volume_l": 35000.0}\n')
    for name in (*DATED_FILES, *record.EXPORTED):
        episode.exported_path(name).write_bytes(b"exported\n")
    assert sorted(p.name for p in episode.path.iterdir()) == sorted(record.FILES)
    assert (episode.path / DATED_FILES[0]).read_bytes() == b"exported\n"
    for name in (
        "other.json",
        "alerts.json",
        "alerts",
        "alerts/../episode.json",
        "alerts//x.json",
        "steps/2026/Nov/x.json",
    ):
        with pytest.raises(ValueError):
            episode.exported_path(name)


def test_every_time_is_written_in_utc_with_microseconds() -> None:
    assert record.utc_text(START) == "2026-11-04T01:30:05.250000Z"


def test_a_copys_record_lies_in_the_directory_the_command_line_names(
    tmp_path: Path,
) -> None:
    """record.json for the episode's fields, the export in export/
    (the living-port contract)."""
    out = tmp_path / "episodes" / "fork-3"
    episode = record.CopyRecord(out)
    episode.set(episode_seed=SEED, fork=3)
    episode.append_step({"step": 0})
    episode.write_startup('{"sim_tug_arrivals": 1}\n')
    for name in (*DATED_FILES, *record.EXPORTED):
        episode.exported_path(name).write_bytes(b"exported\n")
    assert episode.path == out
    assert json.loads((out / "record.json").read_text()) == {
        "episode_seed": SEED,
        "fork": 3,
    }
    assert sorted(p.name for p in out.iterdir()) == [
        "export",
        "record.json",
        "startup.json",
        "steps.jsonl",
    ]
    assert sorted(p.name for p in (out / "export").iterdir()) == sorted(
        (*record.DATED, *record.EXPORTED)
    )
    assert (out / "export" / DATED_FILES[1]).is_file()


def test_a_copys_record_is_never_overwritten(tmp_path: Path) -> None:
    record.CopyRecord(tmp_path).set(fork=1)
    with pytest.raises(FileExistsError):
        record.CopyRecord(tmp_path)
