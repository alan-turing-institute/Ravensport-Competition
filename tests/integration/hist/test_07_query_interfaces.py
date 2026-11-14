"""
Datasource and query-reachability tests (hist-tests.md §7), targeting hist.md §7.

Two concerns:

  - Datasource provisioning (tests 1–2): each host's Grafana is provisioned at
    image-build time with exactly two datasources — a Prometheus-compatible one
    pointed at the local VictoriaMetrics, which implements the Prometheus HTTP
    API, and a VictoriaLogs one pointed at the local VictoriaLogs. On hist-01
    that's 127.0.0.1:8428/:9428 directly; on hist-dmz-01, each store answers a
    second, TLS-only listener on the zone address for hist-01's replication
    traffic, and 0.0.0.0/127.0.0.1 cannot share one port, so its datasources
    address a dedicated loopback pair instead, 127.0.0.1:18428/:19428 (hist.md
    §9.2). Enumerated over Grafana's /api/datasources as the host's own seeded
    user (hist-01 admin; hist-dmz-01 reconciliation user).

  - TLS reachability and certificate distinctness (tests 3–4): Grafana serves
    HTTPS on 443 with a certificate signed by the RavensPort internal CA,
    reachable from any OT host, and the two hosts hold distinct keypairs
    (hist.md §10.2/§10.3). Test 3 reaches hist-01 from hmi-01 (both on vnet-ot),
    by hostname — hmi-01's own `resolve: [hist-01]` entry, load-bearing now that
    the certificate carries a hostname-only SAN — and confirms the handshake and
    the CA signature; test 4 compares the two presented certificates.

Requires deployed hist-01, hist-dmz-01, and hmi-01 (Incus). The datasource reads
reuse the Grafana clients (stubs/grafana.py); the TLS checks drive openssl inside
the containers via incus exec (stubs/tls.py).

Run on the VM: python -m pytest tests/integration/hist/test_07_query_interfaces.py
"""

from __future__ import annotations

from tests.integration.hist.conftest import Config, SeededCredentials
from tests.integration.hist.stubs import tls
from tests.integration.hist.stubs.grafana import GrafanaAuth, IncusExecGrafanaAuth


_PLUGIN_ID = "victoriametrics-logs-datasource"
_VALID_SIGNATURE = "valid"


def _assert_logs_plugin_loaded(
    grafana: GrafanaAuth, username: str, password: str, node: str
) -> None:
    """Assert Grafana has loaded the VictoriaLogs datasource plugin.

    Provisioning alone does not establish this: the `type` in
    /api/datasources is copied from the provisioning file, so a datasource whose
    plugin Grafana rejected is still listed and the tests below still pass. The
    plugin is signed (hist.md §2.1), and Grafana refuses to load a plugin whose
    signature does not verify — so its presence here with a valid signature is
    what shows the archive was installed intact and no unsigned-plugin allowance
    is needed in grafana.ini.
    """
    plugins = grafana.get_json("/api/plugins", username, password)
    matching = [p for p in plugins if p.get("id") == _PLUGIN_ID]
    assert matching, (
        f"{node} Grafana has not loaded {_PLUGIN_ID}; the VictoriaLogs "
        f"datasource would be listed but every query through it would fail. "
        f"Loaded datasource plugins: "
        f"{sorted(p.get('id') for p in plugins if p.get('type') == 'datasource')}"
    )
    signature = matching[0].get("signature")
    assert signature == _VALID_SIGNATURE, (
        f"{node} loaded {_PLUGIN_ID} with signature {signature!r}, expected "
        f"{_VALID_SIGNATURE!r} — the archive was altered in transit or unpacking, "
        "or Grafana is accepting it under an unsigned-plugin allowance"
    )


def test_hist01_victorialogs_plugin_is_loaded(
    hist01_grafana: IncusExecGrafanaAuth, world_credentials: SeededCredentials
) -> None:
    cred = world_credentials.hist_admin
    _assert_logs_plugin_loaded(hist01_grafana, cred.username, cred.password, "hist-01")


def test_histdmz01_victorialogs_plugin_is_loaded(
    histdmz01_grafana: GrafanaAuth, world_credentials: SeededCredentials
) -> None:
    cred = world_credentials.reconciliation_user
    _assert_logs_plugin_loaded(
        histdmz01_grafana, cred.username, cred.password, "hist-dmz-01"
    )


