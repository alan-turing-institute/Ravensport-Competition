#!/usr/bin/env python3
"""Writes the entry's own version of the competition's additional override
file (README.md, "Deployment"; episodes.md §3.2, modes).

    override.py [OUTPUT]

merges the entry's change, override-patch.yaml, into the competition's file,
competition/deploy/override.yaml, and writes the result to OUTPUT, by default
dist/override.yaml beside this program. The entry's file is never edited by
hand: generated from the competition's file at each build, it keeps every
value that the competition's file gives, and changes only what the patch
names. The patch's template directory is a path from this program's
directory, which the output states from its own directory, since render.py
reads it relative to the override file.
"""

from __future__ import annotations

import copy
import os
import sys
from pathlib import Path
from typing import Any

import yaml

HERE = Path(__file__).resolve().parent
COMPETITION = HERE.parents[2] / "deploy" / "override.yaml"
PATCH = HERE / "override-patch.yaml"
OUTPUT = HERE / "dist" / "override.yaml"
HEADER = """\
# The entry's own version of the competition's additional override file,
# written by competition/baselines/track_a/packetbeat/override.py from
# competition/deploy/override.yaml and override-patch.yaml. Do not edit it:
# write it again with override.py.
"""


def merged(base: Any, patch: Any) -> Any:
    """base with patch's values in place of its own, mapping by mapping."""
    if isinstance(base, dict) and isinstance(patch, dict):
        out = copy.deepcopy(base)
        for key, value in patch.items():
            out[key] = merged(base.get(key), value)
        return out
    return copy.deepcopy(patch)


def generate(
    competition: Path = COMPETITION, patch: Path = PATCH, output: Path = OUTPUT
) -> dict[str, Any]:
    """The entry's override file, written to output."""
    change = yaml.safe_load(patch.read_text())
    templates = (patch.parent / change["template_dir"]).resolve()
    change["template_dir"] = os.path.relpath(templates, output.resolve().parent)
    result = merged(yaml.safe_load(competition.read_text()), change)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(HEADER + yaml.safe_dump(result, sort_keys=False))
    return result


def main(argv: list[str]) -> int:
    output = Path(argv[1]) if len(argv) > 1 else OUTPUT
    generate(output=output)
    print(output)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
