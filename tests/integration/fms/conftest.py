"""
Session- and function-scoped fixtures for the FMS integration test suite.

Environment variables (with defaults):

  FMS_HOST                  IP of fms-01 on vnet-ot         (default: 10.40.0.10)
  FMS_NODE_NAME             Incus node name for fms-01       (default: fms-01)
  RUNNER_HOST               Host bridge IP on vnet-ot        (default: 10.40.0.1)
  ATG_STUB_PORT             TLS stub listen port              (default: 10001)
  MODBUS_STUB_PORT          Modbus stub listen port           (default: 502)
  VMAGENT_STUB_PORT         vmagent import stub port          (default: 4029)
  VLAGENT_STUB_PORT         vlagent JSON-line stub port       (default: 4129)
  SYSLOG_STUB_PORT          vlagent syslog stub port (TLS)    (default: 4514)
  SYSLOG_STUB_CERT          syslog stub certificate (PEM)     (default: tests/integration/.stub-tls/log-server-stub.pem)
  SYSLOG_STUB_KEY           syslog stub private key (PEM)     (default: tests/integration/.stub-tls/log-server-stub.key)
  LDAP_STUB_PORT            slapd listen port                 (default: 389)
  FMS_VESSEL_RFID_TCP_PORT  port the test runner listens on for fms-01's vessel RFID client  (default: 9001)
  FMS_OPERATOR_RFID_TCP_PORT port the test runner listens on for fms-01's operator RFID client (default: 9002)
  FMS_POSTGRES_DB           Postgres database name            (default: fms)
  LDAP_BASE_DN              slapd base DN                    (default: dc=ravensport,dc=local)
  LDAP_BIND_DN              slapd admin bind DN              (default: cn=admin,dc=ravensport,dc=local)
  LDAP_BIND_PASSWORD        slapd admin password             (default: LdapAdmin1!)
  FMS_ADMIN_USERNAME        FMS local admin username         (default: fms-admin)
  FMS_ADMIN_PASSWORD        FMS local admin password         (default: RvnsPrt-adm1n)
  FMS_OPERATOR_USERNAME     Operator LDAP username           (default: jrenard)
  FMS_OPERATOR_PASSWORD     Operator LDAP password           (default: D0ck-0p-rvp)
  FMS_OPERATOR_RFID         Operator RFID card value (26-bit int) (default: 1000002)
  FMS_VESSEL_RFID           Vessel RFID card value (26-bit int)   (default: 1000001)
  FMS_VESSEL_IMO_NUMBER     IMO number the card is enrolled to    (default: 1999997)

Prerequisites:
  - fms-01 is running and reachable at FMS_HOST via the Incus vnet-ot bridge.
    No tap setup is needed: the host already has RUNNER_HOST as its bridge IP.
  - fms-01 is configured with FMS_VESSEL_RFID_HOST=RUNNER_HOST so its serial
    client connects to the test runner's RFID servers rather than sim-layer.
  - slapd is running on RUNNER_HOST:LDAP_STUB_PORT (see tests/integration/fms/stubs/start-test-ldap.sh).
  - fms-01 is provisioned with test-mode configuration (update /etc/fms/fms.env
    and restart fms.service):
      FMS_ATG_HOST=RUNNER_HOST, FMS_ATG_PORT=ATG_STUB_PORT
      FMS_DISPENSER_HOST=RUNNER_HOST, FMS_DISPENSER_PORT=MODBUS_STUB_PORT
      FMS_VMAGENT_URL=http://RUNNER_HOST:VMAGENT_STUB_PORT/api/v1/import
      FMS_VLAGENT_JSONLINE_URL=http://RUNNER_HOST:VLAGENT_STUB_PORT/insert/jsonline
      FMS_VLAGENT_SYSLOG_HOST=log-server-stub, FMS_VLAGENT_SYSLOG_PORT=SYSLOG_STUB_PORT
        (log-server-stub mapped to RUNNER_HOST in fms-01's /etc/hosts; the stub's
        certificate is issued for that name by `make stub-tls`)
      FMS_ATG_POLL_INTERVAL=1, FMS_MODBUS_POLL_INTERVAL=1
      FMS_LDAP_URI=ldap://RUNNER_HOST:LDAP_STUB_PORT
      FMS_SESSION_TIMEOUT=5
  - The postgres fixture uses `incus exec FMS_NODE_NAME -- sudo -u postgres psql`
    to run queries directly inside the container, without an SSH tunnel.
"""

