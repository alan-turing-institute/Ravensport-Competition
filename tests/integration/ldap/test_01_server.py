"""
LDAP server verification tests for corp-it (corp-it-tests.md §1-§3).

All tests connect to corp-it on port 636 (LDAPS) via the production two-hop
firewall path (vnet-ot → fw-ot-dmz → fw-dmz-it → vnet-it), except
`test_no_plaintext_listener_on_port_389`, which deliberately connects without
TLS. Passing these tests confirms that slapd is running, TLS-only, correctly
seeded, and the firewall path is open.

Run via:
    make -f tests/integration/Makefile ldap
"""

import socket
import ssl

import ldap3
import pytest

from tests.integration.ldap.conftest import Config, LDAPVerifier

# ---------------------------------------------------------------------------
# §1  Directory content (corp-it-tests.md §1; corp-it.md §5's base
# configuration)
# ---------------------------------------------------------------------------


def test_base_dn_exists(ldap_verifier: LDAPVerifier, config: Config) -> None:
    """The base DN entry is present in the directory."""
    with ldap_verifier.admin_connection() as conn:
        conn.search(
            search_base=config.base_dn,
            search_filter="(objectClass=dcObject)",
            search_scope=ldap3.BASE,
            attributes=["dc"],
        )
        assert len(conn.entries) >= 1, (
            f"Base DN {config.base_dn!r} not found — "
            "slapd may not have been configured with the correct domain"
        )


def test_people_ou_exists(ldap_verifier: LDAPVerifier, config: Config) -> None:
    """The people organisational unit exists directly under the base DN."""
    with ldap_verifier.admin_connection() as conn:
        conn.search(
            search_base=config.base_dn,
            search_filter="(ou=people)",
            search_scope=ldap3.LEVEL,
            attributes=["ou"],
        )
        assert len(conn.entries) == 1, (
            f"Expected exactly one ou=people entry under {config.base_dn!r}, "
            f"found {len(conn.entries)}"
        )


def test_operator01_exists(ldap_verifier: LDAPVerifier, config: Config) -> None:
    """The operator01 account entry is present under ou=people."""
    with ldap_verifier.admin_connection() as conn:
        conn.search(
            search_base=config.people_dn,
            search_filter=f"(uid={config.operator_username})",
            search_scope=ldap3.LEVEL,
            attributes=["uid"],
        )
        assert len(conn.entries) == 1, (
            f"Expected exactly one uid={config.operator_username} entry under "
            f"{config.people_dn!r}, found {len(conn.entries)}"
        )


def test_operator01_attributes(ldap_verifier: LDAPVerifier, config: Config) -> None:
    """The operator01 entry carries the POSIX attributes required by the FMS.

    The expected identifiers come from the renderer that allocated them, not
    from numbers written here: they moved once already, from the 1000 range to
    10000, when a directory group at 1000 was found to be shadowed by the
    container runtime's local `lxd` group on `recon-01`.
    """
    with ldap_verifier.admin_connection() as conn:
        conn.search(
            search_base=config.people_dn,
            search_filter=f"(uid={config.operator_username})",
            search_scope=ldap3.LEVEL,
            attributes=["uidNumber", "gidNumber", "homeDirectory"],
        )
        assert len(conn.entries) == 1
        entry = conn.entries[0]
        assert int(entry.uidNumber.value) == config.operator_uid_number, (
            f"Expected uidNumber {config.operator_uid_number}, "
            f"got {entry.uidNumber.value!r}"
        )
        assert int(entry.gidNumber.value) == config.group_gid_number, (
            f"Expected gidNumber {config.group_gid_number}, "
            f"got {entry.gidNumber.value!r}"
        )
        assert entry.homeDirectory.value == f"/home/{config.operator_username}", (
            f"Expected homeDirectory /home/{config.operator_username}, "
            f"got {entry.homeDirectory.value!r}"
        )


# ---------------------------------------------------------------------------
# §2  Directory authentication (corp-it-tests.md §2)
# ---------------------------------------------------------------------------


