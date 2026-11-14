"""
Deployment and certificates tests (siem-tests.md §8; siem.md §8,
deployment.md §7.2).
"""

from __future__ import annotations

import json

import pytest

from tests.integration.siem.conftest import Config, Credential, Node
from tests.integration.siem.hosts import MONITORED_HOSTS


def test_firewall_reachability_for_agent_traffic(config: Config) -> None:
    """TCP 1514 is reachable from every zone, including the two-hop
    operational-technology path, and from no other port on siem-01.

    A pytest-level counterpart of `deploy/tests/integration/test_firewall.sh`
    and `test_policy.sh`, which assert the same property against the fleet
    policy table; this exercises it directly against the deployed instances.
    """
    for source in ("fms-01", "corp-it", "atg-relay-01"):
        node = Node(source)
        assert node.can_reach(config.siem_host, config.agent_port), (
            f"{source} cannot reach siem-01:{config.agent_port}"
        )

    probe = Node("fms-01")
    assert not probe.can_reach(config.siem_host, 9999, timeout=3), (
        "an arbitrary unpermitted port on siem-01 is unexpectedly reachable"
    )


# Probes run inside a container for each way the shared host kernel could be
# observed. An audit status request is sent over netlink directly, since no
# image installs auditctl; the kernel answers AUDIT_GET (1000) with its status
# only when the caller may read it, and with an error message otherwise.
_KERNEL_VISIBILITY_PROBE = """
import json, socket, struct, subprocess

def readable(path):
    try:
        with open(path, "rb") as f:
            return bool(f.read(1))
    except OSError:
        return False

def audit_status_readable():
    try:
        s = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, 9)
        s.settimeout(5)
        s.bind((0, 0))
        s.send(struct.pack("=LHHLL", 16, 1000, 5, 1, 0))
        reply_type = struct.unpack("=LHHLL", s.recv(4096)[:16])[1]
        return reply_type == 1000
    except OSError:
        return False

with open("/proc/1/uid_map") as f:
    inside, outside, _ = f.readline().split()
print(json.dumps({
    "root_is_host_root": inside == "0" and outside == "0",
    "kernel_log": subprocess.run(["dmesg"], capture_output=True).returncode == 0,
    "kernel_message_device": readable("/dev/kmsg"),
    "audit_status": audit_status_readable(),
    "tracing": readable("/sys/kernel/tracing/trace")
    or readable("/sys/kernel/debug/tracing/trace"),
}))
"""


@pytest.mark.parametrize("host", [*MONITORED_HOSTS, "siem-01"])
def test_no_container_can_observe_the_shared_kernel(host: str) -> None:
    """On the container profiles, every security-monitoring host runs
    unprivileged: its root user is not the host's root user, and it can read
    neither the host kernel log nor the kernel message device, nor host audit
    status, nor kernel-wide tracing (deployment.md §7.2). A host backed by a
    virtual machine has its own kernel and is not checked."""
    node = Node(host)
    if node.run("systemd-detect-virt", "--container").returncode != 0:
        pytest.skip(f"{host} is a virtual machine with its own kernel")
    observed = json.loads(node.stdout("python3", "-c", _KERNEL_VISIBILITY_PROBE))
    visible = [facility for facility, reachable in observed.items() if reachable]
    assert not visible, f"{host} can observe the shared kernel: {visible}"


def test_management_interface_presents_ca_signed_certificate(
    peer: Node, config: Config
) -> None:
    """Fetched from another host in the IT zone, the certificate siem-01
    presents on TCP 55000 chains to the RavensPort internal certificate
    authority and carries the subject alternative name siem-01."""
    script = (
        "import socket, ssl, json\n"
        "ctx = ssl.create_default_context(cafile='/usr/local/share/ca-certificates/ravensport-ca.crt')\n"
        f"with socket.create_connection(('siem-01', {config.api_port}), timeout=5) as raw:\n"
        "    with ctx.wrap_socket(raw, server_hostname='siem-01') as tls:\n"
        "        cert = tls.getpeercert()\n"
        "print(json.dumps(cert))\n"
    )
    result = peer.run("python3", "-c", script, check=True)
    assert "siem-01" in result.stdout, "certificate does not carry the siem-01 SAN"


# The trees in which the package and the manager write the certificate
# material their services present. The manager's bundled Python runtime
# (/var/ossec/framework/python) also carries certificates — the interpreter's
# own test fixtures and several libraries' public-root bundles — which are
# inert package payload that nothing configures or presents, not material
# generated in the image (siem.md §8.6).
_CERTIFICATE_TREES = ("/var/ossec/etc", "/var/ossec/api/configuration")