import os
import time
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

from tests.integration.fms.stubs.atg import ATGStub, atg_stub_fixture
from tests.integration.fms.stubs.log_server import (
    LogServerStub,
    log_server_stub_fixture,
)
from tests.integration.fms.stubs.ldap import (
    LDAPAccount,
    LDAPAccounts,
    ldap_accounts_fixture,
)
from tests.integration.fms.stubs.modbus import ModbusStub, modbus_stub_fixture
from tests.integration.fms.stubs.postgres import PostgresClient, postgres_client_fixture
from tests.integration.fms.stubs.rfid import RFIDPair, rfid_pair_fixture


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Config:
    fms_host: str
    fms_node_name: str
    runner_host: str
    atg_stub_port: int
    modbus_stub_port: int
    vmagent_stub_port: int
    vlagent_stub_port: int
    syslog_stub_port: int
    syslog_stub_cert: str
    syslog_stub_key: str
    ldap_stub_port: int
    fms_vessel_rfid_port: int
    fms_operator_rfid_port: int
    pg_db: str
    ldap_base_dn: str
    ldap_bind_dn: str
    ldap_bind_password: str
    admin_username: str
    admin_password: str
    operator_username: str
    operator_password: str
    operator_rfid: int
    vessel_rfid: int
    vessel_imo_number: str

    @classmethod
    def from_env(cls) -> "Config":
        base_dn = os.environ.get("LDAP_BASE_DN", "dc=ravensport,dc=local")
        stub_tls = Path(__file__).resolve().parent.parent / ".stub-tls"
        return cls(
            fms_host=os.environ.get("FMS_HOST", "10.40.0.10"),
            fms_node_name=os.environ.get("FMS_NODE_NAME", "fms-01"),
            runner_host=os.environ.get("RUNNER_HOST", "10.40.0.1"),
            atg_stub_port=int(os.environ.get("ATG_STUB_PORT", "10001")),
            modbus_stub_port=int(os.environ.get("MODBUS_STUB_PORT", "502")),
            vmagent_stub_port=int(os.environ.get("VMAGENT_STUB_PORT", "4029")),
            vlagent_stub_port=int(os.environ.get("VLAGENT_STUB_PORT", "4129")),
            syslog_stub_port=int(os.environ.get("SYSLOG_STUB_PORT", "4514")),
            syslog_stub_cert=os.environ.get(
                "SYSLOG_STUB_CERT", str(stub_tls / "log-server-stub.pem")
            ),
            syslog_stub_key=os.environ.get(
                "SYSLOG_STUB_KEY", str(stub_tls / "log-server-stub.key")
            ),
            ldap_stub_port=int(os.environ.get("LDAP_STUB_PORT", "389")),
            fms_vessel_rfid_port=int(
                os.environ.get("FMS_VESSEL_RFID_TCP_PORT", "9001")
            ),
            fms_operator_rfid_port=int(
                os.environ.get("FMS_OPERATOR_RFID_TCP_PORT", "9002")
            ),
            pg_db=os.environ.get("FMS_POSTGRES_DB", "fms"),
            ldap_base_dn=base_dn,
            ldap_bind_dn=os.environ.get("LDAP_BIND_DN", f"cn=admin,{base_dn}"),
            ldap_bind_password=os.environ.get("LDAP_BIND_PASSWORD", "LdapAdmin1!"),
            admin_username=os.environ.get("FMS_ADMIN_USERNAME", "fms-admin"),
            admin_password=os.environ.get("FMS_ADMIN_PASSWORD", "RvnsPrt-adm1n"),
            operator_username=os.environ.get("FMS_OPERATOR_USERNAME", "jrenard"),
            operator_password=os.environ.get("FMS_OPERATOR_PASSWORD", "D0ck-0p-rvp"),
            operator_rfid=int(os.environ.get("FMS_OPERATOR_RFID", "1000002")),
            vessel_rfid=int(os.environ.get("FMS_VESSEL_RFID", "1000001")),
            vessel_imo_number=os.environ.get("FMS_VESSEL_IMO_NUMBER", "1999997"),
        )


