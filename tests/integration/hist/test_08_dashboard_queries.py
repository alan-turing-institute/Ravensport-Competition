"""
Provisioned-dashboard queries against the live stores (hist.md §7.1)

This module closes the gap the unit suite structurally cannot.

`deploy/tests/unit/test_hist_grafana_dashboards.py` guards each dashboard's
queries against a vocabulary transcribed into the test module — §4.1's names for
`hist-dmz-01`, and for `hist-01` the names the four pinned products emit, read off
the deployed host's `/metrics` endpoints. That guard compares two files in the
repository. It catches a panel naming a series the transcription does not hold,
and it cannot catch the transcription itself being wrong: a product renaming a
series across a §2.1 version bump would be corrected in the panel and the
transcription together, they would agree, the unit suite would stay green, and the
panel would render empty on the deployed host.

Nothing in the repository can detect that, because the authority is the running
product. So the check has to be made here, where one exists.

Two properties, per host:

  1. The dashboard §7.1 specifies can be retrieved by the account that exists to
     use it. Asked of the HTTP API rather than inferred from a journal line:
     Grafana's provisioning messages report on its own reconciler and say nothing
     about whether a given dashboard was accepted. Nor is any table the right
     place to look — Grafana 12.4 serves dashboards from unified storage, so which
     one holds them changes with a version, whereas retrievability is the property
     the account depends on.

  2. Every metric query the deployed dashboard carries returns at least one series
     from the store it is bound to. The expressions are read out of the deployed
     dashboard rather than the rendered file, so a stale image fails here instead
     of being tested through a copy it does not contain.

Property 2 is the one with teeth. An empty result is what a renamed series, a
mistyped label, a label whose value is not what was assumed, or a scrape that is
not collecting all produce — and it is what the panel would show the administrator
with no other indication that anything is wrong.

Run on the VM: python -m pytest tests/integration/hist/test_08_dashboard_queries.py
"""

from __future__ import annotations

import time

from tests.integration.hist.conftest import Credential, SeededCredentials
from tests.integration.hist.poll import poll_until_count
from tests.integration.hist.stubs.fms_pusher import FMSPusher
from tests.integration.hist.stubs.grafana import IncusExecGrafanaAuth
from tests.integration.hist.stubs.query import MetricsClient

# hist.md §7.1's two dashboards, by the uid render.py provisions them under.
_HIST01_UID = "ravensport-log-server-ops"
_HISTDMZ01_UID = "ravensport-bunkering-ops"

_PROMETHEUS = "prometheus"

# §6.1 scrapes every 60 seconds and hist.md §9.3 leaves VictoriaMetrics'
# -search.latencyOffset at its 30-second default, so a scraped series is
# queryable up to 90 seconds after the process exposing it started. The shared
# helper's 45-second default is sized for a push, which is prompt by comparison,
# and would fail here against a host that is collecting correctly.
_SCRAPE_VISIBILITY_TIMEOUT = 150.0


def _returns_series(
    client: MetricsClient, expr: str, timeout: float = _SCRAPE_VISIBILITY_TIMEOUT
) -> bool:
    """Whether `expr` resolves to at least one series within `timeout`.

    Polls directly rather than through `poll_until_count`, which reports a timeout
    by failing the test. Here an empty result is the finding to collect, not the
    end of the run: reporting every empty panel at once is what makes a version
    bump one edit rather than one per re-run.
    """
    deadline = time.monotonic() + timeout
    while True:
        if client.query(expr):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(1.0)


def _dashboard(grafana: IncusExecGrafanaAuth, uid: str, credential: Credential) -> dict:
    """The dashboard as the deployed instance holds it."""
    payload = grafana.get_json(
        f"/api/dashboards/uid/{uid}", credential.username, credential.password
    )
    assert isinstance(payload, dict) and "dashboard" in payload, (
        f"GET /api/dashboards/uid/{uid} did not return a dashboard: {payload!r}. "
        f"hist.md §7.1 provisions it at image-build time, and the build's own "
        f"check polls this same path before publishing the image"
    )
    return payload["dashboard"]


def _metric_queries(dashboard: dict) -> list[tuple[str, str]]:
    """Every (panel title, expression) pair bound to the metrics datasource."""
    queries = []
    for panel in dashboard.get("panels", []):
        for target in panel.get("targets", []):
            source = target.get("datasource") or {}
            if source.get("type") != _PROMETHEUS:
                continue
            expr = target.get("expr")
            assert expr, f"panel {panel.get('title')!r} has a target with no expr"
            queries.append((panel.get("title", "?"), expr))
    return queries


def _instant(expr: str) -> str:
    """A panel expression made safe to evaluate as an instant query.

    Grafana substitutes `$__rate_interval` and friends when it sends a query; an
    expression carrying one would fail here for a reason that has nothing to do
    with the series it names. None of the current panels use them, so this asserts
    that rather than silently passing a broken expression to the store.
    """
    assert "$" not in expr, (
        f"expression carries a Grafana variable this test cannot substitute: "
        f"{expr!r}. Substitute it here rather than dropping the assertion"
    )
    return expr


# ── hist-01: the operations dashboard ─────────────────────────────────────────


def test_hist01_dashboard_is_retrievable(
    hist01_grafana: IncusExecGrafanaAuth, world_credentials: SeededCredentials
) -> None:
    """§7.1 gives `hist-01` one dashboard and §7 gives it one account. A
    provisioning failure leaves Grafana serving normally with nothing on it."""
    dashboard = _dashboard(hist01_grafana, _HIST01_UID, world_credentials.hist_admin)
    assert dashboard["title"] == "Log server operations"
    assert dashboard["panels"], f"{_HIST01_UID} carries no panels"


