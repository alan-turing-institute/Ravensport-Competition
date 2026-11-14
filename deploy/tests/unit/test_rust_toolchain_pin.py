"""The Rust release is pinned once, at the repository root, and every place that
installs Rust names the same release. Run with: pytest tests/unit -q

rustup selects the toolchain from the directory in which a command runs, using
the nearest `rust-toolchain.toml` at or above it. The root file therefore
governs every build, test and lint run in the repository, unless a crate holds
its own file, which takes precedence within that crate. VM provisioning and the
CI lint job install Rust before any command runs in the repository, so each
names the release itself.
"""

import os
import re
import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]  # deploy/
REPO = ROOT.parent
TOOLCHAIN_FILE = REPO / "rust-toolchain.toml"
TOOLCHAIN_FILE_NAMES = {"rust-toolchain", "rust-toolchain.toml"}
# Cargo build output and Python caches hold no committed toolchain file, and
# `target/` directories are large enough to slow the walk. Directories whose
# names begin with a dot (version control, virtualenvs) are pruned as well.
PRUNED_DIRS = {"target", "__pycache__", "node_modules"}
# The components the pre-commit hooks run (`cargo fmt`, `cargo clippy`).
LINT_COMPONENTS = {"clippy", "rustfmt"}


def _toolchain() -> dict:
    return tomllib.loads(TOOLCHAIN_FILE.read_text())["toolchain"]


def _pinned_release() -> str:
    return _toolchain()["channel"]


def test_root_toolchain_names_an_exact_release():
    """A channel name such as `stable` resolves to whichever release each
    machine has installed, so two machines can build the same commit with
    different compilers."""
    assert TOOLCHAIN_FILE.is_file(), f"{TOOLCHAIN_FILE} does not exist"
    channel = _pinned_release()
    assert re.fullmatch(r"\d+\.\d+\.\d+", channel), (
        f"rust-toolchain.toml names the channel {channel!r}, not an exact release"
    )


def test_root_toolchain_lists_the_lint_components():
    """rustup installs the listed components wherever it installs the pinned
    release, so the pre-commit hooks find them."""
    components = set(_toolchain().get("components", []))
    assert LINT_COMPONENTS <= components, sorted(components)


def test_no_crate_overrides_the_root_toolchain():
    """A toolchain file below the root takes precedence over the root file for
    every command run beneath it."""
    nested = []
    for dirpath, dirnames, filenames in os.walk(REPO):
        dirnames[:] = [
            d for d in dirnames if d not in PRUNED_DIRS and not d.startswith(".")
        ]
        if Path(dirpath) == REPO:
            continue
        nested += [
            str(Path(dirpath, name).relative_to(REPO))
            for name in filenames
            if name in TOOLCHAIN_FILE_NAMES
        ]
    assert nested == [], f"toolchain files below the repository root: {sorted(nested)}"


def test_lima_provisions_the_pinned_release():
    """Provisioning installs Rust and adds the musl target from outside the
    repository, where the root file does not apply, so lima.yaml names the
    release as the VM's default toolchain."""
    lima = yaml.safe_load((ROOT / "lima.yaml").read_text())
    scripts = [p["script"] for p in lima["provision"] if p["mode"] == "user"]
    defaults = [
        release
        for script in scripts
        for release in re.findall(r"--default-toolchain\s+(\S+)", script)
    ]
    assert defaults == [_pinned_release()], defaults


def test_ci_lint_installs_the_pinned_release():
    """The action's reference names the release it installs. It installs the
    minimal profile, so the step also names the components the pre-commit hooks
    run."""
    workflow = yaml.safe_load(
        (REPO / ".github" / "workflows" / "lint.yaml").read_text()
    )
    steps = [
        step
        for job in workflow["jobs"].values()
        for step in job["steps"]
        if step.get("uses", "").startswith("dtolnay/rust-toolchain@")
    ]
    assert len(steps) == 1, steps
    step = steps[0]
    assert step["uses"] == f"dtolnay/rust-toolchain@{_pinned_release()}", step["uses"]
    components = {
        c.strip()
        for c in step.get("with", {}).get("components", "").split(",")
        if c.strip()
    }
    assert LINT_COMPONENTS <= components, step
