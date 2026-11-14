"""The episode runner's command line.

    python -m competition.episode_runner run --episodes N --track A

runs N benign episodes in sequence, each with the defender of siem-01 at fork
0 of a living port of its own, and keeps one episode record for each episode in
competition/records/. Every episode ends with a reset that tears the
deployment down. The episode runner carries out each decision, and in Track A
the alarm ends the episode. With --suppressed, which a replay for Competition
Rule 5 gives, it records each decision and carries none out (episodes.md §9,
suppressed episodes for Competition Rule 5). A discarded episode keeps its
record, which states the cause, and is not run again (episodes.md §12,
terminated and discarded episodes). The run command runs in evaluation mode
only: training runs only in forks of a living port (episodes.md §3.2, modes).
The episode runner runs on the virtual machine that runs Incus, from the
repository's root, in the virtual environment that `make install` creates in
deploy/.

    python -m competition.episode_runner run --type adversarial --track A

runs adversarial episodes instead. Each episode's seed comes first. The
episode then runs one of the library's chains that the seed admits, drawn
uniformly at random, or the chain that --chain names, for which fresh living
port seeds are drawn until one admits it, up to 1,000 (episodes.md §15,
episode mix; predicted_events.py). The binaries come from `make build-attacks`
in deploy/, run once before the episodes, and the episode runner builds with
cargo the helper that repeats the tugs' draws (competition/predicted_events).

    python -m competition.episode_runner copy --fork K --forked-at T \\
        --track A --type benign|adversarial [--chain NAME] \\
        [--mode training --training-volume VOLUME] [--suppressed] \\
        [--extra-override FILE] --out DIR

runs one episode in a fork of a living port, inside the fork's virtual machine,
on the deployment that the fork holds: no deployment and no reset (the
living-port contract). The episode seed derives from the living port seed of
the identity file and the fork's number. The record goes to DIR: record.json,
steps.jsonl, the living port's startup.json, the attack chain's log, the export
in export/, and the runner's log, runner.log. The exit status is 0 once the
record holds the episode's end, whatever the end, and non-zero only when the
runner fails before it writes a record.

Both commands take --extra-override FILE, an entry's own version of the
competition's additional override file, which names the entry's images, in
place of competition/deploy/override.yaml (episodes.md §3.2, modes; §6.3, the
living port). The run command deploys with it, and both read the compression
factor from it.
"""

from __future__ import annotations

import argparse
import functools
import logging
import re
import sys
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from competition.episode_runner import (
    chains,
    deploy,
    episode,
    fork,
    predicted_events,
    volume,
)
from competition.episode_runner.incus import Incus
from competition.episode_runner.seeds import new_episode_seed
from competition.episode_runner.settle import SETTLE_TIMEOUT_S
from competition.episode_runner.steps import MonotonicClock

RECORDS = Path(__file__).resolve().parents[1] / "records"
RUNNER_LOG = "runner.log"
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

log = logging.getLogger("competition.episode_runner")


def _episode_seed(text: str) -> str:
    if not re.fullmatch(r"[0-9a-fA-F]{64}", text):
        raise argparse.ArgumentTypeError("an episode seed is 64 hexadecimal digits")
    return text.lower()


def _fork_number(text: str) -> int:
    if not re.fullmatch(r"[0-9]+", text) or int(text) < 1:
        raise argparse.ArgumentTypeError(
            "a fork's number is a whole number from 1; fork 0 is the run command's"
        )
    return int(text)


def _instant(text: str) -> datetime:
    try:
        return fork.parse_instant(text)
    except fork.ForkError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


SUPPRESSED_HELP = (
    "record each decision and carry none out, in a replay for Competition Rule 5 "
    "(default: carry out each decision)"
)
EXTRA_OVERRIDE_HELP = (
    "an entry's own version of the competition's additional override file, "
    "which names the entry's images (default: the competition's, "
    "competition/deploy/override.yaml)"
)


def extra_override(args: argparse.Namespace) -> Path:
    """The additional override file of the deployment: the entry's own version
    that --extra-override names, or the competition's (episodes.md §3.2,
    modes; §6.3, the living port)."""
    given = args.extra_override or deploy.DEPLOY / deploy.EXTRA_OVERRIDE
    return Path(given).resolve()


