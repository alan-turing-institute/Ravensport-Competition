"""
Wire-format tests for the FMS push simulator (stubs/fms_pusher.py).

These tests require no Incus infrastructure: the pusher is pure client code,
so each test points it at a local capture server and asserts the bytes on
the wire match the contracts in hist.md §4 and §5:

  - push_metric  → VictoriaMetrics import-format JSON line (hist.md §5 point 1)
  - push_event   → JSON-line object with the §4.2 envelope
  - push_syslog  → RFC 5424, LF-terminated, over one persistent TCP
                   connection (§4.3, §5 point 3), facility user (1)

The pusher is the format authority for the whole hist suite — every
ingestion test trusts it to speak the same dialect the real fms-01 will —
so its output is pinned here, byte-level, before it is used anywhere.

Run locally: python -m pytest tests/integration/hist/test_00_pusher_wire_format.py
"""

from __future__ import annotations

import json
import re
import socket
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Generator

import pytest

from tests.integration.hist.stubs.fms_pusher import (
    FACILITY_USER,
    SEVERITY_INFORMATIONAL,
    SEVERITY_NOTICE,
    SEVERITY_WARNING,
    FMSPusher,
)

_WAIT_TIMEOUT = 5.0


# ---------------------------------------------------------------------------
# Local capture servers
# ---------------------------------------------------------------------------


@dataclass
class _Request:
    path: str
    body: bytes


class _HttpRecorder:
    """Accepts any POST, records (path, body), returns 204."""

    def __init__(self) -> None:
        self.requests: list[_Request] = []
        requests = self.requests

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:
                pass

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", 0))
                requests.append(_Request(self.path, self.rfile.read(length)))
                self.send_response(204)
                self.end_headers()

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port: int = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()


class _TcpRecorder:
    """Accepts TCP connections, records received bytes and connection count."""

    def __init__(self) -> None:
        self.data = bytearray()
        self.connection_count = 0
        self._lock = threading.Lock()
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(4)
        self.port: int = self._listener.getsockname()[1]
        self._stopping = False
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    def _accept_loop(self) -> None:
        while not self._stopping:
            try:
                conn, _ = self._listener.accept()
            except OSError:
                return
            with self._lock:
                self.connection_count += 1
            threading.Thread(target=self._read_loop, args=(conn,), daemon=True).start()

    def _read_loop(self, conn: socket.socket) -> None:
        with conn:
            while True:
                try:
                    chunk = conn.recv(4096)
                except OSError:
                    return
                if not chunk:
                    return
                with self._lock:
                    self.data.extend(chunk)

    def lines(self) -> list[bytes]:
        with self._lock:
            return bytes(self.data).split(b"\n")[:-1]

    def wait_for_lines(self, count: int, timeout: float = _WAIT_TIMEOUT) -> list[bytes]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            lines = self.lines()
            if len(lines) >= count:
                return lines
            time.sleep(0.02)
        pytest.fail(f"expected {count} syslog lines, got {len(self.lines())}")

    def stop(self) -> None:
        self._stopping = True
        self._listener.close()


@pytest.fixture()
def http_recorder() -> Generator[_HttpRecorder, None, None]:
    recorder = _HttpRecorder()
    yield recorder
    recorder.stop()


@pytest.fixture()
def tcp_recorder() -> Generator[_TcpRecorder, None, None]:
    recorder = _TcpRecorder()
    yield recorder
    recorder.stop()


@pytest.fixture()
def pusher(
    http_recorder: _HttpRecorder, tcp_recorder: _TcpRecorder
) -> Generator[FMSPusher, None, None]:
    p = FMSPusher(
        "127.0.0.1",
        metrics_port=http_recorder.port,
        jsonline_port=http_recorder.port,
        syslog_port=tcp_recorder.port,
    )
    yield p
    p.close()


# ---------------------------------------------------------------------------
# push_metric — VictoriaMetrics import format (hist.md §5 point 1)
# ---------------------------------------------------------------------------


