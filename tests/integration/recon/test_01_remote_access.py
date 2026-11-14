"""
Remote access — the image-level half (`recon-tests.md` §2), targeting
`recon.md` §4.

**These are not §2's assertions, and must not be mistaken for them.** §2 states
the security layer must be asserted on what the server *negotiates*, because a
correct `xrdp.ini` is not evidence that the running service uses it. That needs
a client speaking Remote Desktop Protocol, which does not exist yet. What is
asserted here is the weaker, still useful property: the service is listening
where the specification says, and the image carries the configuration and the
group membership a correct negotiation depends on.

The tests below therefore catch a build that produced the wrong thing. They
cannot catch a running service that ignores what the build produced. When the
client exists, §2's `security_layer_is_tls_not_the_legacy_layer`,
`xrdp_can_read_its_private_key` and `rejected_credentials_produce_a_defined_outcome`
join this module. Two supersede a test here rather than adding to it:

    security_layer_is_configured_as_tls  ->  security_layer_is_tls_not_the_legacy_layer
    xrdp_key_is_readable_by_xrdp_alone   ->  xrdp_can_read_its_private_key

In each pair the built test reads what the build produced; the pending one
asserts what the running service does with it.
`rejected_credentials_produce_a_defined_outcome` has no image-level counterpart.

`xrdp_presents_the_ca_issued_certificate` is not pending: it needs only the
security handshake, not a session, and is made from a zone peer with a short
client of its own (below).

Run on the VM: python -m pytest tests/integration/recon/test_01_remote_access.py
"""

from __future__ import annotations

from tests.integration.recon.conftest import Node

XRDP_KEY = "/etc/xrdp/tls/recon-01.key"

# The deployment's root as every node's build installs it (`_trust_ca.sh.j2`).
# The handshake is verified against this file alone rather than the system
# store, so that a certificate from any other issuer is refused.
DEPLOYMENT_ROOT = "/usr/local/share/ca-certificates/ravensport-ca.crt"

# Opens the Remote Desktop Protocol connection as far as the security handshake
# and no further. The X.224 Connection Request carries an RDP_NEG_REQ asking for
# TLS alone (PROTOCOL_SSL, 0x1); the server's Connection Confirm must select it,
# after which the stream is TLS. The certificate is then verified against the
# deployment's root and the name `recon-01`, given explicitly, as the
# reconciliation user verifies it. Prints the certificate's subject alternative
# names on success; on any failure, prints the reason and exits non-zero.
_HANDSHAKE = """
import socket, ssl, struct, sys
host, port, root = sys.argv[1], int(sys.argv[2]), sys.argv[3]
neg_req = struct.pack("<BBHI", 0x01, 0x00, 8, 0x00000001)
x224_cr = bytes([6 + len(neg_req), 0xE0, 0, 0, 0, 0, 0]) + neg_req
tpkt = struct.pack(">BBH", 3, 0, 4 + len(x224_cr)) + x224_cr
raw = socket.create_connection((host, port), timeout=10)
raw.sendall(tpkt)
confirm = raw.recv(19)
if len(confirm) != 19 or confirm[5] != 0xD0 or confirm[11] != 0x02:
    sys.exit(f"no TLS negotiation response: {confirm.hex()}")
selected = struct.unpack("<I", confirm[15:19])[0]
if selected != 0x00000001:
    sys.exit(f"server selected protocol {selected:#x}, not TLS")
context = ssl.create_default_context(cafile=root)
try:
    tls = context.wrap_socket(raw, server_hostname="recon-01")
except ssl.SSLError as error:
    sys.exit(f"handshake refused: {error}")
print(repr(tls.getpeercert().get("subjectAltName", ())))
"""


def test_xrdp_is_reachable_on_the_zone_address(
    peer: Node, recon_host: str, rdp_port: int
) -> None:
    """Probed from a real neighbour on `vnet-it` rather than from the test
    runner. The runner joins no zone (see conftest), and the question is whether
    a host on this zone can reach the workstation — which is a property of the
    zone, not of the runner."""
    assert peer.can_reach(recon_host, rdp_port), (
        f"{peer.name} cannot reach recon-01 at {recon_host}:{rdp_port}. The "
        "remote desktop session is the only channel between the reconciliation "
        "user and this workstation."
    )


def test_security_layer_is_configured_as_tls(recon: Node) -> None:
    """Under xrdp's default `negotiate` the server falls back to the legacy
    standard Remote Desktop Protocol layer, which a client requiring enhanced
    security refuses (`recon.md` §4).

    This reads the file. It establishes that the build's substitution matched —
    a `sed` that matches nothing leaves the packaged default and fails silently
    — and nothing more. What the server offers a client is §2's assertion."""
    values = [
        line.strip()
        for line in recon.read("/etc/xrdp/xrdp.ini").splitlines()
        if line.strip().startswith("security_layer=")
    ]
    assert values == ["security_layer=tls"], (
        f"expected exactly one 'security_layer=tls' in xrdp.ini, found: {values}"
    )


def test_xrdp_key_is_readable_by_xrdp_alone(recon: Node) -> None:
    """A key the `xrdp` user cannot read produces a service that starts normally
    and reports `SSL_CERT_NOT_ON_SERVER` only when a client arrives; a key
    readable by anyone else is readable by more than the one service that
    presents it (`recon.md` §4).

    This reads the ownership and mode from the image, establishing what the
    build produced rather than what the service does with it — §2's
    `xrdp_can_read_its_private_key`, which needs a client."""
    owner_and_mode = recon.stdout("stat", "-c", "%U %a", XRDP_KEY).strip()
    assert owner_and_mode == "xrdp 600", (
        f"{XRDP_KEY} is '{owner_and_mode}' (owner, mode); expected 'xrdp 600'"
    )


def test_xrdp_presents_the_ca_issued_certificate(
    peer: Node, recon_host: str, rdp_port: int
) -> None:
    """The reconciliation user verifies the certificate against the deployment's
    root and the name `recon-01`, and refuses anything else before sending the
    workstation credential (`recon.md` §4). The distribution's self-signed pair,
    presented instead, would end every audit at connection.

    Made from a zone peer rather than through the sim host's proxy port: the sim
    host carries no client that could make the handshake, and the proxy passes
    the stream through in TCP mode, so the certificate presented is the same on
    either path. The proxied path is exercised by the reconciliation user
    itself."""
    result = peer.run(
        "python3", "-c", _HANDSHAKE, recon_host, str(rdp_port), DEPLOYMENT_ROOT
    )
    assert result.returncode == 0, (
        f"{peer.name} could not verify recon-01's certificate against the "
        f"deployment's root and the name recon-01: "
        f"{(result.stderr or result.stdout).strip()}"
    )
    assert result.stdout.strip() == "(('DNS', 'recon-01'),)", (
        "recon-01's certificate must name recon-01 and carry no address; its "
        f"subject alternative names are {result.stdout.strip()}"
    )


def test_xrdp_services_are_enabled(recon: Node) -> None:
    """Both units, since `xrdp` accepts the connection and `xrdp-sesman` starts
    the session behind it; a host with only the first accepts a connection and
    then fails to produce a desktop."""
    for unit in ("xrdp", "xrdp-sesman"):
        state = recon.run("systemctl", "is-enabled", unit).stdout.strip()
        assert state == "enabled", f"{unit} is {state or 'not enabled'} at boot"
