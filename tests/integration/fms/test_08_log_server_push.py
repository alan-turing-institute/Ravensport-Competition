"""
Group 8 — Log-server push fidelity.

Verifies that the FMS delivers every lifecycle event to the log-server stub in
the correct sequence, with the correct structure, and retries on transient
stub unavailability, per digital.md §4.5 and §6.1.

All tests are marked full.
"""

from __future__ import annotations

import time

import httpx
import pytest

from tests.integration.fms.conftest import Config, create_job_or_skip, free_job_slot
from tests.integration.fms.stubs.atg import ATGStub
from tests.integration.fms.stubs.log_server import LogServerStub
from tests.integration.fms.stubs.modbus import ENABLE_REG, ModbusStub
from tests.integration.fms.stubs.rfid import RFIDPair

_PRESET_VOLUME_L = 200
_REACTION_TIMEOUT = 5


def _wait_for_enable(modbus_stub: ModbusStub) -> None:
    """Block until the FMS has written enable=1 to the dispenser.

    wait_for_write fires on the first write (the preset); the dispense only
    starts on the second write (enable=1).  Advancing the totaliser before
    enable=1 triggers the stub's auto-stop while the pump is still stopped, so
    the FMS RunningLatch never observes the Running bit set and the transaction
    never closes.  Polling for enable=1 mirrors test_06's running_dispense
    fixture.
    """
    deadline = time.monotonic() + _REACTION_TIMEOUT
    while time.monotonic() < deadline:
        if any(w.value == 1 for w in modbus_stub.writes_to(ENABLE_REG)):
            return
        time.sleep(0.1)
    pytest.fail("FMS did not write enable=1 to Modbus within timeout")


def _run_dispense(
    config: Config,
    fms_client: httpx.Client,
    operator_headers: dict[str, str],
    enrolled_vessel_card: str,
    atg_stub: ATGStub,
    rfid: RFIDPair,
    modbus_stub: ModbusStub,
    log_server_stub: LogServerStub,
) -> str:
    """Helper: drive a complete dispense lifecycle, through the BDN link that
    closes it and frees the job slot, and return the job_id."""
    atg_stub.set_state(volume_L=25_000.0)

    job_resp = create_job_or_skip(
        fms_client,
        operator_headers,
        {
            "type": "dispense",
            "counterparty": config.vessel_imo_number,
            "grade": "MGO",
            "preset_volume": _PRESET_VOLUME_L,
        },
    )
    assert job_resp.status_code == 201
    job_id: str = job_resp.json()["job_id"]

    rfid.vessel.inject(config.vessel_rfid)

    deadline = time.monotonic() + _REACTION_TIMEOUT
    while time.monotonic() < deadline:
        if (
            fms_client.get("/api/v1/jobs/current", headers=operator_headers)
            .json()
            .get("rfid_validated")
        ):
            break
        time.sleep(0.2)

    fms_client.post(
        f"/api/v1/jobs/{job_id}/start",
        headers=operator_headers,
        json={"preset_volume": _PRESET_VOLUME_L},
    )
    _wait_for_enable(modbus_stub)
    modbus_stub.advance_totaliser(float(_PRESET_VOLUME_L))
    log_server_stub.events.wait_for_type(
        "transaction_closed", count=1, timeout=_REACTION_TIMEOUT
    )
    # A completed dispense rests in awaiting_bdn, holding the job slot until its
    # BDN is linked (fms.md §7.4); free_job_slot links one.
    free_job_slot(config, fms_client, job_id, modbus_stub)
    log_server_stub.events.wait_for_type(
        "bdn_linked", count=1, timeout=_REACTION_TIMEOUT
    )
    return job_id


@pytest.mark.full
def test_all_lifecycle_events_delivered_in_order(
    config: Config,
    fms_client: httpx.Client,
    operator_headers: dict[str, str],
    enrolled_vessel_card: str,
    atg_stub: ATGStub,
    rfid: RFIDPair,
    modbus_stub: ModbusStub,
    log_server_stub: LogServerStub,
    fresh_stubs: object,
) -> None:
    """
    After a complete dispense, the log-server stub must have received events in
    the expected sequence: job_opened → transaction_open → transaction_closed →
    bdn_linked.
    """
    _run_dispense(
        config,
        fms_client,
        operator_headers,
        enrolled_vessel_card,
        atg_stub,
        rfid,
        modbus_stub,
        log_server_stub,
    )

    # vlagent receives domain events only: ATG readings are metrics and go to
    # vmagent (hist.md §4.1), so nothing interleaves the lifecycle here.
    lifecycle = [e.event_type for e in log_server_stub.events.all()]
    assert lifecycle == [
        "job_opened",
        "transaction_open",
        "transaction_closed",
        "bdn_linked",
    ], f"Lifecycle events out of order or missing: {lifecycle}"


