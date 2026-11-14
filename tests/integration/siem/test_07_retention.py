"""
Retention tests (siem-tests.md §7; siem.md §7).
"""

from __future__ import annotations

import os
import subprocess

import pytest

from tests.integration.siem.conftest import Config, Node

# Where anything outside the manager could be configured to delete files.
_DELETION_CONFIGURATION = (
    "/etc/logrotate.conf",
    "/etc/logrotate.d",
    "/etc/crontab",
    "/etc/cron.d",
    "/etc/cron.daily",
    "/etc/cron.weekly",
    "/etc/cron.monthly",
    "/var/spool/cron",
)


def test_retention_configuration_compresses_and_never_deletes(siem: Node) -> None:
    """siem.md §7: the analysis daemon writes each day's alerts and archives
    into a dated file, and the manager's monitord daemon compresses each
    previous day's (monitord.compress, the packaged default, not overridden
    locally). Nothing deletes them: no logrotate configuration or scheduled
    job names the alert or archive directories. monitord.keep_log_days
    applies only to the manager's operational log (ossec.log), as the
    packaged internal_options.conf describes it, and is raised to its
    maximum of 500 days."""
    default_conf = siem.read("/var/ossec/etc/internal_options.conf")
    local_conf = siem.read("/var/ossec/etc/local_internal_options.conf")
    assert "monitord.compress=1" in default_conf, (
        "previous days' alerts and archives are not compressed"
    )
    assert "monitord.compress=0" not in local_conf, (
        "local_internal_options.conf disables compression"
    )
    assert "monitord.keep_log_days=500" in local_conf, (
        "local_internal_options.conf does not raise monitord.keep_log_days "
        "to its maximum (500)"
    )

    present = [path for path in _DELETION_CONFIGURATION if siem.path_exists(path)]
    found = siem.run(
        "grep", "-rlE", "/var/ossec/logs/(alerts|archives)", *present, check=False
    )
    assert not found.stdout, (
        f"configuration outside the manager names the outputs:\n{found.stdout}"
    )


@pytest.mark.slow
def test_stores_begin_empty_on_a_rebuilt_environment(siem: Node) -> None:
    """A freshly built and started siem-01 instance holds no prior alert or
    archive content.

    Destructive: tears down and rebuilds the whole deployed topology. Gated
    behind SIEM_ALLOW_REDEPLOY=1 so it cannot fire as a side effect of an
    ordinary suite run.
    """
    if os.environ.get("SIEM_ALLOW_REDEPLOY") != "1":
        pytest.skip("set SIEM_ALLOW_REDEPLOY=1 to run the rebuild test")

    deploy_dir = Config.from_env().world_yaml.parent
    subprocess.run(["make", "down"], cwd=deploy_dir, check=True)
    subprocess.run(["make", "up"], cwd=deploy_dir, check=True)

    for path in (
        "/var/ossec/logs/alerts/alerts.json",
        "/var/ossec/logs/archives/archives.json",
    ):
        result = siem.run("test", "-s", path, check=False)
        assert result.returncode != 0, (
            f"{path} is non-empty on a freshly rebuilt instance"
        )
