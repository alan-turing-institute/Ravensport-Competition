from __future__ import annotations

import shutil
import subprocess

import pytest


def pytest_runtest_setup() -> None:
    if shutil.which("incus") is None:
        pytest.skip("incus client not found on the test runner")
    for node in (
        "sim-host",
        "admin-01",
        "dmz-bastion-01",
        "fms-01",
        "hist-01",
        "hmi-01",
        "siem-01",
    ):
        probe = subprocess.run(
            ["incus", "exec", node, "--", "true"],
            capture_output=True,
            text=True,
            check=False,
        )
        if probe.returncode != 0:
            pytest.skip(f"{node} is not running")
