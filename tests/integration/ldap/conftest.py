"""
Fixtures for the corp-it LDAP integration tests.

These tests connect to the slapd instance on corp-it (10.30.0.20) via the
production network path: vnet-ot → fw-ot-dmz → fw-dmz-it → vnet-it.  The
test runner must have a route to 10.30.0.0/24 via the fw-ot-dmz OT interface;
the 'configure-corp-it' Makefile target adds this route.

What the directory holds is not restated here. The base DN, the people
organisational unit and the operator accounts are read from the same
`world.yaml` the image was rendered from, and the POSIX identifiers those
accounts carry are read from `render.py`, which allocates them. A copy of
either goes stale on the render after it is written, and does so silently:
this suite asserted a `uidNumber` of 2001 for nine weeks after the directory
began issuing 10002.

Environment variables (with defaults):

  CORP_IT_HOST          IP of corp-it on vnet-it     (default: 10.30.0.20)
  CORP_IT_TLS_HOSTNAME  Name corp-it's certificate SAN carries, used for TLS
                         hostname verification independently of CORP_IT_HOST
                         (which may be an IP address, the same way
                         `curl --resolve` separates the connection target
                         from the name presented for verification)
                                                     (default: corp-it)
  CORP_IT_CA_CERT       Path to the CA's public root (build artefact, not a
                         secret) that corp-it's certificate is validated
                         against                     (default: <repo>/deploy/build/ca/ca.pem)
  LDAP_WORLD_YAML       world.yaml the deployment
                        was rendered from            (default: deploy/world.yaml)
  LDAP_BASE_DN          Directory base DN            (default: world.yaml ldap.base_dn)
  LDAP_BIND_DN          Admin bind DN                (default: cn=admin,<base DN>)
  LDAP_BIND_PASSWORD    Admin password               (default: LdapAdmin1!)
  FMS_OPERATOR_USERNAME Operator LDAP username       (default: first world.yaml operator)
  FMS_OPERATOR_PASSWORD Operator LDAP password       (default: that operator's password)
"""

from __future__ import annotations

import os
import ssl
import sys
from dataclasses import dataclass
from pathlib import Path

import ldap3
import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]

# The POSIX identifiers are render-time constants rather than world.yaml fields,
# so they are read from the renderer rather than copied here.
sys.path.insert(0, str(REPO_ROOT / "deploy" / "scripts"))
import render  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class Config:
    corp_it_host: str
    ldaps_port: int
    plain_port: int
    tls_hostname: str
    ca_cert_path: Path
    base_dn: str
    people_ou: str
    admin_dn: str
    admin_password: str
    operator_username: str
    operator_password: str
    operator_uid_number: int
    group_gid_number: int

    @property
    def people_dn(self) -> str:
        return f"{self.people_ou},{self.base_dn}"

    @classmethod
    def from_world(cls, world: dict) -> "Config":
        directory = world["ldap"]
        base_dn = os.environ.get("LDAP_BASE_DN", directory["base_dn"])

        operators = world["identities"]["operators"]
        username = os.environ.get("FMS_OPERATOR_USERNAME", operators[0]["username"])
        index = next(
            (i for i, op in enumerate(operators) if op["username"] == username),
            None,
        )
        if index is None:
            pytest.fail(
                f"operator {username!r} is not in the operators list of the "
                "world.yaml this suite was pointed at, so the identifiers the "
                "directory holds for them cannot be derived. Point "
                "LDAP_WORLD_YAML at the world.yaml the deployment was rendered "
                "from, or drop the FMS_OPERATOR_USERNAME override."
            )

        return cls(
            corp_it_host=os.environ.get("CORP_IT_HOST", "10.30.0.20"),
            ldaps_port=636,
            plain_port=389,
            tls_hostname=os.environ.get("CORP_IT_TLS_HOSTNAME", "corp-it"),
            ca_cert_path=Path(
                os.environ.get(
                    "CORP_IT_CA_CERT", _REPO_ROOT / "deploy" / "build" / "ca" / "ca.pem"
                )
            ),
            base_dn=base_dn,
            people_ou=directory["people_ou"],
            admin_dn=os.environ.get("LDAP_BIND_DN", f"cn=admin,{base_dn}"),
            # The one credential here with no world.yaml field behind it: slapd's
            # administrator password is seeded through debconf by
            # `deploy/build-scripts/corp-it.sh.j2`, which holds it as a literal.
            admin_password=os.environ.get("LDAP_BIND_PASSWORD", "LdapAdmin1!"),
            operator_username=username,
            operator_password=os.environ.get(
                "FMS_OPERATOR_PASSWORD", operators[index]["password"]
            ),
            # `render.build_ldif` allocates one identifier per operator in the
            # order world.yaml lists them, from a base clear of every range a
            # host allocates locally.
            operator_uid_number=render.OPERATOR_UID_BASE + index,
            group_gid_number=render.DIRECTORY_GID_NUMBER,
        )


