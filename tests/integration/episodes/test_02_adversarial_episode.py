"""One adversarial suppressed episode of the first version, on the adversary
host with a real paced attack chain (episodes-tests.md §0.6, the first
version; §8, onset and pacing; §9, horizon and termination).

The attack chain is it-foothold__deny-directory-availability: a scan of the IT
segment, then a denial of the directory that holds until the end of the
episode. The episode runner then stops it with SIGTERM, which releases the
directory. Two hooks read the deployment, as for the benign episode: at the end
of the settle period, and after the export, before the reset.

The suite needs the chain catalogue and the binaries from `make build-attacks`
in deploy/, built from the source that the episode runner's table of stages was
reviewed against.

Run with: make -C tests/integration test-episodes
"""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from typing import Any

import pytest

from competition.episode_runner import chains, deploy, episode
from competition.episode_runner.incus import Incus
from competition.episode_runner.steps import MonotonicClock, ProcessSession
from tests.integration.episodes.conftest import (
    ADVERSARY,
    TRACK,
    EpisodeRun,
    adversary_addresses,
    adversary_binaries,
    node,
    parse,
)

CHAIN = "it-foothold__deny-directory-availability"
SEED = hashlib.sha256(b"episodes suite adversarial 0").hexdigest()
STEP = timedelta(seconds=5)
HORIZON = 144
# The tolerance of episodes-tests.md §8 (onset and pacing) for the start of the
# attack chain and for each wait.
TOLERANCE = timedelta(seconds=1)
GRACEFUL_STOP = "SIGTERM, then standard input closed"


@pytest.fixture(scope="module")
def adversarial_run(tmp_path_factory: pytest.TempPathFactory) -> EpisodeRun:
    topology = deploy.topology()
    if ADVERSARY not in topology["nodes"]:
        pytest.skip("the deployed topology holds no adversary host")
    present = {i["name"] for i in Incus().instances()} & set(topology["nodes"])
    if present:
        pytest.fail(
            f"RavensPort nodes already exist ({', '.join(sorted(present))}); "
            "run `make down` in deploy/ first"
        )
    try:
        catalogue = chains.load()
        chains.check_review()
    except chains.CatalogueError as error:
        pytest.fail(str(error))
    chain = chains.find(catalogue, CHAIN)
    reason = chain.exclusion()
    if reason:
        pytest.fail(f"{CHAIN} is outside the library: {reason}")
    records = tmp_path_factory.mktemp("adversarial-records")
    observed: dict[str, dict[str, Any]] = {
        "at_step_0": {},
        "before_reset": {},
        "after_reset": {},
    }

    def session(argv: list[str]) -> ProcessSession:
        at = observed["at_step_0"]
        at["adversary_addresses"] = adversary_addresses()
        at["adversary_binaries"] = adversary_binaries()
        return ProcessSession(argv)

    def down(clock: MonotonicClock) -> deploy.MakeRun:
        before = observed["before_reset"]
        before["adversary_addresses"] = adversary_addresses()
        before["adversary_processes"] = node(
            ADVERSARY, "ps", "-eo", "pid=,args="
        ).stdout.splitlines()
        # A new connection to the directory, which the denial blocks while it
        # holds: the root entry over the directory's own local socket.
        query = node(
            "corp-it",
            "timeout",
            "10",
            "ldapsearch",
            "-x",
            "-H",
            "ldapi:///",
            "-b",
            "",
            "-s",
            "base",
            "namingContexts",
        )
        before["directory"] = {"returncode": query.returncode, "stdout": query.stdout}
        made = deploy.down(clock)
        names = {i["name"] for i in Incus().instances()}
        observed["after_reset"]["remaining"] = sorted(names & set(topology["nodes"]))
        return made

    testbed = episode.Testbed(
        incus=Incus(), clock=MonotonicClock(), session=session, down=down
    )
    outcome = episode.run_episode(
        testbed,
        episode.Scenario.from_files(),
        SEED,
        TRACK,
        records,
        episode_type=episode.ADVERSARIAL,
        chain=chain,
        chosen={
            "by": "the episode suite",
            "library": [c.id for c in chains.library(catalogue)],
        },
        # A suppressed episode, whose last stage runs (episodes.md §9).
        suppressed=True,
    )
    if outcome.outcome == episode.DISCARDED:
        pytest.fail(f"the episode was discarded: {outcome.cause}")
    lines = (outcome.record / "steps.jsonl").read_text().splitlines()
    return EpisodeRun(
        seed=SEED,
        outcome=outcome,
        record=json.loads((outcome.record / "episode.json").read_text()),
        steps=[json.loads(line) for line in lines],
        records=records,
        topology=topology,
        **observed,
    )


def attack(run: EpisodeRun) -> dict[str, Any]:
    return run.record["attack"]