@pytest.fixture(scope="session")
def config() -> Config:
    return Config.from_env()


# ---------------------------------------------------------------------------
# Stub servers (session-scoped: start once, run for the entire suite)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def atg_stub(config: Config) -> Generator[ATGStub, None, None]:
    yield from atg_stub_fixture("0.0.0.0", config.atg_stub_port)


@pytest.fixture(scope="session")
def modbus_stub(config: Config) -> Generator[ModbusStub, None, None]:
    yield from modbus_stub_fixture("0.0.0.0", config.modbus_stub_port)


@pytest.fixture(scope="session")
def log_server_stub(config: Config) -> Generator[LogServerStub, None, None]:
    yield from log_server_stub_fixture(
        "0.0.0.0",
        config.vmagent_stub_port,
        config.vlagent_stub_port,
        config.syslog_stub_port,
        config.syslog_stub_cert,
        config.syslog_stub_key,
    )


@pytest.fixture(scope="session")
def rfid(config: Config) -> Generator[RFIDPair, None, None]:
    yield from rfid_pair_fixture(
        "0.0.0.0", config.fms_vessel_rfid_port, config.fms_operator_rfid_port
    )


@pytest.fixture(scope="session")
def postgres(config: Config) -> Generator[PostgresClient, None, None]:
    yield from postgres_client_fixture(
        node_name=config.fms_node_name,
        db_name=config.pg_db,
    )


@pytest.fixture(scope="session")
def ldap_accounts(config: Config) -> Generator[LDAPAccounts, None, None]:
    yield from ldap_accounts_fixture(
        config.runner_host,
        config.ldap_stub_port,
        config.ldap_base_dn,
        config.ldap_bind_dn,
        config.ldap_bind_password,
    )


# ---------------------------------------------------------------------------
# FMS HTTP client
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def fms_client(config: Config) -> Generator[httpx.Client, None, None]:
    with httpx.Client(
        base_url=f"https://{config.fms_host}",
        verify=False,  # self-signed cert on OT host
        timeout=10.0,
    ) as client:
        yield client


# ---------------------------------------------------------------------------
# Job creation with slot-contention skip
#
# The FMS admits one job at a time (narrative.md §273). Against a live
# deployment the simulator's dock operator is driving real jobs through the
# same FMS this suite does, so a test that creates its own job can lose the
# slot to it. FMS claims the slot atomically under one write lock
# (fms/src/api/jobs.rs `try_set_current_job`) and returns 409 with
# "a job is already active" to the loser — that 409 *is* the exclusivity
# decision, so checking it here (rather than polling job state beforehand)
# has no check-then-act race to lose. Matched on the response body, not just
# the status code, because 409 is also the cavitation-threshold rejection (a
# different condition some callers assert on directly).
# ---------------------------------------------------------------------------

_SLOT_TAKEN_MESSAGE = "a job is already active"


def create_job_or_skip(
    fms_client: httpx.Client,
    headers: dict[str, str],
    payload: dict[str, object],
) -> httpx.Response:
    """POST /api/v1/jobs, skipping the test if the job slot is already held.

    Returns the response unchanged for every other outcome (success, or a
    rejection for a different reason) so the caller's own assertions still
    apply.
    """
    response = fms_client.post("/api/v1/jobs", headers=headers, json=payload)
    if response.status_code == 409 and _SLOT_TAKEN_MESSAGE in response.text:
        pytest.skip(
            f"FMS job slot already held — likely the simulator driving a real "
            f"job against the same FMS: {response.text}"
        )
    return response


# Statuses in which a job holds the single job slot (fms.md §7.4).
_SLOT_HOLDING_STATUSES = ("active", "running", "awaiting_bdn")


def _current_status(
    fms_client: httpx.Client, headers: dict[str, str], job_id: str
) -> "str | None":
    """The status of `job_id` if it is the current job, otherwise None."""
    response = fms_client.get("/api/v1/jobs/current", headers=headers)
    if response.status_code != 200:
        return None
    body = response.json()
    return body["status"] if body.get("job_id") == job_id else None


