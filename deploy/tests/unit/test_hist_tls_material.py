"""
Shared TLS keypair tests for the log servers (hist-tests.md §8), targeting
hist.md §9/§10.2/§10.3's shared-keypair design.

RED UNTIL IMPLEMENTED. `hist-01.sh.j2`/`hist-dmz-01.sh.j2` currently generate a
self-signed certificate per host under `/etc/grafana/tls/`, used by Grafana
alone. These tests are the contract the CA-signed, shared-keypair design must
satisfy, written before the build scripts that satisfy it (the same "tests
before implementation" pattern `test_hist_systemd_units.py` already uses).

Covers the properties that make the ingestion, replication, and Grafana TLS
tests in `tests/integration/hist/test_01..04` and `test_07` possible, distinct
from what those functional tests already assert about the running services:
one keypair per host, referenced identically by every TLS-terminating flag
rather than copied per service, and the file permissions and group membership
that keep the private key readable only by the services that need it.

Run with: pytest tests/unit -q
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]  # deploy/

_TLS_CERT = "/etc/hist-tls/cert.pem"
_TLS_KEY = "/etc/hist-tls/key.pem"

# hist.md §9/§10.2/§10.3: which service accounts read access the private key,
# per host. hist-01's victoria-metrics/victoria-logs bind loopback with no
# -tls flags of their own (§9.1) and so are not members, despite the
# paragraph after §9's tables naming all five service names collectively
# across both hosts — membership here follows which service actually
# terminates TLS with this keypair, matching this repository's own
# least-knowledge principle (deployment-config.md §5).
_HIST_TLS_GROUP_MEMBERS = {
    "hist-01": {"vmagent", "vlagent", "grafana"},
    "hist-dmz-01": {"victoria-metrics", "victoria-logs", "grafana"},
}

# Every rendered TLS flag that must reference the shared keypair, per host.
_HIST01_TLS_FLAGS = [
    "-tlsCertFile=" + _TLS_CERT,
    "-tlsKeyFile=" + _TLS_KEY,
    "-syslog.tlsCertFile=" + _TLS_CERT,
    "-syslog.tlsKeyFile=" + _TLS_KEY,
]
# hist-dmz-01's stores bind two addresses on one -httpListenAddr list — loopback
# plain HTTP for Grafana's own local queries, the zone address TLS for hist-01's
# replication traffic — so -tls/-tlsCertFile/-tlsKeyFile are positional lists
# too, empty for the loopback entry (hist.md §9.2).
_HISTDMZ01_TLS_FLAGS = [
    "-tls=false,true",
    "-tlsCertFile=," + _TLS_CERT,
    "-tlsKeyFile=," + _TLS_KEY,
]


def _render(profile: str = "ci-minimal") -> Path:
    out = subprocess.run(
        [sys.executable, "scripts/render.py", "--profile", profile],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert out.returncode == 0, out.stderr
    return ROOT / "build"


def _build_script(node: str) -> str:
    build = _render()
    return (build / "node-build" / f"{node}.sh").read_text()


def _units(node: str) -> dict[str, str]:
    """Map service name to its rendered systemd configuration (unit or
    drop-in). Duplicated from test_hist_systemd_units.py's helper of the same
    name rather than imported, matching this suite's convention of
    self-contained modules."""
    script = _build_script(node)
    lines = script.splitlines()

    units: dict[str, str] = {}
    pattern = re.compile(r"/etc/systemd/system/([^/\s\"']+\.service)(\.d/[^\s\"']+)?")
    i = 0
    while i < len(lines):
        match = pattern.search(lines[i])
        heredoc = re.search(r"<<\s*'?([A-Za-z_]+)'?\s*$", lines[i])
        if match and heredoc:
            terminator = heredoc.group(1)
            service = match.group(1).removesuffix(".service")
            body: list[str] = []
            i += 1
            while i < len(lines) and lines[i].strip() != terminator:
                body.append(lines[i])
                i += 1
            units[service] = units.get(service, "") + "\n".join(body) + "\n"
        i += 1
    return units


def _exec_start(unit_body: str) -> str:
    joined = unit_body.replace("\\\n", " ")
    for line in joined.splitlines():
        if line.strip().startswith("ExecStart="):
            return line.strip()
    return ""


# ── One keypair per host, referenced identically everywhere ───────────────────


@pytest.mark.parametrize("node", ["hist-01", "hist-dmz-01"])
def test_exactly_one_certificate_key_pair_is_installed(node: str):
    """hist.md §10.2/§10.3: the build script installs a single copy of the
    CA-signed leaf at /etc/hist-tls/, not a per-service copy — one file to
    rotate at each annual reissuance rather than several kept in sync by
    hand."""
    script = _build_script(node)
    assert f"tls/{node}/cert.pem" in script, (
        f"{node}'s build script does not install its leaf certificate"
    )
    assert f"tls/{node}/key.pem" in script, (
        f"{node}'s build script does not install its leaf key"
    )
    assert _TLS_CERT in script and _TLS_KEY in script, (
        f"{node}'s build script does not place the certificate at {_TLS_CERT}/{_TLS_KEY}"
    )
    # No per-service copy anywhere else — every install of the leaf's bytes
    # goes to the one shared path.
    for stray in ("/etc/grafana/tls/cert.pem", "/etc/grafana/tls/key.pem"):
        assert stray not in script, (
            f"{node}'s build script still installs a per-service copy at {stray}; "
            "hist.md §9/§10.2/§10.3 specifies one shared /etc/hist-tls/ copy"
        )


def test_hist01_agents_and_grafana_reference_the_shared_path():
    """Every TLS-terminating flag on hist-01 — vmagent's and vlagent's
    -tlsCertFile/-tlsKeyFile, vlagent's -syslog.tlsCertFile/-syslog.tlsKeyFile,
    and Grafana's cert_file/cert_key — names the identical /etc/hist-tls/ path.
    This is the regression guard for the shared-keypair design: nothing here
    would fail if a future edit reintroduced per-service copies that all
    happened to be byte-identical at build time but could drift at the next
    renewal."""
    vmagent = _exec_start(_units("hist-01")["vmagent"])
    vlagent = _exec_start(_units("hist-01")["vlagent"])
    for flag in _HIST01_TLS_FLAGS[:2]:
        assert flag in vmagent, f"hist-01 vmagent missing {flag}: {vmagent!r}"
    for flag in _HIST01_TLS_FLAGS:
        assert flag in vlagent, f"hist-01 vlagent missing {flag}: {vlagent!r}"

    grafana_ini = _build_script("hist-01")
    assert f"cert_file={_TLS_CERT}" in grafana_ini or _TLS_CERT in grafana_ini, (
        "hist-01 grafana.ini does not reference the shared certificate"
    )
    assert f"cert_key={_TLS_KEY}" in grafana_ini or _TLS_KEY in grafana_ini, (
        "hist-01 grafana.ini does not reference the shared key"
    )


def test_histdmz01_stores_and_grafana_reference_the_shared_path():
    """hist-dmz-01's VictoriaMetrics, VictoriaLogs, and Grafana all name the
    same /etc/hist-tls/ path (hist.md §9.2) — the stores via the TLS entry of
    their positional -tls/-tlsCertFile/-tlsKeyFile lists (the loopback entry
    stays empty/plain, for Grafana's own local queries, §7)."""
    units = _units("hist-dmz-01")
    for store in ("victoria-metrics", "victoria-logs"):
        exec_start = _exec_start(units[store])
        for flag in _HISTDMZ01_TLS_FLAGS:
            assert flag in exec_start, (
                f"hist-dmz-01 {store} missing {flag}: {exec_start!r}"
            )

    grafana_ini = _build_script("hist-dmz-01")
    assert _TLS_CERT in grafana_ini, (
        "hist-dmz-01 grafana.ini does not reference the shared certificate"
    )
    assert _TLS_KEY in grafana_ini, (
        "hist-dmz-01 grafana.ini does not reference the shared key"
    )


# ── File permissions ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("node", ["hist-01", "hist-dmz-01"])
def test_certificate_and_key_permissions(node: str):
    """hist.md §10.2/§10.3: cert.pem is world-readable (0644, public by
    design); key.pem is owned root:hist-tls and mode 0640, following the
    Debian/Ubuntu ssl-cert-group convention for a certificate shared between
    independent daemons."""
    script = _build_script(node)
    assert (
        re.search(r"chmod\s+644\s+.*cert\.pem", script)
        or "chmod 644 /etc/hist-tls/cert.pem" in script
    ), f"{node}'s build script does not set cert.pem to mode 0644"
    assert (
        re.search(r"chmod\s+640\s+.*key\.pem", script)
        or "chmod 640 /etc/hist-tls/key.pem" in script
    ), f"{node}'s build script does not set key.pem to mode 0640"
    assert re.search(r"chown\s+root:hist-tls\s+.*key\.pem", script), (
        f"{node}'s build script does not set key.pem's owner to root:hist-tls"
    )


# ── Group membership ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("node", ["hist-01", "hist-dmz-01"])
def test_hist_tls_group_membership_matches_exactly(node: str):
    """hist.md §10.2/§10.3: the hist-tls group's membership is exactly the
    services that terminate TLS with this keypair on this host — hist-01's
    vmagent, vlagent, and grafana; hist-dmz-01's victoria-metrics,
    victoria-logs, and grafana (no vmagent/vlagent accounts exist to add
    there). A service account missing from the group is indistinguishable
    from a correctly-configured one until the private key is rotated or the
    permission bits are checked directly — a functional test pushing data
    today would still pass."""
    script = _build_script(node)
    additions = set(re.findall(r"usermod\s+-aG\s+hist-tls\s+(\S+)", script)) | set(
        re.findall(r"gpasswd\s+-a\s+(\S+)\s+hist-tls", script)
    )
    expected = _HIST_TLS_GROUP_MEMBERS[node]
    assert additions == expected, (
        f"{node}'s hist-tls group members are {sorted(additions)}, expected "
        f"{sorted(expected)}"
    )


def test_hist_tls_group_is_created():
    """Both hosts create the shared hist-tls system group before adding any
    service account to it."""
    for node in ("hist-01", "hist-dmz-01"):
        script = _build_script(node)
        assert "groupadd" in script and "hist-tls" in script, (
            f"{node}'s build script never creates the hist-tls group"
        )
