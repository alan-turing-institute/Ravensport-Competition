"""world.yaml category annotation tests. Run with: pytest tests/unit -q

deployment-config.md §3 admits a fact to world.yaml only if it falls into at
least one of four categories
- shared
- determinism
- credential
- scenario

and requires every value to carry a trailing comment opening with them in brackets,
e.g. ``# [shared, scenario] explanatory text``. These tests keep the annotation
complete: a value added without one, or with a category outside the four, fails
here rather than leaving the file's inclusion rule unrecorded.

The annotation lives in comments, which a YAML parser discards, so the file is
read line by line. A cross-check against the parsed document guards the line
scan itself: if a value were written in a form the scan does not recognise
(a flow-style list, a multi-line string), the counts would disagree.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]  # deploy/
WORLD = ROOT / "world.yaml"

CATEGORIES = {"shared", "determinism", "credential", "scenario"}

# `key: value` or `- key: value`, where value is non-empty. A bare `key:`
# (a mapping header) holds no value and carries no annotation.
VALUE_LINE = re.compile(r"^\s*(?:-\s+)?[A-Za-z_][A-Za-z0-9_]*:\s+\S")
TAG = re.compile(r"^#\s*\[([^\]]*)\]")


def _split_comment(line: str) -> tuple[str, str]:
    """Split a line into (content, comment) at the first `#` that is outside
    quotes and preceded by whitespace — the YAML rule, so a `#` inside a quoted
    password is not mistaken for a comment."""
    quote = None
    for i, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
        elif ch == "#" and (i == 0 or line[i - 1] in " \t"):
            return line[:i], line[i:]
    return line, ""


def _value_lines() -> list[tuple[int, str, str]]:
    """(line number, content, comment) for every line holding a scalar value."""
    out = []
    for n, raw in enumerate(WORLD.read_text().splitlines(), start=1):
        content, comment = _split_comment(raw)
        if VALUE_LINE.match(content):
            out.append((n, content.strip(), comment.strip()))
    return out


def _scalar_leaves(node) -> int:
    if isinstance(node, dict):
        return sum(_scalar_leaves(v) for v in node.values())
    if isinstance(node, list):
        return sum(_scalar_leaves(v) for v in node)
    return 1


def test_every_value_carries_a_category():
    missing = [
        f"world.yaml:{n}: {content}"
        for n, content, comment in _value_lines()
        if not TAG.match(comment)
    ]
    assert not missing, (
        "values without a category annotation (deployment-config.md §3):\n"
        + "\n".join(missing)
    )


def test_categories_are_from_the_four():
    bad = []
    for n, content, comment in _value_lines():
        m = TAG.match(comment)
        if not m:
            continue  # reported by test_every_value_carries_a_category
        tags = [t.strip() for t in m.group(1).split(",")]
        unknown = [t for t in tags if t not in CATEGORIES]
        if not tags or unknown or "" in tags:
            bad.append(f"world.yaml:{n}: {content}  {comment}")
    assert not bad, f"category outside {sorted(CATEGORIES)} or empty:\n" + "\n".join(
        bad
    )


def test_session_lifetime_is_tagged_shared():
    """The FMS and the sim's reconciliation user both read the session lifetime
    (deployment-config.md §3), so it is a shared value as well as a scenario one."""
    [comment] = [
        comment
        for _, content, comment in _value_lines()
        if content.startswith("session_timeout_s:")
    ]
    tags = {t.strip() for t in TAG.match(comment).group(1).split(",")}
    assert tags == {"shared", "scenario"}, comment


def test_audit_cadence_is_tagged_scenario():
    """The audit's period and settling delay are set by the port and read by the
    sim alone (deployment-config.md §3): scenario values and nothing else."""
    for key in ("period:", "settling_delay_s:"):
        [comment] = [
            comment for _, content, comment in _value_lines() if content.startswith(key)
        ]
        tags = {t.strip() for t in TAG.match(comment).group(1).split(",")}
        assert tags == {"scenario"}, comment


def _tags(key: str) -> set[str]:
    """The categories of the one value whose line opens with ``key:``."""
    [comment] = [
        comment
        for _, content, comment in _value_lines()
        if content.startswith(key + ":")
    ]
    return {t.strip() for t in TAG.match(comment).group(1).split(",")}


def test_lifted_scenario_values_are_tagged_scenario():
    """The dispenser's flow rate, the overfill interlock level, the fuel
    supplier's ordering values and the refuel range are read by the sim alone
    (deployment-config.md §3): scenario values and nothing else."""
    for key in (
        "overfill_interlock_pct",
        "dispenser_flow_rate_l_per_min",
        "reorder_threshold_l",
        "delivery_volume_l",
        "lead_time_s",
        "tanker_pump_rate_l_per_min",
        "poll_interval_s",
        "refuel_volume_min_l",
        "refuel_volume_max_l",
    ):
        assert _tags(key) == {"scenario"}, key


def test_mean_time_away_is_tagged_scenario():
    """Each tug's mean time away is read by the sim alone (deployment-config.md
    §3): a scenario value and nothing else, on every tug."""
    comments = [
        comment
        for _, content, comment in _value_lines()
        if content.startswith("mean_time_away_s:")
    ]
    assert comments, "world.yaml carries no mean_time_away_s"
    for comment in comments:
        tags = {t.strip() for t in TAG.match(comment).group(1).split(",")}
        assert tags == {"scenario"}, comment


def test_cavitation_threshold_is_tagged_scenario():
    """The FMS alone reads the cavitation level (deployment-config.md §3): a
    scenario value with a single consumer, not a shared one."""
    assert _tags("cavitation_threshold_pct") == {"scenario"}


def test_line_scan_covers_every_value():
    """The line scan must see every scalar the parser sees; otherwise a value
    in a form the scan misses would escape the annotation check unnoticed."""
    parsed = _scalar_leaves(yaml.safe_load(WORLD.read_text()))
    scanned = len(_value_lines())
    assert scanned == parsed, (
        f"line scan found {scanned} values, the YAML parser {parsed}; a value is "
        f"written in a form test_world_categories.py does not recognise"
    )