def free_job_slot(
    config: Config,
    fms_client: httpx.Client,
    job_id: str,
    modbus_stub: ModbusStub,
    timeout: float = 5.0,
) -> None:
    """Release the single job slot after a test, by the port's own procedure.

    Only an active job can be cancelled (fms.md §7.4). The FMS answers 204 to
    the cancel of an active or an already cancelled job, and 409 to the cancel
    of a running dispense, a dispense awaiting its BDN, or a closed job. So an
    active job is cancelled. A running dispense is left to reach its preset, so
    the FMS records its transaction, and a dispense awaiting its BDN is closed
    by linking one, as the dock operator does (digital.md §4.2). A closed or
    cancelled job holds no slot. Logs in as admin, because the operator's
    session may have expired during a long test.

    Fails the test if the slot is still held afterwards: a held slot would
    otherwise make every later test that needs it skip (create_job_or_skip).
    """
    login = fms_client.post(
        "/api/v1/auth/login",
        json={"username": config.admin_username, "password": config.admin_password},
    )
    assert login.status_code == 200, f"Admin login for cleanup failed: {login.text}"
    headers = {"Authorization": f"Bearer {login.json()['session_token']}"}
    try:
        status = _current_status(fms_client, headers, job_id)
        if status == "active":
            cancel = fms_client.delete(f"/api/v1/jobs/{job_id}", headers=headers)
            assert cancel.status_code == 204, (
                f"Cancel of the active job {job_id} failed: {cancel.text}"
            )
        elif status == "running":
            modbus_stub.complete_transaction()
            deadline = time.monotonic() + timeout
            while status == "running" and time.monotonic() < deadline:
                time.sleep(0.2)
                status = _current_status(fms_client, headers, job_id)
        if status == "awaiting_bdn":
            fms_client.post(
                f"/api/v1/jobs/{job_id}/bdn",
                headers=headers,
                json={"bdn_number": f"BDN-{job_id[:8]}"},
            )
        remaining = _current_status(fms_client, headers, job_id)
        if remaining in _SLOT_HOLDING_STATUSES:
            pytest.fail(
                f"job {job_id} still holds the job slot ({remaining}) after cleanup"
            )
    finally:
        fms_client.delete("/api/v1/auth/logout", headers=headers)


# ---------------------------------------------------------------------------
# LDAP user provisioning (session-scoped)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def operator_account(config: Config, ldap_accounts: LDAPAccounts) -> LDAPAccount:
    account = LDAPAccount(
        username=config.operator_username,
        password=config.operator_password,
        uid_number=2001,
    )
    ldap_accounts.add_user(account)
    return account


# ---------------------------------------------------------------------------
# Authenticated sessions
# ---------------------------------------------------------------------------


@pytest.fixture
def admin_headers(config: Config, fms_client: httpx.Client) -> dict[str, str]:
    """Session token for the FMS local admin account (user/pass only, no RFID)."""
    response = fms_client.post(
        "/api/v1/auth/login",
        json={"username": config.admin_username, "password": config.admin_password},
    )
    assert response.status_code == 200, f"Admin login failed: {response.text}"
    token = response.json()["session_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def operator_headers(
    config: Config,
    fms_client: httpx.Client,
    operator_account: LDAPAccount,
    operator_card: str,
    rfid: RFIDPair,
) -> Generator[dict[str, str], None, None]:
    """
    Fresh operator session for each test.  The operator's RFID card must be
    enrolled in the FMS card table before this fixture is used; tests that
    exercise authentication do that enrolment themselves.
    """
    response = fms_client.post(
        "/api/v1/auth/login",
        json={
            "username": operator_account.username,
            "password": operator_account.password,
            "rfid_card_number": config.operator_rfid,
        },
    )
    assert response.status_code == 200, f"Operator login failed: {response.text}"
    token = response.json()["session_token"]
    yield {"Authorization": f"Bearer {token}"}
    fms_client.delete(
        "/api/v1/auth/logout", headers={"Authorization": f"Bearer {token}"}
    )


