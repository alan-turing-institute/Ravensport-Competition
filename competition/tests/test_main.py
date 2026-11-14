"""The episode runner's command line.

Run with: pytest competition/tests -q
"""

from __future__ import annotations

import dataclasses

import json
import logging
import stat
from datetime import datetime, timezone
from pathlib import Path

import pytest

from competition.episode_runner import __main__ as cli
from competition.episode_runner import chains, episode, fork, predicted_events

SEED = "6f1d8c2a9b3e4f5061728394a5b6c7d8e9fa0b1c2d3e4f5061728394a5b6c7d8"


def test_the_defaults_run_one_episode_into_the_records_directory() -> None:
    args = cli.parse(["run", "--track", "A"])
    assert (args.episodes, args.track, args.seed) == (1, "A", None)
    assert args.records == cli.RECORDS
    assert args.settle_timeout == 120.0


def test_the_track_is_required() -> None:
    with pytest.raises(SystemExit):
        cli.parse(["run"])


def test_a_seed_is_64_hexadecimal_digits() -> None:
    assert cli.parse(["run", "--track", "B", "--seed", SEED.upper()]).seed == SEED
    with pytest.raises(SystemExit):
        cli.parse(["run", "--track", "B", "--seed", SEED[:-2]])


def test_a_seed_runs_one_episode() -> None:
    with pytest.raises(SystemExit):
        cli.parse(["run", "--track", "A", "--seed", SEED, "--episodes", "2"])


def test_the_run_command_refuses_training_mode(capsys) -> None:
    """Training runs only in forks of a living port (episodes.md §3.2,
    modes)."""
    assert cli.parse(["run", "--track", "A"]).mode == "evaluation"
    with pytest.raises(SystemExit):
        cli.parse(["run", "--track", "A", "--mode", "training"])
    assert "training runs only in forks of a living port" in capsys.readouterr().err


class Runs:
    """A stand-in for run_episode, which records each call and returns the
    given outcomes, each with its cause, in turn."""

    def __init__(
        self,
        failure: Exception | None = None,
        outcomes: list[tuple[str, str | None]] | None = None,
    ) -> None:
        self.failure = failure
        self.outcomes = outcomes
        self.calls: list[dict] = []

    def __call__(self, testbed, scenario, seed, track, records, **options):
        self.calls.append({"seed": seed, "track": track, "records": records, **options})
        if self.failure:
            raise self.failure
        outcome, cause = (
            self.outcomes[len(self.calls) - 1] if self.outcomes else ("horizon", None)
        )
        return episode.Outcome(outcome, cause, records / f"episode-{len(self.calls)}")


def main(argv: list[str], runs: Runs, **options) -> int:
    options.setdefault("with_adversary", lambda: False)
    options.setdefault("review", lambda catalogue: None)
    options.setdefault("build_helper", lambda: None)
    options.setdefault("draws", lambda seed, timings: POLLING)
    return cli.main(
        argv, testbed=object(), scenario=object(), run_episode=runs, **options
    )


def test_the_episodes_run_in_sequence(tmp_path: Path, capsys) -> None:
    runs = Runs()
    argv = ["run", "--track", "A", "--episodes", "3", "--records", str(tmp_path)]
    assert main(argv + ["--settle-timeout", "300"], runs) == 0
    seeds = [c["seed"] for c in runs.calls]
    assert len(set(seeds)) == 3 and all(len(seed) == 64 for seed in seeds)
    assert all(c["settle_timeout_s"] == 300.0 for c in runs.calls)
    assert all(c["records"] == tmp_path for c in runs.calls)
    assert capsys.readouterr().out.splitlines() == [
        f"{tmp_path / f'episode-{n}'}: horizon" for n in (1, 2, 3)
    ]


