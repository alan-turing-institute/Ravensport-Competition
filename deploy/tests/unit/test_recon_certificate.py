"""recon-01's remote-desktop certificate. Run with: pytest tests/unit -q

Covers `recon.md` §4 and build step 8: `xrdp` presents a certificate issued by
the internal certificate authority rather than the distribution's self-signed
pair, named in `xrdp.ini`, with the key readable by the `xrdp` user alone.

What `test_ca_provisioning.py` already covers for every leaf is not repeated
here: that `recon-01` holds one (its `LEAF_NODES`), that the certificate verifies
against the root with the name `recon-01` and no address, that the script
compares the certificate's and key's moduli before installing them, and that it
references no other node's key. `test_input_hash_symmetry.py` covers that both
halves are hashed.

These read the rendered build script. That the running service presents the
certificate is `xrdp_presents_the_ca_issued_certificate`, and that it can read
the key `xrdp_can_read_its_private_key` (`recon-tests.md` §2).
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]  # deploy/
NODE_BUILD = ROOT / "build" / "node-build" / "recon-01.sh"

XRDP_CERT = "/etc/xrdp/tls/recon-01.pem"
XRDP_KEY = "/etc/xrdp/tls/recon-01.key"


@pytest.fixture(scope="module")
def script() -> str:
    out = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "render.py"), "--profile", "full"],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert out.returncode == 0, f"render --profile full failed:\n{out.stderr}"
    return NODE_BUILD.read_text()


def _pushes(script: str, source: str, destination: str) -> bool:
    return any(
        "incus file push" in line and source in line and destination in line
        for line in script.splitlines()
    )


def test_the_leaf_is_installed_where_xrdp_ini_names_it(script):
    assert _pushes(script, "build/tls/recon-01/cert.pem", XRDP_CERT), (
        f"recon-01's build script does not install its leaf certificate at {XRDP_CERT}"
    )
    assert _pushes(script, "build/tls/recon-01/key.pem", XRDP_KEY), (
        f"recon-01's build script does not install its leaf key at {XRDP_KEY}"
    )


def test_xrdp_ini_names_the_installed_certificate_and_key(script):
    """xrdp's packaged `certificate=` and `key_file=` are empty, which selects
    the distribution's self-signed pair. Both are set, and the result checked: a
    substitution that matches nothing leaves the packaged default and fails
    silently, as for `security_layer`."""
    lines = script.splitlines()
    for key, value in (("certificate", XRDP_CERT), ("key_file", XRDP_KEY)):
        assert any(
            "sed -i" in line and f"^{key}=" in line and f"{key}={value}" in line
            for line in lines
        ), f"recon-01's build script does not set {key}={value} in xrdp.ini"
        assert f"grep -q '^{key}={value}' /etc/xrdp/xrdp.ini" in script, (
            f"recon-01's build script does not check that {key}={value} took effect"
        )


def test_the_key_is_owned_by_xrdp_and_readable_by_it_alone(script):
    """Without read access the server reports `SSL_CERT_NOT_ON_SERVER` when a
    client arrives; with group or world access the key is readable by more than
    the service that presents it."""
    assert re.search(rf"chown xrdp(:xrdp)? {re.escape(XRDP_KEY)}", script), (
        f"recon-01's build script does not give {XRDP_KEY} to the xrdp user"
    )
    assert re.search(rf"chmod 0?600 {re.escape(XRDP_KEY)}", script), (
        f"recon-01's build script does not restrict {XRDP_KEY} to mode 600"
    )


def test_xrdp_is_not_given_the_ssl_cert_group(script):
    """The group served the distribution's self-signed key under
    `/etc/ssl/private/`, which is no longer presented. Membership left in place
    would let the service read every key in that directory while needing none of
    them."""
    assert not re.search(r"adduser xrdp ssl-cert|usermod .*ssl-cert.* xrdp", script), (
        "recon-01's build script still adds the xrdp user to the ssl-cert group"
    )
