"""
Group 10 — authentication and card-table audit events (`hist.md` §4.3).

Path 3 of `hist.md` §5: the FMS emits RFC 5424 syslog messages to vlagent's
listener on every login attempt (`fms.md` §4) and every card-table mutation
(`fms.md` §7.5). These are the only record a failed login leaves anywhere —
nothing is written to `sessions`, and the HTTP response is a generic `401`.

Every failure case asserts its `reason`. The four rejection paths in `auth.rs`
return an identical `401`, so a test asserting only that *a* `LOGIN_FAILED`
arrived would pass whichever path ran, and would still pass if three of the four
stopped emitting. The `reason` is the only thing that distinguishes them.
"""

import httpx
import pytest

from tests.integration.fms.conftest import Config
from tests.integration.fms.stubs.log_server import SD_ID, LogServerStub
from tests.integration.fms.stubs.postgres import PostgresClient

pytestmark = pytest.mark.full

_TIMEOUT = 10.0

# hist.md §4.3: facility user(1), and the severities the audit filters on.
_FACILITY_USER = 1
_SEVERITY_INFORMATIONAL = 6
_SEVERITY_WARNING = 4
_SEVERITY_NOTICE = 5


def _login(client: httpx.Client, **body: object) -> httpx.Response:
    return client.post("/api/v1/auth/login", json=body)


def _last(stub: LogServerStub, msgid: str, **match: str):
    """Wait for a message with this MSGID and these structured-data values.

    Matching on the fields rather than waiting for a count: delivery is
    asynchronous, so clearing the store and waiting for one message does not
    guarantee the message that arrives is the one the test provoked — an
    earlier event still in flight can land after the clear and satisfy the
    wait. Waiting on a count has failed this way before, reading an `enrol` where
    the revocation case expected a `revoke`.
    """

    def matches(m: object) -> bool:
        return m.msgid == msgid and all(  # type: ignore[attr-defined]
            m.sd.get(k) == v
            for k, v in match.items()  # type: ignore[attr-defined]
        )

    assert stub.syslog.wait_for_count(matches, 1, _TIMEOUT), (
        f"no {msgid} message matching {match} reached the syslog stub within "
        f"{_TIMEOUT} s — check FMS_VLAGENT_SYSLOG_HOST/PORT on fms-01"
    )
    return stub.syslog.all(matches)[-1]


# ---------------------------------------------------------------------------
# Successful login
# ---------------------------------------------------------------------------


def test_successful_login_emits_login(
    config: Config,
    fms_client: httpx.Client,
    operator_card: str,
    log_server_stub: LogServerStub,
    fresh_stubs: object,
) -> None:
    resp = _login(
        fms_client,
        username=config.operator_username,
        password=config.operator_password,
        rfid_card_number=config.operator_rfid,
    )
    assert resp.status_code == 200

    message = _last(log_server_stub, "LOGIN")
    assert message.sd["username"] == config.operator_username
    assert message.sd["role"] == "operator"
    assert message.sd["result"] == "success"
    assert message.sd["session_id"], "session_id must identify the session created"
    assert message.sd["source_address"], "source_address must identify the caller"


def test_login_envelope_matches_the_specification(
    config: Config,
    fms_client: httpx.Client,
    operator_card: str,
    log_server_stub: LogServerStub,
    fresh_stubs: object,
) -> None:
    """The envelope fields hist.md §4.3 pins, which govern how a query addresses
    these records: VictoriaLogs prefixes structured data with the SD-ID, so a
    dashboard reads `fms@99999.username`, and it derives severity and facility
    from PRI."""
    _login(
        fms_client,
        username=config.operator_username,
        password=config.operator_password,
        rfid_card_number=config.operator_rfid,
    )
    message = _last(log_server_stub, "LOGIN")

    assert message.facility == _FACILITY_USER
    assert message.severity == _SEVERITY_INFORMATIONAL
    assert message.app_name == "fms"
    assert message.hostname, "HOSTNAME must identify the emitting host"
    assert f"[{SD_ID} " in message.raw, f"structured-data id missing: {message.raw}"

    # Without a MSG, VictoriaLogs substitutes placeholder text of its own, and
    # that is then what the audit trail reads as in Grafana (hist.md §4.3).
    assert message.msg.strip(), f"message carries no MSG: {message.raw}"


