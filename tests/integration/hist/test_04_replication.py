"""
Replication tests (hist-tests.md §4), targeting hist.md §6 — hist-01's vmagent
and vlagent remote-write every record to hist-dmz-01 as well as to their local
stores, so the DMZ replica is an eventually-consistent copy of the primary.

Requires a deployed hist-01 and hist-dmz-01 (Incus). Records are pushed to
hist-01 with the FMS push simulator and read back from hist-dmz-01's stores via
the `histdmz01_metrics` / `histdmz01_logs` fixtures, which use `incus exec`
against the replica's own plain-HTTP loopback listener (hist.md §9.2) — the
runner has no route into the DMZ zone (hist-tests.md §0/§8), so nothing here
is read directly from it.

The two unavailability tests stop and restart hist-dmz-01. The `dmz_control`
fixture guarantees the replica is started again on teardown, so a failing test
never leaves it down for the rest of the suite. Reads from the replica pass
`tolerate_errors=True` so a poll rides out the window in which the replica is
down or booting (see poll.py).

Run on the VM: python -m pytest tests/integration/hist/test_04_replication.py
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Generator

import pytest

from tests.integration.hist.conftest import Config
from tests.integration.hist.poll import poll_until_count
from tests.integration.hist.stubs import tls
from tests.integration.hist.stubs.fms_pusher import (
    SEVERITY_INFORMATIONAL,
    FMSPusher,
    sd_field as _sd,
)
from tests.integration.hist.stubs.query import LogsClient, MetricsClient

# Above VictoriaMetrics' 30-second -search.latencyOffset (hist.md §9.3), which
# both stores carry: a record is not visible to an instant query for that long,
# so a 30-second budget here can never succeed for a metric however promptly it
# replicated.
_REPLICATION_TIMEOUT = 60.0
# Larger budget for the transient-outage tests: the replica must boot and vmagent
# must drain its on-disk buffer before the backlog appears.
_RECOVERY_TIMEOUT = 90.0
# Backlog size for the transient-outage test.
_N = 5

# vlagent RFC 5424 -> VictoriaLogs field name (see test_03 for the full set).
_F_MSGID = "msg_id"


# Services the replica must be answering before this module hands it back. The
# stores are what §5 reads and what replication targets; Grafana is what §6 and
# §7 read.
# victoria-metrics/victoria-logs are probed on their loopback listener, on the
# dedicated port hist-dmz-01 gives it (hist.md §9.2) — distinct from the
# zone-address port hist-01 replicates into, since 0.0.0.0 and 127.0.0.1 cannot
# share one port.
_READINESS_PROBES = (
    ("victoria-metrics", "http://127.0.0.1:18428/health"),
    ("victoria-logs", "http://127.0.0.1:19428/health"),
    ("grafana", "https://127.0.0.1:443/api/health"),
)
_READY_TIMEOUT = 120.0


class _DmzController:
    """Stop/start hist-dmz-01 to simulate a transient replica outage."""

    def __init__(self, node: str) -> None:
        self._node = node

    def stop(self) -> None:
        subprocess.run(
            ["incus", "stop", "--force", self._node],
            capture_output=True,
            text=True,
            check=True,
        )

    def start(self) -> None:
        result = subprocess.run(
            ["incus", "start", self._node], capture_output=True, text=True
        )
        # Idempotent: tolerated so teardown can unconditionally restart.
        if result.returncode != 0 and "already running" not in result.stderr.lower():
            raise RuntimeError(
                f"incus start {self._node} failed: {result.stderr.strip()}"
            )
        self.wait_until_ready()

    def wait_until_ready(self, timeout: float = _READY_TIMEOUT) -> None:
        """Block until the replica answers on every service later tests use.

        `incus start` returns once the instance exists, not once its services
        are listening, so without this the module hands back a replica that is
        still booting. Run alone that is harmless; in a full-suite run it is
        not, because pytest orders modules by name and this one precedes §5, §6
        and §7 — each of which then queries a host that is not there. The
        failures land in those modules and read as faults in them, which is the
        expensive kind of misattribution.
        """
        deadline = time.monotonic() + timeout
        pending = list(_READINESS_PROBES)
        last = ""
        while pending and time.monotonic() < deadline:
            name, url = pending[0]
            probe = subprocess.run(
                [
                    "incus",
                    "exec",
                    self._node,
                    "--",
                    "curl",
                    "--insecure",
                    "--silent",
                    "--fail",
                    "--max-time",
                    "2",
                    "--output",
                    "/dev/null",
                    url,
                ],
                capture_output=True,
                text=True,
            )
            if probe.returncode == 0:
                pending.pop(0)
                continue
            last = f"{name} at {url} (exit {probe.returncode})"
            time.sleep(1.0)
        if pending:
            raise RuntimeError(
                f"{self._node} did not become ready within {timeout}s; still "
                f"waiting on {[name for name, _ in pending]}, last probe {last}"
            )


@pytest.fixture()
def dmz_control(config) -> Generator[_DmzController, None, None]:
    controller = _DmzController(config.hist_dmz_node)
    try:
        yield controller
    finally:
        controller.start()


def test_metric_replicates_to_dmz(
    pusher: FMSPusher, histdmz01_metrics: MetricsClient, unique_id: str
) -> None:
    pusher.push_metric("atg_tank_volume_litres", "01", 42350.0, test_id=unique_id)

    series = poll_until_count(
        histdmz01_metrics,
        f'atg_tank_volume_litres{{test_id="{unique_id}"}}',
        timeout=_REPLICATION_TIMEOUT,
        tolerate_errors=True,
    )
    assert series[0]["metric"]["tank_id"] == "01"
    assert float(series[0]["value"][1]) == 42350.0


def test_event_replicates_to_dmz(
    pusher: FMSPusher, histdmz01_logs: LogsClient, unique_id: str
) -> None:
    pusher.push_event(
        "job_opened",
        job_id=unique_id,
        **{"type": "dispense"},
        operator="jrenard",
        counterparty="MV Harrier",
        grade="MGO",
        atg_volume_at_open_l=42350.0,
        created_at="2026-07-17T14:12:05Z",
    )

    records = poll_until_count(
        histdmz01_logs,
        f'job_id:"{unique_id}"',
        timeout=_REPLICATION_TIMEOUT,
        tolerate_errors=True,
    )
    record = records[0]
    assert record["event_type"] == "job_opened"
    assert record["operator"] == "jrenard"
    assert float(record["atg_volume_at_open_l"]) == 42350.0


def test_syslog_event_replicates_to_dmz_as_native_victorialogs_record(
    pusher: FMSPusher, histdmz01_logs: LogsClient, unique_id: str
) -> None:
    """§6: hist-dmz-01 has no syslog listener — vlagent normalises the syslog
    message to a native VictoriaLogs record before forwarding it."""
    pusher.push_syslog(
        "LOGIN",
        SEVERITY_INFORMATIONAL,
        username="jrenard",
        role="operator",
        session_id="8f2e",
        result="success",
        test_id=unique_id,
    )

    # Structured-data parameters keep their fms@99999 prefix through
    # replication, exactly as on hist-01 (§4.3); the field name is quoted
    # because `@` and `.` are LogsQL syntax.
    records = poll_until_count(
        histdmz01_logs,
        f'"{_sd("test_id")}":"{unique_id}"',
        timeout=_REPLICATION_TIMEOUT,
        tolerate_errors=True,
    )
    record = records[0]
    assert record[_F_MSGID] == "LOGIN"
    assert record[_sd("username")] == "jrenard"
    assert record[_sd("result")] == "success"


def test_replication_survives_transient_dmz_unavailability(
    pusher: FMSPusher,
    histdmz01_metrics: MetricsClient,
    histdmz01_logs: LogsClient,
    dmz_control: _DmzController,
    unique_id: str,
) -> None:
    """With the replica down, a backlog of N metrics and N events is pushed to
    hist-01; after the replica returns, all N of each arrive with none lost —
    validating vmagent/vlagent's on-disk buffer (§6)."""
    dmz_control.stop()
    for i in range(_N):
        pusher.push_metric(
            "atg_tank_volume_litres", "01", 1000.0 + i, test_id=unique_id, seq=str(i)
        )
        pusher.push_event(
            "job_opened",
            job_id=f"{unique_id}-{i}",
            test_id=unique_id,
            seq=str(i),
            **{"type": "dispense"},
            operator="jrenard",
            counterparty="MV Harrier",
            grade="MGO",
            atg_volume_at_open_l=1000.0 + i,
            created_at="2026-07-17T14:12:05Z",
        )
    dmz_control.start()

    metric_series = poll_until_count(
        histdmz01_metrics,
        f'atg_tank_volume_litres{{test_id="{unique_id}"}}',
        _N,
        timeout=_RECOVERY_TIMEOUT,
        tolerate_errors=True,
    )
    assert {s["metric"]["seq"] for s in metric_series} == {str(i) for i in range(_N)}

    event_records = poll_until_count(
        histdmz01_logs,
        f'test_id:"{unique_id}"',
        _N,
        timeout=_RECOVERY_TIMEOUT,
        tolerate_errors=True,
    )
    assert {r["seq"] for r in event_records} == {str(i) for i in range(_N)}