def parse(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m competition.episode_runner")
    commands = parser.add_subparsers(dest="command", required=True)
    copy = commands.add_parser(
        "copy", help="run one episode in a fork of a living port"
    )
    copy.add_argument("--fork", type=_fork_number, required=True)
    copy.add_argument(
        "--forked-at",
        type=_instant,
        required=True,
        help="the fork instant on the living port's clock, such as "
        "2026-11-11T20:15:00.000Z",
    )
    copy.add_argument("--track", choices=("A", "B"), required=True)
    copy.add_argument(
        "--type", choices=(episode.BENIGN, episode.ADVERSARIAL), required=True
    )
    copy.add_argument("--chain", help="the attack chain, by its binary's name")
    copy.add_argument(
        "--mode",
        choices=(volume.EVALUATION, volume.TRAINING),
        default=volume.EVALUATION,
        help="evaluation, with the submitted volume that the living port holds "
        "read-only, or training (default evaluation)",
    )
    copy.add_argument(
        "--training-volume",
        type=Path,
        help="in training mode, the directory of the training volume that the "
        "manager provides in the fork, which the runner attaches read-write to "
        "siem-01",
    )
    copy.add_argument("--out", type=Path, required=True)
    copy.add_argument(
        "--identity",
        type=Path,
        default=fork.IDENTITY,
        help=f"the living port's identity file (default {fork.IDENTITY})",
    )
    copy.add_argument(
        "--settle-timeout",
        type=float,
        default=SETTLE_TIMEOUT_S,
        help="the longest wait for the settle conditions, in seconds, from the "
        f"fork's start (default {SETTLE_TIMEOUT_S:g})",
    )
    copy.add_argument("--suppressed", action="store_true", help=SUPPRESSED_HELP)
    copy.add_argument("--extra-override", type=Path, help=EXTRA_OVERRIDE_HELP)
    copy.add_argument("--verbose", action="store_true")
    run = commands.add_parser("run", help="run episodes at fork 0")
    run.add_argument("--episodes", type=int, default=1, help="the number of episodes")
    run.add_argument("--track", choices=("A", "B"), required=True)
    run.add_argument(
        "--mode",
        choices=(volume.EVALUATION, volume.TRAINING),
        default=volume.EVALUATION,
        help="evaluation (the default); training runs only in forks of a living port",
    )
    run.add_argument(
        "--seed",
        type=_episode_seed,
        help="the living port seed of an episode at fork 0 to replay, which runs "
        "one episode",
    )
    run.add_argument(
        "--records",
        type=Path,
        default=RECORDS,
        help="the directory of the episode records",
    )
    run.add_argument(
        "--settle-timeout",
        type=float,
        default=SETTLE_TIMEOUT_S,
        help="the longest wait for the settle conditions, in seconds, from the "
        f"start of sim-host (default {SETTLE_TIMEOUT_S:g})",
    )
    run.add_argument(
        "--type",
        choices=(episode.BENIGN, episode.ADVERSARIAL),
        help="the episode type (default benign, or adversarial with --chain)",
    )
    run.add_argument(
        "--chain",
        help="the attack chain of each adversarial episode, by its binary's name "
        "(default: one of the library, drawn uniformly at random)",
    )
    run.add_argument(
        "--volume",
        type=Path,
        help="the entry's submitted storage volume, a directory, which siem-01 "
        "mounts read-only at /var/lib/entry, as in evaluation",
    )
    run.add_argument("--suppressed", action="store_true", help=SUPPRESSED_HELP)
    run.add_argument("--extra-override", type=Path, help=EXTRA_OVERRIDE_HELP)
    run.add_argument(
        "--verbose", action="store_true", help="also log every line of make"
    )
    args = parser.parse_args(argv)
    if args.command == "copy":
        if args.chain and args.type == episode.BENIGN:
            parser.error("a benign episode runs no attack chain")
        if (args.mode == volume.TRAINING) != (args.training_volume is not None):
            parser.error(
                "training mode takes --training-volume, and evaluation mode uses "
                "the volume that the living port holds"
            )
        if args.suppressed and args.mode == volume.TRAINING:
            # A suppressed episode belongs to a replay for Competition Rule 5,
            # in evaluation mode (episodes.md §2, terms).
            parser.error(
                "--suppressed runs in evaluation mode, in a replay for "
                "Competition Rule 5"
            )
        return args
    if args.mode == volume.TRAINING:
        # A training lineage passes from fork to fork of one living port, and
        # a living port that makes forks runs no episode at fork 0
        # (episodes.md §3.2, modes).
        parser.error(
            "training runs only in forks of a living port, which the living "
            "port's manager makes: python -m competition.living"
        )
    if args.episodes < 1:
        parser.error("--episodes takes a positive number")
    if args.volume is not None:
        if not args.volume.is_dir():
            parser.error(f"--volume takes a directory, not {args.volume}")
        args.volume = args.volume.resolve()
        # siem-01, an unprivileged container, reads the volume with the
        # permissions for every user, so the volume opens to them before any
        # episode (episodes.md §7, step protocol).
        try:
            volume.open_to_the_container(args.volume, writable=False)
        except OSError as error:
            parser.error(f"the volume {args.volume} cannot be opened: {error}")
    if args.seed and args.episodes != 1:
        parser.error("a replay with --seed runs one episode")
    if args.chain and args.type == episode.BENIGN:
        parser.error("a benign episode runs no attack chain")
    args.type = args.type or (episode.ADVERSARIAL if args.chain else episode.BENIGN)
    return args


def adversary_deployed(topology: Path = deploy.DEPLOY / "topology.yaml") -> bool:
    """Whether the deployment holds the adversary host."""
    return chains.ADVERSARY in (yaml.safe_load(topology.read_text()).get("nodes") or {})


def attack_chains(
    chain_id: str | None, catalogue: Path, dist: Path
) -> tuple[list[chains.Chain], chains.Chain | None]:
    """The library and the chain that chain_id names. Raises CatalogueError for
    a chain outside the library, or for any catalogued binary that is missing,
    because the adversary host's own image holds every binary in every episode,
    benign or adversarial (episodes.md §6.5, adversary host)."""
    catalogued = chains.load(catalogue)
    missing = chains.unbuilt(catalogued, dist)
    if missing:
        raise chains.CatalogueError(
            f"no binary in {dist} for {', '.join(missing)}: run `make build-attacks` "
            "in deploy/"
        )
    library = chains.library(catalogued)
    given = None
    if chain_id:
        given = chains.find(catalogued, chain_id)
        reason = given.exclusion()
        if reason:
            raise chains.CatalogueError(
                f"the attack chain {chain_id} is outside the library: {reason}"
            )
    return library, given


def main(
    argv: Sequence[str] | None = None,
    *,
    testbed: Any = None,
    scenario: Any = None,
    run_episode: Callable[..., episode.Outcome] = episode.run_episode,
    catalogue: Path = chains.CATALOGUE,
    dist: Path = chains.DIST,
    with_adversary: Callable[[], bool] = adversary_deployed,
    review: Callable[[Path], None] = lambda catalogue: chains.check_review(
        catalogue=catalogue
    ),
    build_helper: Callable[[], Any] | None = None,
    draws: Callable[..., predicted_events.Draws] = predicted_events.draws,
    new_seed: Callable[[], str] = new_episode_seed,
    run_copy: Callable[..., episode.Outcome] = episode.run_copy_episode,
    read_identity: Callable[[Path], fork.LivingPort] = fork.read_identity,
) -> int:
    """The run command builds the helper that repeats the tugs' draws
    (competition/predicted_events), with which it plans fork 0; the `copy`
    command reads the fork's simulator log instead."""
    args = parse(argv)
    if args.command == "copy":
        return copy_episode(
            args,
            testbed=testbed,
            scenario=scenario,
            run_copy=run_copy,
            read_identity=read_identity,
            catalogue=catalogue,
            dist=dist,
            with_adversary=with_adversary,
            review=review,
        )
    build_helper = build_helper or predicted_events.build
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format=LOG_FORMAT,
    )
    library: list[chains.Chain] = []
    given = None
    if args.type == episode.ADVERSARIAL or with_adversary():
        try:
            library, given = attack_chains(args.chain, catalogue, dist)
            if args.type == episode.ADVERSARIAL:
                review(catalogue)
                if not given and not library:
                    raise chains.CatalogueError("the library holds no attack chain")
        except chains.CatalogueError as error:
            print(f"the episode runner cannot start: {error}", file=sys.stderr)
            return 2
    timings = None
    override = extra_override(args)
    if args.type == episode.ADVERSARIAL:
        try:
            build_helper()
            timings = predicted_events.competition_timings(override=override)
        except predicted_events.PredictedEventsError as error:
            print(f"the episode runner cannot start: {error}", file=sys.stderr)
            return 2
    testbed = testbed or episode.Testbed(
        incus=Incus(),
        clock=MonotonicClock(),
        up=functools.partial(deploy.up, extra_override=override),
    )
    scenario = scenario or episode.Scenario.from_files(override=override)
    try:
        # Each episode runs once: a discarded episode keeps its record, which
        # states the cause, and the next episode takes a seed of its own
        # (episodes.md §12, terminated and discarded episodes).
        for _ in range(args.episodes):
            chain, chosen = None, None
            if args.type == episode.ADVERSARIAL:
                planned = predicted_events.plan(
                    library, given, args.seed, timings, new_seed=new_seed, draws_=draws
                )
                seed, chain, chosen = planned.seed, planned.chain, planned.chosen
            else:
                seed = args.seed or new_seed()
            outcome = run_episode(
                testbed,
                scenario,
                seed,
                args.track,
                args.records,
                settle_timeout_s=args.settle_timeout,
                episode_type=args.type,
                chain=chain,
                chosen=chosen,
                dist=dist,
                suppressed=args.suppressed,
                entry_volume=args.volume,
            )
            cause = f" ({outcome.cause})" if outcome.cause else ""
            print(f"{outcome.record}: {outcome.outcome}{cause}", flush=True)
    except episode.ResetFailure as failure:
        print(f"the episode runner stopped: {failure}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(
            "interrupted: the deployment is left up, and `make down` in deploy/ "
            "tears it down",
            file=sys.stderr,
        )
        return 130
    return 0


