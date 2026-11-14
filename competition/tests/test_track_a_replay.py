"""The offline replay of exported episodes through a Track A defender
(competition/baselines/track_a/common/replay.py; episodes.md §3.2, modes;
§7, step protocol; §11, horizon and termination; §13, episode record).

Each test writes a small episode record: the episode's file, steps.jsonl of
5-second steps, and the exported files of the security monitoring, whose
records carry the Wazuh manager's timestamps. A test defender reads
archives.json at each step through the replay's query interface, notes on its
standard error the records it received, and raises the alarm at a record
marked as the attack's.

Run with: pytest competition/tests -q
"""

from __future__ import annotations

import gzip
import json
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

COMMON = Path(__file__).resolve().parents[1] / "baselines" / "track_a" / "common"
import sys  # noqa: E402

if str(COMMON) not in sys.path:
    sys.path.insert(0, str(COMMON))

import replay  # noqa: E402

# Step 0 of every test episode: 2026-11-12T16:00:00Z on the clock of siem-01.
T0 = 1_794_499_200.0
REQUEST_DELAY_S = 0.01
DEFENDER = """#!/usr/bin/env python3
import json, sys
import protocol

def decide(request):
    records = protocol.read("archives")
    numbers = [r.get("n") for r in records]
    print("step", request.step, json.dumps(numbers), file=sys.stderr, flush=True)
    return any(r.get("marker") == "attack" for r in records)

sys.exit(protocol.run(decide))
"""


def utc(t: float) -> str:
    return datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def manager_time(t: float) -> str:
    """A timestamp as the Wazuh manager writes it, such as
    2026-11-12T16:00:02.000+0000."""
    stamp = datetime.fromtimestamp(t, UTC)
    return (
        stamp.strftime("%Y-%m-%dT%H:%M:%S.") + f"{stamp.microsecond // 1000:03d}+0000"
    )


def line(t: float, n: int, marker: str | None = None) -> str:
    record = {"timestamp": manager_time(T0 + t), "agent": {"name": "fw-ext-dmz"}}
    record.update({"location": "/var/log/suricata/eve.json", "n": n})
    if marker:
        record["marker"] = marker
    return json.dumps(record, separators=(",", ":")) + "\n"


def write_episode(
    path: Path,
    archives: list[tuple[float, int, str | None]],
    K: int | None = None,
    releases: tuple[float, ...] = (),
    steps: int = 20,
    dated: bool = False,
    track_a: dict | None = None,
) -> Path:
    """An episode record at path: adversarial with K stages released at the
    given times, or benign without K. With `dated`, the export holds the
    dated files below alerts/ and archives/, split at midnight; otherwise the
    single files alerts.json and archives.json."""
    path.mkdir(parents=True)
    record: dict = {"type": "benign" if K is None else "adversarial", "track": "A"}
    record.update({"outcome": "horizon", "steps": {"ended_early": None}})
    if K is not None:
        record["attack"] = {
            "chain": {
                "id": "test-chain",
                "stages": [{"name": f"s{i}"} for i in range(K)],
            },
            "session": {"releases": [utc(T0 + r) for r in releases]},
        }
    if track_a is not None:
        record["track_a"] = track_a
    (path / "record.json").write_text(json.dumps(record))
    with open(path / "steps.jsonl", "w") as f:
        for k in range(steps):
            start = T0 + 5 * k
            f.write(
                json.dumps(
                    {
                        "step": k,
                        "start": utc(start),
                        "end": utc(start + 5),
                        "request_sent": utc(start + REQUEST_DELAY_S),
                    }
                )
                + "\n"
            )
    export = path / "export"
    lines = [(T0 + t, line(t, n, m)) for t, n, m in archives]
    if not dated:
        export.mkdir()
        (export / "alerts.json").write_text("")
        (export / "archives.json").write_text("".join(text for _, text in lines))
        return path
    days: dict[str, list[str]] = {}
    for t, text in lines:
        day = datetime.fromtimestamp(t, UTC)
        name = f"{day.year}/{replay.MONTHS[day.month - 1]}/ossec-archive-{day.day:02d}.json"
        days.setdefault(name, []).append(text)
    first = datetime.fromtimestamp(T0, UTC)
    days.setdefault(
        f"{first.year}/{replay.MONTHS[first.month - 1]}/ossec-archive-{first.day:02d}.json",
        [],
    )
    for name, texts in days.items():
        file = export / "archives" / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("".join(texts))
        alerts = export / "alerts" / name.replace("ossec-archive-", "ossec-alerts-")
        alerts.parent.mkdir(parents=True, exist_ok=True)
        alerts.write_text("")
    return path