def test_metric_line_is_victoriametrics_import_format(
    pusher: FMSPusher, http_recorder: _HttpRecorder
) -> None:
    """One JSON line matching hist.md §5's example, POSTed to /api/v1/import."""
    pusher.push_metric("atg_tank_volume_litres", "01", 42350.0, timestamp=1752761525000)

    assert len(http_recorder.requests) == 1
    request = http_recorder.requests[0]
    assert request.path == "/api/v1/import"
    assert request.body.endswith(b"\n")
    line = json.loads(request.body)
    assert line == {
        "metric": {"__name__": "atg_tank_volume_litres", "tank_id": "01"},
        "values": [42350.0],
        "timestamps": [1752761525000],
    }


def test_metric_accepts_extra_labels(
    pusher: FMSPusher, http_recorder: _HttpRecorder
) -> None:
    """atg_alarm_active carries alarm="bund_leak" alongside tank_id (§4.1)."""
    pusher.push_metric(
        "atg_alarm_active", "01", 1, timestamp=1752761525000, alarm="bund_leak"
    )

    line = json.loads(http_recorder.requests[0].body)
    assert line["metric"] == {
        "__name__": "atg_alarm_active",
        "tank_id": "01",
        "alarm": "bund_leak",
    }


def test_metric_datetime_timestamp_converted_to_epoch_ms(
    pusher: FMSPusher, http_recorder: _HttpRecorder
) -> None:
    when = datetime(2026, 7, 17, 14, 12, 5, tzinfo=timezone.utc)
    pusher.push_metric("atg_tank_temperature_celsius", "01", 18.5, timestamp=when)

    line = json.loads(http_recorder.requests[0].body)
    assert line["timestamps"] == [int(when.timestamp() * 1000)]


def test_post_raw_metric_line_passes_body_through(
    pusher: FMSPusher, http_recorder: _HttpRecorder
) -> None:
    """Escape hatch for §1's malformed-input test: no validation, body verbatim."""
    response = pusher.post_raw_metric_line("this is not import format")

    assert http_recorder.requests[0].body == b"this is not import format"
    assert response.status_code == 204


# ---------------------------------------------------------------------------
# push_event — JSON-line envelope (hist.md §4.2, §5 point 2)
# ---------------------------------------------------------------------------


def test_event_posts_jsonline_with_envelope(
    pusher: FMSPusher, http_recorder: _HttpRecorder
) -> None:
    pusher.push_event(
        "job_opened",
        job_id="6be3c95e-1cbe-4a5c-9d3f-3f1cbb2a6f0a",
        **{"type": "dispense"},
        operator="jrenard",
        counterparty="MV Harrier",
        grade="MGO",
        atg_volume_at_open_l=42350.0,
        created_at="2026-07-17T14:12:05Z",
    )

    request = http_recorder.requests[0]
    assert request.path == "/insert/jsonline"
    assert request.body.endswith(b"\n")
    event = json.loads(request.body)
    assert event["event_type"] == "job_opened"
    assert event["job_id"] == "6be3c95e-1cbe-4a5c-9d3f-3f1cbb2a6f0a"
    assert event["operator"] == "jrenard"
    # Envelope fields the pusher must supply itself (§4.2).
    assert "_time" in event
    assert "_msg" in event


def test_event_envelope_fields_can_be_overridden(
    pusher: FMSPusher, http_recorder: _HttpRecorder
) -> None:
    pusher.push_event(
        "transaction_closed",
        **{"_msg": "dispense transaction closed", "_time": "2026-07-17T14:30:00Z"},
    )

    event = json.loads(http_recorder.requests[0].body)
    assert event["_msg"] == "dispense transaction closed"
    assert event["_time"] == "2026-07-17T14:30:00Z"


# ---------------------------------------------------------------------------
# push_syslog — RFC 5424 over persistent TCP (hist.md §4.3, §5 point 3)
# ---------------------------------------------------------------------------

# <PRI>1 TIMESTAMP fms-01 fms - MSGID [fms@99999 ...] MSG
#
# The MSG is required, not optional: RFC 5424 permits its absence, but
# VictoriaLogs substitutes a placeholder referring to its own documentation when
# a message field is missing, which then becomes the displayed text of every
# security event (hist.md §4.3).
_RFC5424 = re.compile(
    rb"^<(?P<pri>\d{1,3})>1 "
    rb"(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})) "
    rb"(?P<hostname>\S+) (?P<app>\S+) (?P<procid>\S+) (?P<msgid>\S+) "
    rb"(?P<sd>\[fms@99999( [^=\]]+=\"([^\"\\\]]|\\.)*\")*\])"
    rb" (?P<msg>.+)$"
)