def test_a_discarded_episode_is_recorded_with_its_cause_and_not_run_again(
    tmp_path: Path, capsys
) -> None:
    """The episode's record states the cause, and the next episode runs with
    a seed of its own (episodes.md §12, terminated and discarded episodes)."""
    cause = "the deployment failed to settle: condition 3 still failed"
    runs = Runs(outcomes=[("discarded", cause), ("horizon", None)])
    argv = ["run", "--track", "A", "--episodes", "2", "--records", str(tmp_path)]
    assert main(argv, runs) == 0
    first, second = (c["seed"] for c in runs.calls)
    assert first != second
    assert capsys.readouterr().out.splitlines() == [
        f"{tmp_path / 'episode-1'}: discarded ({cause})",
        f"{tmp_path / 'episode-2'}: horizon",
    ]


def test_a_replay_takes_its_seed(tmp_path: Path) -> None:
    runs = Runs()
    assert (
        main(["run", "--track", "B", "--seed", SEED, "--records", str(tmp_path)], runs)
        == 0
    )
    assert [c["seed"] for c in runs.calls] == [SEED]


def test_a_failed_reset_stops_the_episode_runner(tmp_path: Path, capsys) -> None:
    runs = Runs(episode.ResetFailure("the reset left RavensPort nodes behind: siem-01"))
    argv = ["run", "--track", "A", "--episodes", "2", "--records", str(tmp_path)]
    assert main(argv, runs) == 1
    assert len(runs.calls) == 1
    assert "siem-01" in capsys.readouterr().err


SAMPLE = Path(__file__).resolve().parent / "data" / "chains-sample.json"
DISCLOSE = "it-foothold__disclose-account-inventory"
EXFILTRATE = "external-informed__exfiltrate-inventory"
_LIVE = {
    e["record"][-8:]: predicted_events.Draws.from_json(e["draws"])
    for e in json.loads(
        (
            Path(__file__).resolve().parent / "data" / "predicted-events-records.json"
        ).read_text()
    )["episodes"]
}
# The draws of a live episode whose supplier polls throughout, and the same
# draws with the supplier's first poll after the horizon, which admit no chain
# that waits for a poll.
POLLING = _LIVE["caf7178c"]
NO_POLL = dataclasses.replace(POLLING, first_poll_after_s=10_000.0)


def built(tmp_path: Path) -> Path:
    """A directory holding a binary for every chain of the sample."""
    dist = tmp_path / "dist"
    dist.mkdir()
    for chain in chains.load(SAMPLE):
        (dist / chain.id).write_bytes(b"\x7fELF")
    return dist


def test_a_chain_makes_the_episodes_adversarial() -> None:
    assert cli.parse(["run", "--track", "A"]).type == "benign"
    assert cli.parse(["run", "--track", "A", "--chain", DISCLOSE]).type == "adversarial"
    assert cli.parse(["run", "--track", "A", "--type", "adversarial"]).chain is None
    with pytest.raises(SystemExit):
        cli.parse(["run", "--track", "A", "--type", "benign", "--chain", DISCLOSE])


def test_each_adversarial_episode_draws_a_chain_of_the_library(tmp_path: Path) -> None:
    runs = Runs()
    argv = ["run", "--track", "A", "--type", "adversarial", "--episodes", "20"]
    options = {"catalogue": SAMPLE, "dist": built(tmp_path)}
    assert main(argv + ["--records", str(tmp_path)], runs, **options) == 0
    assert {c["chain"].id for c in runs.calls} == {DISCLOSE, EXFILTRATE}
    assert all(c["episode_type"] == "adversarial" for c in runs.calls)
    chosen = runs.calls[0]["chosen"]
    assert chosen["by"] == "a uniform draw from the chains the episode admits"
    assert chosen["library"] == [DISCLOSE, EXFILTRATE]
    assert chosen["admitted"] == [DISCLOSE, EXFILTRATE]
    assert chosen["seeds_drawn"] == 1
    # The seed comes first.
    assert all(len(c["seed"]) == 64 for c in runs.calls)
    assert "new_seed" not in runs.calls[0]


