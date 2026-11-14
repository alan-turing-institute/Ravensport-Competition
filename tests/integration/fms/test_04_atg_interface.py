"""
Group 4 — ATG interface.

Verifies that the FMS correctly polls the ATG console stub, parses Veeder-Root
TLS responses, and records readings, per digital.md §3 (boundary protocols)
and §4.5 (log-server ingestion).

All tests are marked full: they require the ATG stub, the log-server stub, and
the FMS ATG poll interval set to 1 s (FMS_ATG_POLL_INTERVAL=1 on fms-01).
"""

import time

import pytest

from tests.integration.fms.stubs.atg import ATGStub
from tests.integration.fms.stubs.log_server import LogServerStub
from tests.integration.fms.stubs.postgres import PostgresClient

# ATG poll interval on fms-01 in test config, plus tolerance.
_POLL_INTERVAL_S = 1
_POLL_TIMEOUT_S = 5


@pytest.mark.full
def test_atg_polls_i201(atg_stub: ATGStub, fresh_stubs: object) -> None:
    """FMS must send at least one i201 request within the poll interval."""
    time.sleep(_POLL_TIMEOUT_S)
    codes = [r.function_code for r in atg_stub.received_requests]
    assert "i20100" in codes, "FMS never sent i20100 to ATG stub"


@pytest.mark.full
def test_atg_i201_reading_reaches_log_server_stub(
    atg_stub: ATGStub, log_server_stub: LogServerStub, fresh_stubs: object
) -> None:
    """After an i201 poll, the FMS must push an ATG tank-volume sample.

    ATG readings are metrics, not log records (hist.md §4.1), so they reach
    vmagent rather than vlagent.
    """
    atg_stub.set_state(volume_L=30_000.0, temperature_C=15.0)
    received = log_server_stub.metrics.wait_for_metric(
        "atg_tank_volume_litres", count=1, timeout=_POLL_TIMEOUT_S
    )
    assert received, "FMS did not push an ATG metric within the poll window"


@pytest.mark.full
def test_atg_i201_values_forwarded_correctly(
    atg_stub: ATGStub, log_server_stub: LogServerStub, fresh_stubs: object
) -> None:
    """The metric values must match the state reported by the stub.

    Both series carry `tank_id="01"` (hist.md §4.1); the label is asserted
    because a metric that arrives unlabelled still matches by name and would
    otherwise pass.
    """
    atg_stub.set_state(volume_L=12_345.0, temperature_C=16.5)
    log_server_stub.metrics.wait_for_metric(
        "atg_tank_temperature_celsius", count=1, timeout=_POLL_TIMEOUT_S
    )

    volumes = log_server_stub.metrics.named("atg_tank_volume_litres")
    assert volumes, "No atg_tank_volume_litres samples received"
    assert abs(volumes[-1].value - 12_345.0) < 1.0, (
        f"volume mismatch: got {volumes[-1].value}, expected ~12345"
    )
    assert volumes[-1].labels.get("tank_id") == "01"

    temps = log_server_stub.metrics.named("atg_tank_temperature_celsius")
    assert temps, "No atg_tank_temperature_celsius samples received"
    assert abs(temps[-1].value - 16.5) < 0.5


@pytest.mark.full
def test_atg_no_alarm_does_not_raise_alarm_state(
    atg_stub: ATGStub, log_server_stub: LogServerStub, fresh_stubs: object
) -> None:
    """With the i101 body all-normal (000000), no alarm series must appear.

    `atg_alarm_active` is written on transition only (hist.md §4.1), so with
    no alarm the series must be absent entirely, not present and zero.
    """
    atg_stub.set_state(bund_alarm=False)
    time.sleep(_POLL_TIMEOUT_S)
    alarms = log_server_stub.metrics.named("atg_alarm_active")
    assert not alarms, f"Unexpected alarm samples when no alarm is active: {alarms}"


@pytest.mark.full
def test_atg_bund_alarm_raises_alarm_event(
    atg_stub: ATGStub, log_server_stub: LogServerStub, fresh_stubs: object
) -> None:
    """Latching the bund liquid-sensor alarm (i101 entry 030301) must produce an
    `atg_alarm_active` sample. The FMS emits it on the rising edge, so establish
    the all-normal baseline first before latching."""
    atg_stub.set_state(bund_alarm=False)
    time.sleep(_POLL_INTERVAL_S + 1)
    log_server_stub.clear()

    atg_stub.set_state(bund_alarm=True)
    received = log_server_stub.metrics.wait_for_metric(
        "atg_alarm_active", count=1, timeout=_POLL_TIMEOUT_S
    )
    assert received, (
        "FMS did not raise an atg_alarm_active sample after the bund alarm latched"
    )

    alarms = log_server_stub.metrics.named("atg_alarm_active")
    # The alarm name is a label on the series, not a field (hist.md §4.1).
    assert alarms[0].labels.get("alarm") == "bund_leak"
    assert alarms[0].value == 1.0


@pytest.mark.full
def test_atg_readings_persisted_in_postgres(
    atg_stub: ATGStub,
    log_server_stub: LogServerStub,
    postgres: PostgresClient,
    fresh_stubs: object,
) -> None:
    """ATG readings must also be written to the FMS Postgres time-series store."""
    atg_stub.set_state(volume_L=20_000.0)
    log_server_stub.metrics.wait_for_metric(
        "atg_tank_volume_litres", count=1, timeout=_POLL_TIMEOUT_S
    )

    rows = postgres.execute(
        "SELECT volume_l, recorded_at FROM atg_readings ORDER BY recorded_at DESC LIMIT 1"
    )
    assert rows, "No atg_readings row found in Postgres after ATG poll"
    assert abs(rows[0]["volume_l"] - 20_000.0) < 1.0
