"""
Metric ingestion tests (hist-tests.md §1), targeting hist.md §4.1 and §5 point 1.

Requires a deployed hist-01 (Incus): the FMS push simulator writes ATG metrics
over HTTPS to vmagent on hist-01:8429, validating its CA-signed certificate
(hist.md §9.1, digital.md §2.7), and each metric is read back from the loopback
VictoriaMetrics via `incus exec hist-01 -- curl` (hist.md §9.1), provided by the
`hist01_metrics` fixture.

Isolation (§0): every sample is tagged with a unique `test_id` label so a test's
series is distinct from any other's within the shared VictoriaMetrics instance,
and the query selects on that label. Samples are pushed at ingest-time (the
pusher defaults the timestamp to now) so they fall within VictoriaMetrics'
instant-query lookback window; §4.1 fixes the value and labels, not the
timestamp, which is what these tests assert. (N/B 30s latency offset by default)

Run on the VM: python -m pytest tests/integration/hist/test_01_ingestion_metrics.py
"""

from __future__ import annotations

import httpx

from tests.integration.hist.conftest import Config
from tests.integration.hist.poll import poll_until_count
from tests.integration.hist.stubs import tls
from tests.integration.hist.stubs.fms_pusher import FMSPusher
from tests.integration.hist.stubs.query import MetricsClient


def test_metric_push_is_queryable_on_hist01(
    pusher: FMSPusher, hist01_metrics: MetricsClient, unique_id: str
) -> None:
    """§4.1: A pushed atg_tank_volume_litres sample is queryable with its value
    and tank_id label intact."""
    pusher.push_metric("atg_tank_volume_litres", "01", 42350.0, test_id=unique_id)

    series = poll_until_count(
        hist01_metrics,
        f'atg_tank_volume_litres{{tank_id="01",test_id="{unique_id}"}}',
    )

    assert len(series) == 1
    assert series[0]["metric"]["tank_id"] == "01"
    assert float(series[0]["value"][1]) == 42350.0


def test_all_three_atg_metrics_are_ingested(
    pusher: FMSPusher, hist01_metrics: MetricsClient, unique_id: str
) -> None:
    """§4.1: all three ATG metrics land with the labels the table specifies,
    including atg_alarm_active's alarm="bund_leak"."""
    pusher.push_metric("atg_tank_volume_litres", "01", 42350.0, test_id=unique_id)
    pusher.push_metric("atg_tank_temperature_celsius", "01", 18.5, test_id=unique_id)
    pusher.push_metric(
        "atg_alarm_active", "01", 1, test_id=unique_id, alarm="bund_leak"
    )

    volume = poll_until_count(
        hist01_metrics, f'atg_tank_volume_litres{{test_id="{unique_id}"}}'
    )
    temperature = poll_until_count(
        hist01_metrics, f'atg_tank_temperature_celsius{{test_id="{unique_id}"}}'
    )
    alarm = poll_until_count(
        hist01_metrics, f'atg_alarm_active{{test_id="{unique_id}"}}'
    )

    assert volume[0]["metric"]["tank_id"] == "01"
    assert float(volume[0]["value"][1]) == 42350.0

    assert temperature[0]["metric"]["tank_id"] == "01"
    assert float(temperature[0]["value"][1]) == 18.5

    assert alarm[0]["metric"]["tank_id"] == "01"
    assert alarm[0]["metric"]["alarm"] == "bund_leak"
    assert float(alarm[0]["value"][1]) == 1.0


def test_malformed_import_line_is_rejected_without_crashing_vmagent(
    pusher: FMSPusher, hist01_metrics: MetricsClient, unique_id: str
) -> None:
    """§5 point 1: a malformed import line is discarded and vmagent keeps
    ingesting — a subsequent valid push is still queryable.

    vmagent answers 2xx and logs `skipping json line ... cannot parse json line`.
    It does not report the rejection in the response, so the status code says
    nothing about whether the line was understood, test that the malformed line
    neither stops the service nor poisons the stream behind it.
    Confirmed against vmagent 1.148.0 where 4xx was not returned for a malformed
    line.
    """
    response = pusher.post_raw_metric_line("this is not victoriametrics import format")
    assert 200 <= response.status_code < 300, (
        f"vmagent answered {response.status_code} to an unparseable import line; "
        "it is expected to accept the request and skip the line"
    )

    pusher.push_metric("atg_tank_volume_litres", "01", 100.0, test_id=unique_id)
    series = poll_until_count(
        hist01_metrics, f'atg_tank_volume_litres{{test_id="{unique_id}"}}'
    )
    assert float(series[0]["value"][1]) == 100.0


def test_vmagent_presents_ca_signed_certificate(config: Config) -> None:
    """hist.md §9.1/§10.2: vmagent's import endpoint is signed by the RavensPort
    internal CA and carries a DNS SAN of hist-01, independently of whether a push
    happens to succeed — a client that skipped verification would not otherwise
    notice a wrong or expired certificate."""
    pem = tls.certificate_pem(
        None, config.hist_host, config.metrics_push_port, sni="hist-01"
    )
    assert tls.is_signed_by_ravensport_ca(pem), (
        f"vmagent's certificate is not signed by {tls.CA_COMMON_NAME}: "
        f"issuer is {tls.issuer_common_name(pem)}"
    )
    assert tls.has_dns_san(pem, "hist-01"), (
        "vmagent's certificate is missing DNS:hist-01"
    )


def test_plain_http_import_endpoint_is_not_served(config: Config) -> None:
    """hist.md §9.1: vmagent has no unencrypted fallback listener alongside the
    TLS one. The connection is not refused or reset: Go's net/http (vmagent's
    HTTP stack) detects the plaintext request on its TLS listener and answers
    a complete HTTP/1.0 400 response — "Client sent an HTTP request to an
    HTTPS server." — rather than dropping the connection. The property that
    matters is that no data is accepted (confirmed against vmagent 1.148.0)."""
    response = httpx.post(
        f"http://{config.hist_host}:{config.metrics_push_port}/api/v1/import",
        content="",
        timeout=5.0,
    )
    assert response.status_code == 400, (
        f"expected 400 (TLS-required rejection), got {response.status_code}"
    )
