"""
Input-hash symmetry across every node build script (deployment.md §6.1).

§6.1 step 2 requires each per-node build script to compute a hash over "its own
content, all inputs from `build/` it uses, and any artefact cached in step 1",
and to skip the build when that hash matches the published image's
`ravensport.input-hash` property. Two directions of that requirement fail
silently, and no assertion that names particular filenames can cover a node that
later gains a new rendered artefact:

  consumed but not hashed   The script installs a rendered artefact whose path is
                            absent from `current_hash`. Editing the artefact
                            changes no hash, the build reports "inputs unchanged,
                            skipping", and the published image goes on serving the
                            previous contents while the render itself is correct.
                            This is the class of defect behind the
                            FMS_ADMIN_HASH versus FMS_ADMIN_PASSWORD_HASH
                            regression, which surfaced only as a crash loop on
                            the deployed host.

  hashed but not consumed   The script hashes an artefact it never installs, so
                            the service runs on its packaged default while every
                            edit to the artefact rebuilds an image that does not
                            contain it.

Only the first direction is stated by §6.1. The second is not forbidden there,
but it has no legitimate form: an input that changes the image's hash without
changing the image is either an artefact someone forgot to install or one that
should not be hashed.

The assertions are stated over whatever each script actually references, rather
than over a fixed list of filenames, so a node that gains a new rendered artefact
is covered on the render that introduces it with no edit to this file.

Known limit: this establishes that an artefact is installed and hashed, not that
it is installed at the path the consuming software reads. A file pushed to the
wrong destination satisfies every assertion below, and the service then runs on
its packaged default. The destination is a property of the software being
configured rather than of the build, so it cannot be derived from the script;
where it matters it is asserted per artefact elsewhere (see
`test_hist_grafana_artefacts.py`).

Run with: pytest tests/unit -q
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]  # deploy/

# Rendered last, so the shared build/ directory is left in the state the rest of
# the suite renders for itself.
PROFILES = ("full", "ci-minimal")

_HASH_OPEN = "current_hash=$(sha256sum"
_HASH_CLOSE = "| sha256sum"

# Rendered artefacts are referenced by a path relative to deploy/, always under
# build/. Whitespace and quotes terminate the match, so a shell variable such as
# "$GRAFANA_DEB" is not captured: those name pinned upstream downloads resolved
# into the artefact cache rather than rendered files, and §6.1 covers them by a
# separate mechanism asserted in test_hist_artefact_fetch.py.
_BUILD_PATH = re.compile(r"build/[A-Za-z0-9._/-]+")


@lru_cache(maxsize=None)
def _scripts(profile: str) -> dict[str, str]:
    """Render `profile` and return the text of every node build script it emits,
    read immediately, since the next render overwrites the directory."""
    out = subprocess.run(
        [sys.executable, "scripts/render.py", "--profile", profile],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert out.returncode == 0, f"render --profile {profile} failed:\n{out.stderr}"

    build = ROOT / "build"
    manifest = json.loads((build / "manifest.json").read_text())
    scripts: dict[str, str] = {}
    for node in manifest["build_script_nodes"]:
        path = build / "node-build" / f"{node}.sh"
        assert path.exists(), (
            f"manifest.json lists {node} in build_script_nodes for profile "
            f"{profile!r}, but {path.relative_to(ROOT)} was not rendered"
        )
        scripts[node] = path.read_text()
    assert scripts, f"profile {profile!r} rendered no build scripts at all"
    return scripts


def _split_on_hash_list(node: str, text: str) -> tuple[str, str]:
    """Separate a build script into its `current_hash` argument list and the rest
    of the script."""
    if _HASH_OPEN not in text or _HASH_CLOSE not in text.split(_HASH_OPEN, 1)[1]:
        pytest.fail(
            f"{node}.sh computes no input hash: no {_HASH_OPEN!r} list was found. "
            "deployment.md §6.1 requires every per-node build script to hash its "
            "inputs, so that only nodes whose inputs changed are rebuilt."
        )
    head, rest = text.split(_HASH_OPEN, 1)
    hash_list, tail = rest.split(_HASH_CLOSE, 1)
    return hash_list, head + tail


def _paths(fragment: str) -> set[str]:
    """Every `build/` file path named in `fragment`, ignoring whole-line comments.

    A path named only in a comment is neither installed nor omitted, and a
    commented-out entry in the hash list is genuinely not hashed, so both are
    excluded. Trailing comments are not stripped: `#` is not reliably a comment
    introducer mid-line in shell, and stripping it naively would silently
    discard real content.

    A path ending in `/` names a directory, so it is excluded: `sha256sum`
    takes files, and no build script consumes a rendered `build/` directory
    wholesale. Excluding the directory form keeps a directory path in an
    error message from reading as a consumed artefact while leaving every
    file reference checked.
    """
    return {
        path
        for line in fragment.splitlines()
        if not line.lstrip().startswith("#")
        for path in _BUILD_PATH.findall(line)
        if not path.endswith("/")
    }


def _own_path(node: str) -> str:
    return f"build/node-build/{node}.sh"


def _offences(profile: str, check) -> dict[str, list[str]]:
    """`check(node, hashed, consumed) -> set[str]`, applied to every node, with
    empty results dropped."""
    found: dict[str, list[str]] = {}
    for node, text in _scripts(profile).items():
        hash_list, body = _split_on_hash_list(node, text)
        paths = check(node, _paths(hash_list), _paths(body))
        if paths:
            found[node] = sorted(paths)
    return found


@pytest.mark.parametrize("profile", PROFILES)
def test_every_artefact_a_build_script_consumes_is_hashed(profile: str):
    """An input the script uses but does not hash makes the image stale without
    making the render wrong: re-rendering the artefact changes no hash, the build
    skips, and the published image keeps the previous contents. Nothing in the
    build reports it, and a reproducibility check over the rendered files passes,
    because the rendered files are correct — they simply never reached the image.
    """
    offences = _offences(
        profile,
        lambda node, hashed, consumed: consumed - hashed - {_own_path(node)},
    )
    assert not offences, (
        "build scripts reference rendered artefacts that their current_hash does "
        f"not cover (profile {profile!r}): {offences}. Editing one of these "
        "leaves the input hash unchanged, so the build reports 'inputs "
        "unchanged, skipping' and the published image goes on serving the "
        "previous contents. Add each path to that script's current_hash list."
    )


@pytest.mark.parametrize("profile", PROFILES)
def test_every_artefact_a_build_script_hashes_is_one_it_consumes(profile: str):
    """The converse failure is quieter but not harmless: the artefact is rendered
    and hashed but never installed, so the service runs on its packaged default
    and every edit rebuilds an image the edit does not reach."""
    offences = _offences(
        profile,
        lambda node, hashed, consumed: hashed - consumed - {_own_path(node)},
    )
    assert not offences, (
        "build scripts hash rendered artefacts they never install (profile "
        f"{profile!r}): {offences}. Each is either an artefact whose 'incus file "
        "push' is missing — in which case the service is running on its packaged "
        "default — or one that should not be hashed, in which case editing it "
        "rebuilds the image without changing it."
    )


@pytest.mark.parametrize("profile", PROFILES)
def test_every_build_script_hashes_itself(profile: str):
    """§6.1 step 2 puts the script's own content in the hash. Without it a change
    to the build steps — a new package, a changed systemd unit, a different push
    destination — rebuilds nothing, because no rendered artefact it names has
    changed. This is the one entry that is hashed and legitimately not consumed,
    which is why the two assertions above exempt it.
    """
    offences = _offences(
        profile,
        lambda node, hashed, consumed: (
            set() if _own_path(node) in hashed else {_own_path(node)}
        ),
    )
    assert not offences, (
        f"build scripts that do not hash their own content (profile {profile!r}): "
        f"{sorted(offences)}. A change to the build steps alone would rebuild "
        "nothing, since no artefact the script names would have changed."
    )
