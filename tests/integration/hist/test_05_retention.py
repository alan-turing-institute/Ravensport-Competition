"""
Retention configuration tests (hist-tests.md §5), targeting hist.md §8.

A 3-year retention period cannot be exercised by elapsed time in a test run, so
these tests confirm the configuration took effect on the running stores, not the
behaviour at the 3-year boundary. Each store exposes its command-line flags on
its /metrics endpoint as `flag{name="...",value="..."}` samples; the tests read
the retentionPeriod flag from all four stores (VictoriaMetrics and VictoriaLogs
on hist-01 and hist-dmz-01) and assert it is 3y, guarding against one host's
template drifting from the other's.

ASSUMPTION (calibrate on the first VM run): retention is read from the
`flag{name="retentionPeriod",value="..."}` sample on /metrics, with value the
string "3y" as configured (hist.md §9 service args). If VictoriaMetrics /
VictoriaLogs normalise the value (e.g. to months) or expose it differently,
adjust _RETENTION_FLAG / _EXPECTED_RETENTION / _retention_from_metrics below —
a one-place fix.

Run on the VM: python -m pytest tests/integration/hist/test_05_retention.py
"""

from __future__ import annotations

import re

from tests.integration.hist.stubs.query import LogsClient, MetricsClient

_RETENTION_FLAG = "retentionPeriod"
_EXPECTED_RETENTION = "3y"
_FLAG_VALUE = re.compile(r'value="([^"]*)"')


def _retention_from_metrics(metrics_text: str) -> str | None:
    """Return the retentionPeriod flag value from a store's /metrics text, or
    None if no such flag sample is present."""
    for line in metrics_text.splitlines():
        if line.startswith("flag{") and f'name="{_RETENTION_FLAG}"' in line:
            match = _FLAG_VALUE.search(line)
            if match:
                return match.group(1)
    return None


def test_victoriametrics_retention_period_is_3y_on_both_hosts(
    hist01_metrics: MetricsClient, histdmz01_metrics: MetricsClient
) -> None:
    for name, client in (
        ("hist-01", hist01_metrics),
        ("hist-dmz-01", histdmz01_metrics),
    ):
        retention = _retention_from_metrics(client.scrape("/metrics"))
        assert retention == _EXPECTED_RETENTION, (
            f"{name} VictoriaMetrics retentionPeriod is {retention!r}, "
            f"expected {_EXPECTED_RETENTION!r}"
        )


def test_victorialogs_retention_period_is_3y_on_both_hosts(
    hist01_logs: LogsClient, histdmz01_logs: LogsClient
) -> None:
    for name, client in (
        ("hist-01", hist01_logs),
        ("hist-dmz-01", histdmz01_logs),
    ):
        retention = _retention_from_metrics(client.scrape("/metrics"))
        assert retention == _EXPECTED_RETENTION, (
            f"{name} VictoriaLogs retentionPeriod is {retention!r}, "
            f"expected {_EXPECTED_RETENTION!r}"
        )
