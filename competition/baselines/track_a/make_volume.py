#!/usr/bin/env python3
"""The storage volume of an example Track A entry, for live episodes.

    make_volume.py BASELINE OUT_DIR

writes into OUT_DIR, which must be absent or empty, the files of the entry's
storage volume, which siem-01 mounts at /var/lib/entry (episodes.md §3.2,
modes; §7, step protocol). The volume holds the baseline's `defender` and
`telemetry.json`, its `model.json` where it has one, and the shared
`protocol.py` beside them, as replay.py assembles every volume that it
replays. The defender's mode is 0755 and every other file's 0644, so that the
opening of the volume to siem-01's unprivileged container keeps the defender
executable and every file readable. Nothing else of the baseline's
directory enters the volume: an entry's image of siem-01, such as the
Packetbeat entry's, holds its own software.

BASELINE names a directory beside this program, such as `heuristic`, or a
path to one. `idle` gives an empty volume, from which the launcher runs the
idle defender. The program prints the SHA-256 digest, the mode and the name
of each file of the volume.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "common"))

import replay  # noqa: E402

IDLE = "idle"


def make_volume(baseline: str, out: Path) -> Path:
    """Write the volume of `baseline` into the absent or empty directory
    `out`, and return it."""
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise FileExistsError(f"{out} exists and is not an empty directory")
    if baseline == IDLE:
        out.mkdir(parents=True, exist_ok=True)
        out.chmod(0o755)
        return out
    directory = Path(baseline) if "/" in baseline else HERE / baseline
    return replay.assemble(directory, out, read_only=False)


def listing(volume: Path) -> list[str]:
    """Each file of the volume as `digest  mode  name`, by name."""
    lines = []
    for path in sorted(volume.iterdir()):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        lines.append(f"{digest}  {path.stat().st_mode & 0o777:04o}  {path.name}")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="make_volume.py",
        description="Write the storage volume of an example Track A entry.",
    )
    parser.add_argument(
        "baseline", help="heuristic, learning, packetbeat, idle or a path"
    )
    parser.add_argument("out", help="an absent or empty directory")
    args = parser.parse_args(argv)
    try:
        volume = make_volume(args.baseline, Path(args.out))
    except (FileExistsError, FileNotFoundError) as error:
        print(f"make_volume.py: {error}", file=sys.stderr)
        return 1
    for line in listing(volume):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