class LDAPVerifier:
    """
    Thin ldap3 wrapper for verifying the corp-it LDAP directory over LDAPS.

    Connects to corp-it on port 636 over the production vnet-ot → vnet-it
    firewall path, validating the presented certificate against the CA's
    public root and against `config.tls_hostname` — decoupled from the
    connection address the same way `fms_pusher.py` decouples `host` from
    `hist_tls_hostname` for the log-server pushes, so the runner need not
    itself resolve `corp-it` by name. Two methods are provided:
    admin_connection() for directory inspection and bind_as() for
    authentication checks.
    """

    def __init__(self, config: Config) -> None:
        self._cfg = config
        # valid_names, not sni alone: sni only sets the wire-protocol SNI
        # extension. Hostname *validation* is checked against the literal
        # connection address unless valid_names supplies an additional
        # acceptable name — without it, a corp-it certificate with a
        # hostname-only SAN never matches when connected to by IP.
        tls = ldap3.Tls(
            validate=ssl.CERT_REQUIRED,
            version=ssl.PROTOCOL_TLS_CLIENT,
            ca_certs_file=str(config.ca_cert_path),
            valid_names=[config.tls_hostname],
            sni=config.tls_hostname,
        )
        self._server = ldap3.Server(
            config.corp_it_host,
            port=config.ldaps_port,
            use_ssl=True,
            tls=tls,
            get_info=ldap3.NONE,
        )

    def admin_connection(self) -> ldap3.Connection:
        """Return a bound admin connection."""
        conn = ldap3.Connection(
            self._server,
            user=self._cfg.admin_dn,
            password=self._cfg.admin_password,
            # NO_TLS, not TLS_BEFORE_BIND: the connection is already TLS-wrapped
            # via the server's own use_ssl=True (implicit LDAPS on 636).
            # TLS_BEFORE_BIND additionally issues a StartTLS extended
            # operation, which upgrades a *plain* connection and does not
            # apply to one that is already SSL-wrapped.
            auto_bind=ldap3.AUTO_BIND_NO_TLS,
        )
        return conn

    def bind_as(self, dn: str, password: str) -> bool:
        """Attempt a simple bind and return True if successful."""
        try:
            conn = ldap3.Connection(
                self._server,
                user=dn,
                password=password,
                auto_bind=ldap3.AUTO_BIND_NO_TLS,
            )
            return conn.bound
        except ldap3.core.exceptions.LDAPBindError:
            return False
        except ldap3.core.exceptions.LDAPSocketOpenError:
            return False
        # ldap3 refuses an empty password client-side before ever contacting
        # the server (RFC 4513 §5.1.2 unauthenticated-bind semantics) —
        # a bind that never happens is a failed one for this helper's purposes.
        except ldap3.core.exceptions.LDAPPasswordIsMandatoryError:
            return False


@pytest.fixture(scope="session")
def world() -> dict:
    path = Path(
        os.environ.get("LDAP_WORLD_YAML", str(REPO_ROOT / "deploy" / "world.yaml"))
    )
    if not path.exists():
        pytest.skip(f"world.yaml not found at {path}")
    return yaml.safe_load(path.read_text())


@pytest.fixture(scope="session")
def config(world: dict) -> Config:
    return Config.from_world(world)


@pytest.fixture(scope="session")
def ldap_verifier(config: Config) -> LDAPVerifier:
    return LDAPVerifier(config)