def test_hist01_dashboard_queries_return_data(
    hist01_grafana: IncusExecGrafanaAuth,
    hist01_metrics: MetricsClient,
    world_credentials: SeededCredentials,
) -> None:
    """Every panel on the operations dashboard resolves against the live store.

    This is the assertion the unit suite cannot make. Each expression names a
    series emitted by one of §2.1's four pinned products and collected by §6.1's
    scrape, and an empty result is indistinguishable from a working panel until
    somebody looks at it. The failures it catches:

      - a product renaming a series across a version bump, which the repository's
        transcription would follow into the panel without either noticing;
      - `url="1:secret-url"` or `type="promremotewrite"` not being the label value
        assumed, which would leave a correct metric name matching nothing;
      - the scrape configuration not reaching the endpoint that exposes the
        series, which is the state this whole work item began in.
    """
    dashboard = _dashboard(hist01_grafana, _HIST01_UID, world_credentials.hist_admin)
    queries = _metric_queries(dashboard)
    assert queries, f"{_HIST01_UID} carries no metric queries to check"

    empty = [
        (title, expr)
        for title, expr in queries
        if not _returns_series(hist01_metrics, _instant(expr))
    ]

    assert not empty, (
        "these panels on hist-01 would render empty against the live store:\n"
        + "\n".join(f"  {title!r}: {expr}" for title, expr in empty)
        + "\n\nThe series named are the four pinned products' own, so the "
        "authority is the running product rather than hist.md. Re-read the "
        "/metrics endpoints on this host and reconcile what they expose with "
        "_SELF_MONITORING_METRICS in "
        "deploy/tests/unit/test_hist_grafana_dashboards.py and the queries in "
        "render.py's _operations_panels."
    )


def test_hist01_scrape_reaches_all_four_components(
    hist01_metrics: MetricsClient,
) -> None:
    """§6.1 scrapes all four local endpoints, one job per component.

    Asserted separately from the panels above because the dashboard does not name
    every job: a scrape configuration that had lost one of the four would still
    satisfy most panels, and the gap would only appear as one missing line in the
    uptime legend. `vm_app_uptime_seconds` is exposed by all four components under
    the same name, so the `job` label is what distinguishes them and this is the
    one query that sees the whole set.
    """
    series = poll_until_count(
        hist01_metrics,
        "vm_app_uptime_seconds",
        count=4,
        timeout=_SCRAPE_VISIBILITY_TIMEOUT,
    )
    jobs = {entry["metric"].get("job") for entry in series}
    expected = {"victoria-metrics", "vmagent", "victoria-logs", "vlagent"}
    assert expected <= jobs, (
        f"hist-01 is not scraping {sorted(expected - jobs)}; §6.1 specifies all "
        f"four local endpoints. Collected: {sorted(j for j in jobs if j)}"
    )


def test_hist01_self_monitoring_does_not_reach_the_replica(
    histdmz01_metrics: MetricsClient,
) -> None:
    """§6.1: the scraped series are the one exception to §6's replication and are
    held on `hist-01` alone.

    The containment is structural — `victoria-metrics` scrapes and has no
    remote-write, so there is no path — and a unit test asserts that placement in
    the build script. This asserts the outcome on the deployed pair, which is what
    would actually change if the flag moved to `vmagent`, whose every sample goes
    to both of §6's targets.
    """
    leaked = histdmz01_metrics.query("vm_app_uptime_seconds")
    assert not leaked, (
        f"hist-dmz-01 holds {len(leaked)} vm_app_uptime_seconds series, so "
        f"hist-01's internal telemetry is crossing into the demilitarised zone. "
        f"§6.1 places the scrape on victoria-metrics precisely so that it cannot: "
        f"check whether -promscrape.config has moved to vmagent"
    )


# ── hist-dmz-01: the bunkering dashboard ──────────────────────────────────────


def test_histdmz01_dashboard_is_retrievable(
    histdmz01_grafana: IncusExecGrafanaAuth, world_credentials: SeededCredentials
) -> None:
    """The reconciliation user meets this dashboard once a month
    (narrative.md:161), which is also how long a provisioning failure would take
    to surface without this."""
    dashboard = _dashboard(
        histdmz01_grafana, _HISTDMZ01_UID, world_credentials.reconciliation_user
    )
    assert dashboard["title"] == "Bunkering operations"
    assert dashboard["panels"], f"{_HISTDMZ01_UID} carries no panels"


def test_histdmz01_dashboard_queries_return_data(
    histdmz01_grafana: IncusExecGrafanaAuth,
    histdmz01_metrics: MetricsClient,
    world_credentials: SeededCredentials,
    pusher: FMSPusher,
    unique_id: str,
) -> None:
    """The bunkering dashboard's metric panel resolves against the replica.

    A reading is pushed first rather than relying on whatever earlier modules left
    behind: the panel plots `atg_tank_volume_litres` over the selected period, and
    a store with no ATG history would fail this for a reason that is not a defect
    in the dashboard.
    """
    pusher.push_metric("atg_tank_volume_litres", "01", 41200.0, test_id=unique_id)

    dashboard = _dashboard(
        histdmz01_grafana, _HISTDMZ01_UID, world_credentials.reconciliation_user
    )
    queries = _metric_queries(dashboard)
    assert queries, f"{_HISTDMZ01_UID} carries no metric queries to check"

    for title, expr in queries:
        series = poll_until_count(histdmz01_metrics, _instant(expr))
        assert series, f"panel {title!r} returned no series for: {expr}"