# ---------------------------------------------------------------------------
# Rejected logins — one case per exit in auth.rs, each pinned by its reason
# ---------------------------------------------------------------------------


def test_wrong_operator_password_reports_invalid_credentials(
    config: Config,
    fms_client: httpx.Client,
    operator_card: str,
    log_server_stub: LogServerStub,
    fresh_stubs: object,
) -> None:
    resp = _login(
        fms_client,
        username=config.operator_username,
        password="not-the-password",
        rfid_card_number=config.operator_rfid,
    )
    assert resp.status_code == 401

    message = _last(
        log_server_stub,
        "LOGIN_FAILED",
        reason="invalid_credentials",
        role_attempted="operator",
    )
    assert message.severity == _SEVERITY_WARNING
    assert message.sd["username"] == config.operator_username
    assert message.sd["role_attempted"] == "operator"
    assert message.sd["source_address"], "source_address must identify the caller"


def test_wrong_admin_password_reports_invalid_credentials(
    config: Config,
    fms_client: httpx.Client,
    log_server_stub: LogServerStub,
    fresh_stubs: object,
) -> None:
    resp = _login(
        fms_client, username=config.admin_username, password="not-the-password"
    )
    assert resp.status_code == 401

    # The admin path is the branch taken when the username matches the configured
    # admin, so role_attempted must follow the branch rather than default.
    message = _last(
        log_server_stub,
        "LOGIN_FAILED",
        reason="invalid_credentials",
        role_attempted="admin",
    )
    assert message.sd["username"] == config.admin_username


def test_login_without_a_card_number_reports_missing_rfid(
    config: Config,
    fms_client: httpx.Client,
    log_server_stub: LogServerStub,
    fresh_stubs: object,
) -> None:
    """An operator login omitting `rfid_card_number` is rejected before any card
    lookup, so it is neither invalid_credentials nor invalid_rfid (fms.md §4)."""
    resp = _login(
        fms_client,
        username=config.operator_username,
        password=config.operator_password,
    )
    assert resp.status_code == 401

    message = _last(log_server_stub, "LOGIN_FAILED", reason="missing_rfid")
    assert message.sd["username"] == config.operator_username


def test_unknown_card_number_reports_invalid_rfid(
    config: Config,
    fms_client: httpx.Client,
    log_server_stub: LogServerStub,
    fresh_stubs: object,
) -> None:
    resp = _login(
        fms_client,
        username=config.operator_username,
        password=config.operator_password,
        rfid_card_number=9_999_999,
    )
    assert resp.status_code == 401

    message = _last(log_server_stub, "LOGIN_FAILED", reason="invalid_rfid")
    assert message.sd["username"] == config.operator_username


def test_source_address_is_an_address_without_a_port(
    config: Config,
    fms_client: httpx.Client,
    log_server_stub: LogServerStub,
    fresh_stubs: object,
) -> None:
    """`source_address` distinguishes an operator mistyping a password from an
    attempt made from somewhere unexpected — the audit trail is the only place
    either is recorded (fms.md §4).

    The port is excluded deliberately: it identifies a connection rather than a
    client, so it would differ on every attempt and defeat grouping by source.
    """
    import ipaddress

    _login(fms_client, username=config.operator_username, password="wrong")
    message = _last(log_server_stub, "LOGIN_FAILED", reason="missing_rfid")

    source = message.sd["source_address"]
    # Parses as a bare address: a "host:port" string would raise here.
    ipaddress.ip_address(source)


