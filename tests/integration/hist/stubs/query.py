"""
Query clients for the log server test harness (hist-tests.md §0).

hist-01 does not expose its stores to the network: VictoriaMetrics and
VictoriaLogs bind loopback only (hist.md §9.1), read in production solely by
the Grafana process co-located on the same host (external operators reach that
data over Grafana's HTTPS interface, exercised separately in hist-tests.md §7).
To confirm ingestion directly, the test runner reads each store's query API
from inside the container via `incus exec hist-01 -- curl 127.0.0.1:...`,
which avoids exposing the store or standing up a tunnel. This is a
verification mechanism for the tests, not a production access path.

hist-dmz-01's stores bind the zone address (hist.md §9.2), because those ports
double as the replication inbound endpoint, and so are queried directly over
HTTP. Both transports present the same `request` interface, so
MetricsClient/LogsClient are agnostic to which host they target.

Query paths: VictoriaMetrics uses the Prometheus HTTP API `/api/v1/query`
(hist-tests.md §1); VictoriaLogs uses the LogsQL HTTP API
`/select/logsql/query` (VictoriaLogs default — hist-tests.md does not pin the
path).
"""

from __future__ import annotations

import json
import subprocess
from typing import Protocol

import httpx


class QueryTransport(Protocol):
    def request(self, path: str, params: dict[str, str]) -> str:
        """GET `path` with URL-encoded `params`, returning the body as text."""
        ...


class DirectTransport:
    """Query a store bound to a network address (hist-dmz-01, hist.md §9.2)."""

    def __init__(self, base_url: str, *, timeout: float = 10.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = httpx.Client(timeout=timeout)

    def request(self, path: str, params: dict[str, str]) -> str:
        response = self._client.get(self._base_url + path, params=params)
        response.raise_for_status()
        return response.text

    def close(self) -> None:
        self._client.close()


class IncusExecTransport:
    """Query a loopback-bound store from inside its container (hist-01, §9.1)."""

    def __init__(self, node: str, base_url: str, *, timeout: float = 10.0) -> None:
        self._node = node
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    def request(self, path: str, params: dict[str, str]) -> str:
        cmd = [
            "incus",
            "exec",
            self._node,
            "--",
            "curl",
            "--silent",
            "--show-error",
            "--fail",
            "--get",
            self._base_url + path,
        ]
        for key, value in params.items():
            cmd += ["--data-urlencode", f"{key}={value}"]
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=self._timeout
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"incus exec curl on {self._node} failed "
                f"(exit {result.returncode}): {result.stderr.strip()}"
            )
        return result.stdout

    def close(self) -> None:
        pass


class MetricsClient:
    """VictoriaMetrics query client (Prometheus HTTP API)."""

    def __init__(self, transport: QueryTransport) -> None:
        self._transport = transport

    def query(self, promql: str, *, time: int | None = None) -> list[dict]:
        """Instant query via `/api/v1/query`. Returns the `result` array (one
        entry per series); an empty list if nothing matched."""
        params = {"query": promql}
        if time is not None:
            params["time"] = str(time)
        payload = json.loads(self._transport.request("/api/v1/query", params))
        if payload.get("status") != "success":
            raise RuntimeError(f"VictoriaMetrics query failed: {payload}")
        return payload["data"]["result"]

    def scrape(self, path: str = "/metrics") -> str:
        """Fetch a raw endpoint (e.g. /metrics) as text, bypassing query parsing.
        Used to read the store's exposed configuration (hist-tests.md §5)."""
        return self._transport.request(path, {})


class LogsClient:
    """VictoriaLogs query client (LogsQL HTTP API)."""

    def __init__(self, transport: QueryTransport) -> None:
        self._transport = transport

    def query(self, logsql: str, *, limit: int | None = None) -> list[dict]:
        """Query via `/select/logsql/query`. Returns one dict per matching log
        record (the response body is newline-delimited JSON objects)."""
        params = {"query": logsql}
        if limit is not None:
            params["limit"] = str(limit)
        body = self._transport.request("/select/logsql/query", params)
        return [json.loads(line) for line in body.splitlines() if line.strip()]

    def scrape(self, path: str = "/metrics") -> str:
        """Fetch a raw endpoint (e.g. /metrics) as text, bypassing query parsing.
        Used to read the store's exposed configuration (hist-tests.md §5)."""
        return self._transport.request(path, {})