def _log_to(out: Path, verbose: bool) -> list[logging.Handler]:
    """Log to standard error and to the runner's log in out, through the
    handlers returned."""
    handlers: list[logging.Handler] = [
        logging.StreamHandler(),
        logging.FileHandler(out / RUNNER_LOG, encoding="utf-8"),
    ]
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    for handler in handlers:
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        root.addHandler(handler)
    return handlers


def copy_episode(
    args: argparse.Namespace,
    *,
    testbed: Any,
    scenario: Any,
    run_copy: Callable[..., episode.Outcome],
    read_identity: Callable[[Path], fork.LivingPort],
    catalogue: Path,
    dist: Path,
    with_adversary: Callable[[], bool],
    review: Callable[[Path], None],
) -> int:
    """One episode in a fork of a living port (the living-port contract)."""
    try:
        # The contract: --out must not exist, and the runner creates it.
        if args.out.exists():
            raise FileExistsError(f"{args.out} exists; the runner creates --out")
        args.out.mkdir(parents=True)
        handlers = _log_to(args.out, args.verbose)
    except OSError as error:
        print(f"the episode runner cannot start: {error}", file=sys.stderr)
        return 2
    try:
        return _copy_episode(
            args,
            testbed=testbed,
            scenario=scenario,
            run_copy=run_copy,
            read_identity=read_identity,
            catalogue=catalogue,
            dist=dist,
            with_adversary=with_adversary,
            review=review,
        )
    finally:
        for handler in handlers:
            logging.getLogger().removeHandler(handler)
            handler.close()


