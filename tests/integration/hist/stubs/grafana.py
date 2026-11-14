"""
Grafana authentication probe for the log server test harness (hist-tests.md §6).

Each log server runs a Grafana instance terminating TLS on port 443 with a
self-signed certificate (hist.md §7). hist-01's holds a single seeded admin
account (identities.hist_admin); hist-dmz-01's holds the seeded reconciliation
user (identities.reconciliation_user). The two instances have independent,
non-shared internal user stores, so a credential valid on one is rejected by the
other.

These clients test *authentication* only: whether a username/password is
accepted by a given Grafana instance. They probe an authenticated endpoint
(`/api/user`, which requires a logged-in user) with HTTP basic auth and return
the resulting status code — 200 when the credential is accepted, 401 when it is
rejected. Unlike the store query clients (query.py), a rejection is an *expected*
outcome, so these return the status code rather than raising on it.

hist-01's Grafana is reached from inside the container via
`incus exec hist-01 -- curl -k https://127.0.0.1`, consistent with the loopback
store-query path (query.py): this keeps §6 focused on authentication and leaves
network reachability from an OT host to hist-tests.md §7. hist-dmz-01's Grafana
is reached directly over HTTPS at its zone address. The self-signed certificate
is not validated here (curl -k / httpx verify=False); certificate provisioning
is exercised in §7.

ASSUMPTION (calibrate on the first VM run): the probe path `/api/user` is an
authenticated Grafana endpoint that returns 200 with a valid basic-auth
credential and 401 without one. If a different endpoint or status is more
robust, adjust _PROBE_PATH / _ACCEPTED / _REJECTED below — a one-place fix.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any, Protocol

import httpx


# These will need to be confirmed once grafana is plumbed in
_PROBE_PATH = "/api/user"
_ACCEPTED = 200
_REJECTED = 401


class GrafanaAuth(Protocol):
    def login_status(self, username: str, password: str) -> int:
        """Return the HTTP status from a basic-auth probe of an authenticated
        endpoint: _ACCEPTED (200) if the credential is accepted, _REJECTED
        (401) if not."""
        ...

    def get_json(self, path: str, username: str, password: str) -> Any:
        """Authenticated GET of a Grafana API path, returning the parsed JSON
        body. Raises on any non-success status (§7 datasource enumeration —
        unlike login_status, a failure here is a fault, not an expected
        outcome)."""
        ...


class DirectGrafanaAuth:
    """Probe a Grafana instance bound to a network address (hist-dmz-01)."""

    def __init__(self, base_url: str, *, timeout: float = 10.0) -> None:
        self._base_url = base_url.rstrip("/")
        # verify=False: the certificate is self-signed and untrusted by design;
        # certificate provisioning is exercised separately in §7.
        self._client = httpx.Client(timeout=timeout, verify=False)

    def login_status(self, username: str, password: str) -> int:
        response = self._client.get(
            self._base_url + _PROBE_PATH, auth=(username, password)
        )
        return response.status_code

    def get_json(self, path: str, username: str, password: str) -> Any:
        response = self._client.get(self._base_url + path, auth=(username, password))
        response.raise_for_status()
        return response.json()

    def close(self) -> None:
        self._client.close()


class IncusExecGrafanaAuth:
    """Probe a Grafana instance from inside its container (hist-01)."""

    def __init__(self, node: str, base_url: str, *, timeout: float = 10.0) -> None:
        self._node = node
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    def login_status(self, username: str, password: str) -> int:
        cmd = [
            "incus",
            "exec",
            self._node,
            "--",
            "curl",
            "--insecure",
            "--silent",
            "--output",
            "/dev/null",
            "--write-out",
            "%{http_code}",
            "--user",
            f"{username}:{password}",
            self._base_url + _PROBE_PATH,
        ]
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=self._timeout
        )
        code = result.stdout.strip()
        # curl writes the HTTP status even for 401 (no --fail). It writes "000"
        # when the request never completed at all, which is a real fault rather
        # than a rejection — and "000".isdigit() is True, so it has to be
        # excluded explicitly. Returning 0 here makes the caller compare it
        # against 200/401 and report that the instance accepted or rejected a
        # credential when in fact nothing answered.
        if not code.isdigit() or int(code) == 0:
            raise RuntimeError(
                f"incus exec curl on {self._node} did not complete "
                f"(exit {result.returncode}, code {code!r}): {result.stderr.strip()}"
            )
        return int(code)

    def get_json(self, path: str, username: str, password: str) -> Any:
        cmd = [
            "incus",
            "exec",
            self._node,
            "--",
            "curl",
            "--insecure",
            "--silent",
            "--show-error",
            "--fail",
            "--user",
            f"{username}:{password}",
            self._base_url + path,
        ]
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=self._timeout
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"incus exec curl on {self._node} failed "
                f"(exit {result.returncode}): {result.stderr.strip()}"
            )
        return json.loads(result.stdout)

    def close(self) -> None:
        pass
