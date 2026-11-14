"""
Group 7 — Fill transaction lifecycle.

Verifies the road-tanker-to-tank fill flow described in digital.md §4.3.
All tests are marked full.
"""

from __future__ import annotations

import time
from collections.abc import Generator
from typing import Any

import httpx
import pytest

from tests.integration.fms.conftest import Config, create_job_or_skip
from tests.integration.fms.stubs.atg import ATGStub
from tests.integration.fms.stubs.log_server import LogServerStub
from tests.integration.fms.stubs.postgres import PostgresClient

_COUNTERPARTY = "Coastal Fuels Ltd"
_GRADE = "MGO"
_EXPECTED_VOLUME_L = 8_000
_TICKET_NUMBER = "TKT-2026-0099"
_DELIVERED_VOLUME_L = 7_950
_SUPPLIER = "Coastal Fuels Ltd"
_TANKER_REGISTRATION = "WX12 ABC"
# The road tanker's own figures, transcribed from its ticket (fms.md §7).
_DELIVERY_COMMENCED_AT = "2026-01-31T23:58:30Z"
_TEMPERATURE_C = 12.0
_DENSITY_KG_PER_L = 0.845
_TEMP_CORRECTED_VOLUME_L = 7_973.0


# ---------------------------------------------------------------------------
# Fixture chain
# ---------------------------------------------------------------------------


@pytest.fixture
def active_fill_job(
    config: Config,
    fms_client: httpx.Client,
    operator_headers: dict[str, str],
    atg_stub: ATGStub,
    fresh_stubs: object,
) -> Generator[tuple[str, dict[str, str]], None, None]:
    """Creates an active fill job with the tank at 15 000 L (plenty of space)."""
    atg_stub.set_state(volume_L=15_000.0)
    time.sleep(2)  # allow FMS to poll the new level

    response = create_job_or_skip(
        fms_client,
        operator_headers,
        {
            "type": "fill",
            "counterparty": _COUNTERPARTY,
            "grade": _GRADE,
            "expected_volume": _EXPECTED_VOLUME_L,
        },
    )
    assert response.status_code == 201, f"Fill job creation failed: {response.text}"
    job_id = response.json()["job_id"]
    yield job_id, operator_headers
    cleanup = fms_client.post(
        "/api/v1/auth/login",
        json={"username": config.admin_username, "password": config.admin_password},
    )
    if cleanup.status_code == 200:
        h = {"Authorization": f"Bearer {cleanup.json()['session_token']}"}
        fms_client.delete(f"/api/v1/jobs/{job_id}", headers=h)
        fms_client.delete("/api/v1/auth/logout", headers=h)


@pytest.fixture
def closed_fill(
    active_fill_job: tuple[str, dict[str, str]],
    fms_client: httpx.Client,
    atg_stub: ATGStub,
    log_server_stub: LogServerStub,
) -> dict[str, Any]:
    """
    Simulates fill completion: ATG rises to reflect delivery, then the operator
    transcribes the delivery ticket via the API.
    """
    job_id, headers = active_fill_job

    # ATG now shows post-fill level.
    atg_stub.set_state(volume_L=15_000.0 + _DELIVERED_VOLUME_L)

    response = fms_client.post(
        f"/api/v1/jobs/{job_id}/close",
        headers=headers,
        json={
            "ticket_number": _TICKET_NUMBER,
            "delivery_commenced_at": _DELIVERY_COMMENCED_AT,
            "delivered_volume_l": _DELIVERED_VOLUME_L,
            "temperature_c": _TEMPERATURE_C,
            "density_kg_per_l": _DENSITY_KG_PER_L,
            "temp_corrected_volume_l": _TEMP_CORRECTED_VOLUME_L,
            "supplier": _SUPPLIER,
            "tanker_registration": _TANKER_REGISTRATION,
        },
    )
    assert response.status_code in (200, 201), f"Fill close failed: {response.text}"

    log_server_stub.events.wait_for_type("transaction_closed", count=1, timeout=5)
    return {"job_id": job_id, "headers": headers}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.full
def test_fill_job_records_atg_level_at_open(
    active_fill_job: tuple[str, dict[str, str]],
    fms_client: httpx.Client,
    log_server_stub: LogServerStub,
) -> None:
    """The job_opened event must include the ATG volume recorded at job open."""
    log_server_stub.events.wait_for_type("job_opened", count=1, timeout=5)
    events = log_server_stub.events.all(event_type="job_opened")
    assert events
    payload = events[-1].fields
    assert "atg_volume_at_open_l" in payload
    assert abs(payload["atg_volume_at_open_l"] - 15_000.0) < 100.0


@pytest.mark.full
def test_atg_monitored_continuously_during_fill(
    active_fill_job: tuple[str, dict[str, str]],
    log_server_stub: LogServerStub,
) -> None:
    """FMS must continue pushing ATG metrics while a fill job is active."""
    initial_count = len(log_server_stub.metrics.named("atg_tank_volume_litres"))
    time.sleep(3)  # allow several poll cycles
    later_count = len(log_server_stub.metrics.named("atg_tank_volume_litres"))
    assert later_count > initial_count, (
        "FMS stopped pushing ATG readings during an active fill job"
    )


@pytest.mark.full
def test_fill_close_transcription(
    closed_fill: dict[str, Any],
    fms_client: httpx.Client,
) -> None:
    """After close, the job status must be 'closed'."""
    headers = closed_fill["headers"]
    status = fms_client.get("/api/v1/jobs/current", headers=headers).json()
    assert status["status"] == "closed"


@pytest.mark.full
def test_fill_transaction_record_complete(
    closed_fill: dict[str, Any],
    log_server_stub: LogServerStub,
) -> None:
    """
    The transaction_closed event must contain all fields required by
    digital.md §4.3 and §6.1.
    """
    required_fields = {
        "job_id",
        "operator",
        "ticket_number",
        "delivered_volume_l",
        "supplier",
        "tanker_registration",
        "atg_volume_at_open_l",
        "atg_volume_at_close_l",
        "opened_at",
        "closed_at",
    }
    events = log_server_stub.events.all(event_type="transaction_closed")
    assert events, "No transaction_closed event in log-server stub"
    payload = events[-1].fields
    missing = required_fields - payload.keys()
    assert not missing, f"Fill transaction_closed missing fields: {missing}"


@pytest.mark.full
def test_fill_transcribed_values_match_input(
    closed_fill: dict[str, Any],
    log_server_stub: LogServerStub,
) -> None:
    events = log_server_stub.events.all(event_type="transaction_closed")
    payload = events[-1].fields
    assert payload["ticket_number"] == _TICKET_NUMBER
    assert payload["delivered_volume_l"] == _DELIVERED_VOLUME_L
    assert payload["supplier"] == _SUPPLIER
    assert payload["tanker_registration"] == _TANKER_REGISTRATION


@pytest.mark.full
def test_fill_transaction_persisted_in_postgres(
    closed_fill: dict[str, Any],
    fms_client: httpx.Client,
    postgres: PostgresClient,
) -> None:
    job_id = closed_fill["job_id"]
    rows = postgres.execute(
        "SELECT ticket_number, delivered_volume_l FROM transactions WHERE job_id = %s",
        (job_id,),
    )
    assert rows, "No transaction row in Postgres for fill job"
    assert rows[0]["ticket_number"] == _TICKET_NUMBER
    assert rows[0]["delivered_volume_l"] == _DELIVERED_VOLUME_L
