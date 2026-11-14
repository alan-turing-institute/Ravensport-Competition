"""The learning Track A defender (competition/baselines/track_a/learning/).

The tests check the tokens that describe each record, the weights and the
score of the model, the rule that decides which episodes the defender learns
from, the trainer's calibration, and the program itself in replays of small
episodes in evaluation mode and in training mode.

Run with: pytest competition/tests -q
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPOSITORY = Path(__file__).resolve().parents[2]
BASELINE = REPOSITORY / "competition" / "baselines" / "track_a" / "learning"
COMMON = BASELINE.parent / "common"
if str(COMMON) not in sys.path:
    sys.path.insert(0, str(COMMON))

import replay  # noqa: E402


def _load(name: str, path: Path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


learning = _load("learning_defender", BASELINE / "defender")
train = _load("learning_train", BASELINE / "train.py")

from test_track_a_replay import discarded_record, write_episode  # noqa: E402

INSPECTION = "/var/log/suricata/eve.json"


def flow(
    source: str, target: str, proto: str = "TCP", port: int | None = 10001
) -> dict:
    data = {"event_type": "flow", "src_ip": source, "dest_ip": target, "proto": proto}
    data.update({"in_iface": "ext0", "flow": {"state": "closed", "reason": "timeout"}})
    if port is not None:
        data["dest_port"] = str(port)
    return {"agent": {"name": "fw-ext-dmz"}, "location": INSPECTION, "data": data}


def test_a_flow_is_described_by_its_hosts_service_and_end() -> None:
    assert sorted(learning.tokens(flow("10.10.0.10", "10.20.0.10"))) == [
        "flow-end|fw-ext-dmz|ext0|TCP|closed|timeout",
        "flow|fw-ext-dmz|ext0|TCP|10.10.0.10|10.20.0.10|10001",
        "peer|10.10.0.10",
        "peer|10.20.0.10",
    ]


@pytest.mark.parametrize(
    ("text", "token"),
    [
        ("10.74.0.20", "10.74.0.20"),
        ("fe80::216:3eff:fe20:b5b9", "v6-link-local"),
        ("ff02::1:ff20:b5b9", "v6-solicited-node"),
        ("ff02::2", "ff02::2"),
        ("::", "::"),
        ("", "-"),
    ],
)
def test_an_address_that_depends_on_a_hardware_address_becomes_a_class(
    text: str, token: str
) -> None:
    assert learning.address(text) == token


def test_an_ephemeral_port_names_no_service() -> None:
    assert learning.port("TCP", 443) == "443"
    assert learning.port("UDP", 51234) == "ephemeral"
    assert learning.port("ICMP", 0) == "-"


def test_a_message_keeps_its_wording_and_loses_its_variable_parts() -> None:
    text = (
        "\x1b[2m2026-11-12T16:09:41.956Z\x1b[0m INFO vessel RFID validated: "
        "card=4411 job=0b9a6c1e-77d2-4c3e-9f6a-2c1d3e4f5a6b"
    )
    assert learning.template(text) == (
        "#-#-#T#:#:#.#Z INFO vessel RFID validated: card=# job=<id>"
    )


def test_a_burst_of_records_from_one_source_has_a_token() -> None:
    quiet = learning.step_tokens([flow("10.10.0.10", "10.20.0.10")] * 31)
    assert not [t for t in quiet if t.startswith("volume|")]
    burst = learning.step_tokens([flow("10.10.0.10", "10.20.0.10")] * 70)
    assert burst["volume|fw-ext-dmz|/var/log/suricata/eve.json|6"] == 1
    assert burst["volume|all|all|6"] == 1


def model(counts: dict[str, int], steps: int = 1000, threshold: float = 1.5):
    return learning.Model(
        {
            "steps": steps,
            "counts": counts,
            "alpha": 0.5,
            "q": 0.0005,
            "threshold": threshold,
        }
    )


def test_a_token_that_no_benign_step_showed_weighs_1_and_a_common_one_0() -> None:
    m = model({"common": 400})
    assert m.weight("never seen") == 1.0
    assert m.weight("common") == 0.0


def test_a_rare_token_weighs_less_than_an_unseen_one() -> None:
    m = model({"rare": 0}, steps=100_000)
    rare = model({"rare": 2}, steps=100_000).weight("rare")
    assert 0 < rare < m.weight("rare") == 1.0


def test_a_step_counts_the_weightiest_token_of_each_group_once() -> None:
    m = model({})
    score, top = m.score(
        [
            "flow|fw-ext-dmz|ext0|ICMP|10.10.0.20|10.74.0.20|-",
            "flow|fw-ext-dmz|ext0|ICMP|10.10.0.20|10.74.0.21|-",
            "peer|10.10.0.20",
            "peer|10.74.0.20",
        ]
    )
    assert score == 2.0
    assert [learning.group(t) for _, t in top] == ["peer", "flow|fw-ext-dmz"]


@pytest.mark.parametrize(
    ("alarmed", "reward", "free"),
    [
        (False, 0.0, True),
        (True, -1.0, True),
        (False, -1.0, False),
        (True, 0.0, False),
        (True, -0.5, False),
        (False, None, False),
    ],
)
def test_only_an_episode_free_of_attack_is_learned(
    alarmed: bool, reward: float | None, free: bool
) -> None:
    assert learning.attack_free(alarmed, reward) is free


def test_learning_adds_each_step_once_per_token(tmp_path: Path) -> None:
    m = model({"a": 1}, steps=10)
    assert m.learn([["a", "b", "b"], ["b"]]) == 2
    assert (m.steps, m.counts) == (12, {"a": 2, "b": 2})
    m.save(tmp_path / "model.json")
    again = learning.Model.load(tmp_path / "model.json")
    assert (again.steps, again.counts, again.threshold) == (12, {"a": 2, "b": 2}, 1.5)
    assert (tmp_path / "model.json").stat().st_mode & 0o777 == 0o644


def test_the_shipped_model_holds_its_calibrated_threshold() -> None:
    shipped = learning.Model.load(BASELINE / "model.json")
    assert (shipped.alpha, shipped.q) == (train.ALPHA, train.Q)
    calibration = shipped.trained["calibration"]
    assert shipped.threshold == calibration["threshold"]
    assert (
        calibration["highest_benign_score"]
        < shipped.threshold
        < calibration["lowest_attack_score"]
    )
    assert shipped.steps == shipped.trained["offline_steps"] > 0
    assert all(n > 0 for n in shipped.counts.values())


def test_the_calibration_lies_midway_between_benign_and_attack_scores() -> None:
    """Two living ports, each with 3,000 benign steps, one of which holds a
    token of its own, and one adversarial episode with a step of two unseen
    tokens after its first release at 10 seconds."""
    ports = {
        port: {
            "benign": [[["common", f"own {port}"]], [["common"]] * 3000],
            "adversarial": [
                {
                    "path": f"attack {port}",
                    "first_release": 10.0,
                    "steps": [(5.0, ["common"]), (15.0, ["peer|x", "flow|y|z"])],
                }
            ],
        }
        for port in ("p", "q")
    }
    calibration = train.calibrate(learning, ports, alpha=0.5, q=0.0005)
    assert calibration["highest_benign_score"] == 1.0
    assert calibration["lowest_attack_score"] == 2.0
    assert calibration["threshold"] == 1.5


def test_the_trainer_leaves_out_a_discarded_episode(tmp_path: Path) -> None:
    """A discarded episode, such as one whose chain's binary the adversary
    host lacked, holds no steps and no history to learn from."""
    record = discarded_record(tmp_path / "e")
    data = json.loads((record / "record.json").read_text())
    data.update(
        {
            "living_port_seed": "00" * 32,
            "living_port": {"started_at": "2026-11-13T00:00:00Z"},
            "forked_at": "2026-11-13T01:00:00Z",
        }
    )
    (record / "record.json").write_text(json.dumps(data))
    assert train.episode_steps(str(record)) is None
    assert train._histories([str(record)]) == []


def test_the_benign_steps_of_an_adversarial_episode_end_at_the_first_release() -> None:
    item = {
        "kind": "adversarial",
        "first_release": 12.0,
        "steps": [(1.0, ["a"]), (6.0, ["b"]), (11.0, ["c"]), (16.0, ["d"])],
    }
    assert train.benign(item) == [["a"], ["b"], ["c"]]
    assert train.benign({**item, "kind": "benign"}) == [["a"], ["b"], ["c"], ["d"]]


def known_tokens() -> dict[str, int]:
    return {t: 500 for t in learning.step_tokens([flow("10.10.0.10", "10.20.0.10")])}


def episode_with(tmp_path: Path, records: list[dict], **kwargs) -> Path:
    """An episode whose archives hold the given records at 3, 13, 23, ...
    seconds."""
    episode = write_episode(
        tmp_path / "e", [(3.0 + 10 * i, i, None) for i in range(len(records))], **kwargs
    )
    lines = (episode / "export" / "archives.json").read_text().splitlines()
    written = []
    for text, record in zip(lines, records):
        base = json.loads(text)
        base.update(record)
        written.append(json.dumps(base, separators=(",", ":")))
    (episode / "export" / "archives.json").write_text("\n".join(written) + "\n")
    return episode


@pytest.fixture
def entry(tmp_path: Path) -> Path:
    """The learning baseline with a small model in place of the shipped one."""
    directory = tmp_path / "baseline"
    directory.mkdir()
    for name in ("defender", "telemetry.json"):
        (directory / name).write_bytes((BASELINE / name).read_bytes())
    model(known_tokens()).save(directory / "model.json")
    return directory


def test_the_program_raises_the_alarm_at_an_unseen_host(
    tmp_path: Path, entry: Path
) -> None:
    episode = episode_with(
        tmp_path,
        [
            flow("10.10.0.10", "10.20.0.10"),
            flow("10.10.0.20", "10.74.0.20", "ICMP", None),
        ],
        K=2,
        releases=(8.0, 30.0),
    )
    result = replay.replay_fresh(str(entry), str(episode), False, None)
    assert result["problem"] is None and result["defender_exit_status"] == 0
    assert result["alarm"]["step"] == 3
    assert (result["k_n"], result["R_n"]) == (1, 0.0)
    assert "peer|10.10.0.20" in result["stderr"]
    assert result["undeclared_reads"] == []


def test_in_training_mode_a_benign_episode_without_an_alarm_is_learned(
    tmp_path: Path, entry: Path
) -> None:
    episode = episode_with(tmp_path, [flow("10.10.0.10", "10.20.0.10")], steps=6)
    volume = replay.assemble(entry, tmp_path / "volume", read_only=False)
    (result,) = replay.replay_lineage(str(volume), [str(episode)], None)
    assert (result["R_n"], result["alarm"]) == (0.0, None)
    learned = learning.Model.load(volume / "model.json")
    assert learned.steps == 1000 + 6
    assert learned.trained["online_steps"] == 6
    assert learned.counts["peer|10.10.0.10"] == 500 + 1


def test_an_end_notice_without_a_reward_teaches_nothing(
    tmp_path: Path, entry: Path
) -> None:
    """Evaluation mode's end notice carries no reward. Even on a writable
    volume, a benign episode whose end notice holds no reward adds nothing to
    the model, so the defender reads no reward outside training mode."""
    episode = episode_with(tmp_path, [flow("10.10.0.10", "10.20.0.10")], steps=6)
    volume = replay.assemble(entry, tmp_path / "volume", read_only=False)
    before = (volume / "model.json").read_bytes()
    result = replay.replay(
        replay.load_episode(episode), volume, tmp_path / "run", training=False
    )
    assert result["problem"] is None and result["defender_exit_status"] == 0
    assert (result["R_n"], result["alarm"]) == (0.0, None)
    assert (volume / "model.json").read_bytes() == before
    assert "learned" not in result["stderr"]


def test_in_evaluation_mode_the_model_is_not_written(
    tmp_path: Path, entry: Path
) -> None:
    episode = episode_with(tmp_path, [flow("10.10.0.10", "10.20.0.10")], steps=6)
    volume = replay.assemble(entry, tmp_path / "volume", read_only=True)
    before = (volume / "model.json").read_bytes()
    result = replay.replay(replay.load_episode(episode), volume, tmp_path / "run")
    assert result["problem"] is None and result["defender_exit_status"] == 0
    assert (volume / "model.json").read_bytes() == before
    replay.make_writable(volume)
