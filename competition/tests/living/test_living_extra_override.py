"""An entry's own version of the competition's additional override file, which
names the entry's images, at a living port's creation (episodes.md §3.2,
modes; §6.3, the living port; episode-runner-impl.md §1, competition images).
The manager's create sends the file's path in the tree to the guest's create,
which renders, builds and deploys with it.

Run with: pytest competition/tests -q
"""

from __future__ import annotations

from pathlib import Path

import pytest
from test_living_guest import IMAGES, SEED, FakeIncus, FakeRun
from test_living_port import Guests, base, manager, tree  # noqa: F401 (fixtures)

from competition.living import guest
from competition.living import port as living
from competition.living.__main__ import parse
from competition.living.port import PortError, episode_command

ENTRY = "competition/baselines/track_a/packetbeat/dist/override.yaml"
SETTLING = {"settled_at": "2026-11-11T14:37:12.345Z", "conditions": {}}


def guest_create(tmp_path: Path, run: FakeRun, **options) -> dict:
    return guest.create(
        SEED,
        incus=FakeIncus(),
        run=run,
        state_dir=tmp_path / "sim-state",
        identity_path=tmp_path / "living-port.json",
        log=tmp_path / "deploy.log",
        manifest=lambda: {"images": IMAGES, "nodes": {"siem-01": {}}},
        sleep=lambda s: None,
        settle_wait=lambda incus, rendered, line: SETTLING,
        settled_path=tmp_path / "living-port-settled.json",
        **options,
    )


@pytest.mark.parametrize("build", [False, True])
def test_the_guest_renders_builds_and_deploys_with_the_entrys_file(
    tmp_path: Path, build: bool
) -> None:
    run = FakeRun()
    guest_create(tmp_path, run, build=build, extra_override=f"/opt/ravensport/{ENTRY}")
    override = f"EXTRA_OVERRIDE=/opt/ravensport/{ENTRY}"
    assert run.makes() == [
        ["build" if build else "render", override],
        ["deploy", override, f"SIM_STATE_DIR={tmp_path / 'sim-state'}"],
    ]


def test_the_guest_takes_the_file_by_its_path_in_the_tree(monkeypatch) -> None:
    given = {}
    monkeypatch.setattr(guest, "create", lambda seed, **options: given.update(options))
    assert guest.main(["create", "--seed", SEED, "--extra-override", ENTRY]) == 0
    assert given["extra_override"] == str(Path(ENTRY).resolve())
    assert guest.main(["create", "--seed", SEED]) == 0
    assert given["extra_override"] == "../competition/deploy/override.yaml"


def test_the_manager_sends_the_entrys_file_to_the_guest(
    short_tmp,
    host,
    base,  # noqa: F811
    tree,  # noqa: F811
) -> None:
    (tree / Path(ENTRY).parent).mkdir(parents=True)
    (tree / ENTRY).write_text("{}\n")
    guests = Guests()
    record = manager(short_tmp, host, guests).create(
        "lp1", base, tree, seed="5e" * 32, extra_override=ENTRY
    )
    ((_, command),) = guests.streams
    assert f"guest create --seed {'5e' * 32} --extra-override {ENTRY}" in command
    assert record["extra_override"] == ENTRY


def test_each_copys_episode_reads_the_living_ports_file(
    short_tmp,
    host,
    base,  # noqa: F811
    tree,  # noqa: F811
) -> None:
    """The manager's episode passes the file that the living port's record
    holds to the episode runner's `copy` command, so a fork reads the file
    that its living port was built from. A living port created without one
    passes none, and its forks read the competition's file."""
    (tree / Path(ENTRY).parent).mkdir(parents=True)
    (tree / ENTRY).write_text("{}\n")
    guests = Guests()
    lots = manager(short_tmp, host, guests)
    lots.create("lp1", base, tree, seed="5e" * 32, extra_override=ENTRY)
    fork = lots.fork("lp1")
    episode = lots.episode("lp1", 1, track="A", episode_type="benign")
    command = episode_command(1, fork["forked_at"], "A", "benign", extra_override=ENTRY)
    assert command.endswith(
        f"--type benign --extra-override {ENTRY} "
        "--out /var/lib/competition/episodes/fork-1"
    )
    assert guests.streams[-1][1] == living.login(command)
    assert episode["command"] == command
    without = episode_command(1, fork["forked_at"], "A", "benign")
    assert "--extra-override" not in without


def test_the_manager_refuses_a_file_that_the_tree_lacks(
    short_tmp,
    host,
    base,  # noqa: F811
    tree,  # noqa: F811
) -> None:
    with pytest.raises(PortError, match="no override file"):
        manager(short_tmp, host, Guests()).create(
            "lp1", base, tree, seed="5e" * 32, extra_override=ENTRY
        )


def test_the_create_command_takes_the_file() -> None:
    argv = ["create", "lp1", "--base-disk", "d", "--mac", "m", "--tree", "t"]
    assert parse(argv).extra_override is None
    assert parse([*argv, "--extra-override", ENTRY]).extra_override == ENTRY
