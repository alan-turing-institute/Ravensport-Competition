"""
Package index freshness in every node build script (deployment.md §6.1).

§6.1 step 3 has each per-node build script install its packages with `apt-get`
inside a throwaway container created from the Ubuntu base image. `apt-get
install` resolves names against the index on disk, and the base image carries
one frozen at the moment its daily was built. Ubuntu's pool holds only the
current version of a package in each pocket, so once a package is superseded
the index names a path that no longer exists and the install fails with a 404 —
on an input nobody changed, in a build that succeeded the day before.

Observed on the deployment: base image daily `20260916_14:46` named
`perl 5.38.2-3.2ubuntu0.4`, superseded and pruned within two days. `fms-01`
(postgresql) and `corp-it` (slapd) both depend on perl, so both builds failed.
Neither image was republished, and the `fms-01` image left in place predated
the internal certificate authority and went on serving a self-signed
certificate that no current build would produce.

The remedy is `apt-get update` between the container's creation and its first
install. The hand-written script this template family replaced performed it;
the conversion to templates dropped it, and every template written since
inherited the omission.

Position is the whole of the requirement, so both orderings are asserted:

  after the cloud-init wait   cloud-init performs its own apt operations and
                              holds the dpkg lock until it has finished, so a
                              refresh before it either fails on the lock or is
                              undone by what cloud-init does next.

  before the first install    the index is what `apt-get install` reads. A
                              refresh placed after the installs updates nothing
                              the build went on to use.

An assertion that `apt-get update` appears somewhere in the script passes on a
script that runs it last, which is why neither ordering is left implicit.

Stated over the rendered scripts rather than the templates, so a template that
renders differently per node is covered per node, and a node that gains a new
template is covered on the render that introduces it.

Not covered: the `sim-host` image, which is Alpine and is built by
`scripts/build-sim-image.sh` rather than this template family. Its
`apk --root ... --no-cache` fetches the index on every run, so it cannot hold a
stale one.

Run with: pytest tests/unit -q
"""

from __future__ import annotations

import json
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]  # deploy/

# ci-minimal is rendered last, so the shared build/ directory is left in the
# state the rest of the suite renders for itself.
PROFILES = ("full", "ci-minimal")

_WAIT = "cloud-init status --wait"
_UPDATE = "apt-get update"
_INSTALL = "apt-get install"


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


@lru_cache(maxsize=None)
def _template_of() -> dict[str, str]:
    """Each node's build-script template, so a failure names the file to edit.

    The assertions are stated over rendered scripts, but the correction is made
    in the template, and one template serves several nodes — `fw-zone.sh.j2`
    renders all three firewall nodes. Without this the report lists the same
    edit three times and names it none.
    """
    topology = yaml.safe_load((ROOT / "topology.yaml").read_text())
    return {
        name: f"{node['build_script']}.sh.j2"
        for name, node in topology["nodes"].items()
        if node.get("build_script")
    }


def _line_of(text: str, needle: str) -> int | None:
    """The 1-based line number of the first line running `needle`, or None.

    Whole-line comments are skipped: a commented-out command is not run, so
    counting one would let a script pass on a line that does nothing. Trailing
    comments are not stripped, since `#` is not reliably a comment introducer
    mid-line in shell.
    """
    for number, line in enumerate(text.splitlines(), start=1):
        if line.lstrip().startswith("#"):
            continue
        if needle in line:
            return number
    return None


def _offences(profile: str, check) -> dict[str, str]:
    """`check(text) -> str | None`, applied to every node's script, with the
    nodes that returned no complaint dropped. Keyed by the template to edit as
    well as the node, since the two are not one to one."""
    templates = _template_of()
    found: dict[str, str] = {}
    for node, text in _scripts(profile).items():
        complaint = check(text)
        if complaint:
            found[f"{node} ({templates.get(node, 'template unknown')})"] = complaint
    return dict(sorted(found.items()))


@pytest.mark.parametrize("profile", PROFILES)
def test_every_build_script_refreshes_the_index_before_its_first_install(profile: str):
    """Without the refresh the build installs against whatever the base image's
    daily happened to capture. That index is correct on the day it is cut and
    decays from then on, so the build's success depends on how recently the
    daily was built and on whether anything it names has since been superseded —
    neither of which is an input to the build or visible in its output."""

    def check(text: str) -> str | None:
        install = _line_of(text, _INSTALL)
        if install is None:
            return None  # installs nothing, so has no index to refresh
        update = _line_of(text, _UPDATE)
        if update is None:
            return f"no {_UPDATE!r}; first install at line {install}"
        if update > install:
            return f"{_UPDATE!r} at line {update}, after the install at line {install}"
        return None

    offences = _offences(profile, check)
    assert not offences, (
        f"build scripts that install without refreshing the package index "
        f"(profile {profile!r}): {offences}. Each resolves package names "
        "against the index baked into the Ubuntu base image, so any package "
        "superseded and pruned from the pool since that image was built fails "
        f'to fetch with a 404. Add `incus exec "$BUILD_CTR" -- {_UPDATE}` '
        "after the cloud-init wait and before the first install."
    )


@pytest.mark.parametrize("profile", PROFILES)
def test_every_index_refresh_follows_the_cloud_init_wait(profile: str):
    """cloud-init runs its own apt operations in the freshly launched container
    and holds the dpkg lock until it reports completion. A refresh issued before
    that point contends for the lock, and anything it does retrieve is liable to
    be replaced by cloud-init's own work."""

    def check(text: str) -> str | None:
        update = _line_of(text, _UPDATE)
        if update is None:
            return None  # reported by the preceding test
        wait = _line_of(text, _WAIT)
        if wait is None:
            return f"no {_WAIT!r} before the refresh at line {update}"
        if update < wait:
            return f"{_UPDATE!r} at line {update}, before the wait at line {wait}"
        return None

    offences = _offences(profile, check)
    assert not offences, (
        f"build scripts that refresh the package index before cloud-init has "
        f"finished (profile {profile!r}): {offences}. cloud-init holds the dpkg "
        "lock until it reports completion, so the refresh contends with it and "
        "any index it retrieves may be superseded by cloud-init's own apt work."
    )
