"""
Pinned-artefact fetch tests for the log servers (hist-tests.md §8), targeting
hist.md §2.1's version table and §10.2's build steps.

The log servers are the first nodes to install upstream release artefacts: the
FMS ships a binary this repository builds, and every other node installs from the
distribution archive. Five downloads therefore arrive with no existing precedent
for how they are pinned, verified, or cached, and this module is where that
contract is fixed.

Assertions are on the rendered `build/node-build/hist-*.sh` rather than on the
Jinja templates, because the fetch logic is shared through an include and a
mistake in the include is only observable once expanded.

Run with: pytest tests/unit -q
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]  # deploy/

_CACHE_DIR = ".artefact-cache"

# hist.md §2.1. Keyed by the artefact name the build script asks for; the value
# is the pinned version, which must appear in the URL the script resolves.
_PINS = {
    "victoria-metrics": "1.148.0",
    "vmutils": "1.148.0",
    "victoria-logs": "1.52.0",
    "vlutils": "1.52.0",
    "grafana": "12.4.6",
    "victoriametrics-logs-datasource": "0.30.1",
}

# hist.md §2: the agent tarballs belong to hist-01 alone. vmagent ships inside
# vmutils and vlagent inside vlutils.
_AGENT_ARTEFACTS = {"vmutils", "vlutils"}

_EXPECTED = {
    "hist-01": set(_PINS),
    "hist-dmz-01": set(_PINS) - _AGENT_ARTEFACTS,
}

# The Grafana datasource plugin differs from the other five in three ways, each
# of which the fetch helper has to handle specially (hist.md §2.1).
_PLUGIN = "victoriametrics-logs-datasource"
_PLUGIN_SHA256 = "328e4fd2057de057e3646fdcfcefd43e62005b99b9677489fa64c77c26395946"


def _render(profile: str = "ci-minimal") -> Path:
    out = subprocess.run(
        [sys.executable, "scripts/render.py", "--profile", profile],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert out.returncode == 0, out.stderr
    return ROOT / "build"


def _script(node: str) -> str:
    return (_render() / "node-build" / f"{node}.sh").read_text()


def _fetch_calls(script: str) -> dict[str, str]:
    """Map each artefact the script fetches to the shell variable holding its
    cached path, from lines of the form `VAR=$(fetch_artefact <name>)`.

    Recovering the variable name rather than assuming it lets the later
    input-hash assertion check the artefact is hashed without also dictating what
    the build script calls it.
    """
    pattern = re.compile(r"^(\w+)=\$\(fetch_artefact\s+(\S+)\)", re.MULTILINE)
    return {artefact: var for var, artefact in pattern.findall(script)}


def _hash_block(node: str, script: str) -> str:
    """The `current_hash` assignment, joined across its line continuations."""
    joined = script.replace("\\\n", " ")
    for line in joined.splitlines():
        if line.startswith("current_hash="):
            return line
    pytest.fail(
        f"{node}'s build script has no current_hash assignment, so every "
        "`make build-node-images` would rebuild the image from scratch"
    )


def _fetch_function(node: str, script: str) -> str:
    """The body of fetch_artefact, from its definition to the closing brace at
    column zero."""
    lines = script.splitlines()
    for i, line in enumerate(lines):
        if re.match(r"^fetch_artefact\s*\(\)", line):
            for j in range(i + 1, len(lines)):
                if lines[j] == "}":
                    return "\n".join(lines[i : j + 1])
            pytest.fail(f"{node}: fetch_artefact is not closed by a '}}' at column 0")
    pytest.fail(
        f"{node}'s build script defines no fetch_artefact function. hist.md §10.2 "
        "requires the upstream artefacts to be fetched and verified on the build "
        "host before the image is assembled."
    )


def _case_arm(node: str, artefact: str) -> str:
    """The body of the `case` arm that resolves one artefact to a URL.

    Several artefacts share an arm (`victoria-metrics|vmutils`), so the label is
    matched against each alternative rather than the whole line.
    """
    lines = _fetch_function(node, _script(node)).splitlines()
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.endswith(")"):
            continue
        labels = [x.strip() for x in stripped.removesuffix(")").split("|")]
        if artefact in labels:
            arm: list[str] = []
            for j in range(i + 1, len(lines)):
                if lines[j].strip() == ";;":
                    return "\n".join(arm)
                arm.append(lines[j])
    pytest.fail(
        f"{node}'s fetch_artefact has no case arm for {artefact!r}; it would fall "
        "through to the unknown-artefact branch and fail the build"
    )


# ── The artefact set matches the service set ──────────────────────────────────


@pytest.mark.parametrize("node", sorted(_EXPECTED))
def test_each_host_fetches_exactly_the_artefacts_its_services_need(node: str):
    """hist-01 runs both stores and both agents; the replica runs the stores
    alone. Fetching an agent tarball onto the replica would put the binary for an
    outbound remote-write path on the host that must not have one, whether or not
    a unit is written for it.

    The Grafana datasource plugin is fetched by *both* hosts, because both
    provision the VictoriaLogs datasource it implements (hist.md §7). That is not
    a relaxation of the rule above: the plugin's backend queries a store on
    Grafana's behalf and cannot originate a remote-write, so it is not a path out
    of the DMZ. The set is asserted exactly, so adding an artefact to one host
    without deciding it belongs there is a failure rather than a silent drift.
    """
    fetched = set(_fetch_calls(_script(node)))
    assert fetched == _EXPECTED[node], (
        f"{node} fetches {sorted(fetched)}; hist.md §2 gives it "
        f"{sorted(_EXPECTED[node])}"
    )


# ── Pinned, not resolved at build time ────────────────────────────────────────


@pytest.mark.parametrize("node", sorted(_EXPECTED))
def test_every_artefact_is_pinned_to_its_specified_version(node: str):
    """A version resolved at build time makes the image non-reproducible: two
    builds a week apart would install different software from identical inputs,
    and the input hash would not change to say so."""
    script = _script(node)
    for artefact in _EXPECTED[node]:
        version = _PINS[artefact]
        assert version in script, (
            f"{node} fetches {artefact} without the pinned version {version} "
            "appearing anywhere in the script (hist.md §2.1)"
        )


@pytest.mark.parametrize("node", sorted(_EXPECTED))
def test_no_artefact_is_resolved_through_a_latest_redirect(node: str):
    """GitHub's `/releases/latest` redirect is both non-reproducible and subject
    to the unauthenticated API rate limit, which fails the build for a reason
    unrelated to anything that changed."""
    script = _script(node)
    for floating in ("/releases/latest", "/latest/download"):
        assert floating not in script, (
            f"{node} resolves an artefact through {floating}; hist.md §2.1 "
            "requires a pinned version"
        )


@pytest.mark.parametrize("node", sorted(_EXPECTED))
def test_architecture_is_resolved_on_the_build_host(node: str):
    """The same script builds on amd64 and arm64 (hist.md §2.1). A hard-coded
    architecture in a URL produces an image whose binaries do not run, and the
    failure appears at first boot rather than at build."""
    script = _script(node)
    assert "uname -m" in script, (
        f"{node} does not derive the artefact architecture from the build host"
    )
    baked = [
        line.strip()
        for line in script.splitlines()
        if "http" in line and ("amd64" in line or "arm64" in line)
    ]
    assert not baked, f"{node} hard-codes an architecture into a download URL: {baked}"


# ── Verified before it enters an image ────────────────────────────────────────


@pytest.mark.parametrize("node", sorted(_EXPECTED))
def test_artefacts_are_checksummed_against_the_published_digest(node: str):
    """hist.md §2.1: every artefact is verified before it enters an image. The
    VictoriaMetrics projects publish `<asset>_checksums.txt` and Grafana a
    `.sha256` sidecar, so both forms must be read."""
    body = _fetch_function(node, _script(node))
    assert "sha256sum" in body, (
        f"{node}'s fetch_artefact computes no digest, so a truncated or "
        "substituted download would be installed unnoticed"
    )
    for source in ("_checksums.txt", ".sha256"):
        assert source in body, (
            f"{node}'s fetch_artefact never reads {source}; it has nothing to "
            "compare the downloaded artefact against"
        )


@pytest.mark.parametrize("node", sorted(_EXPECTED))
def test_a_failed_verification_does_not_leave_the_artefact_cached(node: str):
    """A cache is only safe if a bad entry cannot be reused. If a mismatched
    download survives in `.artefact-cache/`, the next build finds the file
    present, skips the download, and the corruption becomes permanent."""
    body = _fetch_function(node, _script(node))
    assert re.search(r"\brm\b[^\n]*\$\{?\w*path", body, re.IGNORECASE), (
        f"{node}'s fetch_artefact does not remove the cached file when "
        "verification fails, so a bad download would persist across builds"
    )


@pytest.mark.parametrize("node", sorted(_EXPECTED))
def test_artefacts_are_fetched_on_the_build_host_not_in_the_container(node: str):
    """The build container reaches the internet through a NAT bridge, so an
    in-container download would work — and would defeat the cache, repeat the
    transfer on every build, and place the verification inside the environment
    being provisioned rather than before it.

    """
    script = _script(node)
    offenders = [
        line.strip()
        for line in script.splitlines()
        if "incus exec" in line and re.search(r"\b(curl|wget)\b", line)
    ]
    assert not offenders, (
        f"{node} downloads inside the build container: {offenders}. hist.md §10.2 "
        "fetches on the build host and pushes the artefact in."
    )


# ── The plugin, which none of the above shapes fit ────────────────────────────


@pytest.mark.parametrize("node", sorted(_EXPECTED))
def test_the_plugin_is_verified_against_the_digest_pinned_in_the_specification(
    node: str,
):
    """The plugin's publisher offers only a SHA-1 digest, which is not an
    acceptable basis for deciding that a download is the published release —
    chosen-prefix collisions against SHA-1 have been demonstrated since 2019.
    hist.md §2.1 pins the SHA-256 instead, so the literal must appear in the
    script; without it the helper has nothing to check the archive against.
    """
    arm = _case_arm(node, _PLUGIN)
    assert _PLUGIN_SHA256 in _script(node), (
        f"{node} does not carry the SHA-256 pinned for the plugin in hist.md "
        f"§2.1 ({_PLUGIN_SHA256}); its arm resolves to {arm!r}"
    )
    assert "_checksums_tar.gz.txt" not in arm, (
        f"{node} reads the plugin's published checksum file, which holds a SHA-1 "
        "digest that sha256sum cannot verify against"
    )


@pytest.mark.parametrize("node", sorted(_EXPECTED))
def test_the_plugin_archive_is_not_architecture_specific(node: str):
    """One plugin archive carries the backend binaries for every platform, so its
    name has no architecture in it. Interpolating the build host's architecture
    would produce a URL that does not exist, and the failure would appear as a
    fetch error on whichever machine built the image next."""
    arm = _case_arm(node, _PLUGIN)
    assert "ARTEFACT_ARCH" not in arm, (
        f"{node} interpolates the build-host architecture into the plugin's "
        f"filename; no such asset is published. Arm is {arm!r}"
    )
    for architecture_specific in ("victoria-metrics", "grafana"):
        assert "ARTEFACT_ARCH" in _case_arm(node, architecture_specific), (
            f"{node}'s {architecture_specific} arm resolves no architecture, so "
            "the assertion above would pass even if the mechanism were removed"
        )


# ── Ordering and the input hash ───────────────────────────────────────────────


@pytest.mark.parametrize("node", sorted(_EXPECTED))
def test_artefacts_are_fetched_before_the_input_hash_is_computed(node: str):
    """`current_hash` reads the cached artefacts, so a fetch placed after it
    would hash files that do not exist yet. sha256sum fails on a missing path and
    `set -e` aborts, making the first build on a clean checkout fail every
    time."""
    script = _script(node)
    lines = script.splitlines()
    first_fetch = next(
        (
            i
            for i, line in enumerate(lines)
            if "fetch_artefact " in line and "()" not in line
        ),
        None,
    )
    assert first_fetch is not None, f"{node} calls fetch_artefact nowhere"
    hash_line = next(
        (i for i, line in enumerate(lines) if line.startswith("current_hash=")), None
    )
    assert hash_line is not None, f"{node} has no current_hash assignment"
    assert first_fetch < hash_line, (
        f"{node} computes current_hash at line {hash_line + 1} before fetching at "
        f"line {first_fetch + 1}; the hash would run against missing files"
    )


@pytest.mark.parametrize("node", sorted(_EXPECTED))
def test_every_fetched_artefact_is_part_of_the_input_hash(node: str):
    """Selective rebuild compares this hash against the published image property.
    An artefact outside it means a version bump in hist.md §2.1 re-renders the
    script but the build reports "inputs unchanged" and keeps the old binary, so
    the deployed image carries a version the repository no longer names and
    nothing reports the disagreement. This has happened on this deployment
    before."""
    script = _script(node)
    block = _hash_block(node, script)
    for artefact, var in _fetch_calls(script).items():
        assert f"${var}" in block or f"${{{var}}}" in block, (
            f"{node} hashes its configuration but not the {artefact} artefact "
            f"(${var}), so changing its pinned version would not rebuild the image"
        )


# ── The cache stays out of the repository ─────────────────────────────────────


def test_the_artefact_cache_is_excluded_from_version_control():
    """The five artefacts total several hundred megabytes of upstream binaries.
    Committing them would be caught in review once; the exclusion is what stops
    it happening at all."""
    probe = f"{_CACHE_DIR}/grafana_{_PINS['grafana']}_amd64.deb"
    result = subprocess.run(
        ["git", "check-ignore", "-q", probe], cwd=ROOT, capture_output=True, text=True
    )
    assert result.returncode == 0, (
        f"deploy/{probe} is not git-ignored; the artefact cache would be "
        "committable. Add deploy/.artefact-cache/ to .gitignore."
    )


@pytest.mark.parametrize("node", sorted(_EXPECTED))
def test_both_hosts_share_one_cache_directory(node: str):
    """The two hosts install four of the same five artefacts. Separate caches
    would double the download and leave the two copies free to diverge."""
    script = _script(node)
    assert _CACHE_DIR in script, (
        f"{node} does not cache into {_CACHE_DIR}, so its artefacts are either "
        "re-downloaded every build or stored somewhere the other host cannot "
        "reuse"
    )
