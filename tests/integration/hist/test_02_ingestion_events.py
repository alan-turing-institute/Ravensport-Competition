"""
Structured event ingestion tests (hist-tests.md §2), targeting hist.md §4.2 and
§5 point 2.

Requires a deployed hist-01 (Incus): the FMS push simulator posts JSON-line
events to vlagent on hist-01:9429, and each event is read back from the loopback
VictoriaLogs via `incus exec hist-01 -- curl` (hist.md §9.1), provided by the
`hist01_logs` fixture.

Isolation (§0): each event carries a unique id — job_id for job_opened and
job_cancelled, transaction_id for the transaction/BDN events, set to `unique_id` — and the
query selects on it, so a test reads back only its own record.

VictoriaLogs stores every ingested field as a string and returns it as one, so
numeric fields (volumes, pulses) are compared after float() coercion: the value
must round-trip, but its stored form is a string, not a number.

Run on the VM: python -m pytest tests/integration/hist/test_02_ingestion_events.py
"""

from __future__ import annotations

import httpx

from tests.integration.hist.conftest import Config
from tests.integration.hist.poll import poll_until_count
from tests.integration.hist.stubs import tls
from tests.integration.hist.stubs.fms_pusher import FMSPusher
from tests.integration.hist.stubs.query import LogsClient

# Common envelope fields carried on every §4.2 record, minus the ones the pusher
# supplies itself (_time, _msg, event_type). job_id is overridden per test where
# it is the isolation key.
_COMMON = {
    "job_id": "job-0000",
    "type": "dispense",
    "operator": "jrenard",
    "counterparty": "MV Harrier",
    "grade": "MGO",
}


def _assert_landed(
    record: dict,
    *,
    event_type: str,
    string_fields: dict[str, str],
    numeric_fields: dict[str, float],
) -> None:
    """Assert every hist.md §4.2 field for this event landed with its pushed value, plus
    the envelope's _time and _msg."""
    assert record["event_type"] == event_type
    for key, value in string_fields.items():
        assert record.get(key) == value, f"{key}: {record.get(key)!r} != {value!r}"
    for key, value in numeric_fields.items():
        assert key in record, f"missing numeric field {key}"
        assert float(record[key]) == float(value), (
            f"{key}: {record.get(key)!r} != {value!r}"
        )
    assert record.get("_time"), "envelope _time missing"
    assert record.get("_msg"), "envelope _msg missing"


def test_job_opened_event_fields_land_verbatim(
    pusher: FMSPusher, hist01_logs: LogsClient, unique_id: str
) -> None:
    string_fields = {
        **_COMMON,
        "job_id": unique_id,
        "created_at": "2026-07-17T14:12:05Z",
    }
    numeric_fields = {"atg_volume_at_open_l": 42350.0}
    pusher.push_event("job_opened", **string_fields, **numeric_fields)

    records = poll_until_count(hist01_logs, f'job_id:"{unique_id}"')
    assert len(records) == 1
    _assert_landed(
        records[0],
        event_type="job_opened",
        string_fields=string_fields,
        numeric_fields=numeric_fields,
    )


def test_transaction_closed_dispense_fields_land_verbatim(
    pusher: FMSPusher, hist01_logs: LogsClient, unique_id: str
) -> None:
    string_fields = {
        **_COMMON,
        "job_id": f"job-{unique_id}",
        "type": "dispense",
        "transaction_id": unique_id,
        "authorisation_id": "auth-7f3a",
        "opened_at": "2026-07-17T14:12:05Z",
        "closed_at": "2026-07-17T14:38:11Z",
    }
    numeric_fields = {
        "start_totaliser_pulses": 1000000,
        "end_totaliser_pulses": 1042350,
        "volume_dispensed_l": 4235.0,
        "atg_volume_at_open_l": 42350.0,
        "atg_volume_at_close_l": 38115.0,
    }
    pusher.push_event("transaction_closed", **string_fields, **numeric_fields)

    records = poll_until_count(hist01_logs, f'transaction_id:"{unique_id}"')
    assert len(records) == 1
    _assert_landed(
        records[0],
        event_type="transaction_closed",
        string_fields=string_fields,
        numeric_fields=numeric_fields,
    )


