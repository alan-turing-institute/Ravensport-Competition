"""
Build container logs cleared before publishing

A port host enters service with no history except the record of its package
installation. Every per-node build script must therefore, as its last step
before the container is stopped, stop rsyslog, move the journal to /run and
empty every file under /var/log except the package installation record
(deployment.md §6.1 step 9). Both profiles are rendered, and every node
template is checked for the shared step, so a template no profile currently
renders (the stub) is covered as well.

This unit test checks the *rendered* build scripts statically (no Incus needed).
The runtime counterpart — that each built image carries no log from its build —
lives in tests/integration/test_image_build.sh.

Run with: pytest tests/unit -q
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = ROOT / "build-scripts"

STEP_START = 'incus exec "$BUILD_CTR" -- systemctl stop syslog.socket rsyslog.service'
PACKAGE_RECORD = (
    '! -path "/var/log/apt/*"',
    "! -name dpkg.log",
    "! -name alternatives.log",
    "! -name bootstrap.log",
)


def render(profile: str) -> list[Path]:
    out = subprocess.run(
        [sys.executable, "scripts/render.py", "--profile", profile],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode == 0, out.stderr
    build = ROOT / "build"
    manifest = json.loads((build / "manifest.json").read_text())
    scripts = [build / "node-build" / f"{n}.sh" for n in manifest["build_script_nodes"]]
    assert scripts, "no per-node build scripts were rendered"
    for script in scripts:
        assert script.is_file(), f"{script.name} was not rendered"
    return scripts


@pytest.fixture(scope="module", params=["ci-minimal", "full"])
def build_scripts(request) -> dict[str, str]:
    return {s.name: s.read_text() for s in render(request.param)}


def final_step(name: str, text: str) -> str:
    """The commands between the start of the step and the container stop."""
    assert STEP_START in text, f"{name} does not clear the build logs"
    step = text[text.index(STEP_START) :]
    return step[: step.index('incus stop "$BUILD_CTR"')]


def test_step_is_last_before_stop(build_scripts):
    """A command run in the container after the step would log under the build
    container's name again."""
    for name, text in build_scripts.items():
        step = final_step(name, text)
        commands = [line for line in step.splitlines() if line.startswith("incus ")]
        assert len(commands) == 3, (
            f"{name} runs another container command between clearing the logs "
            f"and stopping the container: {commands}"
        )


def test_journal_is_moved_and_deleted(build_scripts):
    for name, text in build_scripts.items():
        step = final_step(name, text)
        assert "journalctl --relinquish-var" in step, (
            f"{name} does not move the journal to /run before clearing it"
        )
        assert "find /var/log/journal -type f -delete" in step, (
            f"{name} does not delete the build container's journal"
        )


def test_package_record_is_kept(build_scripts):
    for name, text in build_scripts.items():
        step = final_step(name, text)
        for exclusion in PACKAGE_RECORD:
            assert exclusion in step, f"{name} does not keep {exclusion}"


def test_other_logs_are_recreated_empty(build_scripts):
    for name, text in build_scripts.items():
        step = final_step(name, text)
        assert 'install -m "$mode" -o "$user" -g "$group" /dev/null "$path"' in step, (
            f"{name} does not recreate each log file empty with its owner and mode"
        )


def test_every_node_template_includes_the_step():
    templates = sorted(
        t for t in TEMPLATES.glob("*.sh.j2") if not t.name.startswith("_")
    )
    assert templates, "no node build templates found"
    for template in templates:
        assert '{% include "_clear-build-logs.sh.j2" %}' in template.read_text(), (
            f"{template.name} does not include _clear-build-logs.sh.j2"
        )