def test_local_ingestion_continues_during_dmz_unavailability(
    pusher: FMSPusher,
    hist01_metrics: MetricsClient,
    dmz_control: _DmzController,
    unique_id: str,
) -> None:
    """While the replica is unreachable, a push to hist-01 still succeeds and is
    queryable locally — the replication buffer must not back-pressure local
    ingestion."""
    dmz_control.stop()

    pusher.push_metric("atg_tank_volume_litres", "01", 42350.0, test_id=unique_id)

    series = poll_until_count(
        hist01_metrics,
        f'atg_tank_volume_litres{{test_id="{unique_id}"}}',
        timeout=_REPLICATION_TIMEOUT,
    )
    assert float(series[0]["value"][1]) == 42350.0


def test_replication_targets_present_ca_signed_certificate(config: Config) -> None:
    """hist.md §6/§9.2: hist-dmz-01's VictoriaMetrics (:8428) and VictoriaLogs
    (:9428) both present certificates signed by the RavensPort internal CA with
    a DNS SAN of hist-dmz-01 — the replication leg is encrypted end to end, not
    only the Grafana instance §7's `grafana_certificates_differ_between_hosts`
    already covers. Each store binds two listeners (§9.2): a plain-HTTP one on
    loopback for Grafana's own local queries, and this TLS one on the zone
    address for hist-01's replication traffic — fetched here from that zone
    address, not loopback, via `incus exec` against the replica itself (§0):
    the runner has no network route into the DMZ zone."""
    for port, label in (
        (config.metrics_query_port, "VictoriaMetrics"),
        (config.logs_query_port, "VictoriaLogs"),
    ):
        pem = tls.certificate_pem(
            config.hist_dmz_node, config.hist_dmz_host, port, sni="hist-dmz-01"
        )
        assert tls.is_signed_by_ravensport_ca(pem), (
            f"hist-dmz-01's {label} certificate is not signed by {tls.CA_COMMON_NAME}: "
            f"issuer is {tls.issuer_common_name(pem)}"
        )
        assert tls.has_dns_san(pem, "hist-dmz-01"), (
            f"hist-dmz-01's {label} certificate is missing DNS:hist-dmz-01"
        )


def test_plain_http_replication_endpoints_are_not_served(config: Config) -> None:
    """hist.md §9.2: the zone-address listener each store binds for hist-01's
    replication traffic has no unencrypted fallback — a firewall
    misconfiguration that let OT traffic bypass the intended HTTPS port would
    still find no working plaintext endpoint behind it. Checked against the
    zone address, not loopback: loopback legitimately answers plain HTTP on
    these same ports, for Grafana's own local queries (§7), so asserting
    refusal there would be wrong under the current design rather than a
    regression guard. Checked via `incus exec` against the replica itself
    (§0): the runner has no network route into the DMZ zone."""
    for port, path in (
        (config.metrics_query_port, "/api/v1/write"),
        (config.logs_query_port, "/insert/native"),
    ):
        result = subprocess.run(
            [
                "incus",
                "exec",
                config.hist_dmz_node,
                "--",
                "curl",
                "-fsS",
                "--max-time",
                "5",
                f"http://{config.hist_dmz_host}:{port}{path}",
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0, (
            f"plain HTTP {path} on hist-dmz-01:{port} was served: {result.stdout!r}"
        )