def test_syslog_line_is_rfc5424_with_fms_envelope(
    pusher: FMSPusher, tcp_recorder: _TcpRecorder
) -> None:
    pusher.push_syslog(
        "LOGIN",
        SEVERITY_INFORMATIONAL,
        username="jrenard",
        role="operator",
        session_id="8f2e",
        result="success",
    )

    (line,) = tcp_recorder.wait_for_lines(1)
    match = _RFC5424.match(line)
    assert match, f"not a valid RFC 5424 line: {line!r}"
    assert int(match["pri"]) == FACILITY_USER * 8 + SEVERITY_INFORMATIONAL  # 14
    assert match["hostname"] == b"fms-01"
    assert match["app"] == b"fms"
    assert match["msgid"] == b"LOGIN"
    assert match["msg"] == b"login"
    for param in (
        b'username="jrenard"',
        b'role="operator"',
        b'session_id="8f2e"',
        b'result="success"',
    ):
        assert param in match["sd"]


@pytest.mark.parametrize(
    ("msgid", "severity", "expected_pri"),
    [
        ("LOGIN", SEVERITY_INFORMATIONAL, 14),
        ("LOGIN_FAILED", SEVERITY_WARNING, 12),
        ("CARD-CHANGE", SEVERITY_NOTICE, 13),
    ],
)
def test_syslog_pri_encodes_facility_user_and_severity(
    pusher: FMSPusher,
    tcp_recorder: _TcpRecorder,
    msgid: str,
    severity: int,
    expected_pri: int,
) -> None:
    """PRI = facility × 8 + severity, facility pinned to user (1)."""
    pusher.push_syslog(msgid, severity)

    (line,) = tcp_recorder.wait_for_lines(1)
    assert line.startswith(f"<{expected_pri}>1 ".encode())


def test_syslog_structured_data_values_are_escaped(
    pusher: FMSPusher, tcp_recorder: _TcpRecorder
) -> None:
    """RFC 5424 §6.3.3: '\\', '\"', and ']' must be backslash-escaped in PARAM-VALUE."""
    pusher.push_syslog("CARD-CHANGE", SEVERITY_NOTICE, label='MV "Har]rier" \\ tug')

    (line,) = tcp_recorder.wait_for_lines(1)
    assert rb'label="MV \"Har\]rier\" \\ tug"' in line


def test_syslog_connection_is_persistent(
    pusher: FMSPusher, tcp_recorder: _TcpRecorder
) -> None:
    """Three messages arrive over one TCP connection (hist.md §5 point 3)."""
    for _ in range(3):
        pusher.push_syslog("LOGIN", SEVERITY_INFORMATIONAL, username="jrenard")

    tcp_recorder.wait_for_lines(3)
    assert tcp_recorder.connection_count == 1


def test_send_raw_syslog_passes_bytes_through_on_same_connection(
    pusher: FMSPusher, tcp_recorder: _TcpRecorder
) -> None:
    """Escape hatch for §3's malformed-input test: bytes verbatim, no framing."""
    pusher.push_syslog("LOGIN", SEVERITY_INFORMATIONAL)
    pusher.send_raw_syslog(b"not rfc 5424 at all\n")
    pusher.push_syslog("LOGIN", SEVERITY_INFORMATIONAL)

    lines = tcp_recorder.wait_for_lines(3)
    assert lines[1] == b"not rfc 5424 at all"
    assert tcp_recorder.connection_count == 1


def test_syslog_reconnects_after_close(
    pusher: FMSPusher, tcp_recorder: _TcpRecorder
) -> None:
    """Explicit close then push opens a fresh connection — needed by §3's
    persistent-connection test to control connection boundaries."""
    pusher.push_syslog("LOGIN", SEVERITY_INFORMATIONAL)
    tcp_recorder.wait_for_lines(1)
    pusher.close_syslog()
    pusher.push_syslog("LOGIN", SEVERITY_INFORMATIONAL)

    tcp_recorder.wait_for_lines(2)
    assert tcp_recorder.connection_count == 2
