"""The living port manager's command line.

Run with: pytest competition/tests -q
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from competition.living.__main__ import main, parse
from competition.living.port import Base, PortError


class FakeManager:
    def __init__(self, fail: Exception | None = None) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []
        self.fail = fail

    def __getattr__(self, name):
        def call(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            if self.fail:
                raise self.fail
            return {"called": name}

        return call


def test_create_needs_the_base_disk_its_network_address_and_the_tree() -> None:
    with pytest.raises(SystemExit):
        parse(["create", "lp1", "--base-disk", "/d", "--mac", "52:55:55:a4:9c:41"])
    args = parse(["create", "lp1", "--base-disk", "/d", "--mac", "m", "--tree", "/t"])
    assert (args.cpus, args.memory_mib, args.build) == (4, 8192, False)


def test_a_benign_episode_with_a_chain_is_refused() -> None:
    with pytest.raises(SystemExit):
        parse(["episode", "lp1", "--fork", "1", "--track", "A", "--chain", "c-01"])
    args = parse(
        ["loop", "lp1", "--track", "B", "--type", "adversarial", "--chain", "c-01"]
    )
    assert (args.type, args.chain, args.forks) == ("adversarial", "c-01", None)
    with pytest.raises(SystemExit):
        parse(["loop", "lp1", "--track", "A", "--cadence-s", "900"])


def test_create_passes_the_base_and_the_tree(tmp_path, capsys) -> None:
    living = FakeManager()
    argv = [
        "--root",
        str(tmp_path),
        "create",
        "lp1",
        "--base-disk",
        str(tmp_path / "disk"),
    ]
    argv += ["--mac", "52:55:55:a4:9c:41", "--tree", str(tmp_path), "--user", "aicd"]
    argv += ["--key", str(tmp_path / "user"), "--affinity", "0-15", "--seed", "5e" * 32]
    assert main(argv, manager=lambda args: living) == 0
    name, args, kwargs = living.calls[0]
    assert name == "create" and args[0] == "lp1" and args[2] == tmp_path.resolve()
    assert args[1] == Base(
        disk=(tmp_path / "disk").resolve(),
        mac="52:55:55:a4:9c:41",
        user="aicd",
        key=(tmp_path / "user").resolve(),
    )
    assert kwargs["affinity"] == "0-15" and kwargs["seed"] == "5e" * 32
    assert json.loads(capsys.readouterr().out) == {"called": "create"}


def test_an_episode_and_a_loop_pass_the_episodes_inputs(tmp_path) -> None:
    living = FakeManager()
    root = ["--root", str(tmp_path)]
    main([*root, "episode", "lp1", "--fork", "3", "--track", "A", "--type", "adversarial", "--chain", "c-01"],
         manager=lambda args: living)  # fmt: skip
    main(
        [*root, "loop", "lp1", "--track", "A", "--forks", "2"],
        manager=lambda args: living,
    )
    (_, args, kwargs), (_, loop_args, loop_kwargs) = living.calls
    assert args == ("lp1", 3)
    assert kwargs == {
        "keep_copy": False,
        "timeout_s": 10800.0,
        "track": "A",
        "episode_type": "adversarial",
        "chain": "c-01",
    }
    assert loop_args == ("lp1",)
    assert (loop_kwargs["forks"], loop_kwargs["episode_type"]) == (2, "benign")
    assert "max_copies" not in loop_kwargs and "cadence_s" not in loop_kwargs


def test_a_failure_is_reported_on_standard_error(tmp_path, capsys) -> None:
    living = FakeManager(fail=PortError("the living port lp1 is stopped"))
    assert (
        main(["--root", str(tmp_path), "fork", "lp1"], manager=lambda args: living) == 1
    )
    assert "fork failed: the living port lp1 is stopped" in capsys.readouterr().err


def test_the_default_key_is_limas_under_lima_home(monkeypatch) -> None:
    monkeypatch.setenv("LIMA_HOME", "/home/runner/lima-home")
    args = parse(["create", "lp1", "--base-disk", "/d", "--mac", "m", "--tree", "/t"])
    assert args.key == Path("/home/runner/lima-home/_config/user")


def test_a_living_port_takes_one_volume_for_its_mode(tmp_path) -> None:
    base = ["create", "lp1", "--base-disk", "/d", "--mac", "m", "--tree", "/t"]
    with pytest.raises(SystemExit):
        parse([*base, "--volume", "/v", "--training-volume", "/w"])
    living = FakeManager()
    main(
        ["--root", str(tmp_path), *base, "--volume", str(tmp_path)],
        manager=lambda a: living,
    )
    _, _, kwargs = living.calls[0]
    assert kwargs["volume"] == tmp_path.resolve() and kwargs["training_volume"] is None
    # The volume mounts at /var/lib/entry alone (episode-runner-impl.md §5,
    # forks).
    assert "volume_mount" not in kwargs
    with pytest.raises(SystemExit):
        parse([*base, "--volume", "/v", "--volume-mount", "/srv/v"])


def test_replay_shorten_and_the_memory_directory_reach_the_manager(tmp_path) -> None:
    living = FakeManager()
    root = ["--root", str(tmp_path)]
    main([*root, "replay", "lp1", "--snapshot", "3"], manager=lambda a: living)
    main([*root, "fold", "lp1"], manager=lambda a: living)
    main([*root, "delete-replay", "lp1", "--replay", "3-1"], manager=lambda a: living)
    assert [(name, args) for name, args, _ in living.calls] == [
        ("replay", ("lp1", 3)),
        ("fold", ("lp1",)),
        ("delete_replay", ("lp1", "3-1")),
    ]
    args = parse([*root, "--memory-dir", "/dev/shm", "list"])
    assert args.memory_dir == Path("/dev/shm")
