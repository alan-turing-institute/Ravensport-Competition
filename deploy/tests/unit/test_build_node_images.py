"""
Failure reporting when a per-node image build fails (deployment.md §6.1).

`make build-node-images` runs every rendered per-node build script in turn.
Each image is independent, so one node failing does not invalidate the others
and the run should continue — but the run as a whole has not done what was
asked, and must say so.

Until this was fixed the loop ran inline in the Makefile with no failure check,
so the recipe exited with the status of its last iteration. The scripts are
taken in glob order, so whether a failed build was reported depended on where
its node fell alphabetically: a failure in any script but the last was
discarded. Observed on the deployment, where `corp-it` and `fms-01` both failed
to build while `make` reported success, leaving two images silently stale —
one of them serving a certificate from before the internal certificate
authority existed, which a later test run attributed to the image rather than
to the build that never replaced it.

The two properties asserted here are what make that impossible: every script is
attempted regardless of what failed before it, and any failure at all is
reported by name and by exit status.

The directory of scripts may be given as an argument so that this suite can
exercise the loop against stubs. `make` relies on the default, and the default
is asserted below so the argument cannot drift away from the path the Makefile
depends on.

Run with: pytest tests/unit -q
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]  # deploy/
SCRIPT = ROOT / "scripts" / "build-node-images.sh"


def _node_build_dir(tmp_path: Path, outcomes: dict[str, int]) -> Path:
    """A stand-in for `build/node-build/`, holding one script per node that
    announces itself and exits with the given status."""
    directory = tmp_path / "node-build"
    directory.mkdir()
    for node, status in outcomes.items():
        script = directory / f"{node}.sh"
        script.write_text(f'#!/usr/bin/env bash\necho "ran {node}"\nexit {status}\n')
        script.chmod(0o755)
    return directory


def _run(directory: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SCRIPT), str(directory)],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize("failing", ["aaa-first", "mmm-middle", "zzz-last"])
def test_any_failing_script_fails_the_run(tmp_path: Path, failing: str):
    """Stated over a failure in each position, because the defect was positional:
    with no failure check the recipe took the status of the last iteration, so
    the same fault was reported or discarded according to where its node sorted.
    """
    outcomes = {"aaa-first": 0, "mmm-middle": 0, "zzz-last": 0}
    outcomes[failing] = 1

    result = _run(_node_build_dir(tmp_path, outcomes))

    assert result.returncode != 0, (
        f"{failing} exited non-zero, but the run reported success "
        f"(stdout: {result.stdout!r}, stderr: {result.stderr!r})"
    )
    assert failing in result.stderr, (
        f"the run failed but did not name {failing} on stderr, so the operator "
        f"is told the build failed without being told which node "
        f"(stderr: {result.stderr!r})"
    )


def test_every_script_runs_even_after_one_fails(tmp_path: Path):
    """Per-node images are independent, so a failure is not a reason to abandon
    the nodes after it. Stopping at the first would also have hidden half of the
    observed fault: `corp-it` and `fms-01` failed for the same reason in one run,
    and only the earlier would have been reported."""
    outcomes = {"aaa-first": 1, "mmm-middle": 0, "zzz-last": 0}

    result = _run(_node_build_dir(tmp_path, outcomes))

    for node in outcomes:
        assert f"ran {node}" in result.stdout, (
            f"{node} did not run after an earlier script failed, so one broken "
            f"node leaves the rest of the fleet unbuilt (stdout: {result.stdout!r})"
        )


def test_every_failure_is_named_not_just_the_first(tmp_path: Path):
    """The run that exposed this had two nodes failing for one shared cause.
    Reporting only one turns a single diagnosis into two separate ones."""
    outcomes = {"aaa-first": 1, "mmm-middle": 0, "zzz-last": 1}

    result = _run(_node_build_dir(tmp_path, outcomes))

    assert result.returncode != 0
    for node in ("aaa-first", "zzz-last"):
        assert node in result.stderr, (
            f"{node} failed but was not named in the summary (stderr: "
            f"{result.stderr!r})"
        )


def test_a_clean_run_succeeds(tmp_path: Path):
    """The converse: nothing failed, so nothing is reported and the status is
    zero. Without this a script that always failed would satisfy every
    assertion above."""
    result = _run(_node_build_dir(tmp_path, {"aaa-first": 0, "zzz-last": 0}))

    assert result.returncode == 0, (
        f"every script succeeded but the run reported failure "
        f"(stdout: {result.stdout!r}, stderr: {result.stderr!r})"
    )


def test_a_missing_directory_fails_rather_than_building_nothing(tmp_path: Path):
    """An unglobbed directory would otherwise iterate zero scripts and exit
    zero, reporting a successful build of nothing — the same class of silence
    this file exists to remove."""
    result = _run(tmp_path / "never-rendered")

    assert result.returncode != 0, (
        "a missing node-build directory reported success "
        f"(stdout: {result.stdout!r}, stderr: {result.stderr!r})"
    )


def test_the_makefile_calls_the_script_without_an_argument():
    """The argument exists for this suite. `make` must rely on the default, or
    the path the tests exercise is not the path the build takes."""
    makefile = (ROOT / "Makefile").read_text()
    recipe = makefile.split("build-node-images:", 1)[1].split("\n\n", 1)[0]

    assert "scripts/build-node-images.sh" in recipe, (
        "build-node-images no longer calls scripts/build-node-images.sh, so the "
        "behaviour asserted in this file is not the behaviour make performs"
    )
    called = [line for line in recipe.splitlines() if "build-node-images.sh" in line]
    assert len(called) == 1, f"expected one call, found {called}"
    after = called[0].split("build-node-images.sh", 1)[1].strip()
    assert not after, (
        f"the Makefile passes {after!r} to build-node-images.sh; the default "
        "directory is what this suite exercises"
    )
