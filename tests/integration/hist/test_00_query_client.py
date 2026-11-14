"""
Local pin tests for the harness query clients (stubs/query.py).

Like test_00_pusher_wire_format, these require no Incus infrastructure: the
clients are pure request builders and response parsers, so their command/URL
construction and JSON parsing are pinned here against fakes before the
ingestion modules (§1–§3) trust them to read back what the pusher wrote.

Run locally: python -m pytest tests/integration/hist/test_00_query_client.py
"""

from __future__ import annotations

import json
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import Generator

import pytest

from tests.integration.hist.stubs import query as query_module
from tests.integration.hist.stubs.query import (
    DirectTransport,
    IncusExecTransport,
    LogsClient,
    MetricsClient,
)


class _FakeTransport:
    """Records (path, params) and replays a canned body."""

    def __init__(self, body: str) -> None:
        self.body = body
        self.calls: list[tuple[str, dict[str, str]]] = []

    def request(self, path: str, params: dict[str, str]) -> str:
        self.calls.append((path, dict(params)))
        return self.body


# ---------------------------------------------------------------------------
# MetricsClient — Prometheus /api/v1/query
# ---------------------------------------------------------------------------


def test_metrics_client_builds_query_and_parses_vector_result() -> None:
    body = json.dumps(
        {
            "status": "success",
            "data": {
                "resultType": "vector",
                "result": [
                    {
                        "metric": {
                            "__name__": "atg_tank_volume_litres",
                            "tank_id": "01",
                        },
                        "value": [1752761525, "42350"],
                    }
                ],
            },
        }
    )
    transport = _FakeTransport(body)
    result = MetricsClient(transport).query('atg_tank_volume_litres{tank_id="01"}')

    assert transport.calls == [
        ("/api/v1/query", {"query": 'atg_tank_volume_litres{tank_id="01"}'})
    ]
    assert result[0]["metric"]["tank_id"] == "01"
    assert result[0]["value"][1] == "42350"


def test_metrics_client_passes_time_param() -> None:
    transport = _FakeTransport(
        json.dumps({"status": "success", "data": {"result": []}})
    )
    MetricsClient(transport).query("up", time=1752761525)

    assert transport.calls[0][1] == {"query": "up", "time": "1752761525"}


def test_metrics_client_raises_on_error_status() -> None:
    transport = _FakeTransport(json.dumps({"status": "error", "error": "boom"}))
    with pytest.raises(RuntimeError, match="VictoriaMetrics query failed"):
        MetricsClient(transport).query("up")


# ---------------------------------------------------------------------------
# LogsClient — LogsQL /select/logsql/query (newline-delimited JSON)
# ---------------------------------------------------------------------------


def test_logs_client_builds_query_and_parses_ndjson() -> None:
    body = (
        '{"_time":"2026-07-17T14:12:05Z","event_type":"job_opened","job_id":"abc"}\n'
        '{"_time":"2026-07-17T14:13:00Z","event_type":"job_opened","job_id":"def"}\n'
    )
    transport = _FakeTransport(body)
    rows = LogsClient(transport).query("event_type:job_opened")

    assert transport.calls == [
        ("/select/logsql/query", {"query": "event_type:job_opened"})
    ]
    assert [r["job_id"] for r in rows] == ["abc", "def"]


def test_logs_client_ignores_blank_lines() -> None:
    transport = _FakeTransport('{"a":1}\n\n')
    assert LogsClient(transport).query("*") == [{"a": 1}]


def test_logs_client_passes_limit_param() -> None:
    transport = _FakeTransport("")
    LogsClient(transport).query("*", limit=5)
    assert transport.calls[0][1] == {"query": "*", "limit": "5"}


def test_scrape_fetches_raw_path_with_no_params() -> None:
    """Both clients expose scrape() for reading a raw endpoint (e.g. /metrics)."""
    for client_cls in (MetricsClient, LogsClient):
        transport = _FakeTransport('flag{name="retentionPeriod",value="3y"} 1\n')
        body = client_cls(transport).scrape("/metrics")
        assert transport.calls == [("/metrics", {})]
        assert "retentionPeriod" in body


# ---------------------------------------------------------------------------
# IncusExecTransport — command construction (no real container)
# ---------------------------------------------------------------------------


def test_incus_exec_transport_builds_curl_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, list[str]] = {}

    def fake_run(cmd: list[str], **_: object) -> subprocess.CompletedProcess:
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="RESP", stderr="")

    monkeypatch.setattr(query_module.subprocess, "run", fake_run)
    transport = IncusExecTransport("hist-01", "http://127.0.0.1:8428")
    out = transport.request("/api/v1/query", {"query": "up"})

    assert out == "RESP"
    cmd = captured["cmd"]
    assert cmd[:5] == ["incus", "exec", "hist-01", "--", "curl"]
    assert "http://127.0.0.1:8428/api/v1/query" in cmd
    assert "--get" in cmd
    assert cmd[cmd.index("--data-urlencode") + 1] == "query=up"


def test_incus_exec_transport_raises_on_nonzero_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(cmd: list[str], **_: object) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(
            cmd, 7, stdout="", stderr="curl: (7) refused"
        )

    monkeypatch.setattr(query_module.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="curl: \\(7\\) refused"):
        IncusExecTransport("hist-01", "http://127.0.0.1:8428").request("/x", {})


# ---------------------------------------------------------------------------
# DirectTransport — real GET against a local recorder
# ---------------------------------------------------------------------------


class _GetRecorder:
    """Records the request path of one GET and returns a canned body."""

    def __init__(self, body: str) -> None:
        self.body = body
        self.last_path: str | None = None
        recorder = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:
                pass

            def do_GET(self) -> None:
                recorder.last_path = self.path
                payload = recorder.body.encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port: int = self._server.server_address[1]
        self._thread = Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture()
def get_recorder() -> Generator[_GetRecorder, None, None]:
    recorder = _GetRecorder('{"status":"success","data":{"result":[]}}')
    yield recorder
    recorder.stop()


def test_direct_transport_issues_get_with_params(get_recorder: _GetRecorder) -> None:
    transport = DirectTransport(f"http://127.0.0.1:{get_recorder.port}")
    body = transport.request("/api/v1/query", {"query": "up", "time": "123"})

    assert get_recorder.last_path is not None
    assert get_recorder.last_path.startswith("/api/v1/query?")
    assert "query=up" in get_recorder.last_path
    assert "time=123" in get_recorder.last_path
    assert json.loads(body) == {"status": "success", "data": {"result": []}}
    transport.close()
