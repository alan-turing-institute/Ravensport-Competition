"""The heuristic Track A defender (competition/baselines/track_a/heuristic/).

The tests check the defender's two rules on records written for them, its
address plan against deploy/topology.yaml, its telemetry specification
against the reads it makes, and the program itself in a replay of a small
episode.

Run with: pytest competition/tests -q
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import sys
from pathlib import Path

import pytest
import yaml

REPOSITORY = Path(__file__).resolve().parents[2]
BASELINE = REPOSITORY / "competition" / "baselines" / "track_a" / "heuristic"
COMMON = BASELINE.parent / "common"
if str(COMMON) not in sys.path:
    sys.path.insert(0, str(COMMON))

import protocol  # noqa: E402
import replay  # noqa: E402

_loader = importlib.machinery.SourceFileLoader(
    "heuristic_defender", str(BASELINE / "defender")
)
_spec = importlib.util.spec_from_loader(_loader.name, _loader)
heuristic = importlib.util.module_from_spec(_spec)
_loader.exec_module(heuristic)

from test_track_a_replay import line, write_episode  # noqa: E402


def alert(level: int, rule: str = "203") -> dict:
    return {
        "rule": {"level": level, "id": rule, "description": "a test alert"},
        "agent": {"name": "corp-it"},
        "location": "wazuh-agent",
    }


def flow(source: str, target: str) -> dict:
    return {
        "agent": {"name": "fw-ext-dmz"},
        "location": heuristic.INSPECTION,
        "data": {"event_type": "flow", "src_ip": source, "dest_ip": target},
    }


def test_the_address_plan_is_the_topology_without_the_adversary_host() -> None:
    topology = yaml.safe_load((REPOSITORY / "deploy" / "topology.yaml").read_text())
    addresses = {
        nic["ipv4"]
        for name, node in topology["nodes"].items()
        if name != "adversary"
        for nic in node.get("nics") or []
        if isinstance(nic, dict) and nic.get("ipv4")
    }
    assert set(heuristic.ADDRESS_PLAN) == addresses


def test_an_alert_below_the_lowest_level_raises_no_alarm() -> None:
    assert heuristic.reasons([alert(heuristic.MIN_LEVEL - 1, "533")], []) == []


def test_an_alert_at_the_lowest_level_raises_the_alarm() -> None:
    found = heuristic.reasons([alert(heuristic.MIN_LEVEL)], [])
    assert len(found) == 1 and "rule 203" in found[0]


@pytest.mark.parametrize(
    ("source", "target", "unknown"),
    [
        ("10.10.0.10", "10.20.0.10", []),
        ("10.10.0.20", "10.74.0.20", ["10.10.0.20", "10.74.0.20"]),
        ("10.30.0.50", "10.30.0.2", ["10.30.0.50"]),
        ("10.40.0.10", "224.0.0.251", []),
        ("10.10.0.10", "10.10.0.255", []),
        ("0.0.0.0", "255.255.255.255", []),
        ("fe80::216:3eff:fe20:b5b9", "ff02::2", []),
    ],
)
def test_only_an_ipv4_host_outside_the_address_plan_is_unknown(
    source: str, target: str, unknown: list[str]
) -> None:
    assert list(heuristic.unknown_addresses(flow(source, target))) == unknown


def test_a_record_without_addresses_names_no_unknown_address() -> None:
    assert list(heuristic.unknown_addresses({"location": heuristic.INSPECTION})) == []


def test_the_telemetry_specification_declares_exactly_the_defenders_reads() -> None:
    document = json.loads((BASELINE / "telemetry.json").read_text())
    assert document == {
        "reads": [
            {"output": "alerts"},
            {"output": "archives", "input": heuristic.INSPECTION},
        ]
    }
    telemetry = protocol.Telemetry.from_json(document)
    assert telemetry.allows("alerts", None, None)
    assert telemetry.allows("archives", None, heuristic.INSPECTION)
    assert not telemetry.allows("archives", None, None)


def test_the_program_raises_the_alarm_at_the_first_unknown_address(
    tmp_path: Path,
) -> None:
    """Two stages, released at 12 and 30 seconds; packet inspection's record
    of an unknown host arrives at 21 seconds and is read at step 5."""
    episode = write_episode(
        tmp_path / "e",
        [(8.0, 1, None), (21.0, 2, None)],
        K=2,
        releases=(12.0, 30.0),
    )
    archives = (episode / "export" / "archives.json").read_text().splitlines()
    unknown = json.loads(archives[1])
    unknown["data"] = {
        "event_type": "flow",
        "src_ip": "10.10.0.20",
        "dest_ip": "10.74.0.20",
    }
    known = json.loads(archives[0])
    known["data"] = {
        "event_type": "flow",
        "src_ip": "10.10.0.10",
        "dest_ip": "10.20.0.10",
    }
    (episode / "export" / "archives.json").write_text(
        json.dumps(known) + "\n" + json.dumps(unknown) + "\n"
    )
    result = replay.replay_fresh(str(BASELINE), str(episode), False, None)
    assert result["problem"] is None and result["defender_exit_status"] == 0
    assert result["alarm"]["step"] == 5
    assert (result["k_n"], result["R_n"], result["outcome"]) == (1, 0.0, 1)
    assert "10.10.0.20" in result["stderr"]
    assert result["undeclared_reads"] == []
    assert line  # the shared fixture's record writer
