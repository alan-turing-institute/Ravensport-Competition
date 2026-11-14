"""
Group 6 — Dispense transaction lifecycle.

Verifies the full tank-to-tug dispense flow described in digital.md §4.2.
Each test builds on a shared fixture chain that advances the dispense to the
required state before asserting.

All tests are marked full.

Fixture chain:
  active_dispense_job  →  rfid_validated_job  →  running_dispense  →  completed_dispense

The fixture chain re-runs for each test (function scope), so each test starts
from a clean FMS state.  The Modbus poll interval is 1 s on the test image
(FMS_MODBUS_POLL_INTERVAL=1), so fixture setup is fast.
"""

from __future__ import annotations

import time
from collections.abc import Generator
from typing import Any

import httpx
import pytest

from tests.integration.fms.conftest import Config, create_job_or_skip, free_job_slot
from tests.integration.fms.stubs.atg import ATGStub
from tests.integration.fms.stubs.log_server import LogServerStub
from tests.integration.fms.stubs.modbus import (
    CUMULATIVE_REG,
    ENABLE_REG,
    K_FACTOR,
    PRESET_REG,
    TRANSACTION_REG,
    ModbusStub,
)
from tests.integration.fms.stubs.postgres import PostgresClient
from tests.integration.fms.stubs.rfid import RFIDPair

# Chosen above the old 16-bit register ceiling (655.35 L at K=100): a 1000 L
# dispense is 100 000 pulses, so the transaction and cumulative totalisers must
# be read as 32-bit register pairs.  A 16-bit-only client would wrap and fail.
_PRESET_VOLUME_L = 1000  # litres
_GRADE = "MGO"

# How long to wait for the FMS to react to a signal (seconds).
_REACTION_TIMEOUT = 5


# ---------------------------------------------------------------------------
# Fixture chain
# ---------------------------------------------------------------------------


@pytest.fixture
def active_dispense_job(
    config: Config,
    fms_client: httpx.Client,
    operator_headers: dict[str, str],
    admin_headers: dict[str, str],
    atg_stub: ATGStub,
    modbus_stub: ModbusStub,
    fresh_stubs: object,
    enrolled_vessel_card: str,  # ensures vessel RFID is in card table
) -> Generator[tuple[str, dict[str, str]], None, None]:
    """
    Creates an active dispense job.
    Yields (job_id, operator_headers). Afterwards the job slot is freed
    whatever state the test left the dispense in (free_job_slot).
    """
    atg_stub.set_state(volume_L=25_000.0)  # well above cavitation threshold
    response = create_job_or_skip(
        fms_client,
        operator_headers,
        {
            "type": "dispense",
            "counterparty": config.vessel_imo_number,
            "grade": _GRADE,
            "preset_volume": _PRESET_VOLUME_L,
        },
    )
    assert response.status_code == 201, f"Job creation failed: {response.text}"
    job_id = response.json()["job_id"]
    yield job_id, operator_headers
    free_job_slot(config, fms_client, job_id, modbus_stub)


@pytest.fixture
def rfid_validated_job(
    config: Config,
    active_dispense_job: tuple[str, dict[str, str]],
    rfid: RFIDPair,
    fms_client: httpx.Client,
) -> tuple[str, dict[str, str]]:
    """
    Injects the vessel RFID card and waits for the FMS to validate it.
    Yields (job_id, operator_headers).
    """
    job_id, headers = active_dispense_job
    rfid.vessel.inject(config.vessel_rfid)

    deadline = time.monotonic() + _REACTION_TIMEOUT
    while time.monotonic() < deadline:
        # There is only ever one current job (narrative §273); the per-id GET
        # route no longer exists, so read /jobs/current.
        status = fms_client.get("/api/v1/jobs/current", headers=headers).json()
        if status.get("rfid_validated"):
            break
        time.sleep(0.2)
    else:
        pytest.fail("FMS did not validate vessel RFID within timeout")

    return job_id, headers


