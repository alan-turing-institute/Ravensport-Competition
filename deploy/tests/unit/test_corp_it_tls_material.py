"""
Directory-service TLS material tests for `corp-it` (corp-it-tests.md §5),
targeting corp-it.md §5/§6's certificate installation and LDAPS-only service
list.

Covers the properties that make the transport-security tests in
`tests/integration/ldap/test_01_server.py` possible, distinct from what those
functional tests already assert about the running service: the certificate
and key are installed at the expected path, owned and permissioned correctly,
referenced by `slapd`'s TLS configuration, and no plaintext listener remains
configured.

Run with: pytest tests/unit -q
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # deploy/

_TLS_CERT = "/etc/ldap/tls/cert.pem"
_TLS_KEY = "/etc/ldap/tls/key.pem"


def _render(profile: str = "ci-minimal") -> Path:
    out = subprocess.run(
        [sys.executable, "scripts/render.py", "--profile", profile],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert out.returncode == 0, out.stderr
    return ROOT / "build"


def _build_script(node: str = "corp-it") -> str:
    build = _render()
    return (build / "node-build" / f"{node}.sh").read_text()


# ── Certificate and key installed, and referenced ──────────────────────────


def test_exactly_one_certificate_key_pair_is_installed():
    """corp-it.md §6: the build script installs the CA-signed leaf at
    /etc/ldap/tls/, owned by the openldap system user — `slapd` is the only
    TLS-terminating service on this host, so there is exactly one keypair to
    install."""
    script = _build_script()
    assert "tls/corp-it/cert.pem" in script, (
        "corp-it's build script does not install its leaf certificate"
    )
    assert "tls/corp-it/key.pem" in script, (
        "corp-it's build script does not install its leaf key"
    )
    assert _TLS_CERT in script and _TLS_KEY in script, (
        f"corp-it's build script does not place the certificate at {_TLS_CERT}/{_TLS_KEY}"
    )


def test_slapd_tls_attributes_reference_the_installed_material():
    """corp-it.md §6: slapd's cn=config TLS attributes point at the installed
    certificate, key, and CA root — a build script that installs the files but
    never wires them into slapd's configuration would leave the directory
    still serving ldap:/// unencrypted."""
    script = _build_script()
    assert "olcTLSCertificateFile" in script and _TLS_CERT in script, (
        "corp-it's build script does not set olcTLSCertificateFile"
    )
    assert "olcTLSCertificateKeyFile" in script and _TLS_KEY in script, (
        "corp-it's build script does not set olcTLSCertificateKeyFile"
    )
    assert "olcTLSCACertificateFile" in script, (
        "corp-it's build script does not set olcTLSCACertificateFile"
    )


def test_no_plaintext_ldap_service_is_configured():
    """corp-it.md §3/§5: slapd's service list carries only ldaps:/// and the
    local ldapi:/// socket. A service list that still names ldap:/// would
    leave the plaintext listener configured even if a certificate is also
    installed."""
    script = _build_script()
    assert "ldaps:///" in script, "corp-it's build script does not enable ldaps:///"
    # Matched as a whole service-list token so "ldaps:///" itself (which
    # contains the substring "ldap") does not produce a false negative.
    assert not re.search(r"(?<!ldaps)(?<!ldapi)\bldap:///", script), (
        "corp-it's build script still configures a plaintext ldap:/// listener"
    )


# ── File permissions ────────────────────────────────────────────────────────


def test_certificate_and_key_permissions():
    """corp-it.md §6: cert.pem is world-readable (0644, public by design);
    key.pem is owned by the openldap system user and not group- or
    world-readable. A key readable by any other account would let a
    lower-privileged process on the host impersonate the directory."""
    script = _build_script()
    assert (
        re.search(r"chmod\s+644\s+.*cert\.pem", script)
        or f"chmod 644 {_TLS_CERT}" in script
    ), "corp-it's build script does not set cert.pem to mode 0644"
    assert (
        re.search(r"chmod\s+0?600\s+.*key\.pem", script)
        or f"chmod 600 {_TLS_KEY}" in script
    ), "corp-it's build script does not set key.pem to mode 0600"
    assert re.search(r"chown\s+openldap(:openldap)?\s+.*key\.pem", script), (
        "corp-it's build script does not set key.pem's owner to openldap"
    )