def test_a_named_chain_takes_fresh_seeds_until_one_admits_it(tmp_path: Path) -> None:
    supplies = iter([NO_POLL, NO_POLL, POLLING])
    runs = Runs()
    argv = ["run", "--track", "A", "--chain", EXFILTRATE, "--records", str(tmp_path)]
    options = {
        "catalogue": SAMPLE,
        "dist": built(tmp_path),
        "draws": lambda s, t: next(supplies),
    }
    assert main(argv, runs, **options) == 0
    assert runs.calls[0]["chain"].id == EXFILTRATE
    assert runs.calls[0]["chosen"]["seeds_drawn"] == 3


def test_a_given_chain_runs_in_each_episode(tmp_path: Path) -> None:
    runs = Runs()
    argv = ["run", "--track", "B", "--chain", EXFILTRATE, "--episodes", "2"]
    options = {"catalogue": SAMPLE, "dist": built(tmp_path)}
    assert main(argv + ["--records", str(tmp_path)], runs, **options) == 0
    assert [c["chain"].id for c in runs.calls] == [EXFILTRATE, EXFILTRATE]
    assert runs.calls[0]["chosen"]["by"] == "the command line"


def test_a_chain_outside_the_library_is_refused(tmp_path: Path, capsys) -> None:
    runs = Runs()
    argv = [
        "run",
        "--track",
        "A",
        "--chain",
        "it-foothold__exfiltrate-replica-telemetry-1",
    ]
    options = {"catalogue": SAMPLE, "dist": built(tmp_path)}
    assert main(argv, runs, **options) == 2
    assert runs.calls == []
    assert "outside the library: it needs audit_running" in capsys.readouterr().err


def test_the_episode_runner_needs_every_binary_while_the_adversary_host_is_deployed(
    tmp_path: Path, capsys
) -> None:
    """The adversary host's own image holds every binary in every episode,
    so a benign episode needs them too (episodes.md §6.5, adversary host)."""
    runs = Runs()
    dist = built(tmp_path)
    (dist / DISCLOSE).unlink()
    options = {"catalogue": SAMPLE, "dist": dist, "with_adversary": lambda: True}
    assert main(["run", "--track", "A"], runs, **options) == 2
    assert runs.calls == []
    assert f"no binary in {dist} for {DISCLOSE}" in capsys.readouterr().err
    assert main(["run", "--track", "A"], runs, catalogue=SAMPLE, dist=dist) == 0


def test_a_changed_attack_source_refuses_adversarial_episodes(
    tmp_path: Path, capsys
) -> None:
    def changed(catalogue: Path) -> None:
        raise chains.CatalogueError("the attack chains' source has changed")

    runs = Runs()
    argv = ["run", "--track", "A", "--type", "adversarial"]
    options = {"catalogue": SAMPLE, "dist": built(tmp_path), "review": changed}
    assert main(argv, runs, **options) == 2
    assert runs.calls == []
    assert "the attack chains' source has changed" in capsys.readouterr().err
    # A benign episode chooses no chain, so it runs.
    assert main(["run", "--track", "A"], runs, **options) == 0


# The `copy` command (the living-port contract).

STARTED = datetime(2026, 11, 11, 20, 0, tzinfo=timezone.utc)
LIVING = fork.LivingPort("ab" * 32, 7, STARTED, Path("startup.json"), {})


def copy_argv(out: Path, *extra: str) -> list[str]:
    return [
        "copy",
        "--fork",
        "3",
        "--forked-at",
        "2026-11-11T20:45:00.500Z",
        "--track",
        "A",
        "--type",
        "benign",
        "--out",
        str(out),
        *extra,
    ]


class Copies:
    """A stand-in for run_copy_episode, which records each call."""

    def __init__(self, outcome: str = "horizon", failure: Exception | None = None):
        self.outcome, self.failure = outcome, failure
        self.calls: list[dict] = []

    def __call__(self, testbed, scenario, forked, track, out, **options):
        self.calls.append({"fork": forked, "track": track, "out": out, **options})
        logging.getLogger("competition.episode_runner.episode").info("an episode")
        if self.failure:
            raise self.failure
        (out / "record.json").write_text("{}\n")
        return episode.Outcome(self.outcome, None, out)