def events(run: EpisodeRun, kind: str) -> dict[int, dict[str, Any]]:
    """The stage events of one kind, begin or end, by stage number."""
    return {
        e["stage_number"]: e
        for e in attack(run)["session"]["events"]
        if e["event"] == kind
    }


# §8, onset and pacing


def test_the_attack_chain_starts_at_the_onset_step(adversarial_run) -> None:
    step_0 = parse(adversarial_run.record["settle"]["step_0"])
    onset = step_0 + attack(adversarial_run)["onset_step"] * STEP
    started = parse(attack(adversarial_run)["session"]["started"])
    assert abs(started - onset) <= TOLERANCE


def test_each_wait_starts_after_the_previous_stage_ends(adversarial_run) -> None:
    """The first wait counts from the start of the attack chain's session, and
    each later wait from the end event of the stage before."""
    plan = attack(adversarial_run)
    begins, ends = events(adversarial_run, "begin"), events(adversarial_run, "end")
    assert sorted(begins) == list(range(1, len(plan["chain"]["stages"]) + 1))
    since = parse(plan["session"]["started"])
    for position, wait in enumerate(plan["waits_s"], start=1):
        began = parse(begins[position]["at"])
        assert abs((began - since) - timedelta(seconds=wait)) <= TOLERANCE, position
        since = parse(ends[position]["at"]) if position in ends else since


def test_stage_events_name_the_target_host(adversarial_run) -> None:
    chain = chains.find(chains.load(), CHAIN)
    begins = events(adversarial_run, "begin")
    assert [begins[n]["target"] for n in sorted(begins)] == [
        stage.target.as_json() for stage in chain.stages
    ]
    assert [begins[n]["target"]["node"] for n in sorted(begins)] == [
        "vnet-it",
        "corp-it",
    ]


def test_stage_events_carry_the_episode_runner_clock(adversarial_run) -> None:
    """Each stage event carries a time in Coordinated Universal Time (UTC), in
    the order of the events, between the start and the end of the attack
    chain's session."""
    session = attack(adversarial_run)["session"]
    times = [parse(e["at"]) for e in session["events"]]
    assert times == sorted(times)
    assert parse(session["started"]) <= times[0]
    assert times[-1] <= parse(session["ended"])


# §9, horizon and termination


def test_the_attack_chain_stops_gracefully_at_the_end(adversarial_run) -> None:
    """At the end of the episode the denial still holds, so the episode runner
    sends SIGTERM, the stage releases the directory and ends as completed, and
    the binary exits with status 0."""
    plan = attack(adversarial_run)
    session = plan["session"]
    assert session["stop"] == GRACEFUL_STOP
    step_0 = parse(adversarial_run.record["settle"]["step_0"])
    assert parse(session["stop_sent"]) >= step_0 + HORIZON * STEP
    last = events(adversarial_run, "end")[len(plan["chain"]["stages"])]
    assert last["outcome"] == "completed"
    assert parse(last["at"]) >= parse(session["stop_sent"])
    assert session["exit_status"] == 0
    assert session["problems"] == []
    assert plan["early_stop"] is None
    assert adversarial_run.outcome.outcome == "horizon"


def test_the_stop_releases_the_directory(adversarial_run) -> None:
    directory = adversarial_run.before_reset["directory"]
    assert directory["returncode"] == 0
    assert "namingContexts: dc=ravensport,dc=local" in directory["stdout"]


def test_the_adversary_host_is_down_and_unaddressed_before_and_after(
    adversarial_run,
) -> None:
    """Each stage brings its interface up for its own duration only
    (adversary.md §1, placement), so the adversary host is quiet at step 0 and
    again once the attack chain has stopped, and no process of the chain is
    left."""
    for moment in (adversarial_run.at_step_0, adversarial_run.before_reset):
        interfaces = moment["adversary_addresses"]
        assert interfaces, "the adversary host lists no interface"
        assert all(not i["up"] and not i["ipv4"] for i in interfaces.values())
    assert not [
        line
        for line in adversarial_run.before_reset["adversary_processes"]
        if CHAIN in line
    ]


def test_the_adversary_host_holds_every_binary_as_built(adversarial_run) -> None:
    """The adversary host holds every catalogued binary, as in a benign episode,
    so its files leave the episode type to the episode record."""
    held = adversarial_run.at_step_0["adversary_binaries"]
    for chain in chains.load():
        assert held.get(chain.id) == chain.sha256(), chain.id
    assert held[CHAIN] == attack(adversarial_run)["sha256"]


def test_the_reset_leaves_no_node(adversarial_run) -> None:
    assert adversarial_run.after_reset["remaining"] == []
    assert adversarial_run.record["reset"]["remaining"] == []
