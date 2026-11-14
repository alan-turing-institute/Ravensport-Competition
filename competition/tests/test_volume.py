"""The entry's storage volume on siem-01 in a fork of a living port
(episodes.md §3.2, modes).

Run with: pytest competition/tests -q
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from competition.episode_runner import volume
from competition.episode_runner.incus import IncusError, Result


class FakeIncus:
    def __init__(self, stdout: str = "", returncode: int = 0, devices=None) -> None:
        self.stdout, self.returncode, self.devices = stdout, returncode, devices
        self.calls: list[tuple] = []

    def exec(self, node: str, *argv: str, timeout_s: float = 60.0) -> Result:
        self.calls.append((node, argv))
        return Result(self.returncode, self.stdout, "")

    def command(self, *args: str, timeout_s: float = 60.0) -> Result:
        self.calls.append(args)
        return Result(self.returncode, "", "")

    def query(self, path: str, timeout_s: float = 60.0) -> dict:
        if self.devices is None:
            raise IncusError("incus query failed")
        return {"devices": self.devices}


def test_the_version_is_the_digest_of_the_volumes_files() -> None:
    digest = "0f" * 32
    incus = FakeIncus(stdout=f"{digest}  -\n")
    assert volume.version(incus) == f"sha256:{digest}"
    ((node, argv),) = incus.calls
    assert node == "siem-01"
    assert argv == ("sh", "-c", volume.DIGEST, "sh", "/var/lib/entry")


def test_a_volume_that_cannot_be_read_has_no_version() -> None:
    assert volume.version(FakeIncus(returncode=3)) is None
    assert volume.version(FakeIncus(stdout="not a digest\n")) is None


def test_an_evaluation_volume_is_the_living_ports_read_only_device() -> None:
    assert volume.attached(FakeIncus(devices={"entry-volume": {"readonly": "true"}}))
    assert not volume.attached(FakeIncus(devices={"eth0": {}}))
    assert not volume.attached(FakeIncus(devices=None))


def test_the_attach_and_the_detach_change_the_device_of_siem_01() -> None:
    """The training volume mounts at /var/lib/entry, the one path that the
    launcher and the episode runner's test read (episode-runner-impl.md §5,
    forks)."""
    incus = FakeIncus()
    volume.attach(incus, Path("/var/lib/competition/training/fork-3"))
    volume.detach(incus)
    assert incus.calls == [
        (
            "config",
            "device",
            "add",
            "siem-01",
            "training-volume",
            "disk",
            "source=/var/lib/competition/training/fork-3",
            "path=/var/lib/entry",
        ),
        ("config", "device", "remove", "siem-01", "training-volume"),
    ]


def mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def entry_files(directory: Path) -> Path:
    """A volume as an entrant may submit it: a defender of mode 0700, a model
    of mode 0600 in a directory of mode 0700, and a link out of the volume."""
    (directory / "defender").write_text("#!/bin/sh\n")
    (directory / "defender").chmod(0o700)
    (directory / "state").mkdir(mode=0o700)
    (directory / "state" / "model.bin").write_bytes(b"\x00")
    (directory / "state" / "model.bin").chmod(0o600)
    outside = directory.parent / "outside"
    outside.write_text("not the volume's\n")
    outside.chmod(0o600)
    (directory / "link").symlink_to(outside)
    return outside


@pytest.mark.parametrize(
    ("writable", "directories", "defender", "model"),
    [(True, 0o777, 0o777, 0o666), (False, 0o755, 0o755, 0o644)],
    ids=["training", "evaluation"],
)
def test_the_entrys_volume_opens_to_the_containers_users(
    tmp_path: Path, writable: bool, directories: int, defender: int, model: int
) -> None:
    """Root in the unprivileged siem-01 gets the permissions for every user,
    so a volume as an entrant may submit it, with directories of mode 0700,
    opens to them: each directory readable and searchable, each file
    readable, and executable where its owner may execute it. The training
    volume also becomes writable; the evaluation volume, which the run
    command attaches at fork 0, does not. A link out of the volume changes
    nothing outside it (episodes-tests.md,
    the_entrys_volume_opens_to_the_containers_users)."""
    volume_dir = tmp_path / "volume"
    volume_dir.mkdir(mode=0o700)
    outside = entry_files(volume_dir)
    volume.open_to_the_container(volume_dir, writable=writable)
    assert mode(volume_dir) == directories
    assert mode(volume_dir / "state") == directories
    assert mode(volume_dir / "defender") == defender
    assert mode(volume_dir / "state" / "model.bin") == model
    assert mode(outside) == 0o600
