"""
FMS push simulator.

The log server test suite replaces the real FMS with this thin client,
run from the test runner host.

It impersonates the FMS's three push paths into hist-01 (hist.md §5), so the
log server's own contract — store what is pushed, in the format specified,
and replicate it — is tested decoupled from the FMS's schedule and ability to
push the data.

API (one method per push path):

  push_metric(name, tank_id, value, timestamp, **labels)
      POST one VictoriaMetrics import-format JSON line to
      <scheme>://<host>:8429/api/v1/import (hist.md §5 point 1).

  push_event(event_type, **fields)
      POST one JSON object to <scheme>://<host>:9429/insert/jsonline with the
      envelope of hist.md §4.2. _time and _msg are supplied if not given.

  push_syslog(msgid, severity, **structured_data)
      Send one RFC 5424 message, LF-terminated, over a persistent connection
      to <host>:6514 (hist.md §4.3, §5 point 3). Envelope: APP-NAME=fms,
      HOSTNAME=fms-01, structured-data ID fms@99999, facility user (1).

TLS (hist.md §9.1, digital.md §2.7): passing `ca_cert_path` switches all three
paths from plain HTTP/TCP to HTTPS/TLS, validating hist-01's certificate
against that CA root file, exactly as the real FMS does. `hist_tls_hostname`
(default "hist-01") is the name the certificate's SAN carries and so the name
verification checks against — set independently of `host`, which may be an
IP address, the same way `curl --resolve` separates the connection target
from the name presented for verification. Omitting `ca_cert_path` keeps the
plain-transport behaviour `test_00_pusher_wire_format.py`'s local capture
servers exercise: that module pins the wire format, a property TLS does not
change, so it does not need a certificate authority of its own to test it.

Escape hatches for the malformed-input tests:

  post_raw_metric_line(body)   POST body verbatim to the import endpoint.
  send_raw_syslog(data)        Write bytes verbatim to the syslog connection.

There is no long-lived stub process holding state between tests, only this
client. The syslog connection is opened lazily on first use and held until
close_syslog()/close().
"""

from __future__ import annotations

import json
import socket
import ssl
from datetime import datetime, timezone
from pathlib import Path

import httpx

# RFC 5424 severity codes used by hist.md §4.3.
SEVERITY_WARNING = 4
SEVERITY_NOTICE = 5
SEVERITY_INFORMATIONAL = 6

# Facility for all FMS-emitted messages: user-level (1), per hist.md §4.3's
# envelope. Severity varies per MSGID; the facility does not.
FACILITY_USER = 1

SD_ID = "fms@99999"


def sd_field(name: str) -> str:
    """The VictoriaLogs field name under which structured-data parameter `name`
    is stored.

    VictoriaLogs prefixes every parameter with the structured-data identifier,
    so `username` is stored and queried as `fms@99999.username`. A selector
    naming the bare parameter matches nothing (hist.md §4.3). Defined here
    because the pusher owns the identifier it writes.
    """
    return f"{SD_ID}.{name}"