def test_the_four_rejections_are_distinguishable(
    config: Config,
    fms_client: httpx.Client,
    operator_card: str,
    log_server_stub: LogServerStub,
    fresh_stubs: object,
) -> None:
    """All four exits return an identical 401, so the audit trail is the only
    place they differ. Run every one and assert the reasons are distinct."""
    _login(fms_client, username=config.admin_username, password="wrong")
    _login(
        fms_client,
        username=config.operator_username,
        password="wrong",
        rfid_card_number=config.operator_rfid,
    )
    _login(
        fms_client,
        username=config.operator_username,
        password=config.operator_password,
    )
    _login(
        fms_client,
        username=config.operator_username,
        password=config.operator_password,
        rfid_card_number=9_999_999,
    )

    assert log_server_stub.syslog.wait_for_msgid(
        "LOGIN_FAILED", count=4, timeout=_TIMEOUT
    ), "expected one LOGIN_FAILED per rejected attempt"

    reasons = {
        m.sd["reason"] for m in log_server_stub.syslog.with_msgid("LOGIN_FAILED")
    }
    assert reasons == {"invalid_credentials", "missing_rfid", "invalid_rfid"}, (
        f"the rejection paths are not distinguishable in the audit trail: {reasons}"
    )


def test_failed_login_writes_no_session_row(
    config: Config,
    fms_client: httpx.Client,
    log_server_stub: LogServerStub,
    postgres: PostgresClient,
    fresh_stubs: object,
) -> None:
    """`sessions` stays a record of successful sessions only (fms.md §4); the
    syslog event is a failed attempt's sole record."""

    def session_count() -> int:
        rows = postgres.execute("SELECT count(*) AS n FROM sessions")
        return int(rows[0]["n"])

    before = session_count()
    _login(fms_client, username=config.operator_username, password="wrong")
    assert log_server_stub.syslog.wait_for_msgid("LOGIN_FAILED", timeout=_TIMEOUT)
    assert session_count() == before, "a rejected login must not write a sessions row"


# ---------------------------------------------------------------------------
# Card-table changes
# ---------------------------------------------------------------------------


def test_card_enrolment_emits_card_change(
    fms_client: httpx.Client,
    admin_headers: dict[str, str],
    log_server_stub: LogServerStub,
    fresh_stubs: object,
) -> None:
    resp = fms_client.post(
        "/api/v1/cards",
        headers=admin_headers,
        json={"card_number": 1_000_501, "label": "Audit test card"},
    )
    assert resp.status_code == 201

    message = _last(log_server_stub, "CARD-CHANGE", card_id=resp.json()["card_id"])
    assert message.severity == _SEVERITY_NOTICE
    assert message.sd["action"] == "enrol"
    assert message.sd["card_number"] == "1000501"
    assert message.sd["label"] == "Audit test card"
    assert message.sd["card_id"] == resp.json()["card_id"]


def test_card_revocation_emits_card_change(
    fms_client: httpx.Client,
    admin_headers: dict[str, str],
    log_server_stub: LogServerStub,
    fresh_stubs: object,
) -> None:
    created = fms_client.post(
        "/api/v1/cards",
        headers=admin_headers,
        json={"card_number": 1_000_502, "label": "Audit revoke card"},
    )
    card_id = created.json()["card_id"]

    resp = fms_client.delete(f"/api/v1/cards/{card_id}", headers=admin_headers)
    assert resp.status_code == 204

    # Both this card's enrol and its revoke are in flight; match on the action.
    message = _last(log_server_stub, "CARD-CHANGE", card_id=card_id, action="revoke")
    assert message.sd["action"] == "revoke"
    assert message.sd["card_id"] == card_id
    # The revoke row carries the card's own number and label, not blanks: the
    # handler reads them back from the latest enrol row before inserting.
    assert message.sd["card_number"] == "1000502"
    assert message.sd["label"] == "Audit revoke card"
