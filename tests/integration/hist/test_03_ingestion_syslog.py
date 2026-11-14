"""
Security and audit event ingestion tests (hist-tests.md §3), targeting hist.md
§4.3 and §5 point 3.

Requires a deployed hist-01 (Incus): the FMS push simulator sends RFC 5424
syslog over a persistent TLS connection (RFC 5425) to vlagent on hist-01:6514,
validating its CA-signed certificate, and each event is read back from the
loopback VictoriaLogs via `incus exec` (hist.md §9.1), provided by the
`hist01_logs` fixture.

Isolation (§0): every message carries a `test_id` structured-data parameter set
to `unique_id`; the query selects on it, so a test reads back only its own
records. Where a test sends several messages, an extra `seq` parameter tells
them apart.

FIELD NAMES, confirmed against vlagent 1.52.0
  - MSGID     -> "msg_id"
  - severity  -> "severity"  (numeric, as a string, e.g. "6")
  - MSG       -> "_msg"      (hist.md §4.3; absent MSG yields a placeholder)
  - SD params -> prefixed with the structured-data identifier, so `username`
                 is stored and queried as `fms@99999.username`, not `username`

Run on the VM: python -m pytest tests/integration/hist/test_03_ingestion_syslog.py
"""

from __future__ import annotations

import socket

import pytest

from tests.integration.hist.conftest import Config
from tests.integration.hist.poll import poll_until_count
from tests.integration.hist.stubs import tls
from tests.integration.hist.stubs.fms_pusher import (
    SEVERITY_INFORMATIONAL,
    SEVERITY_NOTICE,
    SEVERITY_WARNING,
    FMSPusher,
    sd_field as _sd,
)
from tests.integration.hist.stubs.query import LogsClient

# vlagent RFC 5424 -> VictoriaLogs field names (see module docstring).
_F_MSGID = "msg_id"
_F_SEVERITY = "severity"
_F_MESSAGE = "_msg"


def _by_test_id(unique_id: str) -> str:
    """A LogsQL selector matching this test's records. The field name is quoted
    because it contains `@` and `.`, which LogsQL would otherwise read as
    syntax."""
    return f'"{_sd("test_id")}":"{unique_id}"'


def test_login_success_event_is_ingested(
    pusher: FMSPusher, hist01_logs: LogsClient, unique_id: str
) -> None:
    """§4.3: a LOGIN (informational) message lands with its MSGID, severity, MSG
    and structured-data fields."""
    pusher.push_syslog(
        "LOGIN",
        SEVERITY_INFORMATIONAL,
        username="jrenard",
        role="operator",
        session_id="8f2e",
        result="success",
        test_id=unique_id,
    )

    records = poll_until_count(hist01_logs, _by_test_id(unique_id), 1)
    assert len(records) == 1
    record = records[0]
    assert record[_F_MSGID] == "LOGIN"
    assert record[_F_SEVERITY] == str(SEVERITY_INFORMATIONAL)
    assert record[_F_MESSAGE] == "login"
    assert record[_sd("username")] == "jrenard"
    assert record[_sd("role")] == "operator"
    assert record[_sd("session_id")] == "8f2e"
    assert record[_sd("result")] == "success"


def test_login_failed_event_distinguishes_reason(
    pusher: FMSPusher, hist01_logs: LogsClient, unique_id: str
) -> None:
    """§4.3: two LOGIN_FAILED (warning) messages land distinctly, separated by
    `reason` — the granularity the FMS's generic 401 does not expose."""
    pusher.push_syslog(
        "LOGIN_FAILED",
        SEVERITY_WARNING,
        username="jrenard",
        role_attempted="operator",
        reason="invalid_credentials",
        test_id=unique_id,
    )
    pusher.push_syslog(
        "LOGIN_FAILED",
        SEVERITY_WARNING,
        username="ghost",
        role_attempted="operator",
        reason="invalid_rfid",
        test_id=unique_id,
    )

    records = poll_until_count(hist01_logs, _by_test_id(unique_id), 2)
    assert len(records) == 2
    for record in records:
        assert record[_F_MSGID] == "LOGIN_FAILED"
        assert record[_F_SEVERITY] == str(SEVERITY_WARNING)
        assert record[_F_MESSAGE] == "login failed"
    assert {r[_sd("reason")] for r in records} == {
        "invalid_credentials",
        "invalid_rfid",
    }