def _copy_episode(
    args: argparse.Namespace,
    *,
    testbed: Any,
    scenario: Any,
    run_copy: Callable[..., episode.Outcome],
    read_identity: Callable[[Path], fork.LivingPort],
    catalogue: Path,
    dist: Path,
    with_adversary: Callable[[], bool],
    review: Callable[[Path], None],
) -> int:
    try:
        living = read_identity(args.identity)
    except fork.ForkError as error:
        log.error("the episode runner cannot start: %s", error)
        return 2
    if args.forked_at < living.started_at:
        log.error(
            "the episode runner cannot start: the fork instant precedes the "
            "living port's start, %s",
            living.started_at.isoformat(),
        )
        return 2
    library: list[chains.Chain] = []
    given = None
    try:
        if args.type == episode.ADVERSARIAL or with_adversary():
            library, given = attack_chains(args.chain, catalogue, dist)
            if args.type == episode.ADVERSARIAL:
                review(catalogue)
                if not given and not library:
                    raise chains.CatalogueError("the library holds no attack chain")
    except chains.CatalogueError as error:
        log.error("the episode runner cannot start: %s", error)
        return 2
    forked = episode.Fork(living, args.fork, args.forked_at)
    log.info(
        "fork %d of the living port %s, %.0f s after its start: episode seed %s",
        args.fork,
        living.seed[:8],
        forked.age_s,
        forked.episode_seed[:8],
    )
    testbed = testbed or episode.Testbed(incus=Incus(), clock=MonotonicClock())
    scenario = scenario or episode.Scenario.from_files(override=extra_override(args))
    try:
        outcome = run_copy(
            testbed,
            scenario,
            forked,
            args.track,
            args.out,
            settle_timeout_s=args.settle_timeout,
            episode_type=args.type,
            library=library,
            given=given,
            dist=dist,
            mode=args.mode,
            training_volume=args.training_volume,
            suppressed=args.suppressed,
        )
    except KeyboardInterrupt:
        log.error("interrupted: the fork is left as it is")
        return 130
    except Exception:
        log.exception("the episode runner failed")
        return 1
    cause = f" ({outcome.cause})" if outcome.cause else ""
    print(f"{outcome.record}: {outcome.outcome}{cause}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