def _rfc3339_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _escape_sd_value(value: str) -> str:
    """RFC 5424 §6.3.3: backslash-escape '\\', '\"', and ']' in PARAM-VALUE."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("]", "\\]")


class FMSPusher:
    def __init__(
        self,
        host: str,
        *,
        metrics_port: int = 8429,
        jsonline_port: int = 9429,
        syslog_port: int = 6514,
        app_name: str = "fms",
        fms_hostname: str = "fms-01",
        timeout: float = 5.0,
        ca_cert_path: str | Path | None = None,
        hist_tls_hostname: str = "hist-01",
    ) -> None:
        scheme = "https" if ca_cert_path is not None else "http"
        self._import_url = f"{scheme}://{host}:{metrics_port}/api/v1/import"
        self._jsonline_url = f"{scheme}://{host}:{jsonline_port}/insert/jsonline"
        self._syslog_address = (host, syslog_port)
        self._app_name = app_name
        self._fms_hostname = fms_hostname
        self._ca_cert_path = ca_cert_path
        self._hist_tls_hostname = hist_tls_hostname
        self._request_extensions = (
            {"sni_hostname": hist_tls_hostname.encode()}
            if ca_cert_path is not None
            else {}
        )
        verify: bool | ssl.SSLContext = (
            ssl.create_default_context(cafile=str(ca_cert_path))
            if ca_cert_path is not None
            else True
        )
        self._http = httpx.Client(timeout=timeout, verify=verify)
        self._syslog_socket: socket.socket | None = None
        self._timeout = timeout

    # --- Metrics (hist.md §5 point 1) ---

    def push_metric(
        self,
        name: str,
        tank_id: str,
        value: float,
        timestamp: datetime | int | None = None,
        **labels: str,
    ) -> None:
        """Push one sample. timestamp is a datetime or epoch milliseconds;
        omitted means now. Extra keyword arguments become labels (e.g.
        alarm="bund_leak" on atg_alarm_active, §4.1)."""
        if timestamp is None:
            timestamp = datetime.now(timezone.utc)
        if isinstance(timestamp, datetime):
            timestamp = int(timestamp.timestamp() * 1000)
        line = {
            "metric": {"__name__": name, "tank_id": tank_id, **labels},
            "values": [value],
            "timestamps": [timestamp],
        }
        self.post_raw_metric_line(json.dumps(line) + "\n").raise_for_status()

    def post_raw_metric_line(self, body: str) -> httpx.Response:
        return self._http.post(
            self._import_url, content=body, extensions=self._request_extensions
        )

    # --- Structured events (hist.md §4.2, §5 point 2) ---

    def push_event(self, event_type: str, **fields: object) -> None:
        """Push one JSON-line event. _time and _msg (§4.2's envelope) are
        supplied if not passed in fields."""
        event = {
            "_time": _rfc3339_now(),
            "_msg": event_type.replace("_", " "),
            "event_type": event_type,
            **fields,
        }
        response = self._http.post(
            self._jsonline_url,
            content=json.dumps(event) + "\n",
            extensions=self._request_extensions,
        )
        response.raise_for_status()

    # --- Syslog (hist.md §4.3, §5 point 3) ---

    def push_syslog(
        self,
        msgid: str,
        severity: int,
        msg: str | None = None,
        **structured_data: str,
    ) -> None:
        """Send one RFC 5424 message over the persistent TCP connection,
        opening it if needed.

        `msg` defaults to the MSGID in words, which is hist.md §4.3's MSG column
        and matches the `_msg` convention push_event uses. A message with no MSG
        is valid RFC 5424, but VictoriaLogs substitutes a placeholder pointing at
        its own documentation, which is then what every security event reads as
        in Grafana.
        """
        if msg is None:
            msg = msgid.replace("_", " ").replace("-", " ").lower()
        pri = FACILITY_USER * 8 + severity
        sd_params = "".join(
            f' {name}="{_escape_sd_value(str(value))}"'
            for name, value in structured_data.items()
        )
        line = (
            f"<{pri}>1 {_rfc3339_now()} {self._fms_hostname} {self._app_name} "
            f"- {msgid} [{SD_ID}{sd_params}] {msg}\n"
        )
        self.send_raw_syslog(line.encode())

    def send_raw_syslog(self, data: bytes) -> None:
        if self._syslog_socket is None:
            raw = socket.create_connection(self._syslog_address, timeout=self._timeout)
            if self._ca_cert_path is not None:
                context = ssl.create_default_context(cafile=str(self._ca_cert_path))
                self._syslog_socket = context.wrap_socket(
                    raw, server_hostname=self._hist_tls_hostname
                )
            else:
                self._syslog_socket = raw
        self._syslog_socket.sendall(data)

    def close_syslog(self) -> None:
        if self._syslog_socket is not None:
            self._syslog_socket.close()
            self._syslog_socket = None

    # --- Lifecycle ---

    def close(self) -> None:
        self.close_syslog()
        self._http.close()

    def __enter__(self) -> "FMSPusher":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
