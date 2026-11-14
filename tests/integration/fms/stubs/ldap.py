"""
LDAP account manager for the slapd stub.

The test environment uses a plain OpenLDAP (slapd) server started externally
before running the suite.  The FMS VM is provisioned with SSSD configured for
simple-bind LDAP authentication against this server (no Kerberos in the test
image — a documented deviation from the production FreeIPA deployment).

This module manages test user accounts via ldap3.  It does not start or stop
slapd; that is the responsibility of scripts/start-test-ldap.sh.

Expected slapd configuration:
  Base DN:       dc=ravensport,dc=local
  Admin bind DN: cn=admin,dc=ravensport,dc=local
  People OU:     ou=people,dc=ravensport,dc=local
  objectClass for users: inetOrgPerson + posixAccount
"""

from dataclasses import dataclass
from typing import Generator

import ldap3


PEOPLE_OU_TEMPLATE = "ou=people,{base_dn}"


@dataclass
class LDAPAccount:
    username: str
    password: str
    uid_number: int
    gid_number: int = 1000


class LDAPAccounts:
    """
    Creates and removes LDAP user accounts for the duration of the test session.
    """

    def __init__(
        self,
        host: str,
        port: int,
        base_dn: str,
        bind_dn: str,
        bind_password: str,
    ) -> None:
        self._base_dn = base_dn
        self._bind_dn = bind_dn
        self._bind_password = bind_password
        self._server = ldap3.Server(host, port=port, get_info=ldap3.NONE)
        self._conn: ldap3.Connection | None = None
        self._created: list[str] = []

    def connect(self) -> None:
        self._conn = ldap3.Connection(
            self._server,
            user=self._bind_dn,
            password=self._bind_password,
            auto_bind=ldap3.AUTO_BIND_TLS_BEFORE_BIND
            if False  # plain LDAP in test env
            else ldap3.AUTO_BIND_NO_TLS,
        )
        if not self._conn.bound:
            raise RuntimeError(
                f"Cannot bind to LDAP at {self._server.host}:{self._server.port} — "
                "is slapd running? (see scripts/start-test-ldap.sh)"
            )

    def disconnect(self) -> None:
        self._cleanup()
        if self._conn is not None:
            self._conn.unbind()

    def add_user(self, account: LDAPAccount) -> str:
        """Add a posixAccount user entry.  Returns the DN."""
        assert self._conn is not None
        people_ou = PEOPLE_OU_TEMPLATE.format(base_dn=self._base_dn)
        dn = f"uid={account.username},{people_ou}"
        self._conn.add(
            dn,
            object_class=["inetOrgPerson", "posixAccount", "shadowAccount"],
            attributes={
                "uid": account.username,
                "cn": account.username,
                "sn": account.username,
                "userPassword": account.password,
                "uidNumber": account.uid_number,
                "gidNumber": account.gid_number,
                "homeDirectory": f"/home/{account.username}",
            },
        )
        if self._conn.result["result"] not in (0, 68):  # 68 = already exists
            raise RuntimeError(f"LDAP add failed: {self._conn.result}")
        self._created.append(dn)
        return dn

    def remove_user(self, username: str) -> None:
        assert self._conn is not None
        people_ou = PEOPLE_OU_TEMPLATE.format(base_dn=self._base_dn)
        dn = f"uid={username},{people_ou}"
        self._conn.delete(dn)
        self._created = [d for d in self._created if d != dn]

    def _cleanup(self) -> None:
        if self._conn is None:
            return
        for dn in list(self._created):
            self._conn.delete(dn)
        self._created.clear()


def ldap_accounts_fixture(
    host: str,
    port: int,
    base_dn: str,
    bind_dn: str,
    bind_password: str,
) -> Generator[LDAPAccounts, None, None]:
    manager = LDAPAccounts(host, port, base_dn, bind_dn, bind_password)
    manager.connect()
    yield manager
    manager.disconnect()