@pytest.mark.full
def test_transaction_closed_event_fields(
    config: Config,
    fms_client: httpx.Client,
    operator_headers: dict[str, str],
    enrolled_vessel_card: str,
    atg_stub: ATGStub,
    rfid: RFIDPair,
    modbus_stub: ModbusStub,
    log_server_stub: LogServerStub,
    fresh_stubs: object,
) -> None:
    """Every field listed in digital.md §6.1 must appear in transaction_closed."""
    _run_dispense(
        config,
        fms_client,
        operator_headers,
        enrolled_vessel_card,
        atg_stub,
        rfid,
        modbus_stub,
        log_server_stub,
    )
    required = {
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
    assert events
    record = events[-1].fields
    missing = required - record.keys()
    assert not missing, f"transaction_closed missing fields: {missing}"

    # The VictoriaLogs envelope (hist.md §4.2). `_msg` matters because
    # VictoriaLogs substitutes placeholder text of its own when it is absent,
    # and that placeholder is what a Grafana reader would see.
    assert record.get("_msg"), f"record carries no _msg: {record}"
    assert record.get("_time"), f"record carries no _time: {record}"

    # Flat, not nested. The fields above are addressed at the top level by
    # §4.2's selectors; the pre-existing pusher nested them under `payload`,
    # which no selector would have matched.
    assert "payload" not in record, f"fields must not be nested: {record}"


@pytest.mark.full
def test_events_delivered_after_transient_stub_unavailability(
    config: Config,
    fms_client: httpx.Client,
    operator_headers: dict[str, str],
    enrolled_vessel_card: str,
    atg_stub: ATGStub,
    rfid: RFIDPair,
    modbus_stub: ModbusStub,
    log_server_stub: LogServerStub,
    fresh_stubs: object,
) -> None:
    """
    The FMS must retry event delivery if the log-server is temporarily
    unavailable.  We block the log-server stub for 3 s during a lifecycle step
    and confirm all events eventually arrive.
    """
    # Create the job while the stub is available.
    atg_stub.set_state(volume_L=25_000.0)
    job_resp = create_job_or_skip(
        fms_client,
        operator_headers,
        {
            "type": "dispense",
            "counterparty": config.vessel_imo_number,
            "grade": "MGO",
            "preset_volume": _PRESET_VOLUME_L,
        },
    )
    assert job_resp.status_code == 201
    job_id = job_resp.json()["job_id"]

    # Stop the stub before the RFID step so the transaction_open event cannot be delivered.
    log_server_stub.stop()
    time.sleep(0.1)

    rfid.vessel.inject(config.vessel_rfid)
    deadline = time.monotonic() + _REACTION_TIMEOUT
    while time.monotonic() < deadline:
        if (
            fms_client.get("/api/v1/jobs/current", headers=operator_headers)
            .json()
            .get("rfid_validated")
        ):
            break
        time.sleep(0.2)

    fms_client.post(
        f"/api/v1/jobs/{job_id}/start",
        headers=operator_headers,
        json={"preset_volume": _PRESET_VOLUME_L},
    )
    _wait_for_enable(modbus_stub)
    modbus_stub.advance_totaliser(float(_PRESET_VOLUME_L))

    # Restart the stub after 3 s of unavailability.
    time.sleep(3)
    log_server_stub.start()

    received = log_server_stub.events.wait_for_type(
        "transaction_closed", count=1, timeout=15
    )
    assert received, (
        "FMS did not deliver transaction_closed after the log-server stub returned — "
        "the pusher retries without an attempt limit (fms.md §5.4), so a 3 s "
        "outage must cost nothing"
    )

    free_job_slot(config, fms_client, job_id, modbus_stub)