@pytest.fixture
def running_dispense(
    rfid_validated_job: tuple[str, dict[str, str]],
    fms_client: httpx.Client,
    modbus_stub: ModbusStub,
) -> dict[str, Any]:
    """
    Starts the dispense (operator action via HMI API) and waits for the FMS
    to write preset and enable to the Modbus stub.
    Yields {"job_id": str, "preset_volume": int}.
    """
    job_id, headers = rfid_validated_job
    response = fms_client.post(
        f"/api/v1/jobs/{job_id}/start",
        headers=headers,
        json={"preset_volume": _PRESET_VOLUME_L},
    )
    assert response.status_code == 200, f"Start dispense failed: {response.text}"

    # Wait for the FMS to write enable=1 to the Modbus stub.
    # wait_for_write fires on the first write (preset); polling for enable=1
    # explicitly avoids returning before the second write has been recorded.
    deadline = time.monotonic() + _REACTION_TIMEOUT
    while time.monotonic() < deadline:
        if any(w.value == 1 for w in modbus_stub.writes_to(ENABLE_REG)):
            break
        time.sleep(0.1)
    else:
        pytest.fail("FMS did not write enable=1 to Modbus within timeout")

    return {"job_id": job_id, "headers": headers, "preset_volume": _PRESET_VOLUME_L}


@pytest.fixture
def completed_dispense(
    running_dispense: dict[str, Any],
    fms_client: httpx.Client,
    modbus_stub: ModbusStub,
    log_server_stub: LogServerStub,
) -> dict[str, Any]:
    """
    Advances the Modbus totaliser to the preset volume, triggering auto-stop.
    Waits for the FMS to detect the stop and close the transaction.
    Yields {"job_id": str, "headers": ..., "preset_volume": int}.
    """
    job_id = running_dispense["job_id"]
    headers = running_dispense["headers"]
    preset = running_dispense["preset_volume"]

    # Jump the transaction totaliser directly to the preset volume.
    modbus_stub.advance_totaliser(float(preset))

    # Wait for a transaction_closed event in the log-server stub.
    received = log_server_stub.events.wait_for_type(
        "transaction_closed", count=1, timeout=_REACTION_TIMEOUT
    )
    assert received, "FMS did not close the transaction after totaliser reached preset"

    return {"job_id": job_id, "headers": headers, "preset_volume": preset}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.full
def test_rfid_gate_correct_card(
    rfid_validated_job: tuple[str, dict[str, str]], fms_client: httpx.Client
) -> None:
    """Injecting the enrolled vessel card transitions the job to rfid_validated."""
    job_id, headers = rfid_validated_job
    status = fms_client.get("/api/v1/jobs/current", headers=headers).json()
    assert status["rfid_validated"] is True


@pytest.mark.full
def test_rfid_gate_wrong_card(
    config: Config,
    active_dispense_job: tuple[str, dict[str, str]],
    rfid: RFIDPair,
    fms_client: httpx.Client,
) -> None:
    """Injecting a card not enrolled for the active job must not validate the gate."""
    job_id, headers = active_dispense_job
    rfid.vessel.inject(999999)  # unknown card
    time.sleep(1)
    status = fms_client.get("/api/v1/jobs/current", headers=headers).json()
    assert status["rfid_validated"] is False


@pytest.mark.full
def test_rfid_gate_operator_card(
    config: Config,
    active_dispense_job: tuple[str, dict[str, str]],
    operator_card: str,
    rfid: RFIDPair,
    fms_client: httpx.Client,
) -> None:
    """An active operator's card presented at the vessel reader must not
    validate the gate: it is enrolled to the operator, not the job's vessel."""
    job_id, headers = active_dispense_job
    rfid.vessel.inject(config.operator_rfid)
    time.sleep(1)
    status = fms_client.get("/api/v1/jobs/current", headers=headers).json()
    assert status["rfid_validated"] is False


@pytest.mark.full
def test_rfid_gate_another_vessels_card(
    active_dispense_job: tuple[str, dict[str, str]],
    admin_headers: dict[str, str],
    rfid: RFIDPair,
    fms_client: httpx.Client,
) -> None:
    """An active card enrolled to a vessel other than the one the job names
    must not validate the gate."""
    job_id, headers = active_dispense_job
    card_number = 1_000_602
    enrol = fms_client.post(
        "/api/v1/cards",
        headers=admin_headers,
        json={"card_number": card_number, "label": "Tug Bravo"},
    )
    assert enrol.status_code == 201, f"Card enrolment failed: {enrol.text}"
    try:
        rfid.vessel.inject(card_number)
        time.sleep(1)
        status = fms_client.get("/api/v1/jobs/current", headers=headers).json()
        assert status["rfid_validated"] is False
    finally:
        fms_client.delete(
            f"/api/v1/cards/{enrol.json()['card_id']}", headers=admin_headers
        )


