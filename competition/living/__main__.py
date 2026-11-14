"""The living port manager's command line, run on the physical host from the
repository's root with the host's own Python:

    python3 -m competition.living create lp1 --base-disk DISK --mac MAC --tree TREE
    python3 -m competition.living fork lp1
    python3 -m competition.living episode lp1 --fork 1 --track A --type benign
    python3 -m competition.living loop lp1 --track A --type benign
    python3 -m competition.living snapshot lp1
    python3 -m competition.living replay lp1 --snapshot 3
    python3 -m competition.living fold lp1
    python3 -m competition.living list
    python3 -m competition.living stop lp1 [--delete]

`create` starts a living port from a base disk, through an overlay of its own,
and deploys RavensPort in it from TREE, with the entry's submitted storage
volume in evaluation (--volume) or the start of the training volumes' lineage
in training (--training-volume). With --extra-override, a path in TREE, the
deployment applies an entry's own version of the competition's additional
override file, which names the entry's images, and each fork's episode reads
the same file. `fork` saves the living port and restores the fork from the
save. `episode` runs one episode in a fork by the contract's command, copies
its outputs to ROOT/NAME/episodes/fork-K and deletes the fork. `loop` forks
after drawn waits, one episode at a time, and with --snapshot-cache-gb keeps
the snapshots for replay on their schedule. `snapshot` saves the living port
for replay and restores no fork from the save, and `replay` restores a kept
snapshot on a virtual machine of its own. `fold` folds now every frozen image
that no fork, no replay and no kept snapshot needs into the living port's top,
as the manager does by itself whenever a fork, a replay or a snapshot goes,
unless --no-fold. Where the file system clones files, each save clones the
living port's top in place of adding a layer, unless --no-clones. With
--memory-dir, saves go to a file system in memory, such as /dev/shm. Every
record lives under --root, by default $RAVENSPORT_LIVING_ROOT or
~/ravensport-living.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from competition.living import hypervisor as hypervisors
from competition.living.port import Base, Manager, PortError
from competition.living.ssh import SSHError
from competition.living.vm import Engine, VMError

BENIGN, ADVERSARIAL = "benign", "adversarial"


def default_root() -> Path:
    return Path(
        os.environ.get("RAVENSPORT_LIVING_ROOT", "~/ravensport-living")
    ).expanduser()


def default_key() -> Path:
    """Lima's key for its guests, under LIMA_HOME or ~/.lima."""
    lima = Path(os.environ.get("LIMA_HOME", "~/.lima")).expanduser()
    return lima / "_config" / "user"