def test_card_change_event_is_ingested(
    pusher: FMSPusher, hist01_logs: LogsClient, unique_id: str
) -> None:
    """§4.3: a CARD-CHANGE (notice) message lands for each of enrol and revoke,
    with card_id, card_number, label."""
    for action in ("enrol", "revoke"):
        pusher.push_syslog(
            "CARD-CHANGE",
            SEVERITY_NOTICE,
            card_id="C-1001",
            card_number="1000002",
            label="MV Harrier tug",
            action=action,
            test_id=unique_id,
        )

    records = poll_until_count(hist01_logs, _by_test_id(unique_id), 2)
    assert len(records) == 2
    for record in records:
        assert record[_F_MSGID] == "CARD-CHANGE"
        assert record[_F_MESSAGE] == "card change"
        assert record[_sd("card_id")] == "C-1001"
        assert record[_sd("card_number")] == "1000002"
        assert record[_sd("label")] == "MV Harrier tug"
    assert {r[_sd("action")] for r in records} == {"enrol", "revoke"}


def test_persistent_connection_carries_multiple_messages(
    pusher: FMSPusher, hist01_logs: LogsClient, unique_id: str
) -> None:
    """§5 point 3: three messages sent over one persistent TCP connection are all
    ingested (the pusher reuses a single socket across push_syslog calls)."""
    for seq in ("1", "2", "3"):
        pusher.push_syslog(
            "LOGIN",
            SEVERITY_INFORMATIONAL,
            username="jrenard",
            seq=seq,
            test_id=unique_id,
        )

    records = poll_until_count(hist01_logs, _by_test_id(unique_id), 3)
    assert len(records) == 3
    assert {r[_sd("seq")] for r in records} == {"1", "2", "3"}


def test_malformed_syslog_line_does_not_crash_vlagent(
    pusher: FMSPusher, hist01_logs: LogsClient, unique_id: str
) -> None:
    """§5 point 3: a malformed line over the connection does not wedge vlagent —
    a subsequent well-formed message on the same connection still ingests.

    vlagent does not reject the line: it falls back to RFC 3164, where the whole
    text becomes the message, and stores it with `format="rfc3164"`. Survival is
    therefore the property to assert, not rejection — the same shape as vmagent
    answering 2xx to an unparseable import line (§1).
    """
    pusher.push_syslog(
        "LOGIN", SEVERITY_INFORMATIONAL, username="jrenard", seq="1", test_id=unique_id
    )
    pusher.send_raw_syslog(b"this is not a valid rfc 5424 line\n")
    pusher.push_syslog(
        "LOGIN", SEVERITY_INFORMATIONAL, username="jrenard", seq="2", test_id=unique_id
    )

    # The malformed line carries no test_id, so only the two well-formed messages
    # match; both must be present for vlagent to have survived the bad input.
    records = poll_until_count(hist01_logs, _by_test_id(unique_id), 2)
    assert {r[_sd("seq")] for r in records} == {"1", "2"}


def test_vlagent_syslog_presents_ca_signed_certificate(config: Config) -> None:
    """hist.md §9.1/§10.2: vlagent's syslog listener is signed by the RavensPort
    internal CA and carries a DNS SAN of hist-01 — the same certificate vmagent
    presents on :8429 (§1), reused rather than a distinct keypair per service."""
    pem = tls.certificate_pem(None, config.hist_host, config.syslog_port, sni="hist-01")
    assert tls.is_signed_by_ravensport_ca(pem), (
        f"vlagent's syslog certificate is not signed by {tls.CA_COMMON_NAME}: "
        f"issuer is {tls.issuer_common_name(pem)}"
    )
    assert tls.has_dns_san(pem, "hist-01"), (
        "vlagent's syslog certificate is missing DNS:hist-01"
    )


def test_plain_tcp_syslog_port_is_not_served(config: Config) -> None:
    """hist.md §5, §10.1: port 514 is no longer listened on now that the syslog
    path has moved to TLS on 6514."""
    with pytest.raises(OSError):
        socket.create_connection((config.hist_host, 514), timeout=5.0).close()
