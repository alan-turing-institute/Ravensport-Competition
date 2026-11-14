"""
Group 1 — Bootstrap and health.

Verifies that fms-01 is deployed, the FMS service is running, and the
database schema matches the specification.

All tests except test_atg_stub_reachable are marked fast: they require only
the FMS HTTPS endpoint and a Postgres connection, not the boundary-protocol
stubs.
"""

import pytest

from tests.integration.fms.stubs.atg import ATGStub
from tests.integration.fms.stubs.log_server import LogServerStub
import httpx

from tests.integration.fms.stubs.postgres import PostgresClient

# Tables that must carry append-only triggers (confirmed in Group 9, but
# the trigger existence check belongs here as a schema verification).
APPEND_ONLY_TABLES = ("transactions", "jobs", "sessions", "job_cancellations")

# Every table the FMS schema must contain.
REQUIRED_TABLES = {*APPEND_ONLY_TABLES, "card_table", "atg_readings"}


@pytest.mark.fast
def test_fms_responds(fms_client: httpx.Client) -> None:
    response = fms_client.get("/api/v1/health")
    assert response.status_code == 200


@pytest.mark.fast
def test_health_body(fms_client: httpx.Client) -> None:
    response = fms_client.get("/api/v1/health")
    body = response.json()
    assert body.get("status") == "ok"
    assert "version" in body


@pytest.mark.fast
def test_schema_tables_exist(postgres: PostgresClient) -> None:
    rows = postgres.execute(
        """
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = 'public'
        """
    )
    present = {r["table_name"] for r in rows}
    missing = REQUIRED_TABLES - present
    assert not missing, f"Missing tables in FMS schema: {missing}"


@pytest.mark.fast
def test_append_only_triggers_installed(postgres: PostgresClient) -> None:
    rows = postgres.execute(
        """
        SELECT event_object_table AS table_name,
               trigger_name,
               event_manipulation
        FROM information_schema.triggers
        WHERE trigger_schema = 'public'
        """
    )
    covered: dict[str, set[str]] = {}
    for r in rows:
        covered.setdefault(r["table_name"], set()).add(r["event_manipulation"])

    for table in APPEND_ONLY_TABLES:
        assert table in covered, f"No triggers found on table '{table}'"
        missing_events = {"UPDATE", "DELETE"} - covered[table]
        assert not missing_events, (
            f"Table '{table}' is missing triggers for: {missing_events}"
        )


@pytest.mark.full
def test_atg_stub_reachable_from_fms(
    atg_stub: ATGStub, log_server_stub: LogServerStub, fresh_stubs: object
) -> None:
    """
    Within a short window of the test runner starting the stubs, the FMS
    (which polls ATG every 1 s in test configuration) should have made at
    least one ATG i201 request and pushed an ATG metric to the log-server stub.

    Both checks wait rather than sample once: the metric for a given poll
    cycle's request is pushed by a separate background task, after a DB
    write, so it can land slightly after that request was recorded. A
    request recorded moments before this test's own fresh_stubs reset can
    have its metric arrive within this test's window while the request
    itself was already cleared — a single point-in-time check of
    received_requests would then see nothing, even though the FMS is
    polling correctly and the *next* cycle's request is only a fraction of a
    second away.
    """
    received_sample = log_server_stub.metrics.wait_for_metric(
        "atg_tank_volume_litres", count=1, timeout=5
    )
    assert received_sample, (
        "FMS did not push an ATG metric to the log-server stub within 5 s — "
        "check FMS_ATG_HOST, ATG_STUB_PORT and FMS_VMAGENT_URL on fms-01"
    )
    assert atg_stub.wait_for_request("i20100", timeout=2), (
        "ATG stub received no i20100 request from FMS"
    )