def parse(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python3 -m competition.living")
    parser.add_argument(
        "--root", type=Path, default=default_root(), help="the records' directory"
    )
    parser.add_argument(
        "--min-free-gb",
        type=float,
        default=10.0,
        help="the disk a save must leave free, in gigabytes (default 10)",
    )
    parser.add_argument(
        "--snapshot-cache-gb",
        type=float,
        default=0.0,
        help="the snapshot cache, in gigabytes; the loop keeps snapshots only "
        "with one (default 0)",
    )
    parser.add_argument(
        "--memory-dir",
        type=Path,
        help="save to this file system in memory, such as /dev/shm, and move a "
        "kept state file to the disk once the living port runs again",
    )
    parser.add_argument(
        "--no-fold",
        action="store_true",
        help="leave frozen images in the living port's chain until `fold`",
    )
    parser.add_argument(
        "--no-clones",
        action="store_true",
        help="add a layer at each save even where the file system clones files",
    )
    parser.add_argument("--firmware", type=Path, help="the VMs' firmware, if not found")
    parser.add_argument("--verbose", action="store_true")
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create", help="start a living port")
    create.add_argument("name")
    create.add_argument(
        "--base-disk", type=Path, required=True, help="a qcow2 disk, never written"
    )
    create.add_argument(
        "--mac", required=True, help="the network card's address, as the base's"
    )
    create.add_argument(
        "--tree", type=Path, required=True, help="the RavensPort tree to deploy"
    )
    create.add_argument(
        "--user", default=os.environ.get("USER", ""), help="the guest's user"
    )
    create.add_argument(
        "--key", type=Path, default=default_key(), help="the guest's SSH key"
    )
    create.add_argument("--cpus", type=int, default=4)
    create.add_argument("--memory-mib", type=int, default=8192)
    create.add_argument("--ssh-port", type=int)
    create.add_argument(
        "--affinity", help="host threads to pin the VMs to, such as 0-15"
    )
    create.add_argument(
        "--seed", help="the living port seed (default: 32 random bytes)"
    )
    create.add_argument(
        "--build", action="store_true", help="build the images in the VM first"
    )
    create.add_argument(
        "--extra-override",
        help="an entry's own version of the competition's additional override "
        "file, which names the entry's images: a path in the tree (default: "
        "the competition's)",
    )
    create.add_argument(
        "--cidata",
        type=Path,
        help="a cloud-init image that the base's guest expects, attached "
        "read-only for the VM's life",
    )
    volumes = create.add_mutually_exclusive_group()
    volumes.add_argument(
        "--volume",
        type=Path,
        help="evaluation: the entry's submitted storage volume, a directory, "
        "read-only on siem-01 from the creation on",
    )
    volumes.add_argument(
        "--training-volume",
        type=Path,
        help="training: the first version of the training volume, a directory, "
        "which passes from fork to fork",
    )

    fork = commands.add_parser(
        "fork", help="save the living port and restore a fork from the save"
    )
    fork.add_argument("name")
    fork.add_argument(
        "--fork", type=int, dest="k", help="the fork's number (default: the next)"
    )
    fork.add_argument("--keep-state", action="store_true", help="keep the state file")

    episode = commands.add_parser(
        "episode", help="run one episode in a fork, then delete it"
    )
    episode.add_argument("name")
    episode.add_argument("--fork", type=int, dest="k", required=True)
    _episode_arguments(episode)
    episode.add_argument(
        "--keep-copy", action="store_true", help="leave the fork running"
    )

    delete = commands.add_parser("delete-copy", help="stop and delete one fork")
    delete.add_argument("name")
    delete.add_argument("--fork", type=int, dest="k", required=True)

    loop = commands.add_parser(
        "loop", help="fork after drawn waits, one episode at a time"
    )
    loop.add_argument("name")
    _episode_arguments(loop)
    loop.add_argument("--forks", type=int, help="stop after this many forks")

    snapshot = commands.add_parser("snapshot", help="save the living port for replay")
    snapshot.add_argument("name")

    replay = commands.add_parser(
        "replay", help="restore a kept snapshot on a virtual machine of its own"
    )
    replay.add_argument("name")
    replay.add_argument("--snapshot", type=int, dest="n", required=True)

    unreplay = commands.add_parser("delete-replay", help="stop and delete a replay")
    unreplay.add_argument("name")
    unreplay.add_argument("--replay", dest="replay_id", required=True)

    fold = commands.add_parser(
        "fold", help="fold the frozen images that nothing needs into the top"
    )
    fold.add_argument("name")

    commands.add_parser("list", help="every living port, its forks and its replays")

    stop = commands.add_parser(
        "stop", help="stop a living port, its forks and its replays"
    )
    stop.add_argument("name")
    stop.add_argument("--delete", action="store_true", help="also delete its directory")

    args = parser.parse_args(argv)
    if getattr(args, "chain", None) and args.type == BENIGN:
        parser.error("a benign episode runs no attack chain")
    return args


def _episode_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--track", choices=("A", "B"), required=True)
    parser.add_argument("--type", choices=(BENIGN, ADVERSARIAL), default=BENIGN)
    parser.add_argument("--chain", help="the attack chain of an adversarial episode")
    parser.add_argument("--timeout-s", type=float, default=3 * 3600.0)


def main(
    argv: Sequence[str] | None = None,
    *,
    manager: Callable[[argparse.Namespace], Manager] | None = None,
) -> int:
    args = parse(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        living = manager(args) if manager else _manager(args)
        result = _dispatch(living, args)
    except (PortError, SSHError, VMError, hypervisors.HypervisorError) as error:
        print(f"{args.command} failed: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("interrupted: `list` shows what still runs", file=sys.stderr)
        return 130
    if result is not None:
        print(json.dumps(result, indent=1))
    return 0


def _manager(args: argparse.Namespace) -> Manager:
    engine = Engine(hypervisors.for_host(args.firmware))
    args.root.mkdir(parents=True, exist_ok=True)
    return Manager(
        args.root,
        engine,
        min_free_bytes=int(args.min_free_gb * 1e9),
        snapshot_cache_bytes=int(args.snapshot_cache_gb * 1e9),
        memory_dir=args.memory_dir.expanduser().resolve() if args.memory_dir else None,
        auto_fold=not args.no_fold,
        clones=False if args.no_clones else None,
    )


def _dispatch(living: Manager, args: argparse.Namespace) -> Any:
    if args.command == "create":
        base = Base(
            disk=args.base_disk.expanduser().resolve(),
            mac=args.mac,
            user=args.user,
            key=args.key.expanduser().resolve(),
            cpus=args.cpus,
            memory_mib=args.memory_mib,
            cidata=args.cidata.expanduser().resolve() if args.cidata else None,
        )
        return living.create(
            args.name,
            base,
            args.tree.expanduser().resolve(),
            seed=args.seed,
            ssh_port=args.ssh_port,
            affinity=args.affinity,
            build=args.build,
            volume=args.volume.expanduser().resolve() if args.volume else None,
            training_volume=(
                args.training_volume.expanduser().resolve()
                if args.training_volume
                else None
            ),
            extra_override=args.extra_override,
        )
    if args.command == "fork":
        return living.fork(args.name, args.k, keep_state=args.keep_state)
    if args.command == "snapshot":
        return living.snapshot(args.name)
    if args.command == "replay":
        return living.replay(args.name, args.n)
    if args.command == "delete-replay":
        living.delete_replay(args.name, args.replay_id)
        return None
    if args.command == "fold":
        return living.fold(args.name)
    episode = {}
    if args.command in ("episode", "loop"):
        episode = {"track": args.track, "episode_type": args.type, "chain": args.chain}
    if args.command == "episode":
        return living.episode(
            args.name,
            args.k,
            keep_copy=args.keep_copy,
            timeout_s=args.timeout_s,
            **episode,
        )
    if args.command == "delete-copy":
        living.delete_copy(args.name, args.k)
        return None
    if args.command == "loop":
        made = living.loop(
            args.name, forks=args.forks, timeout_s=args.timeout_s, **episode
        )
        return {"forks": made}
    if args.command == "list":
        return living.list()
    if args.command == "stop":
        living.stop(args.name, delete=args.delete)
        return None
    raise AssertionError(args.command)


if __name__ == "__main__":
    sys.exit(main())