def copy_main(argv: list[str], copies: Copies, **options) -> int:
    options.setdefault("with_adversary", lambda: False)
    options.setdefault("review", lambda catalogue: None)
    options.setdefault("build_helper", lambda: None)
    options.setdefault("read_identity", lambda path: LIVING)
    return cli.main(
        argv, testbed=object(), scenario=object(), run_copy=copies, **options
    )


def test_the_copy_command_takes_the_contracts_arguments(tmp_path: Path) -> None:
    args = cli.parse(copy_argv(tmp_path / "out"))
    assert (args.command, args.fork, args.track, args.type) == (
        "copy",
        3,
        "A",
        "benign",
    )
    assert args.forked_at == datetime(2026, 11, 11, 20, 45, 0, 500000, timezone.utc)
    assert args.identity == fork.IDENTITY
    assert args.settle_timeout == 120.0
    for wrong in (["--fork", "0"], ["--forked-at", "2026-11-11T20:45:00"]):
        argv = copy_argv(tmp_path)
        argv[argv.index(wrong[0]) + 1] = wrong[1]
        with pytest.raises(SystemExit):
            cli.parse(argv)
    with pytest.raises(SystemExit):
        cli.parse(copy_argv(tmp_path, "--chain", DISCLOSE))


def test_the_run_command_takes_the_entrys_volume(tmp_path: Path, capsys) -> None:
    """--volume DIR gives the entry's submitted volume to each episode at fork
    0, attached read-only at /var/lib/entry (episodes.md §3.2, modes)."""
    runs = Runs()
    volume = tmp_path / "volume"
    volume.mkdir(mode=0o700)
    argv = ["run", "--track", "A", "--records", str(tmp_path)]
    assert main(argv, runs) == 0
    assert main(argv + ["--volume", str(volume)], runs) == 0
    assert [c["entry_volume"] for c in runs.calls] == [None, volume.resolve()]
    # The volume opens to siem-01's users before any episode, as a directory
    # that pytest made with mode 0700 needs.
    assert stat.S_IMODE(volume.stat().st_mode) == 0o755
    with pytest.raises(SystemExit):
        cli.parse(argv + ["--volume", str(tmp_path / "absent")])
    assert "--volume takes a directory" in capsys.readouterr().err


def test_only_a_run_marked_as_a_replay_is_suppressed(tmp_path: Path) -> None:
    """Without --suppressed the episode runner carries out each decision in
    either mode (episodes-tests.md, only_a_run_marked_as_a_replay_is_suppressed)."""
    runs = Runs()
    argv = ["run", "--track", "A", "--records", str(tmp_path)]
    assert main(argv, runs) == 0
    assert main(argv + ["--suppressed"], runs) == 0
    assert [c["suppressed"] for c in runs.calls] == [False, True]
    copies = Copies()
    assert copy_main(copy_argv(tmp_path / "plain"), copies) == 0
    assert copy_main(copy_argv(tmp_path / "replay", "--suppressed"), copies) == 0
    training = copy_argv(
        tmp_path / "training",
        "--mode",
        "training",
        "--training-volume",
        "default/training",
    )
    assert copy_main(training, copies) == 0
    assert [c["suppressed"] for c in copies.calls] == [False, True, False]


def test_a_suppressed_copy_runs_in_evaluation_mode_alone(
    tmp_path: Path, capsys
) -> None:
    """A suppressed episode is an episode of a replay for Competition Rule 5,
    in evaluation mode, so the `copy` command refuses --suppressed in
    training mode (episodes.md §2, terms)."""
    training = copy_argv(
        tmp_path / "out",
        "--mode",
        "training",
        "--training-volume",
        "default/training",
        "--suppressed",
    )
    with pytest.raises(SystemExit) as refused:
        cli.parse(training)
    assert refused.value.code == 2
    assert "--suppressed runs in evaluation mode" in capsys.readouterr().err
    assert cli.parse(copy_argv(tmp_path / "out", "--suppressed")).suppressed


