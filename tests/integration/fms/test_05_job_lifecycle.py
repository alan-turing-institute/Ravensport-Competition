"""
Group 5 — Job lifecycle.

Verifies job creation and the guards that block invalid jobs, per digital.md
§4.1.

Fast tests: job creation and concurrent-job rejection (no stubs required).
Full tests: cavitation threshold check (requires ATG stub) and log-server
push on job creation (requires the log-server stub).
"""

import httpx
import pytest

from tests.integration.fms.conftest import create_job_or_skip
from tests.integration.fms.stubs.atg import ATGStub
from tests.integration.fms.stubs.log_server import LogServerStub

_DISPENSE_JOB = {
    "type": "dispense",
    "counterparty": "MV Harrier",
    "grade": "MGO",
    "preset_volume": 200,
}

_FILL_JOB = {
    "type": "fill",
    "counterparty": "Coastal Fuels Ltd",
    "grade": "MGO",
    "expected_volume": 10_000,
}


@pytest.mark.fast
def test_create_dispense_job(
    fms_client: httpx.Client, operator_headers: dict[str, str]
) -> None:
    response = create_job_or_skip(fms_client, operator_headers, _DISPENSE_JOB)
    assert response.status_code == 201
    body = response.json()
    assert "job_id" in body
    assert body.get("status") == "active"
    assert body.get("type") == "dispense"

    fms_client.delete(f"/api/v1/jobs/{body['job_id']}", headers=operator_headers)


@pytest.mark.fast
def test_create_fill_job(
    fms_client: httpx.Client, operator_headers: dict[str, str]
) -> None:
    response = create_job_or_skip(fms_client, operator_headers, _FILL_JOB)
    assert response.status_code == 201
    body = response.json()
    assert "job_id" in body
    assert body.get("status") == "active"
    assert body.get("type") == "fill"

    fms_client.delete(f"/api/v1/jobs/{body['job_id']}", headers=operator_headers)


@pytest.mark.fast
def test_get_current_job(
    fms_client: httpx.Client, operator_headers: dict[str, str]
) -> None:
    """The single in-flight job is addressed as the fixed resource
    /jobs/current (narrative §273: never more than one current job)."""
    create = create_job_or_skip(fms_client, operator_headers, _DISPENSE_JOB)
    assert create.status_code == 201
    job_id = create.json()["job_id"]

    response = fms_client.get("/api/v1/jobs/current", headers=operator_headers)
    assert response.status_code == 200
    assert response.json()["job_id"] == job_id

    fms_client.delete(f"/api/v1/jobs/{job_id}", headers=operator_headers)


@pytest.mark.fast
def test_concurrent_job_rejected(
    fms_client: httpx.Client, operator_headers: dict[str, str]
) -> None:
    """The FMS must not allow a second active job while one is already open."""
    first = create_job_or_skip(fms_client, operator_headers, _DISPENSE_JOB)
    assert first.status_code == 201
    first_id = first.json()["job_id"]

    # The conflict under test, not a contention with the simulator: this must
    # stay a direct call. Routing it through create_job_or_skip would make the
    # very outcome this test asserts on indistinguishable from a slot lost to
    # the simulator, and skip the test instead of confirming the rejection.
    second = fms_client.post("/api/v1/jobs", headers=operator_headers, json=_FILL_JOB)
    assert second.status_code == 409, (
        f"Expected 409 for concurrent job, got {second.status_code}: {second.text}"
    )

    fms_client.delete(f"/api/v1/jobs/{first_id}", headers=operator_headers)


@pytest.mark.full
def test_dispense_rejected_below_cavitation_threshold(
    fms_client: httpx.Client,
    operator_headers: dict[str, str],
    atg_stub: ATGStub,
    fresh_stubs: object,
) -> None:
    """
    When ATG reports volume ≤ 10 % of capacity (5 000 L), the FMS must refuse
    a new dispense job with 409 or 422, citing the cavitation threshold.
    """
    atg_stub.set_state(volume_L=4_500.0)  # below 10 % of 50 000 L
    import time

    time.sleep(2)  # allow FMS to poll the new ATG state

    response = create_job_or_skip(fms_client, operator_headers, _DISPENSE_JOB)
    assert response.status_code in (409, 422), (
        f"Expected 409/422 for dispense below cavitation threshold, got {response.status_code}"
    )
    assert "cavitation" in response.text.lower() or "level" in response.text.lower()


@pytest.mark.full
def test_job_opened_event_pushed_to_log_server(
    fms_client: httpx.Client,
    operator_headers: dict[str, str],
    log_server_stub: LogServerStub,
    fresh_stubs: object,
) -> None:
    """Job creation must push a job_opened event to the log-server."""
    # Wait for FMS to poll the ATG stub and update its in-memory reading to the
    # volume set by fresh_stubs (25 000 L).  Without this, a preceding test that
    # left the stub below the cavitation threshold causes a 422 here.
    log_server_stub.metrics.wait_for_metric(
        "atg_tank_volume_litres", count=1, timeout=5
    )

    create = create_job_or_skip(fms_client, operator_headers, _DISPENSE_JOB)
    assert create.status_code == 201
    job_id = create.json()["job_id"]

    try:
        received = log_server_stub.events.wait_for_type(
            "job_opened", count=1, timeout=5
        )
        assert received, "FMS did not push job_opened event to the log-server stub"

        events = log_server_stub.events.all(event_type="job_opened")
        assert any(e.fields.get("job_id") == job_id for e in events)
    finally:
        fms_client.delete(f"/api/v1/jobs/{job_id}", headers=operator_headers)


@pytest.mark.full
def test_job_cancelled_event_pushed_to_log_server(
    fms_client: httpx.Client,
    operator_headers: dict[str, str],
    log_server_stub: LogServerStub,
    fresh_stubs: object,
) -> None:
    """Cancelling an active job must push a job_cancelled event naming the
    canceller (fms.md §7.4, hist.md §4.2)."""
    log_server_stub.metrics.wait_for_metric(
        "atg_tank_volume_litres", count=1, timeout=5
    )

    create = create_job_or_skip(fms_client, operator_headers, _DISPENSE_JOB)
    assert create.status_code == 201
    job_id = create.json()["job_id"]

    cancel = fms_client.delete(f"/api/v1/jobs/{job_id}", headers=operator_headers)
    assert cancel.status_code == 204, f"Cancel failed: {cancel.text}"

    received = log_server_stub.events.wait_for_type("job_cancelled", count=1, timeout=5)
    assert received, "FMS did not push job_cancelled event to the log-server stub"

    events = log_server_stub.events.all(event_type="job_cancelled")
    mine = [e for e in events if e.fields.get("job_id") == job_id]
    assert mine, f"no job_cancelled for {job_id}: {events}"
    for field in ("cancelled_by", "cancelled_session_id", "cancelled_at"):
        assert mine[0].fields.get(field), f"job_cancelled carries no {field}"