def _assert_two_local_datasources(
    datasources: list[dict], metrics_port: int, logs_port: int
) -> None:
    """Assert the provisioned datasource set is exactly the two local stores
    (hist.md §7): VictoriaMetrics on the metrics query port and VictoriaLogs on
    the logs query port, both on loopback. hist-dmz-01's ports differ from
    hist-01's: each of its stores answers a second, TLS-only listener on the
    zone address for hist-01's replication traffic, and 0.0.0.0/127.0.0.1
    cannot share one port, so its loopback pair moves to dedicated ports
    (hist.md §9.2)."""
    metrics_url = f"http://127.0.0.1:{metrics_port}"
    logs_url = f"http://127.0.0.1:{logs_port}"

    urls = sorted(ds["url"] for ds in datasources)
    assert len(datasources) == 2, (
        f"expected exactly two provisioned datasources, got {len(datasources)}: {urls}"
    )
    assert set(urls) == {metrics_url, logs_url}, (
        f"datasource urls {urls} != expected {sorted((metrics_url, logs_url))}"
    )

    by_url = {ds["url"]: ds for ds in datasources}
    metrics_type = by_url[metrics_url]["type"].lower()
    logs_type = by_url[logs_url]["type"].lower()
    assert "prometheus" in metrics_type or "victoriametrics" in metrics_type, (
        f"datasource at {metrics_url} has type {metrics_type!r}, "
        f"expected a Prometheus-compatible metrics datasource"
    )
    assert "logs" in logs_type or "victorialogs" in logs_type, (
        f"datasource at {logs_url} has type {logs_type!r}, "
        f"expected a VictoriaLogs datasource"
    )


def test_hist01_datasources_are_provisioned(
    hist01_grafana: IncusExecGrafanaAuth,
    world_credentials: SeededCredentials,
    config: Config,
) -> None:
    cred = world_credentials.hist_admin
    datasources = hist01_grafana.get_json(
        "/api/datasources", cred.username, cred.password
    )
    _assert_two_local_datasources(
        datasources, config.metrics_query_port, config.logs_query_port
    )


def test_histdmz01_datasources_are_provisioned(
    histdmz01_grafana: GrafanaAuth,
    world_credentials: SeededCredentials,
    config: Config,
) -> None:
    cred = world_credentials.reconciliation_user
    datasources = histdmz01_grafana.get_json(
        "/api/datasources", cred.username, cred.password
    )
    _assert_two_local_datasources(
        datasources,
        config.histdmz01_metrics_query_port,
        config.histdmz01_logs_query_port,
    )


def test_grafana_reachable_over_https_from_another_ot_host(config: Config) -> None:
    """From hmi-01 (an OT host, vnet-ot), hist-01's Grafana is reachable over
    HTTPS by hostname and presents a certificate signed by the RavensPort
    internal CA — hist.md §7's "any other OT host" reachability. Reached by
    name, not IP: the certificate's SAN is hostname-only (digital.md §2.7), so
    this also confirms hmi-01's own `resolve: [hist-01]` entry resolves it
    unaided, the same way a human's browser would. This asserts network
    reachability and TLS only; the HMI application itself never calls the log
    server (§9)."""
    output = tls.s_client_output(
        config.hmi_node, config.hist_reach_target, config.grafana_port
    )
    assert tls.has_certificate(output), (
        f"hmi-01 did not complete a TLS handshake to hist-01 at "
        f"{config.hist_reach_target}:{config.grafana_port} — no certificate "
        f"returned:\n{output}"
    )
    pem = tls.certificate_pem(
        config.hmi_node, config.hist_reach_target, config.grafana_port
    )
    assert tls.is_signed_by_ravensport_ca(pem), (
        f"hist-01's certificate did not validate against {tls.CA_COMMON_NAME} "
        f"from hmi-01: issuer is {tls.issuer_common_name(pem)}"
    )


def test_grafana_certificates_differ_between_hosts(config: Config) -> None:
    """The two log servers must hold distinct keypairs (hist.md §10.2/§10.3): one
    certificate reused across both images would be a build-time leak. Fetch each
    host's presented certificate and assert the fingerprints differ."""
    hist_pem = tls.certificate_pem(config.hist_node, "127.0.0.1", config.grafana_port)
    dmz_pem = tls.certificate_pem(
        config.hist_dmz_node, "127.0.0.1", config.grafana_port
    )
    assert tls.fingerprint(hist_pem) != tls.fingerprint(dmz_pem), (
        "hist-01 and hist-dmz-01 present the same TLS certificate — the keypair "
        "was reused across images rather than generated per host"
    )