@pytest.fixture
def baseline(tmp_path: Path) -> Path:
    """A baseline's directory holding the test defender and a telemetry
    specification that declares every read of archives.json."""
    directory = tmp_path / "baseline"
    directory.mkdir()
    (directory / "defender").write_text(DEFENDER)
    (directory / "telemetry.json").write_text(
        json.dumps({"reads": [{"output": "archives"}]})
    )
    return directory


def run(baseline: Path, episode: Path) -> dict:
    result = replay.replay_fresh(str(baseline), str(episode), False, None)
    assert result["problem"] is None, result["stderr"]
    assert result["defender_exit_status"] == 0, result["stderr"]
    return result


def served(result: dict) -> dict[int, list[int]]:
    """The records that the test defender received at each step."""
    found = re.findall(r"step (\d+) (\[[^\]]*\])", result["stderr"])
    return {int(step): json.loads(numbers) for step, numbers in found}


def test_each_record_is_served_once_the_manager_has_written_it(
    tmp_path: Path, baseline: Path
) -> None:
    """The manager writes a record at most WRITE_DELAY_S after its timestamp.
    Record 0 precedes the start of the reads; record 2 is stamped just early
    enough for step 1's request, 5.01 seconds after step 0 began, and record 3
    just too late."""
    delay = replay.WRITE_DELAY_S
    episode = write_episode(
        tmp_path / "e",
        [
            (-1.0 - delay, 0, None),
            (2.0, 1, None),
            (5.005 - delay, 2, None),
            (5.02 - delay, 3, None),
        ],
        steps=4,
    )
    result = run(baseline, episode)
    assert served(result) == {0: [], 1: [1, 2], 2: [3], 3: []}
    assert (result["steps_run"], result["R_n"], result["outcome"]) == (4, 0.0, "none")
    assert result["undeclared_reads"] == []


@pytest.mark.parametrize(
    ("marked_at", "k_n", "R_n", "alarm_step"),
    [(13.0, 1, 0.0, 3), (31.0, 2, -0.5, 7)],
)
def test_an_alarm_scores_by_the_stages_released_before_it(
    tmp_path: Path,
    baseline: Path,
    marked_at: float,
    k_n: int,
    R_n: float,
    alarm_step: int,
) -> None:
    """Three stages, released at 12, 30 and 48 seconds."""
    episode = write_episode(
        tmp_path / "e",
        [(marked_at, 1, "attack")],
        K=3,
        releases=(12.0, 30.0, 48.0),
    )
    result = run(baseline, episode)
    assert result["alarm"]["step"] == alarm_step
    assert (result["k_n"], result["R_n"], result["outcome"]) == (k_n, R_n, 1)
    assert (result["end_cause"], result["steps_run"]) == ("alarm", alarm_step + 1)


def test_an_alarm_before_the_first_release_is_outcome_3(
    tmp_path: Path, baseline: Path
) -> None:
    episode = write_episode(
        tmp_path / "e", [(3.0, 1, "attack")], K=3, releases=(12.0, 30.0, 48.0)
    )
    result = run(baseline, episode)
    assert (result["k_n"], result["R_n"], result["outcome"]) == (0, -1.0, 3)


def test_without_an_alarm_the_episode_ends_when_the_last_stage_falls_due(
    tmp_path: Path, baseline: Path
) -> None:
    """An export made before the Track A alarm existed holds the last stage's
    records, which the replay never serves."""
    episode = write_episode(
        tmp_path / "e",
        [(20.0, 1, None), (49.0, 2, "attack")],
        K=3,
        releases=(12.0, 30.0, 48.0),
    )
    result = run(baseline, episode)
    assert result["alarm"] is None
    assert (result["end_cause"], result["steps_run"]) == ("last_stage", 10)
    assert (result["k_n"], result["R_n"], result["outcome"]) == (3, -1.0, 4)
    assert all(2 not in numbers for numbers in served(result).values())


def test_a_benign_episode_scores_a_false_alarm_or_none(
    tmp_path: Path, baseline: Path
) -> None:
    quiet = run(baseline, write_episode(tmp_path / "quiet", [(7.0, 1, None)]))
    assert (quiet["R_n"], quiet["outcome"], quiet["steps_run"]) == (0.0, "none", 20)
    alarmed = run(baseline, write_episode(tmp_path / "alarm", [(7.0, 1, "attack")]))
    assert (alarmed["R_n"], alarmed["outcome"]) == (-1.0, "false alarm")
    assert alarmed["alarm"]["step"] == 2