def test_a_training_copy_takes_the_managers_volume(tmp_path: Path) -> None:
    argv = copy_argv(tmp_path / "out", "--mode", "training")
    assert cli.parse(copy_argv(tmp_path)).mode == "evaluation"
    with pytest.raises(SystemExit):
        cli.parse(argv)
    with pytest.raises(SystemExit):
        cli.parse(copy_argv(tmp_path, "--training-volume", "default/training"))
    copies = Copies()
    assert copy_main(argv + ["--training-volume", "default/training"], copies) == 0
    (call,) = copies.calls
    assert call["mode"] == "training"
    assert str(call["training_volume"]) == "default/training"


def test_a_copy_runs_one_episode_and_logs_to_its_directory(
    tmp_path: Path, capsys
) -> None:
    copies = Copies()
    out = tmp_path / "episodes" / "fork-3"
    before = list(logging.getLogger().handlers)
    assert copy_main(copy_argv(out), copies) == 0
    (call,) = copies.calls
    assert call["fork"] == episode.Fork(
        LIVING, 3, datetime(2026, 11, 11, 20, 45, 0, 500000, timezone.utc)
    )
    assert (call["track"], call["out"], call["episode_type"]) == ("A", out, "benign")
    assert (call["library"], call["given"]) == ([], None)
    assert call["settle_timeout_s"] == 120.0
    assert capsys.readouterr().out == f"{out}: horizon\n"
    runner_log = (out / cli.RUNNER_LOG).read_text()
    assert "fork 3 of the living port abababab, 2700 s after its start" in runner_log
    assert "an episode" in runner_log
    assert logging.getLogger().handlers == before


def test_a_copy_that_admits_no_chain_still_ends_with_its_record(tmp_path: Path) -> None:
    copies = Copies(outcome="not admitted")
    argv = copy_argv(tmp_path / "out")
    argv[argv.index("benign")] = "adversarial"
    options = {"catalogue": SAMPLE, "dist": built(tmp_path)}
    assert copy_main(argv + ["--chain", EXFILTRATE], copies, **options) == 0
    (call,) = copies.calls
    assert call["given"].id == EXFILTRATE
    assert [c.id for c in call["library"]] == [DISCLOSE, EXFILTRATE]


@pytest.mark.parametrize(
    "problem",
    ["identity", "record", "fork_instant", "chain"],
)
def test_a_copy_that_cannot_start_writes_no_record(
    tmp_path: Path, problem: str
) -> None:
    copies = Copies()
    out = tmp_path / "out"
    argv = copy_argv(out)
    options: dict = {"catalogue": SAMPLE, "dist": built(tmp_path)}
    if problem == "identity":

        def unreadable(path: Path) -> fork.LivingPort:
            raise fork.ForkError(f"cannot read the identity file {path}")

        options["read_identity"] = unreadable
    elif problem == "record":
        out.mkdir()
        (out / "record.json").write_text('{"fork": 3}\n')
    elif problem == "fork_instant":
        argv[argv.index("2026-11-11T20:45:00.500Z")] = "2026-11-11T19:59:00Z"
    else:
        argv[argv.index("benign")] = "adversarial"
        argv += ["--chain", "it-foothold__exfiltrate-replica-telemetry-1"]
    assert copy_main(argv, copies, **options) == 2
    assert copies.calls == []
    if problem == "record":
        assert (out / "record.json").read_text() == '{"fork": 3}\n'
    else:
        assert not (out / "record.json").exists()


def test_a_runner_that_fails_during_the_episode_exits_with_status_1(
    tmp_path: Path,
) -> None:
    out = tmp_path / "out"
    assert copy_main(copy_argv(out), Copies(failure=RuntimeError("a defect"))) == 1
    assert "RuntimeError: a defect" in (out / cli.RUNNER_LOG).read_text()