def test_transaction_closed_fill_fields_land_verbatim(
    pusher: FMSPusher, hist01_logs: LogsClient, unique_id: str
) -> None:
    string_fields = {
        **_COMMON,
        "job_id": f"job-{unique_id}",
        "type": "fill",
        "transaction_id": unique_id,
        "ticket_number": "TKT-88231",
        "supplier": "Harrier Marine Fuels",
        "tanker_registration": "GB-AB12 CDE",
        "opened_at": "2026-07-17T09:02:00Z",
        "closed_at": "2026-07-17T10:15:44Z",
    }
    numeric_fields = {
        "delivered_volume_l": 12000.0,
        "atg_volume_at_open_l": 30000.0,
        "atg_volume_at_close_l": 42000.0,
    }
    pusher.push_event("transaction_closed", **string_fields, **numeric_fields)

    records = poll_until_count(hist01_logs, f'transaction_id:"{unique_id}"')
    assert len(records) == 1
    _assert_landed(
        records[0],
        event_type="transaction_closed",
        string_fields=string_fields,
        numeric_fields=numeric_fields,
    )


def test_bdn_linked_event_fields_land_verbatim(
    pusher: FMSPusher, hist01_logs: LogsClient, unique_id: str
) -> None:
    string_fields = {
        **_COMMON,
        "job_id": f"job-{unique_id}",
        "transaction_id": unique_id,
        "references_transaction_id": f"ref-{unique_id}",
        "bdn_number": "BDN-2026-0412",
    }
    pusher.push_event("bdn_linked", **string_fields)

    records = poll_until_count(hist01_logs, f'transaction_id:"{unique_id}"')
    assert len(records) == 1
    _assert_landed(
        records[0],
        event_type="bdn_linked",
        string_fields=string_fields,
        numeric_fields={},
    )


def test_job_cancelled_event_fields_land_verbatim(
    pusher: FMSPusher, hist01_logs: LogsClient, unique_id: str
) -> None:
    string_fields = {
        **_COMMON,
        "job_id": unique_id,
        "cancelled_by": "jrenard",
        "cancelled_session_id": f"session-{unique_id}",
        "cancelled_at": "2026-07-17T14:20:41Z",
    }
    pusher.push_event("job_cancelled", **string_fields)

    records = poll_until_count(hist01_logs, f'job_id:"{unique_id}"')
    assert len(records) == 1
    _assert_landed(
        records[0],
        event_type="job_cancelled",
        string_fields=string_fields,
        numeric_fields={},
    )


def test_vlagent_jsonline_presents_ca_signed_certificate(config: Config) -> None:
    """hist.md §9.1/§10.2: vlagent's JSON-line endpoint is signed by the
    RavensPort internal CA and carries a DNS SAN of hist-01. The JSON-line
    listener and the syslog listener (§3) are independent TLS server
    configurations within the same vlagent process, so one can be misconfigured
    while the other works — this is the counterpart to §1's vmagent test and
    §3's syslog test."""
    pem = tls.certificate_pem(
        None, config.hist_host, config.event_push_port, sni="hist-01"
    )
    assert tls.is_signed_by_ravensport_ca(pem), (
        f"vlagent's JSON-line certificate is not signed by {tls.CA_COMMON_NAME}: "
        f"issuer is {tls.issuer_common_name(pem)}"
    )
    assert tls.has_dns_san(pem, "hist-01"), (
        "vlagent's JSON-line certificate is missing DNS:hist-01"
    )


def test_plain_http_jsonline_endpoint_is_not_served(config: Config) -> None:
    """hist.md §9.1: vlagent has no unencrypted fallback listener on :9429
    either. The connection is not refused or reset: Go's net/http (vlagent's
    HTTP stack) detects the plaintext request on its TLS listener and answers
    a complete HTTP/1.0 400 response — "Client sent an HTTP request to an
    HTTPS server." — rather than dropping the connection. The property that
    matters is that no data is accepted (confirmed against vlagent 1.52.0)."""
    response = httpx.post(
        f"http://{config.hist_host}:{config.event_push_port}/insert/jsonline",
        content="",
        timeout=5.0,
    )
    assert response.status_code == 400, (
        f"expected 400 (TLS-required rejection), got {response.status_code}"
    )
