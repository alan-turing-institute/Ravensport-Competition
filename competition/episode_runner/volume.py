"""The entry's storage volume on siem-01 in a fork of a living port
(episodes.md §3.2, modes).

In evaluation mode the living port's manager attaches the entry's submitted
volume read-only to siem-01 when it creates the living port, as the device
`entry-volume`, and every fork holds it as it is. In training mode the manager
provides the training volume as a directory in the fork, and the episode
runner attaches it read-write to siem-01, as the device `training-volume`,
before it confirms the settle conditions. After the steps the runner detaches
it, and the manager carries it to the next fork. Both devices mount at
/var/lib/entry, the path that episodes.md §3.2 names, where the launcher and
the episode runner's test of the entry's defender look; neither the episode
runner nor the manager can mount them elsewhere.

The episode record keeps a version identifier of the volume at the episode's
start, training_volume_in, and at its end, training_volume_out: the SHA-256
digest of the list of the volume's files, each with its path and the digest
of its contents, read inside siem-01. Both are null where no volume is
attached. The idle defender writes nothing to the volume, so the version
stays as it was through each of its episodes. An entry's defender in
training may write to the training volume, and the next fork starts from the
version that the episode left (episodes.md §3.3, reset).
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Protocol

from competition.episode_runner.incus import IncusError, Result
from competition.episode_runner.settle import SIEM

TRAINING = "training"
EVALUATION = "evaluation"
# The devices on siem-01, as the living port's manager names them.
TRAINING_DEVICE = "training-volume"
EVALUATION_DEVICE = "entry-volume"
MOUNT = "/var/lib/entry"
# The digest of the volume's files, in an order that the locale cannot change.
DIGEST = (
    'cd "$1" || exit 3; '
    "find . -type f -print0 | LC_ALL=C sort -z | xargs -0 -r sha256sum | sha256sum"
)


class Incus(Protocol):
    def exec(self, node: str, *argv: str, timeout_s: float = 60.0) -> Result: ...

    def command(self, *args: str, timeout_s: float = 60.0) -> Result: ...

    def query(self, path: str, timeout_s: float = 60.0) -> dict[str, Any]: ...


def open_to_the_container(directory: Path, writable: bool = True) -> None:
    """Let siem-01's users use the volume. The unprivileged container maps
    none of the VM's users, so root in siem-01 gets only the permissions for
    every user: each file becomes readable by every user, and executable by
    every user where its owner may execute it, and each directory readable
    and searchable by every user. A writable volume, the training volume,
    also becomes writable by every user. A symbolic link is left as it is,
    so that nothing outside the volume changes."""
    write = 0o222 if writable else 0

    def open_directory(path: str) -> None:
        os.chmod(path, os.stat(path).st_mode | 0o555 | write)

    for root, dirs, files in os.walk(directory):
        for name in dirs:
            path = os.path.join(root, name)
            if not os.path.islink(path):
                open_directory(path)
        for name in files:
            path = os.path.join(root, name)
            if os.path.islink(path):
                continue
            mode = os.stat(path).st_mode
            run = 0o111 if mode & 0o100 else 0
            os.chmod(path, mode | 0o444 | run | write)
    open_directory(str(directory))


def attach(incus: Incus, directory: Path) -> Result:
    """Attach the training volume, a directory of the fork, read-write to
    siem-01 at /var/lib/entry."""
    return incus.command(
        "config",
        "device",
        "add",
        SIEM,
        TRAINING_DEVICE,
        "disk",
        f"source={directory}",
        f"path={MOUNT}",
    )


def attach_read_only(incus: Incus, directory: Path) -> Result:
    """Attach an entry's submitted volume, a directory of the episode
    runner's host, read-only to siem-01 at /var/lib/entry, as the device
    `entry-volume`, as the living port's manager attaches it in evaluation."""
    return incus.command(
        "config",
        "device",
        "add",
        SIEM,
        EVALUATION_DEVICE,
        "disk",
        f"source={directory}",
        f"path={MOUNT}",
        "readonly=true",
    )


def detach(incus: Incus) -> Result:
    """Detach the training volume from siem-01."""
    return incus.command("config", "device", "remove", SIEM, TRAINING_DEVICE)


def attached(incus: Incus) -> bool:
    """Whether siem-01 holds the evaluation's read-only volume."""
    try:
        devices = incus.query(f"/1.0/instances/{SIEM}").get("devices") or {}
    except (IncusError, AttributeError):
        return False
    return EVALUATION_DEVICE in devices


def version(incus: Incus) -> str | None:
    """The volume's version identifier, or None where it cannot be read."""
    result = incus.exec(SIEM, "sh", "-c", DIGEST, "sh", MOUNT)
    words = result.stdout.split()
    if not result.ok or not words or not re.fullmatch(r"[0-9a-f]{64}", words[0]):
        return None
    return f"sha256:{words[0]}"