def test_admin_bind_succeeds(ldap_verifier: LDAPVerifier) -> None:
    """Admin bind succeeds; confirms slapd is reachable over LDAPS via the
    firewall path."""
    conn = ldap_verifier.admin_connection()
    assert conn.bound, (
        f"Admin bind to {ldap_verifier._cfg.corp_it_host}:{ldap_verifier._cfg.ldaps_port} "
        "failed — check that slapd is running on corp-it, serving LDAPS, and "
        "that the route to 10.30.0.0/24 is configured (make configure-corp-it)"
    )
    conn.unbind()


def test_operator_bind_succeeds(ldap_verifier: LDAPVerifier, config: Config) -> None:
    """The seeded operator account can bind with its configured password —
    the exact bind the FMS performs at operator login."""
    people_dn = f"ou=people,{config.base_dn}"
    operator_dn = f"uid={config.operator_username},{people_dn}"
    assert ldap_verifier.bind_as(operator_dn, config.operator_password), (
        f"Bind failed for {operator_dn!r} — the password in world.yaml does not "
        "match the salted verifier the directory holds. That verifier is rendered "
        "into build/ldif/corp-it.ldif and applied to slapd when the corp-it image "
        "is built, so a password changed since that build reaches the directory "
        "only on a rebuild."
    )


def test_operator_bind_fails_with_wrong_password(
    ldap_verifier: LDAPVerifier, config: Config
) -> None:
    """The same account, presented with an incorrect password, is refused —
    confirms the bind actually verifies the password rather than merely
    confirming the account's existence."""
    people_dn = f"ou=people,{config.base_dn}"
    operator_dn = f"uid={config.operator_username},{people_dn}"
    assert not ldap_verifier.bind_as(
        operator_dn, config.operator_password + "-wrong"
    ), f"Bind for {operator_dn!r} succeeded with an incorrect password"


# ---------------------------------------------------------------------------
# §3  Directory transport security (corp-it-tests.md §3; corp-it.md §3/§5)
# ---------------------------------------------------------------------------


def test_slapd_presents_ca_signed_certificate(config: Config) -> None:
    """Fetch the TLS certificate slapd presents on :636. Assert it validates
    against the RavensPort internal CA and its subject alternative name is
    corp-it — confirms corp-it.md §5/§6's certificate installation,
    independently of whether a bind happens to succeed (a client that skips
    verification would not otherwise notice a wrong or expired
    certificate)."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_verify_locations(cafile=str(config.ca_cert_path))
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED

    with socket.create_connection(
        (config.corp_it_host, config.ldaps_port), timeout=10
    ) as sock:
        with context.wrap_socket(sock, server_hostname=config.tls_hostname) as tls_sock:
            cert = tls_sock.getpeercert()

    assert cert is not None, "slapd presented no certificate on :636"
    sans = [value for kind, value in cert.get("subjectAltName", ()) if kind == "DNS"]
    assert sans == ["corp-it"], (
        f"corp-it's certificate carries DNS SANs {sans!r}, expected exactly ['corp-it']"
    )


def test_no_plaintext_listener_on_port_389(config: Config) -> None:
    """Attempt a TCP connection to corp-it:389 over the same two-hop
    production firewall path every other test in this module uses. The
    connection times out: no firewall rule admits port 389 to corp-it on
    either hop (deployment.md §4.2), so the packet is dropped in transit and
    never reaches a host that could answer it either way. This is a stronger
    property than "nothing is listening" — a refusal would mean the packet
    got through and slapd's absent listener answered it; a timeout means the
    firewall never let it through at all (test_policy.sh §4 documents the
    same refusal/timeout distinction for exactly this reason)."""
    with pytest.raises(TimeoutError):
        with socket.create_connection(
            (config.corp_it_host, config.plain_port), timeout=10
        ):
            pass


def test_unauthenticated_bind_over_tls_still_requires_a_password(
    ldap_verifier: LDAPVerifier, config: Config
) -> None:
    """A bind attempt against the LDAPS listener with no password is
    refused. Confirms that moving the transport to TLS did not loosen the
    bind requirement it wraps — TLS protects the credential in transit; it
    does not substitute for one."""
    people_dn = f"ou=people,{config.base_dn}"
    operator_dn = f"uid={config.operator_username},{people_dn}"
    assert not ldap_verifier.bind_as(operator_dn, ""), (
        f"Unauthenticated bind for {operator_dn!r} succeeded"
    )
