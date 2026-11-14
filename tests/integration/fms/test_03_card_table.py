"""
Group 3 — Card table management.

Verifies RFID card enrolment, revocation, and audit logging per digital.md
§5.3.  All tests are marked fast.
"""

import httpx
import pytest

from tests.integration.fms.conftest import Config, create_job_or_skip, free_job_slot
from tests.integration.fms.stubs.modbus import ModbusStub
from tests.integration.fms.stubs.postgres import PostgresClient

_LABEL = "Test vessel — MV Harrier"
_TEST_CARD = 200001  # 26-bit value not used elsewhere in the suite


@pytest.mark.fast
def test_enrol_card(fms_client: httpx.Client, admin_headers: dict[str, str]) -> None:
    response = fms_client.post(
        "/api/v1/cards",
        headers=admin_headers,
        json={"card_number": _TEST_CARD, "label": _LABEL},
    )
    assert response.status_code == 201
    body = response.json()
    assert "card_id" in body

    # Cleanup
    fms_client.delete(f"/api/v1/cards/{body['card_id']}", headers=admin_headers)


@pytest.mark.fast
def test_enrolment_appears_in_card_list(
    fms_client: httpx.Client, admin_headers: dict[str, str]
) -> None:
    enrol = fms_client.post(
        "/api/v1/cards",
        headers=admin_headers,
        json={"card_number": _TEST_CARD, "label": _LABEL},
    )
    card_id = enrol.json()["card_id"]

    listing = fms_client.get("/api/v1/cards", headers=admin_headers)
    assert listing.status_code == 200
    cards = listing.json()
    assert any(c["card_id"] == card_id for c in cards)

    fms_client.delete(f"/api/v1/cards/{card_id}", headers=admin_headers)


@pytest.mark.fast
def test_enrolment_logged_in_postgres(
    fms_client: httpx.Client,
    admin_headers: dict[str, str],
    postgres: PostgresClient,
) -> None:
    """Card enrolment must produce an append-only row in card_table."""
    enrol = fms_client.post(
        "/api/v1/cards",
        headers=admin_headers,
        json={"card_number": _TEST_CARD, "label": _LABEL},
    )
    card_id = enrol.json()["card_id"]

    rows = postgres.execute(
        "SELECT card_number, label, active, enrolled_at FROM card_table WHERE card_id = %s",
        (card_id,),
    )
    assert rows, "No card_table row found after enrolment"
    row = rows[0]
    assert row["card_number"] == _TEST_CARD
    assert row["label"] == _LABEL
    assert row["active"] is True
    assert row["enrolled_at"] is not None

    fms_client.delete(f"/api/v1/cards/{card_id}", headers=admin_headers)


@pytest.mark.fast
def test_revoke_card(fms_client: httpx.Client, admin_headers: dict[str, str]) -> None:
    enrol = fms_client.post(
        "/api/v1/cards",
        headers=admin_headers,
        json={"card_number": _TEST_CARD, "label": _LABEL},
    )
    card_id = enrol.json()["card_id"]

    response = fms_client.delete(f"/api/v1/cards/{card_id}", headers=admin_headers)
    assert response.status_code in (200, 204)


@pytest.mark.fast
def test_revoked_card_is_inactive_in_postgres(
    fms_client: httpx.Client,
    admin_headers: dict[str, str],
    postgres: PostgresClient,
) -> None:
    """Revocation must write a new row with active=false, not modify the original."""
    enrol = fms_client.post(
        "/api/v1/cards",
        headers=admin_headers,
        json={"card_number": _TEST_CARD, "label": _LABEL},
    )
    card_id = enrol.json()["card_id"]
    fms_client.delete(f"/api/v1/cards/{card_id}", headers=admin_headers)

    # There must be at least one row for this card_id and the latest must be inactive.
    rows = postgres.execute(
        "SELECT active FROM card_table WHERE card_id = %s ORDER BY enrolled_at DESC",
        (card_id,),
    )
    assert rows, "No card_table rows found after revocation"
    assert rows[0]["active"] is False


@pytest.mark.fast
def test_revocation_produces_new_row_not_update(
    fms_client: httpx.Client,
    admin_headers: dict[str, str],
    postgres: PostgresClient,
) -> None:
    """
    The append-only requirement means revocation is recorded as a new row
    (action='revoke') referencing the original card_id, not an in-place
    update of the enrolment row.
    """
    enrol = fms_client.post(
        "/api/v1/cards",
        headers=admin_headers,
        json={"card_number": _TEST_CARD, "label": _LABEL},
    )
    card_id = enrol.json()["card_id"]
    fms_client.delete(f"/api/v1/cards/{card_id}", headers=admin_headers)

    rows = postgres.execute(
        "SELECT action FROM card_table WHERE card_id = %s ORDER BY enrolled_at",
        (card_id,),
    )
    actions = [r["action"] for r in rows]
    assert "enrol" in actions
    assert "revoke" in actions
    # Must be two distinct rows, not one mutated row.
    assert len(rows) >= 2, "Expected separate enrol and revoke rows"


@pytest.mark.full
def test_revoked_card_rejected_at_rfid_gate(
    config: Config,
    fms_client: httpx.Client,
    admin_headers: dict[str, str],
    operator_headers: dict[str, str],
    modbus_stub: ModbusStub,
    fresh_stubs: object,
) -> None:
    """
    A vessel RFID card that has been revoked must not satisfy the RFID gate
    during a dispense job.  The FMS must return an authorisation failure.
    """
    # Enrol then immediately revoke the vessel card.
    enrol = fms_client.post(
        "/api/v1/cards",
        headers=admin_headers,
        json={"card_number": config.vessel_rfid, "label": "Test vessel"},
    )
    card_id = enrol.json()["card_id"]
    fms_client.delete(f"/api/v1/cards/{card_id}", headers=admin_headers)

    # Free the job slot of any job left over from a previous failed run. Only an
    # active job can be cancelled (fms.md §7.4), so free_job_slot completes and
    # closes one that has progressed further.
    existing = fms_client.get("/api/v1/jobs", headers=operator_headers)
    for j in existing.json() if existing.status_code == 200 else []:
        free_job_slot(config, fms_client, j["job_id"], modbus_stub)

    # Create a dispense job.
    job_resp = create_job_or_skip(
        fms_client,
        operator_headers,
        {
            "type": "dispense",
            "counterparty": config.vessel_imo_number,
            "grade": "MGO",
            "preset_volume": 100,
        },
    )
    assert job_resp.status_code == 201
    job_id = job_resp.json()["job_id"]

    try:
        # Attempt to validate the revoked card via the API (simulating the serial-port read).
        validate = fms_client.post(
            f"/api/v1/jobs/{job_id}/rfid",
            headers=operator_headers,
            json={"card_number": config.vessel_rfid},
        )
        assert validate.status_code == 403
    finally:
        fms_client.delete(f"/api/v1/jobs/{job_id}", headers=operator_headers)
        # Restore the card. Unlike the fixtures, this test must revoke to make
        # its assertion, so it re-enrols instead of not revoking: the number is
        # one the deployment seeds (FMS_SEED_CARDS) and the simulator's vessel
        # actor presents it, so leaving it revoked breaks the deployed testbed
        # for whatever runs next.
        fms_client.post(
            "/api/v1/cards",
            headers=admin_headers,
            json={"card_number": config.vessel_rfid, "label": config.vessel_imo_number},
        )