# ---------------------------------------------------------------------------
# RFID card enrolment
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def operator_card(
    config: Config, fms_client: httpx.Client
) -> Generator[str, None, None]:
    """Enrols the operator RFID card for the entire test session.

    Session-scoped so that all tests using operator_headers share a single
    enrolment rather than depending on accidental ordering side-effects.

    **It is not revoked afterwards.** This card number is one of the two the
    deployment seeds at FMS startup (`FMS_SEED_CARDS`), and the simulator's dock
    operator authenticates with it. Revoking it on teardown — which this fixture
    used to do — left the deployed testbed unable to authenticate its own
    operator until the FMS was restarted or the card re-enrolled by hand, and
    crashed the simulator. A suite run against a live deployment must leave it
    usable.

    The enrolment itself is kept rather than relying on the seeding, so the suite
    still runs against a database where seeding has not happened. It is labelled
    with the operator's username, as the seeding does: login requires the card
    to be enrolled to the user signing in (fms.md §4), and this row becomes the
    card's most recent one.
    """
    login = fms_client.post(
        "/api/v1/auth/login",
        json={"username": config.admin_username, "password": config.admin_password},
    )
    assert login.status_code == 200, (
        f"Admin login for card enrolment failed: {login.text}"
    )
    headers = {"Authorization": f"Bearer {login.json()['session_token']}"}
    enrol = fms_client.post(
        "/api/v1/cards",
        headers=headers,
        json={"card_number": config.operator_rfid, "label": config.operator_username},
    )
    assert enrol.status_code == 201, f"Operator card enrolment failed: {enrol.text}"
    card_id: str = enrol.json()["card_id"]
    yield card_id


@pytest.fixture
def enrolled_vessel_card(
    config: Config, fms_client: httpx.Client, admin_headers: dict[str, str]
) -> Generator[str, None, None]:
    """Enrols the vessel RFID card.

    Not revoked afterwards, for the reason given on `operator_card`: this number
    is seeded by the deployment and the simulator's vessel actor presents it.
    For the same reason it carries the deployment's label for the card, the
    vessel name in world.yaml, since this row becomes the card's most recent one.
    A dispense job must name this vessel for the card to pass its vessel RFID
    gate (fms.md §5.3).
    """
    response = fms_client.post(
        "/api/v1/cards",
        headers=admin_headers,
        json={
            "card_number": config.vessel_rfid,
            "label": config.vessel_imo_number,
        },
    )
    assert response.status_code == 201, f"Card enrolment failed: {response.text}"
    card_id: str = response.json()["card_id"]
    yield card_id


# ---------------------------------------------------------------------------
# FMS readiness gate (session-scoped, autouse)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session", autouse=True)
def fms_ready(atg_stub: ATGStub, log_server_stub: LogServerStub) -> None:
    """Block until the FMS has pushed at least one ATG tank-volume sample.

    Listing atg_stub as a dependency ensures the ATG stub is listening before
    the wait begins; without it the FMS ATG poller has nothing to connect to
    and emits nothing. The readiness signal is a metric rather than an event
    because hist.md §4.1 makes ATG readings metrics: they now reach vmagent,
    not vlagent. The 60-second timeout covers two full log-pusher backoff
    cycles at the 30 s cap (fms.md §5.4).
    """
    if not log_server_stub.metrics.wait_for_metric(
        "atg_tank_volume_litres", count=1, timeout=60
    ):
        pytest.fail(
            "FMS did not push an atg_tank_volume_litres sample within 60 s — "
            "run 'make restart-fms' or 'make setup' to restart the service"
        )


# ---------------------------------------------------------------------------
# Stub reset between tests (opt-in: tests request fresh_stubs explicitly)
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh_stubs(
    atg_stub: ATGStub, modbus_stub: ModbusStub, log_server_stub: LogServerStub
) -> tuple[ATGStub, ModbusStub, LogServerStub]:
    """Reset all stub state.  Request this fixture in any test that uses stubs."""
    atg_stub.set_state(volume_L=25_000.0, temperature_C=15.0, bund_alarm=False)
    atg_stub.clear_requests()
    modbus_stub.reset()
    log_server_stub.clear()
    return atg_stub, modbus_stub, log_server_stub
