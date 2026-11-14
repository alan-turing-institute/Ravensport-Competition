#!/usr/bin/env python3
"""Offline training of the learning defender's model (README.md).

    train.py --out MODEL [--alpha A] [--q Q] [--threshold H] [--jobs N] RECORD...

Each RECORD is an exported episode record, which replay.py reads. The trainer
reads each step's records as the defender's reads would return them at the
step's decision request (replay.step_reads), turns them into the defender's
tokens, and counts the benign steps in which each token appears:

- every step of a benign episode;
- the steps of an adversarial episode before its first stage's release;
- the steps of each living port's history: the export of an episode holds the
  living port's records since its start, and the trainer takes 5-second steps
  from SKIP_S after the living port's start to the fork instant of the latest
  record of that living port.

A record whose outcome scoring omits, such as a discarded episode, holds no
steps to learn from, and the trainer leaves it out.

Without --threshold, the trainer sets the threshold by leaving out each living
port in turn, scoring each port's steps under the counts of the other living
ports. The highest score of any benign step bounds the threshold from below.
An adversarial episode's attack score is the score of its first step after
the first release that exceeds that bound. The threshold lies midway between
the highest benign score and the lowest attack score.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import importlib.machinery
import importlib.util
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent / "common")]

import replay  # noqa: E402

ALPHA = 0.5
Q = 0.0005
STEP_S = 5.0
# The start of a living port's history that the trainer leaves out: its
# deployment's start-up, which no episode holds.
SKIP_S = 600.0
REQUESTS = (("alerts", None, None), ("archives", None, None))


def load_defender():
    """The defender's program, loaded as a module for its tokens and model."""
    path = HERE / "defender"
    loader = importlib.machinery.SourceFileLoader("learning_defender", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _time(text: str) -> float:
    return datetime.fromisoformat(text).timestamp()


def _record(path: Path) -> dict[str, Any]:
    for name in ("record.json", "episode.json"):
        if (path / name).is_file():
            return json.loads((path / name).read_text())
    raise FileNotFoundError(f"{path} holds neither record.json nor episode.json")


def episode_steps(path: str) -> dict[str, Any] | None:
    """The tokens of each step of an episode, with the step's time, or None
    for an episode that scoring omits, such as a discarded one."""
    defender = load_defender()
    record = _record(Path(path))
    try:
        episode = replay.load_episode(Path(path))
    except replay.Unscored:
        return None
    steps = [
        (t, sorted(defender.step_tokens(alerts + archives)))
        for _, t, (alerts, archives) in replay.step_reads(episode, REQUESTS)
    ]
    return {
        "path": path,
        "living_port": record.get("living_port_seed") or path,
        "kind": episode.kind,
        "first_release": episode.releases[0] if episode.releases else None,
        "steps": steps,
    }


def history_steps(path: str, start: float, end: float) -> list[list[str]]:
    """The tokens of each 5-second step of a living port's history, from the
    records that the export of the episode at `path` holds."""
    defender = load_defender()
    episode = replay.load_episode(Path(path))
    times = []
    t = start + SKIP_S
    while t + STEP_S <= end:
        times.append(t)
        t += STEP_S
    reads = replay.timed_reads(
        episode.sources, start + SKIP_S - STEP_S, times, REQUESTS
    )
    return [
        sorted(defender.step_tokens(alerts + archives)) for alerts, archives in reads
    ]


def _histories(paths: list[str]) -> list[tuple[str, str, float, float]]:
    """For each living port, its latest record, its start and the fork
    instant of that record."""
    latest: dict[str, tuple[float, str, float]] = {}
    for path in paths:
        record = _record(Path(path))
        if record.get("outcome") in replay.UNSCORED:
            continue
        seed = record.get("living_port_seed")
        started = (record.get("living_port") or {}).get("started_at")
        forked = record.get("forked_at")
        if not (seed and started and forked):
            continue
        fork_at = _time(forked)
        if seed not in latest or fork_at > latest[seed][0]:
            latest[seed] = (fork_at, path, _time(started))
    return [
        (seed, path, start, fork_at) for seed, (fork_at, path, start) in latest.items()
    ]


def benign(item: dict[str, Any]) -> list[list[str]]:
    """The benign steps of an episode: all of a benign episode's, and an
    adversarial episode's before its first release."""
    if item["kind"] == "benign":
        return [tokens for _, tokens in item["steps"]]
    cut = item["first_release"]
    return [tokens for t, tokens in item["steps"] if cut is None or t < cut]


def counts_of(groups: list[list[list[str]]]) -> tuple[Counter[str], int]:
    counts: Counter[str] = Counter()
    steps = 0
    for group in groups:
        for tokens in group:
            counts.update(set(tokens))
            steps += 1
    return counts, steps


def calibrate(
    defender: Any, ports: dict[str, dict[str, Any]], alpha: float, q: float
) -> dict[str, Any]:
    """The threshold, from each living port scored under the counts of the
    others. The highest score of any benign step bounds it from below. The
    attack score of an adversarial episode is the score of its first step
    after the first release whose score exceeds that bound: the earliest step
    that any threshold above the bound could alarm on. The threshold lies
    midway between the highest benign score and the lowest attack score."""
    models = {}
    for left_out in ports:
        others = [
            group
            for seed, port in ports.items()
            if seed != left_out
            for group in port["benign"]
        ]
        counts, steps = counts_of(others)
        models[left_out] = defender.Model(
            {"steps": steps, "counts": counts, "alpha": alpha, "q": q, "threshold": 0.0}
        )
    benign_score = max(
        (
            models[seed].score(tokens)[0]
            for seed, port in ports.items()
            for group in port["benign"]
            for tokens in group
        ),
        default=0.0,
    )
    attack_scores = []
    for seed, port in ports.items():
        for item in port["adversarial"]:
            for t, tokens in item["steps"]:
                score = models[seed].score(tokens)[0]
                if t >= item["first_release"] and score > benign_score:
                    attack_scores.append((score, item["path"]))
                    break
            else:
                print(f"train.py: no step of {item['path']} exceeds the benign scores")
    if not attack_scores:
        raise SystemExit("train.py: no adversarial episode to calibrate against")
    attack_score = min(attack_scores)[0]
    return {
        "threshold": round((benign_score + attack_score) / 2, 3),
        "highest_benign_score": round(benign_score, 3),
        "lowest_attack_score": round(attack_score, 3),
        "adversarial_episodes": len(attack_scores),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="train.py", description="Train the learning defender's model offline."
    )
    parser.add_argument("records", nargs="+", help="exported episode records")
    parser.add_argument("--out", required=True, help="the model file to write")
    parser.add_argument("--alpha", type=float, default=ALPHA)
    parser.add_argument("--q", type=float, default=Q)
    parser.add_argument("--threshold", type=float, help="the threshold, uncalibrated")
    parser.add_argument("--jobs", type=int, default=1)
    args = parser.parse_args(argv)
    defender = load_defender()
    ports: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"benign": [], "adversarial": []}
    )
    with concurrent.futures.ProcessPoolExecutor(args.jobs) as pool:
        histories = _histories(args.records)
        history_jobs = {
            seed: pool.submit(history_steps, path, start, end)
            for seed, path, start, end in histories
        }
        for item in pool.map(episode_steps, args.records):
            if item is None:
                continue
            port = ports[item["living_port"]]
            port["benign"].append(benign(item))
            if item["kind"] == "adversarial" and item["first_release"] is not None:
                port["adversarial"].append(item)
        for seed, job in history_jobs.items():
            ports[seed]["benign"].append(job.result())
    calibration = None
    threshold = args.threshold
    if threshold is None:
        calibration = calibrate(defender, ports, args.alpha, args.q)
        threshold = calibration["threshold"]
    counts, steps = counts_of([g for port in ports.values() for g in port["benign"]])
    model = defender.Model(
        {
            "steps": steps,
            "counts": counts,
            "alpha": args.alpha,
            "q": args.q,
            "threshold": threshold,
            "trained": {
                "living_ports": len(ports),
                "episodes": len(args.records),
                "offline_steps": steps,
                "calibration": calibration,
            },
        }
    )
    model.save(Path(args.out).resolve())
    print(
        json.dumps(
            {"steps": steps, "tokens": len(counts), "threshold": threshold,
             "calibration": calibration}, indent=2
        )
    )  # fmt: skip
    return 0


if __name__ == "__main__":
    sys.exit(main())
