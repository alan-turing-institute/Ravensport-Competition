"""
Management-interface certificate tests for security monitoring
(siem-tests.md §10), targeting siem.md §8.1 and §8.6.

The Wazuh manager generates a self-signed certificate at its first start when
none is present, so the order of operations in siem-01's build script is what
guarantees no self-signed material is ever generated in the image. The chain,
subject alternative name, and render-to-render determinism of the leaf itself
are covered with every other leaf in test_ca_provisioning.py.

Run with: pytest tests/unit -q
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # deploy/


def _siem_build_script() -> str:
    out = subprocess.run(
        [sys.executable, "scripts/render.py", "--profile", "ci-minimal"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode == 0, out.stderr
    return (ROOT / "build" / "node-build" / "siem-01.sh").read_text()


def _first_line_index(lines: list[str], needle: str) -> int:
    for i, line in enumerate(lines):
        if needle in line:
            return i
    raise AssertionError(f"siem-01's build script has no line containing {needle!r}")


def test_leaf_is_installed_before_the_managers_first_start():
    """siem.md §8.6: the authority-issued leaf and key are in place as the
    management interface's server certificate before the manager first runs,
    so the manager never generates a self-signed pair of its own."""
    lines = _siem_build_script().splitlines()
    first_start = _first_line_index(lines, "systemctl start wazuh-manager")
    for published in ("ssl/server.crt", "ssl/server.key"):
        # `incus file push SRC \` continues onto the destination line.
        pushes = [
            i
            for i, line in enumerate(lines)
            if published in line
            and ("incus file push" in line or "incus file push" in lines[i - 1])
        ]
        assert pushes, f"siem-01's build script never installs {published}"
        assert pushes[0] < first_start, (
            f"{published} is installed after the manager's first start"
        )


def test_https_block_presents_the_leaf_without_client_certificate_verification():
    """siem.md §8.6: api.yaml's https block is set explicitly to present the
    installed pair, with client-certificate verification off."""
    script = _siem_build_script()
    for setting in (
        "enabled: yes",
        'key: "server.key"',
        'cert: "server.crt"',
        "use_ca: False",
    ):
        assert setting in script, f"api.yaml https block does not set {setting!r}"


def test_enrolment_service_is_disabled_and_its_self_signed_pair_removed():
    """siem.md §8.1: agent keys are derived at render time, so the manager's
    enrolment service is not enabled; its certificate and key, generated
    self-signed by the package at install, are removed from the image."""
    script = _siem_build_script()
    assert r'r"\1yes\2"' in script and "<auth>" in script, (
        "siem-01's build script does not disable the enrolment service"
    )
    assert (
        "rm /var/ossec/etc/sslmanager.cert /var/ossec/etc/sslmanager.key" in script
    ), "siem-01's build script leaves the enrolment service's self-signed pair"