def test_no_self_signed_material_exists_in_the_image(siem: Node) -> None:
    """Every certificate in the manager's configuration trees is issued by
    the RavensPort internal certificate authority; nothing self-signed was
    ever generated, confirming the leaf was installed before the manager's
    first start and that the enrolment service's package-generated pair is
    absent (siem.md §8.1, §8.6)."""
    listing = siem.run(
        "grep",
        "-rl",
        "--include=*.pem",
        "--include=*.crt",
        "--include=*.cert",
        "-e",
        "BEGIN CERTIFICATE",
        *_CERTIFICATE_TREES,
        check=False,
    )
    certificates = listing.stdout.split()
    assert "/var/ossec/api/configuration/ssl/server.crt" in certificates, (
        "no management interface certificate found"
    )
    for path in certificates:
        issuer = siem.run(
            "openssl", "x509", "-noout", "-issuer", "-in", path, check=True
        ).stdout
        assert "CN = RavensPort Internal CA" in issuer, (
            f"{path} is not issued by the RavensPort internal authority: {issuer}"
        )


def test_enrolment_service_is_not_listening(siem: Node) -> None:
    """siem.md §8.1: agent keys are generated at render time, so the
    manager's enrolment service is not enabled and nothing listens on its
    port, TCP 1515."""
    listeners = siem.run("ss", "-Htln", check=True).stdout
    assert not any(
        line.split()[3].endswith(":1515") for line in listeners.splitlines() if line
    ), f"something listens on TCP 1515:\n{listeners}"


def test_client_certificate_verification_is_not_enabled(
    config: Config, siem_admin_credential: Credential, siem: Node
) -> None:
    """A connection to the management interface presenting no client
    certificate succeeds at the transport level and is refused only by the
    credential, not by certificate policy."""
    script = (
        "import socket, ssl\n"
        "ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)\n"
        "ctx.check_hostname = False\n"
        "ctx.verify_mode = ssl.CERT_NONE\n"
        f"with socket.create_connection(('127.0.0.1', {config.api_port}), timeout=5) as raw:\n"
        "    with ctx.wrap_socket(raw):\n"
        "        pass\n"
        "print('handshake-ok')\n"
    )
    result = siem.run("python3", "-c", script, check=True)
    assert "handshake-ok" in result.stdout, (
        "the TLS handshake itself failed with no client certificate presented"
    )

    wrong_password_probe = siem.run(
        "curl",
        "--silent",
        "--insecure",
        "-o",
        "/dev/null",
        "-w",
        "%{http_code}",
        "--user",
        f"{siem_admin_credential.username}:not-the-password",
        "-X",
        "POST",
        f"https://127.0.0.1:{config.api_port}/security/user/authenticate",
        check=True,
    )
    assert wrong_password_probe.stdout.strip() == "401", (
        "a wrong password was not refused with 401 — access may be gated by "
        "something other than the credential"
    )


def test_only_the_management_account_can_authenticate(
    config: Config, siem_admin_credential: Credential, siem: Node
) -> None:
    """siem.md §8.5: the manager's two default superusers, `wazuh` and
    `wazuh-wui`, are refused with their well-known passwords, and the
    management-interface account is the only user in rbac.db, with no role
    assignment for a user that does not exist."""
    for username in ("wazuh", "wazuh-wui"):
        probe = siem.run(
            "curl",
            "--silent",
            "--insecure",
            "-o",
            "/dev/null",
            "-w",
            "%{http_code}",
            "--user",
            f"{username}:{username}",
            "-X",
            "POST",
            f"https://127.0.0.1:{config.api_port}/security/user/authenticate",
            check=True,
        )
        assert probe.stdout.strip() == "401", (
            f"the default credential {username}/{username} was not refused "
            f"({probe.stdout.strip()})"
        )

    rbac = "/var/ossec/api/configuration/security/rbac.db"
    users = siem.run(
        "sqlite3", "-readonly", rbac, "SELECT username FROM users;", check=True
    ).stdout.split()
    assert users == [siem_admin_credential.username], f"rbac.db users: {users}"
    orphans = siem.run(
        "sqlite3",
        "-readonly",
        rbac,
        "SELECT user_id, role_id FROM user_roles "
        "WHERE user_id NOT IN (SELECT id FROM users);",
        check=True,
    ).stdout.split()
    assert not orphans, f"role assignments for removed users: {orphans}"


def test_agent_channel_presents_no_certificate_at_all(config: Config) -> None:
    """Distinct from the check above: the agent-to-manager channel on TCP
    1514 performs no certificate exchange, consistent with siem.md §8.6's
    statement that this channel is a pre-shared key, not TLS.

    Run from corp-it via incus exec: no firewall path admits vnet-ext to
    siem-01 at all, so a direct socket from the test runner would time out
    regardless of what the manager presents on this port. corp-it is
    same-zone with siem-01 and needs no route."""
    script = f"""
import socket, ssl
context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
context.check_hostname = False
context.verify_mode = ssl.CERT_NONE
try:
    with socket.create_connection(("{config.siem_host}", {config.agent_port}), timeout=5) as raw:
        with context.wrap_socket(raw):
            pass
    print("NO_TLS_ERROR")
except ssl.SSLError:
    print("TLS_REJECTED")
"""
    result = Node("corp-it").run("python3", "-c", script, check=True)
    assert "TLS_REJECTED" in result.stdout, (
        "a TLS handshake against the agent channel did not fail — a "
        "certificate may be in use where siem.md §8.6 says none should be"
    )