def test_a_read_that_the_telemetry_specification_omits_is_reported(
    tmp_path: Path, baseline: Path
) -> None:
    (baseline / "telemetry.json").write_text(
        json.dumps({"reads": [{"output": "alerts"}]})
    )
    result = run(baseline, write_episode(tmp_path / "e", [], steps=2))
    assert result["undeclared_reads"] == [
        {"output": "archives", "host": None, "input": None, "since": None},
        {"output": "archives", "host": None, "input": None, "since": None},
    ]


def test_the_dated_files_are_served_across_midnight(
    tmp_path: Path, baseline: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Step 0 begins 30 seconds before midnight, and the manager writes the
    second record into the next day's dated file."""
    monkeypatch.setattr(sys.modules[__name__], "T0", 1_794_527_970.0)
    episode = write_episode(
        tmp_path / "e", [(10.0, 1, None), (40.0, 2, None)], steps=10, dated=True
    )
    assert (episode / "export/archives/2026/Nov/ossec-archive-13.json").exists()
    result = run(baseline, episode)
    assert served(result)[3] == [1]
    assert served(result)[9] == [2]


# A defender that reads archives.json twice at each step: from step 0, and
# from a start before step 0, SINCE, which reads the security monitoring's
# history.
HISTORY = """#!/usr/bin/env python3
import json, sys
import protocol

def decide(request):
    for since in (None, SINCE):
        numbers = [r.get("n") for r in protocol.read("archives", since=since)]
        print("step", request.step, since or "-", json.dumps(numbers),
              file=sys.stderr, flush=True)
    return False

sys.exit(protocol.run(decide))
"""


def test_a_read_with_a_start_before_step_0_reads_the_history(
    tmp_path: Path, baseline: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Step 0 begins 30 seconds after midnight. Record 1 lies in the previous
    day's dated file, which the manager has compressed, and record 2 in the
    current day's, before step 0. The request with a start before step 0
    reads both at step 0, and keeps a position of its own apart from the
    request without a start, which reads neither."""
    monkeypatch.setattr(sys.modules[__name__], "T0", 1_794_528_030.0)
    episode = write_episode(
        tmp_path / "e",
        [(-90.0, 1, None), (-20.0, 2, None), (7.0, 3, None)],
        steps=4,
        dated=True,
    )
    earlier = episode / "export/archives/2026/Nov/ossec-archive-12.json"
    with gzip.open(f"{earlier}.gz", "wb") as f:
        f.write(earlier.read_bytes())
    earlier.unlink()
    since = utc(T0 - 120.0)
    (baseline / "defender").write_text(HISTORY.replace("SINCE", repr(since)))
    volume = replay.assemble(baseline, tmp_path / "entry", read_only=False)
    result = replay.replay(replay.load_episode(episode), volume, tmp_path / "run")
    assert result["problem"] is None, result["stderr"]
    assert result["defender_exit_status"] == 0, result["stderr"]
    found = re.findall(r"step (\d+) (\S+) (\[[^\]]*\])", result["stderr"])
    reads = {(int(k), start): json.loads(numbers) for k, start, numbers in found}
    assert [reads[k, since] for k in range(4)] == [[1, 2], [], [3], []]
    assert [reads[k, "-"] for k in range(4)] == [[], [], [3], []]
    text = (tmp_path / "run/state/reads.jsonl").read_text()
    entries = [json.loads(x) for x in text.splitlines()]
    first = next(e for e in entries if e["request"]["since"] is not None)
    assert first["request"]["since"] == since
    assert [r["file"] for r in first["ranges"]] == [
        "archives/2026/Nov/ossec-archive-12.json",
        "archives/2026/Nov/ossec-archive-13.json",
    ]


def test_a_day_held_both_as_written_and_compressed_is_read_once_as_written(
    tmp_path: Path,
) -> None:
    """The replay takes a dated file as siem-read opens it."""
    episode = write_episode(tmp_path / "e", [(7.0, 1, None)], steps=4, dated=True)
    written = episode / "export/archives/2026/Nov/ossec-archive-12.json"
    with gzip.open(f"{written}.gz", "wb") as f:
        f.write(written.read_bytes())
    sources = replay.load_episode(episode).sources["archives"]
    assert sources == [("archives/2026/Nov/ossec-archive-12.json", written)]


def test_a_recording_that_its_own_alarm_ended_has_no_outcome_without_an_alarm(
    tmp_path: Path, baseline: Path
) -> None:
    episode = write_episode(
        tmp_path / "e",
        [],
        K=3,
        releases=(12.0,),
        steps=5,
        track_a={"end_cause": "alarm"},
    )
    result = run(baseline, episode)
    assert (result["end_cause"], result["R_n"]) == ("recorded_alarm", None)


# A defender that notes the reward of each step message it receives, and
# raises the alarm at step 7.
REWARDS = """#!/usr/bin/env python3
import json, sys
import protocol

def decide(request):
    print("reward", request.step, json.dumps(request.previous_reward),
          file=sys.stderr, flush=True)
    return request.step == 7

def end(notice):
    print("end", notice.step, json.dumps(notice.previous_reward),
          file=sys.stderr, flush=True)

sys.exit(protocol.run(decide, end))
"""


def rewards(result: dict) -> tuple[list, object]:
    """The rewards that the defender received in each decision request, and
    in the end notice."""
    requests = re.findall(r"reward \d+ (\S+)", result["stderr"])
    (end,) = re.findall(r"end \d+ (\S+)", result["stderr"])
    return [json.loads(r) for r in requests], json.loads(end)


@pytest.mark.parametrize(
    ("training", "expected"),
    [(False, ([None] * 8, None)), (True, ([None] + [0.0] * 7, -0.5))],
    ids=["evaluation", "training"],
)
def test_a_step_message_carries_a_reward_in_training_mode_alone(
    tmp_path: Path, baseline: Path, training: bool, expected: tuple
) -> None:
    """In evaluation mode no step message carries a reward. In training mode
    each decision request after step 0 carries the previous step's reward,
    0 in Track A, and the end notice R_n: here -0.5, for an alarm after the
    second of three releases."""
    (baseline / "defender").write_text(REWARDS)
    episode = write_episode(tmp_path / "e", [], K=3, releases=(12.0, 30.0, 48.0))
    result = replay.replay_fresh(str(baseline), str(episode), training, None)
    assert result["problem"] is None and result["defender_exit_status"] == 0
    assert (result["k_n"], result["R_n"]) == (2, -0.5)
    assert rewards(result) == expected


def test_a_decision_of_continue_is_an_invalid_line(
    tmp_path: Path, baseline: Path
) -> None:
    """Track A's decision without an alarm is no action, and the string
    continue is no decision value."""
    (baseline / "defender").write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "for line in sys.stdin:\n"
        "    if json.loads(line)['ended']:\n"
        "        break\n"
        "    print(json.dumps({'decision': 'continue'}), flush=True)\n"
    )
    result = run(baseline, write_episode(tmp_path / "e", [], steps=4))
    assert (result["invalid_lines"], result["decisions"]) == (4, 0)
    assert (result["alarm"], result["R_n"]) == (None, 0.0)


def discarded_record(path: Path) -> Path:
    """The record of an adversarial episode that the episode runner discarded
    before step 0, since the adversary host lacked the chain's binary."""
    path.mkdir(parents=True)
    record = {
        "type": "adversarial",
        "track": "A",
        "attack": {"chain": {"id": "test-chain", "stages": [{"name": "s0"}] * 3}},
        "outcome": "discarded",
        "cause": "the adversary host holds no executable /root/test-chain",
    }
    (path / "record.json").write_text(json.dumps(record))
    return path


def test_a_discarded_episode_is_unscored_and_runs_no_defender(
    tmp_path: Path, baseline: Path
) -> None:
    """Scoring omits a discarded episode (episodes.md §12, terminated and
    discarded episodes), and the record holds no step to replay."""
    episode = discarded_record(tmp_path / "e")
    result = replay.replay_fresh(str(baseline), str(episode), False, None)
    assert (result["end_cause"], result["R_n"], result["K"]) == ("discarded", None, 3)
    assert result["cause"].endswith("/root/test-chain")
    assert "stderr" not in result
    assert replay.summary([result])["unscored"] == 1
    (lineage,) = replay.replay_lineage(str(baseline), [str(episode)], None)
    assert lineage == result


def test_step_reads_gives_what_the_program_reads(
    tmp_path: Path, baseline: Path
) -> None:
    archives = [(t / 2, n, None) for n, t in enumerate(range(1, 60, 3))]
    episode = write_episode(tmp_path / "e", archives, steps=8)
    by_program = served(run(baseline, episode))
    loaded = replay.load_episode(episode)
    in_process = {
        k: [r["n"] for r in records[0]]
        for k, _, records in replay.step_reads(loaded, [("archives", None, None)])
    }
    assert in_process == by_program
