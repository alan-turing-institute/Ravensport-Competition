"""Fixtures of the competition image's suite (episodes-tests.md §12, reset and
the competition image).

The suite builds the images of RavensPort with deploy/'s Makefile, with and
without the competition's additional override file (episodes.md §3.4,
competition image), and exports the two images of siem-01. Both images of
siem-01 are built afresh in each run, from the same Ubuntu base image and
package archive. The suite needs incus and the build's network access, and no
deployment.
"""

from __future__ import annotations

import itertools
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
DEPLOY = REPO / "deploy"
# Relative to deploy/, where make runs.
OVERRIDE = "../competition/deploy/override.yaml"
OPERATIONAL_IMAGE = "ravensport/siem-01:latest"
COMPETITION_IMAGE = "ravensport/siem-01-competition:latest"


def pytest_runtest_setup() -> None:
    if shutil.which("incus") is None:
        pytest.skip("incus client not found on the test runner")


# Every build's whole output, kept so that a failing build's own error can be
# read, since a failed image build reports only the nodes that failed.
LOGS = Path(tempfile.mkdtemp(prefix="competition-image-builds-"))
_builds = itertools.count(1)


def make_build(with_override: bool) -> str:
    """Run `make build`, keep its whole output in LOGS, and return it."""
    cmd = ["make", "build"]
    if with_override:
        cmd.append(f"EXTRA_OVERRIDE={OVERRIDE}")
    out = subprocess.run(cmd, cwd=DEPLOY, capture_output=True, text=True, check=False)
    log = out.stdout + out.stderr
    path = (
        LOGS
        / f"build-{next(_builds):02d}-{'competition' if with_override else 'operational'}.log"
    )
    path.write_text(log)
    assert out.returncode == 0, (
        f"{' '.join(cmd)} failed; its whole output is in {path}:\n{log}"
    )
    return log


def image_fingerprints() -> dict[str, str]:
    """The fingerprint of each image, by its alias."""
    out = subprocess.run(
        ["incus", "image", "list", "--format", "csv", "-c", "lf"],
        capture_output=True,
        text=True,
        check=True,
    )
    pairs = (line.split(",", 1) for line in out.stdout.splitlines() if line.strip())
    return {alias: fingerprint for alias, fingerprint in pairs if alias}


INPUT_HASH = "ravensport.input-hash"


def force_rebuild(alias: str) -> None:
    """Make the next build of the image `alias` build it afresh. A build skips
    an image whose recorded input hash matches its inputs (deployment.md §6.1),
    and this one's no longer does. The image stays in the store until the
    build publishes its replacement. An absent image is built anyway."""
    present = subprocess.run(
        ["incus", "image", "get-property", alias, INPUT_HASH],
        capture_output=True,
        text=True,
        check=False,
    )
    if present.returncode == 0:
        subprocess.run(
            ["incus", "image", "set-property", alias, INPUT_HASH, "rebuild"],
            capture_output=True,
            text=True,
            check=True,
        )


@pytest.fixture(scope="session")
def both_images() -> dict[str, str]:
    """Build with and without the competition's additional override file, so
    that both images of siem-01 exist, and return every image's fingerprint.

    Both images of siem-01 are built in this run. Each build launches the
    Ubuntu base image of its day and installs the packages current that day,
    so an image kept from an earlier build can hold other patch versions of
    the base packages, such as libc6, than one built today. Their comparison
    would then fail on a difference that neither the competition's software
    nor its build made."""
    for alias in (COMPETITION_IMAGE, OPERATIONAL_IMAGE):
        force_rebuild(alias)
    make_build(with_override=True)
    make_build(with_override=False)
    fingerprints = image_fingerprints()
    assert OPERATIONAL_IMAGE in fingerprints
    assert COMPETITION_IMAGE in fingerprints
    return fingerprints


@pytest.fixture(scope="session")
def exported(both_images, tmp_path_factory) -> dict[str, Path]:
    """Each image of siem-01, exported as a unified image archive."""
    paths: dict[str, Path] = {}
    for kind, alias in (
        ("operational", OPERATIONAL_IMAGE),
        ("competition", COMPETITION_IMAGE),
    ):
        target = tmp_path_factory.mktemp(kind) / "image"
        subprocess.run(
            ["incus", "image", "export", alias, str(target)],
            capture_output=True,
            text=True,
            check=True,
        )
        (archive,) = target.parent.glob("image*")
        paths[kind] = archive
    return paths


def read_members(archive: Path, wanted) -> dict[str, tuple[int, bytes | None]]:
    """The mode and content of every member of the archive whose path,
    relative to the image's root filesystem, `wanted` accepts. A symbolic
    link's content is its target, and a directory has none."""
    members: dict[str, tuple[int, bytes | None]] = {}
    with tarfile.open(archive) as tar:
        for member in tar:
            if not member.name.startswith("rootfs/"):
                continue
            path = member.name[len("rootfs/") :]
            if not wanted(path):
                continue
            if member.isfile():
                content = tar.extractfile(member).read()
            elif member.issym():
                content = f"-> {member.linkname}".encode()
            else:
                content = None
            members[path] = (member.mode, content)
    return members
