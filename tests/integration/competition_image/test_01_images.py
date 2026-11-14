"""The operational and the competition images of siem-01 (episodes-tests.md
§12, reset and the competition image; episodes.md §3.4, competition image).

Run with: make -C tests/integration test-competition-image
"""

from __future__ import annotations

import re

from tests.integration.competition_image.conftest import (
    DEPLOY,
    REPO,
    image_fingerprints,
    make_build,
    read_members,
)

# The competition's programs: the idle defender's program, the launcher and
# the query interface, each by its path in the image and its source.
PROGRAMS = {
    "usr/local/bin/idle-defender": REPO
    / "competition"
    / "idle_defender"
    / "idle-defender",
    "usr/local/bin/defender": REPO / "competition" / "launcher" / "defender",
    "usr/local/bin/siem-read": REPO / "competition" / "siem_read" / "siem-read",
}
DPKG_STATUS = "var/lib/dpkg/status"
# The configuration files: the system's, and those of the security monitoring.
CONFIGURATION = ("etc/", "var/ossec/etc/")
# Files that every build of an image writes afresh, so two builds of the same
# steps differ in them: the build container's name and machine identity, the
# Secure Shell (SSH) host keys that its first boot generates, and the time
# stamp in the sudoers file that cloud-init writes before the build removes it.
PER_BUILD = frozenset(
    {
        "etc/hostname",
        "etc/machine-id",
        "etc/sudoers.d/90-cloud-init-users",
        *(
            f"etc/ssh/ssh_host_{kind}_key{suffix}"
            for kind in ("ecdsa", "ed25519", "rsa")
            for suffix in ("", ".pub")
        ),
    }
)


# The output line of an image build, and the alias in a rendered build script.
BUILT = re.compile(r"^==> Building (ravensport/\S+:latest)", re.MULTILINE)
ALIAS = re.compile(r'^IMAGE_ALIAS="(ravensport/[^"]+)"', re.MULTILINE)
# What a render keeps in deploy/build: the certificate authority's files.
KEPT = frozenset({"ca", "tls"})
# The security monitoring's three schedules on siem-01, whose values the
# compression factor scales (siem.md §2.1, the agent on every monitored host).
SCALED = (
    re.compile(rb"(<syscheck>.*?<frequency>)\d+(</frequency>)", re.DOTALL),
    re.compile(
        rb'(<wodle name="syscollector">.*?<interval>)\d+s?(</interval>)', re.DOTALL
    ),
    re.compile(
        rb"(<alias>netstat listening ports</alias>\s*<frequency>)\d+(</frequency>)"
    ),
)
OSSEC_CONF = "var/ossec/etc/ossec.conf"


def _comparable(
    path: str, member: tuple[int, bytes | None] | None
) -> tuple[int, bytes | None] | None:
    """A member's mode and content, with the values of the scaled schedules
    taken out of the security monitoring's configuration, since the two images
    come from renders at different compression factors (episodes.md §5.2, the
    compression factor)."""
    if member is None:
        return None
    mode, content = member
    if path == OSSEC_CONF and content is not None:
        for pattern in SCALED:
            content = pattern.sub(rb"\g<1>SCALED\g<2>", content)
    return mode, content


def _wanted(path: str) -> bool:
    return path == DPKG_STATUS or path in PROGRAMS or path.startswith(CONFIGURATION)


def _packages(status: bytes) -> dict[str, tuple[str, str]]:
    """Each installed package's version and status, from /var/lib/dpkg/status."""
    packages: dict[str, tuple[str, str]] = {}
    for stanza in status.decode().split("\n\n"):
        fields = dict(
            line.split(": ", 1)
            for line in stanza.splitlines()
            if ": " in line and not line.startswith(" ")
        )
        if "Package" in fields:
            packages[fields["Package"]] = (
                fields.get("Version", ""),
                fields.get("Status", ""),
            )
    return packages


def _rebuilt(log: str) -> set[str]:
    """The aliases of the images that a build's output says it built."""
    return set(BUILT.findall(log))


def _render() -> dict[str, bytes]:
    """Every file that the last render wrote under deploy/build, by its path,
    apart from the certificate authority's files, which a render keeps."""
    build = DEPLOY / "build"
    return {
        str(path.relative_to(build)): path.read_bytes()
        for path in sorted(build.rglob("*"))
        if path.is_file() and path.relative_to(build).parts[0] not in KEPT
    }


def _aliases(render: dict[str, bytes]) -> dict[str, str]:
    """The alias of the image that each node's build script builds, by node."""
    aliases = {}
    for path, content in render.items():
        if path.startswith("node-build/") and path.endswith(".sh"):
            match = ALIAS.search(content.decode())
            if match:
                aliases[path[len("node-build/") : -len(".sh")]] = match.group(1)
    return aliases


def _changed(one: dict[str, bytes], other: dict[str, bytes]) -> set[str]:
    """The aliases that the two renders share, of the nodes whose rendered
    inputs differ between them. A node's rendered inputs are the files whose
    path names the node."""
    first, second = _aliases(one), _aliases(other)
    differing = {
        path for path in one.keys() | other.keys() if one.get(path) != other.get(path)
    }
    return {
        alias
        for node, alias in first.items()
        if second.get(node) == alias and any(node in path for path in differing)
    }


def test_a_build_rebuilds_only_the_images_whose_inputs_change(both_images) -> None:
    # The fixture's last build was without the competition's file.
    repeated = _rebuilt(make_build(with_override=False))
    assert not repeated, f"a repeated build rebuilt {sorted(repeated)}"
    operational = _render()

    to_competition = _rebuilt(make_build(with_override=True))
    competition = _render()
    repeated = _rebuilt(make_build(with_override=True))
    assert not repeated, f"a repeated build rebuilt {sorted(repeated)}"
    to_operational = _rebuilt(make_build(with_override=False))

    changed = _changed(operational, competition)
    assert changed, "the two renders differ in no image that they share"
    assert to_competition == changed, (
        f"the switch to the competition's render rebuilt {sorted(to_competition)}, "
        f"and the images whose inputs differ are {sorted(changed)}"
    )
    assert to_operational == changed, (
        f"the switch back rebuilt {sorted(to_operational)}, "
        f"and the images whose inputs differ are {sorted(changed)}"
    )
    assert set(image_fingerprints()) == set(both_images)


def test_the_competition_image_adds_only_the_competition_software(exported) -> None:
    operational = read_members(exported["operational"], _wanted)
    competition = read_members(exported["competition"], _wanted)

    assert _packages(operational[DPKG_STATUS][1]) == _packages(
        competition[DPKG_STATUS][1]
    )

    configuration = (set(operational) | set(competition)) - {DPKG_STATUS, *PROGRAMS}
    differing = sorted(
        path
        for path in configuration - PER_BUILD
        if _comparable(path, operational.get(path))
        != _comparable(path, competition.get(path))
    )
    assert not differing, (
        f"configuration files differ between the two images: {differing}"
    )

    # The launcher, the idle defender's program and the query interface exist
    # in the competition image alone, each as its source holds it.
    for path, source in PROGRAMS.items():
        assert path not in operational, path
        mode, content = competition[path]
        assert mode & 0o777 == 0o755, path
        assert content == source.read_bytes(), path
