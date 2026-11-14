"""recon-01 browser artefact pinning. Run with: pytest tests/unit -q

Covers `recon.md` §6.2's three departures from every other pinned artefact. Each
fails silently if unhandled: the first yields a URL that does not exist, the
second a missing digest rather than a wrong one, and the third an image built
from a version no document names.

Asserted against the rendered `_artefacts.sh.j2` fragment rather than by running
it, since fetching would need the network and the point is the shape of the
request rather than its result.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # deploy/
sys.path.insert(0, str(ROOT / "scripts"))

ARTEFACTS = (ROOT / "build-scripts" / "_artefacts.sh.j2").read_text()
RECON_SPEC = (ROOT.parent / "specs" / "host_specs" / "recon" / "recon.md").read_text()


def _assignment(name: str) -> str:
    """The value of a top-level `NAME="value"` assignment in the fragment."""
    match = re.search(rf'^{name}="([^"]+)"', ARTEFACTS, re.MULTILINE)
    assert match, f"{name} is not assigned in _artefacts.sh.j2"
    return match.group(1)


def test_the_architecture_mapping_is_applied():
    """Mozilla publishes `x86_64`/`aarch64`; the fleet's `ARTEFACT_ARCH` is
    `amd64`/`arm64`. The firefox case must read the mapped value, or the
    download path names an architecture Mozilla does not publish."""
    case = ARTEFACTS.split("firefox)", 1)[1].split(";;", 1)[0]
    assert "MOZILLA_ARCH" in case, (
        "the firefox artefact does not use MOZILLA_ARCH, so the URL carries "
        "the fleet's architecture name and resolves to nothing"
    )
    assert "${ARTEFACT_ARCH}" not in case, (
        "the firefox artefact uses ARTEFACT_ARCH; Mozilla does not publish "
        "`amd64` or `arm64` paths"
    )
    for uname, mozilla in (("x86_64", "x86_64"), ("aarch64|arm64", "aarch64")):
        assert re.search(
            rf'{re.escape(uname)}\)\s+ARTEFACT_ARCH=.*MOZILLA_ARCH="{mozilla}"',
            ARTEFACTS,
        ), f"no MOZILLA_ARCH={mozilla} for uname {uname}"


def test_the_digest_is_selected_from_a_path_keyed_checksums_file():
    """Mozilla's `SHA256SUMS` covers every locale and architecture in one file,
    keyed by a path relative to the release directory.

    The bare-filename match the other artefacts use selects no row against
    `linux-x86_64/en-GB/firefox-…tar.xz`, which presents as a missing digest
    rather than a wrong one — so the failure is a build that cannot verify,
    not one that verifies against the wrong thing.
    """
    case = ARTEFACTS.split("firefox)", 1)[1].split(";;", 1)[0]
    assert 'digest_format="sha256sum_path"' in case, (
        "the firefox artefact does not use the path-keyed digest format"
    )
    assert "digest_key=" in case, "no digest_key is set for the path-keyed match"
    handler = ARTEFACTS.split("sha256sum_path)", 1)[1].split(";;", 1)[0]
    assert 'awk -v f="$digest_key"' in handler, (
        "the sha256sum_path handler does not match on digest_key, so it falls "
        f"back to the bare-filename behaviour it exists to replace: {handler!r}"
    )


def test_the_cached_file_name_distinguishes_architecture_and_locale():
    """The bare filename is identical across architectures and locales, so a
    cache shared between build hosts would collide. Not one of §6.2's three, but
    a consequence of them: a collision serves one architecture's tarball to the
    other, and the digest check then fails with nothing naming the cause."""
    case = ARTEFACTS.split("firefox)", 1)[1].split(";;", 1)[0]
    final_file = re.findall(r'file="([^"]+)"', case)[-1]
    assert "MOZILLA_ARCH" in final_file and "FIREFOX_LOCALE" in final_file, (
        f"the cached name is {final_file!r}, which does not vary by "
        "architecture and locale"
    )


def test_the_pinned_version_is_the_one_the_specification_names():
    """Guards the version drifting from `recon.md` §6.2, which states it as a
    property of the host rather than as a build detail."""
    version = _assignment("FIREFOX_VERSION")
    assert version in RECON_SPEC, (
        f"_artefacts.sh.j2 pins Firefox {version}, which `recon.md` does not "
        "name. The pinned version is specified, so the two move together."
    )


def test_the_locale_is_the_one_the_specification_names():
    """Mozilla builds per locale, so the locale is part of the download path
    rather than a runtime setting (`recon.md` §6.2)."""
    locale = _assignment("FIREFOX_LOCALE")
    assert locale in RECON_SPEC, (
        f"_artefacts.sh.j2 pins the {locale} build, which `recon.md` does not name."
    )