@pytest.mark.full
def test_preset_written_to_modbus(
    running_dispense: dict[str, Any], modbus_stub: ModbusStub
) -> None:
    """FMS must write the correct preset value to Modbus register 0x0001."""
    preset_writes = modbus_stub.writes_to(PRESET_REG)
    assert preset_writes, "FMS never wrote to preset register (0x0001)"
    # Preset register encodes litres × 10 (digital.md §3).
    expected_raw = _PRESET_VOLUME_L * 10
    assert preset_writes[-1].value == expected_raw, (
        f"Preset register value {preset_writes[-1].value} != expected {expected_raw}"
    )


@pytest.mark.full
def test_pump_enabled_after_rfid_validation(
    running_dispense: dict[str, Any], modbus_stub: ModbusStub
) -> None:
    """FMS must write enable=1 to Modbus register 0x0002 to start the pump."""
    enable_writes = modbus_stub.writes_to(ENABLE_REG)
    assert any(w.value == 1 for w in enable_writes), (
        "FMS never wrote enable=1 to register 0x0002"
    )


@pytest.mark.full
def test_atg_level_recorded_at_transaction_open(
    running_dispense: dict[str, Any],
    fms_client: httpx.Client,
    log_server_stub: LogServerStub,
    atg_stub: ATGStub,
) -> None:
    """Transaction open event must include ATG volume at the time of the preset write."""
    events = log_server_stub.events.all(event_type="transaction_open")
    assert events, "No transaction_open event pushed to log-server stub"
    payload = events[-1].fields
    assert "atg_volume_at_open_l" in payload
    assert payload["atg_volume_at_open_l"] > 0


@pytest.mark.full
def test_totaliser_polled_during_dispense(
    running_dispense: dict[str, Any], modbus_stub: ModbusStub
) -> None:
    """FMS must read the transaction totaliser pair (0x0005–0x0006) during dispense."""
    # The stub records every read range, so we can assert the FMS actually polled
    # the transaction totaliser — and at the correct address.  Reading the wrong
    # register (e.g. 0x0004) would leave 0x0005 uncovered and fail here.
    deadline = time.monotonic() + _REACTION_TIMEOUT
    while time.monotonic() < deadline:
        if modbus_stub.did_read(TRANSACTION_REG):
            break
        time.sleep(0.1)
    else:
        pytest.fail("FMS never read the transaction totaliser pair (0x0005–0x0006)")

    # The FMS reads both registers of the pair together (32-bit value).
    pair_reads = [r for r in modbus_stub.received_reads if r.covers(TRANSACTION_REG)]
    assert any(r.count >= 2 for r in pair_reads), (
        "FMS read the transaction totaliser as a single register, not a 32-bit pair"
    )


