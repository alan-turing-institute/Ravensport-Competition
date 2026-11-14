"""
Group 2 — Authentication and session handling.

Verifies the two-factor operator login (LDAP password + FMS card table RFID)
and session lifecycle, per digital.md §5.2.

All tests (except timeout) are marked fast: they require the LDAP stub &
the FMS HTTPS endpoint but not the boundary-protocol stubs (ATG, Modbus).
"""

import time

import httpx
import pytest

from tests.integration.fms.conftest import Config
from tests.integration.fms.stubs.ldap import LDAPAccount
from tests.integration.fms.stubs.postgres import PostgresClient


# ---------------------------------------------------------------------------
# Admin login (local FMS account, user/pass only)
# ---------------------------------------------------------------------------


@pytest.mark.fast
def test_admin_login(config: Config, fms_client: httpx.Client) -> None:
    response = fms_client.post(
        "/api/v1/auth/login",
        json={"username": config.admin_username, "password": config.admin_password},
    )
    assert response.status_code == 200
    body = response.json()
    assert "session_token" in body
    assert body.get("role") == "admin"


@pytest.mark.fast
def test_admin_login_wrong_password(config: Config, fms_client: httpx.Client) -> None:
    response = fms_client.post(
        "/api/v1/auth/login",
        json={"username": config.admin_username, "password": "wrong-password"},
    )
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# Operator login (LDAP + RFID, per digital.md §5.2)
# ---------------------------------------------------------------------------


@pytest.mark.fast
def test_operator_login_valid(
    config: Config,
    fms_client: httpx.Client,
    operator_account: LDAPAccount,
    operator_card: str,
) -> None:
    """Full two-factor login: LDAP password + enrolled RFID card."""
    response = fms_client.post(
        "/api/v1/auth/login",
        json={
            "username": operator_account.username,
            "password": operator_account.password,
            "rfid_card_number": config.operator_rfid,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert "session_token" in body
    assert body.get("role") == "operator"


@pytest.mark.fast
def test_operator_login_wrong_password(
    config: Config,
    fms_client: httpx.Client,
    operator_account: LDAPAccount,
    operator_card: str,
) -> None:
    response = fms_client.post(
        "/api/v1/auth/login",
        json={
            "username": operator_account.username,
            "password": "wrong-password",
            "rfid_card_number": config.operator_rfid,
        },
    )
    assert response.status_code == 401


@pytest.mark.fast
def test_operator_login_unregistered_rfid(
    config: Config,
    fms_client: httpx.Client,
    operator_account: LDAPAccount,
) -> None:
    """Correct LDAP credentials but RFID card not in the FMS card table."""
    response = fms_client.post(
        "/api/v1/auth/login",
        json={
            "username": operator_account.username,
            "password": operator_account.password,
            "rfid_card_number": 9999999,  # not enrolled
        },
    )
    assert response.status_code == 401


@pytest.mark.fast
def test_operator_login_with_vessel_card(
    config: Config,
    fms_client: httpx.Client,
    operator_account: LDAPAccount,
    enrolled_vessel_card: str,
) -> None:
    """Correct LDAP credentials but the card presented is an active vessel tag.

    The card is enrolled and active, but not to this operator (fms.md §4).
    """
    response = fms_client.post(
        "/api/v1/auth/login",
        json={
            "username": operator_account.username,
            "password": operator_account.password,
            "rfid_card_number": config.vessel_rfid,
        },
    )
    assert response.status_code == 401


@pytest.mark.fast
def test_operator_login_with_another_operators_card(
    fms_client: httpx.Client,
    operator_account: LDAPAccount,
    admin_headers: dict[str, str],
) -> None:
    """Correct LDAP credentials but the card is enrolled to a different user."""
    card_number = 1_000_601
    enrol = fms_client.post(
        "/api/v1/cards",
        headers=admin_headers,
        json={"card_number": card_number, "label": "another-operator"},
    )
    assert enrol.status_code == 201, f"Card enrolment failed: {enrol.text}"
    try:
        response = fms_client.post(
            "/api/v1/auth/login",
            json={
                "username": operator_account.username,
                "password": operator_account.password,
                "rfid_card_number": card_number,
            },
        )
        assert response.status_code == 401
    finally:
        fms_client.delete(
            f"/api/v1/cards/{enrol.json()['card_id']}", headers=admin_headers
        )


@pytest.mark.fast
def test_operator_login_no_rfid(
    config: Config,
    fms_client: httpx.Client,
    operator_account: LDAPAccount,
) -> None:
    """Correct LDAP credentials but rfid_card_number absent from request body."""
    response = fms_client.post(
        "/api/v1/auth/login",
        json={
            "username": operator_account.username,
            "password": operator_account.password,
        },
    )
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# Session logging and lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.fast
def test_login_event_logged_to_postgres(
    config: Config,
    fms_client: httpx.Client,
    operator_account: LDAPAccount,
    operator_card: str,
    postgres: PostgresClient,
) -> None:
    """Successful login must produce a row in the sessions table."""
    fms_client.post(
        "/api/v1/auth/login",
        json={
            "username": operator_account.username,
            "password": operator_account.password,
            "rfid_card_number": config.operator_rfid,
        },
    )
    rows = postgres.execute(
        "SELECT username, logged_in_at FROM sessions WHERE username = %s ORDER BY logged_in_at DESC LIMIT 1",
        (operator_account.username,),
    )
    assert rows, "No session row found for operator after login"
    assert rows[0]["username"] == operator_account.username


@pytest.mark.fast
def test_session_token_rejected_after_logout(
    config: Config,
    fms_client: httpx.Client,
    operator_account: LDAPAccount,
    operator_card: str,
) -> None:
    login = fms_client.post(
        "/api/v1/auth/login",
        json={
            "username": operator_account.username,
            "password": operator_account.password,
            "rfid_card_number": config.operator_rfid,
        },
    )
    token = login.json()["session_token"]
    headers = {"Authorization": f"Bearer {token}"}

    fms_client.delete("/api/v1/auth/logout", headers=headers)

    probe = fms_client.get("/api/v1/jobs", headers=headers)
    assert probe.status_code == 401


@pytest.mark.full
def test_session_timeout(
    config: Config,
    fms_client: httpx.Client,
    operator_account: LDAPAccount,
    operator_card: str,
) -> None:
    """
    The FMS session timeout is configured to 5 s in the test VM image
    (FMS_SESSION_TIMEOUT=5) to make this test feasible without a 30-minute wait.
    The session expires 5 s after login regardless of activity.
    """
    login = fms_client.post(
        "/api/v1/auth/login",
        json={
            "username": operator_account.username,
            "password": operator_account.password,
            "rfid_card_number": config.operator_rfid,
        },
    )
    assert login.status_code == 200
    token = login.json()["session_token"]
    headers = {"Authorization": f"Bearer {token}"}

    time.sleep(7)  # exceed 5 s session timeout

    probe = fms_client.get("/api/v1/jobs", headers=headers)
    assert probe.status_code == 401
