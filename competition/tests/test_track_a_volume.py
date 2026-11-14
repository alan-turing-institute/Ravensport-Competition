"""The storage volumes of the example Track A entries
(competition/baselines/track_a/make_volume.py; episodes.md §3.2, modes; §7,
step protocol).

Run with: pytest competition/tests -q
"""

from __future__ import annotations

import importlib.util
import stat
from pathlib import Path

import pytest

TRACK_A = Path(__file__).resolve().parents[1] / "baselines" / "track_a"
_spec = importlib.util.spec_from_file_location(
    "make_volume", TRACK_A / "make_volume.py"
)
make_volume = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(make_volume)


def modes(volume: Path) -> dict[str, int]:
    return {p.name: stat.S_IMODE(p.stat().st_mode) for p in volume.iterdir()}


@pytest.mark.parametrize(
    ("baseline", "files"),
    [
        ("heuristic", {"defender", "protocol.py", "telemetry.json"}),
        ("learning", {"defender", "protocol.py", "telemetry.json", "model.json"}),
        ("packetbeat", {"defender", "protocol.py", "telemetry.json"}),
    ],
)
def test_a_volume_holds_the_files_that_the_defender_reads_and_no_other(
    tmp_path: Path, baseline: str, files: set[str]
) -> None:
    volume = make_volume.make_volume(baseline, tmp_path / "volume")
    assert set(modes(volume)) == files
    assert all(
        mode == (0o755 if name == "defender" else 0o644)
        for name, mode in modes(volume).items()
    )
    for name in files - {"protocol.py"}:
        assert (volume / name).read_bytes() == (TRACK_A / baseline / name).read_bytes()
    shared = (TRACK_A / "common" / "protocol.py").read_bytes()
    assert (volume / "protocol.py").read_bytes() == shared


def test_the_idle_volume_is_empty(tmp_path: Path) -> None:
    volume = make_volume.make_volume("idle", tmp_path / "volume")
    assert list(volume.iterdir()) == []


def test_a_volume_is_never_written_over(tmp_path: Path) -> None:
    out = tmp_path / "volume"
    out.mkdir()
    (out / "defender").write_text("an earlier entry")
    with pytest.raises(FileExistsError):
        make_volume.make_volume("heuristic", out)


def test_a_baseline_without_a_defender_gives_no_volume(tmp_path: Path) -> None:
    (tmp_path / "baseline").mkdir()
    with pytest.raises(FileNotFoundError):
        make_volume.make_volume(str(tmp_path / "baseline"), tmp_path / "volume")


def test_the_listing_gives_each_files_digest_and_mode(tmp_path: Path) -> None:
    volume = make_volume.make_volume("heuristic", tmp_path / "volume")
    lines = make_volume.listing(volume)
    assert [line.split()[2] for line in lines] == [
        "defender",
        "protocol.py",
        "telemetry.json",
    ]
    assert lines[0].split()[1] == "0755"