@pytest.mark.full
def test_dispense_not_closed_before_pump_asserts_running(
    rfid_validated_job: tuple[str, dict[str, str]],
    fms_client: httpx.Client,
    modbus_stub: ModbusStub,
    log_server_stub: LogServerStub,
) -> None:
    """
    Regression for the spin-up gap (dispenser-modbus-tcp-spec.md §7): a real
    dispenser does not assert the Running bit the instant the pump is enabled.
    The FMS must not read the still-clear Running bit on the first poll as
    completion — doing so disables the pump and records a zero-volume
    transaction. It must wait until Running has been observed set before
    treating a cleared bit as auto-stop.

    The synchronous default stub hides this (it sets Running on the enable
    write), so this test configures a spin-up delay to open the gap.
    """
    job_id, headers = rfid_validated_job

    # The dispenser asserts Running only after the FMS has polled status twice.
    modbus_stub.set_spin_up_reads(2)

    response = fms_client.post(
        f"/api/v1/jobs/{job_id}/start",
        headers=headers,
        json={"preset_volume": _PRESET_VOLUME_L},
    )
    assert response.status_code == 200, f"Start dispense failed: {response.text}"

    # Wait for the FMS to write enable=1.
    deadline = time.monotonic() + _REACTION_TIMEOUT
    while time.monotonic() < deadline:
        if any(w.value == 1 for w in modbus_stub.writes_to(ENABLE_REG)):
            break
        time.sleep(0.1)
    else:
        pytest.fail("FMS did not write enable=1 to Modbus within timeout")

    # Give the FMS several poll cycles (interval 1 s) to read the clear Running
    # bit during spin-up. The totaliser is deliberately NOT advanced: a correct
    # FMS stays in the dispense waiting for the pump, whereas the bug closes a
    # zero-volume transaction here.
    time.sleep(4)

    closed = log_server_stub.events.all(event_type="transaction_closed")
    assert not closed, (
        "FMS closed a transaction before the pump asserted Running: a cleared "
        "Running bit during spin-up was misread as auto-stop "
        f"(volume_dispensed_l="
        f"{closed[-1].fields.get('volume_dispensed_l') if closed else None})"
    )
    assert not any(w.value == 0 for w in modbus_stub.writes_to(ENABLE_REG)), (
        "FMS wrote enable=0 during spin-up, aborting the dispense prematurely"
    )

    # Let the dispense complete normally. The pump has since asserted Running
    # (spin-up consumed by the polls above), so advancing the totaliser to the
    # preset triggers auto-stop and a full-volume transaction.
    modbus_stub.advance_totaliser(float(_PRESET_VOLUME_L))
    assert log_server_stub.events.wait_for_type(
        "transaction_closed", count=1, timeout=_REACTION_TIMEOUT
    ), "FMS did not close the transaction after the pump ran and reached preset"

    payload = log_server_stub.events.all(event_type="transaction_closed")[-1].fields
    assert abs(payload["volume_dispensed_l"] - float(_PRESET_VOLUME_L)) < 0.1, (
        f"volume_dispensed_l {payload['volume_dispensed_l']} != {_PRESET_VOLUME_L} L"
    )


@pytest.mark.full
def test_dispense_auto_stop_closes_transaction(
    completed_dispense: dict[str, Any],
    fms_client: httpx.Client,
    modbus_stub: ModbusStub,
) -> None:
    """After auto-stop, FMS must write enable=0 and the job must move to
    awaiting_bdn (a dispense is not closed until its BDN is linked, §279)."""
    enable_writes = modbus_stub.writes_to(ENABLE_REG)
    assert any(w.value == 0 for w in enable_writes), (
        "FMS never wrote enable=0 after auto-stop"
    )
    headers = completed_dispense["headers"]
    status = fms_client.get("/api/v1/jobs/current", headers=headers).json()
    assert status["status"] == "awaiting_bdn"


@pytest.mark.full
def test_transaction_record_complete(
    completed_dispense: dict[str, Any],
    fms_client: httpx.Client,
    log_server_stub: LogServerStub,
) -> None:
    """
    The transaction_closed event must contain all fields required by
    digital.md §4.2 and §6.1.
    """
    required_fields = {
        "job_id",
        "authorisation_id",
        "operator",
        "counterparty",
        "grade",
        "preset_volume_l",
        "start_totaliser_pulses",
        "end_totaliser_pulses",
        "volume_dispensed_l",
        "atg_volume_at_open_l",
        "atg_volume_at_close_l",
        "opened_at",
        "closed_at",
    }
    events = log_server_stub.events.all(event_type="transaction_closed")
    assert events, "No transaction_closed event in log-server stub"
    payload = events[-1].fields
    missing = required_fields - payload.keys()
    assert not missing, f"transaction_closed payload missing fields: {missing}"


@pytest.mark.full
def test_volume_dispensed_matches_known_amount(
    completed_dispense: dict[str, Any],
    log_server_stub: LogServerStub,
) -> None:
    """
    volume_dispensed_l must match the volume independently advanced on the stub.

    Anchoring to the known dispensed amount (not to arithmetic on the payload's
    own fields) is what makes this catch a wrong register address or width: a
    16-bit read of a 100 000-pulse dispense would wrap and report ~344 L.
    """
    events = log_server_stub.events.all(event_type="transaction_closed")
    payload = events[-1].fields

    # The completed_dispense fixture advanced the totaliser by exactly the preset.
    expected_volume = float(_PRESET_VOLUME_L)
    assert abs(payload["volume_dispensed_l"] - expected_volume) < 0.1, (
        f"volume_dispensed_l {payload['volume_dispensed_l']} != advanced {expected_volume} L"
    )

    # The reported totaliser delta must also agree with the known pulse count.
    delta = payload["end_totaliser_pulses"] - payload["start_totaliser_pulses"]
    assert delta == int(expected_volume * K_FACTOR), (
        f"totaliser delta {delta} != {int(expected_volume * K_FACTOR)} pulses"
    )


@pytest.mark.full
def test_totaliser_read_as_32bit_pair(
    completed_dispense: dict[str, Any],
    log_server_stub: LogServerStub,
    modbus_stub: ModbusStub,
) -> None:
    """
    Regression for the 16-bit register-map bug: a 1000 L dispense is 100 000
    pulses, above the 16-bit ceiling (65 535).  The recorded totaliser must
    exceed that ceiling, proving the FMS reconstructs the 32-bit pair rather
    than reading (or wrapping at) a single register.
    """
    events = log_server_stub.events.all(event_type="transaction_closed")
    payload = events[-1].fields
    delta = payload["end_totaliser_pulses"] - payload["start_totaliser_pulses"]
    assert delta > 0xFFFF, (
        f"totaliser delta {delta} did not exceed the 16-bit ceiling; "
        "the FMS is not reading the totaliser as a 32-bit pair"
    )
    # The FMS must have read the cumulative pair as well (start/end totaliser).
    assert any(r.count >= 2 for r in modbus_stub.read_covering(CUMULATIVE_REG)), (
        "FMS did not read the cumulative totaliser as a 32-bit pair"
    )


@pytest.mark.full
def test_bdn_linkage_creates_new_row(
    completed_dispense: dict[str, Any],
    fms_client: httpx.Client,
    log_server_stub: LogServerStub,
    postgres: PostgresClient,
) -> None:
    """
    Entering a BDN number must create a new Postgres row referencing the
    original transaction and push a bdn_linked event.
    The original transaction row must remain unmodified.
    """
    job_id = completed_dispense["job_id"]
    headers = completed_dispense["headers"]
    bdn = f"BDN-{job_id[:8]}"

    # A BDN is now linked by job_id; the FMS resolves the underlying metering
    # transaction itself (POST /jobs/{job_id}/bdn). The metering transaction is
    # the row for this job with no references_transaction_id.
    meter_rows = postgres.execute(
        "SELECT transaction_id FROM transactions "
        "WHERE job_id = %s AND references_transaction_id IS NULL",
        (job_id,),
    )
    assert meter_rows, "dispense transaction not recorded before BDN linkage"
    tx_id = meter_rows[0]["transaction_id"]

    response = fms_client.post(
        f"/api/v1/jobs/{job_id}/bdn",
        headers=headers,
        json={"bdn_number": bdn},
    )
    assert response.status_code in (200, 201)

    # The original metering row must still exist unchanged (no BDN on it).
    # transaction_id is selected alongside bdn_number because the psql client
    # returns no rows when every selected column is NULL.
    rows = postgres.execute(
        "SELECT transaction_id, bdn_number FROM transactions WHERE transaction_id = %s",
        (tx_id,),
    )
    assert rows and rows[0]["bdn_number"] is None
    # The BDN linkage is a new row referencing the original transaction.
    bdn_rows = postgres.execute(
        "SELECT bdn_number, references_transaction_id FROM transactions WHERE bdn_number = %s",
        (bdn,),
    )
    assert bdn_rows, "No BDN row found in transactions table"
    assert bdn_rows[0]["references_transaction_id"] == tx_id

    event_received = log_server_stub.events.wait_for_type(
        "bdn_linked", count=1, timeout=5
    )
    assert event_received
