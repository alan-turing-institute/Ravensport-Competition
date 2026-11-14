"""
Session- and function-scoped fixtures for the log server integration suite
(`hist-01` primary, OT zone; `hist-dmz-01` read-only replica, DMZ), per
hist-tests.md §0.

Environment variables (with defaults):

  HIST_NODE_NAME         Incus node name for hist-01           (default: hist-01)
  HIST_DMZ_NODE_NAME     Incus node name for hist-dmz-01       (default: hist-dmz-01)
  HIST_HOST              hist-01 address on vnet-ot            (default: 10.40.0.11)
  HIST_DMZ_HOST          hist-dmz-01 address on vnet-dmz       (default: 10.20.0.20)
  HIST_METRICS_PUSH_PORT vmagent import-format inbound port    (default: 8429)
  HIST_EVENT_PUSH_PORT   vlagent JSON-line inbound port        (default: 9429)
  HIST_SYSLOG_PORT       vlagent syslog TLS inbound port       (default: 6514)
  HIST_METRICS_QUERY_PORT VictoriaMetrics query port, hist-01  (default: 8428)
  HIST_LOGS_QUERY_PORT   VictoriaLogs query port, hist-01      (default: 9428)
  HIST_DMZ_METRICS_QUERY_PORT VictoriaMetrics loopback query port, hist-dmz-01
                         — distinct from HIST_METRICS_QUERY_PORT because that
                         host's store answers a second, TLS-only listener on
                         8428 for hist-01's replication traffic, and 0.0.0.0/
                         127.0.0.1 cannot share one port (hist.md §9.2)
                         (default: 18428)
  HIST_DMZ_LOGS_QUERY_PORT VictoriaLogs loopback query port, hist-dmz-01,
                         for the same reason                    (default: 19428)
  HIST_GRAFANA_PORT      Grafana HTTPS port                    (default: 443)
  HIST_WORLD_YAML        Path to world.yaml (seeded credentials) (default:
                         <repo>/deploy/world.yaml)
  HIST_HMI_NODE          OT host used as the §7 reachability source (default:
                         hmi-01)
  HIST_REACH_TARGET      hostname hmi-01 targets in the §7 reachability test
                         (default: hist-01 — hmi-01's own /etc/hosts entry
                         must resolve it unaided; every certificate carries a
                         hostname-only SAN, digital.md §2.7)
  HIST_CA_CERT           Path to the CA's public root (build artefact, not a
                         secret) the pusher validates hist-01's certificate
                         against (default: <repo>/deploy/build/ca/ca.pem)

Prerequisites:
  - hist-01 and hist-dmz-01 are running Incus images built from
    deploy/build-scripts/.
  - The runner has joined vnet-ot as RUNNER_HOST —
    `make -f tests/integration/Makefile configure-hist`, which `test-hist`
    depends on. The zone bridges carry no host address of their own
    (deployment.md §2.1), so without this the runner has no route to hist-01 and
    every push fails with a connect timeout. This is the same single line
    configure-fms and configure-corp-it already run, so it grants the host no
    reach it does not already have.
  - The test runner has an `incus` client authorised against the same daemon.
    Everything on the far side of a zone boundary is read through it rather than
    over the network: hist-01's loopback-bound stores (hist.md §9.1), and both
    of hist-dmz-01's services. Only the pushes to hist-01 go over the wire, and
    only because the FMS simulator must be a real HTTP and TCP client for the
    §3 syslog framing to mean anything.

    The runner therefore sits on one zone and reaches no other, which is what
    keeps it from being a path between zones that bypasses the firewall nodes.
    For hist-dmz-01's Grafana this is not a preference: 443 into the DMZ is
    permitted only from vnet-it, so an OT-positioned client is refused,
    correctly.
  - The FMS push simulator (stubs/fms_pusher.py) stands in for fms-01: each
    test pushes its own fixture data, tagged with `unique_id`, and queries it
    back — there is no long-lived stub process (§0).
"""

import os
import uuid
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

from tests.integration.hist.stubs.fms_pusher import FMSPusher
from tests.integration.hist.stubs.grafana import IncusExecGrafanaAuth
from tests.integration.hist.stubs.query import (
    IncusExecTransport,
    LogsClient,
    MetricsClient,
)

# conftest.py -> hist -> integration -> tests -> <RavensPort repo root>.
_REPO_ROOT = Path(__file__).resolve().parents[3]


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Config:
    hist_node: str
    hist_dmz_node: str
    hist_host: str
    hist_dmz_host: str
    metrics_push_port: int
    event_push_port: int
    syslog_port: int
    metrics_query_port: int
    logs_query_port: int
    histdmz01_metrics_query_port: int
    histdmz01_logs_query_port: int
    grafana_port: int
    world_yaml: Path
    hmi_node: str
    hist_reach_target: str
    ca_cert_path: Path

    @classmethod
    def from_env(cls) -> "Config":
        hist_host = os.environ.get("HIST_HOST", "10.40.0.11")
        return cls(
            hist_node=os.environ.get("HIST_NODE_NAME", "hist-01"),
            hist_dmz_node=os.environ.get("HIST_DMZ_NODE_NAME", "hist-dmz-01"),
            hist_host=hist_host,
            hist_dmz_host=os.environ.get("HIST_DMZ_HOST", "10.20.0.20"),
            metrics_push_port=int(os.environ.get("HIST_METRICS_PUSH_PORT", "8429")),
            event_push_port=int(os.environ.get("HIST_EVENT_PUSH_PORT", "9429")),
            syslog_port=int(os.environ.get("HIST_SYSLOG_PORT", "6514")),
            metrics_query_port=int(os.environ.get("HIST_METRICS_QUERY_PORT", "8428")),
            logs_query_port=int(os.environ.get("HIST_LOGS_QUERY_PORT", "9428")),
            histdmz01_metrics_query_port=int(
                os.environ.get("HIST_DMZ_METRICS_QUERY_PORT", "18428")
            ),
            histdmz01_logs_query_port=int(
                os.environ.get("HIST_DMZ_LOGS_QUERY_PORT", "19428")
            ),
            grafana_port=int(os.environ.get("HIST_GRAFANA_PORT", "443")),
            world_yaml=Path(
                os.environ.get("HIST_WORLD_YAML", _REPO_ROOT / "deploy" / "world.yaml")
            ),
            hmi_node=os.environ.get("HIST_HMI_NODE", "hmi-01"),
            # Target hist-01 by hostname, not IP: every certificate carries a
            # hostname-only SAN (digital.md §2.7), so hmi-01's own /etc/hosts
            # entry (deployment.md §5.2) must resolve it unaided, the same way a
            # human's browser would — the property hist-tests.md §7's
            # "grafana_reachable_over_https_from_another_ot_host" exists to
            # confirm, not merely that some route to the IP exists.
            hist_reach_target=os.environ.get("HIST_REACH_TARGET", "hist-01"),
            ca_cert_path=Path(
                os.environ.get(
                    "HIST_CA_CERT", _REPO_ROOT / "deploy" / "build" / "ca" / "ca.pem"
                )
            ),
        )


@pytest.fixture(scope="session")
def config() -> Config:
    return Config.from_env()


# ---------------------------------------------------------------------------
# FMS push simulator (function-scoped: closes its sockets after each test)
# ---------------------------------------------------------------------------


@pytest.fixture()
def pusher(config: Config) -> Generator[FMSPusher, None, None]:
    p = FMSPusher(
        config.hist_host,
        metrics_port=config.metrics_push_port,
        jsonline_port=config.event_push_port,
        syslog_port=config.syslog_port,
        ca_cert_path=config.ca_cert_path,
    )
    yield p
    p.close()


# ---------------------------------------------------------------------------
# Query clients (session-scoped)
#
# Both hosts' stores are read via incus exec, not directly from the runner,
# which has no route into either zone beyond vnet-ot (§0/§8):
# hist-01: its one loopback listener (§9.1).
# hist-dmz-01: its plain-HTTP loopback listener, on its own dedicated port
# (§9.2) — the one Grafana uses locally, distinct from the TLS listener on the
# zone address's standard port that hist-01 replicates into (0.0.0.0 and
# 127.0.0.1 cannot share one port).
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def hist01_metrics(config: Config) -> Generator[MetricsClient, None, None]:
    transport = IncusExecTransport(
        config.hist_node, f"http://127.0.0.1:{config.metrics_query_port}"
    )
    yield MetricsClient(transport)
    transport.close()


@pytest.fixture(scope="session")
def hist01_logs(config: Config) -> Generator[LogsClient, None, None]:
    transport = IncusExecTransport(
        config.hist_node, f"http://127.0.0.1:{config.logs_query_port}"
    )
    yield LogsClient(transport)
    transport.close()


@pytest.fixture(scope="session")
def histdmz01_metrics(config: Config) -> Generator[MetricsClient, None, None]:
    transport = IncusExecTransport(
        config.hist_dmz_node, f"http://127.0.0.1:{config.histdmz01_metrics_query_port}"
    )
    yield MetricsClient(transport)
    transport.close()


@pytest.fixture(scope="session")
def histdmz01_logs(config: Config) -> Generator[LogsClient, None, None]:
    transport = IncusExecTransport(
        config.hist_dmz_node, f"http://127.0.0.1:{config.histdmz01_logs_query_port}"
    )
    yield LogsClient(transport)
    transport.close()


# ---------------------------------------------------------------------------
# Grafana authentication (§6)
#
# hist-01: Grafana on :443, probed from inside the container (incus exec), the
#   same test-only path used for its loopback stores.
# hist-dmz-01: Grafana on :443, probed directly over HTTPS at its zone address.
# The seeded credentials are read from world.yaml so the test tracks the single
# source, not a hardcoded copy.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Credential:
    username: str
    password: str


@dataclass(frozen=True)
class SeededCredentials:
    hist_admin: Credential
    reconciliation_user: Credential


@pytest.fixture(scope="session")
def world_credentials(config: Config) -> SeededCredentials:
    """The seeded Grafana credentials as authored in world.yaml
    (identities.hist_admin, identities.reconciliation_user)."""
    identities = yaml.safe_load(config.world_yaml.read_text())["identities"]

    def _cred(entry: str) -> Credential:
        node = identities[entry]
        return Credential(username=node["username"], password=node["password"])

    return SeededCredentials(
        hist_admin=_cred("hist_admin"),
        reconciliation_user=_cred("reconciliation_user"),
    )


@pytest.fixture(scope="session")
def hist01_grafana(config: Config) -> Generator[IncusExecGrafanaAuth, None, None]:
    auth = IncusExecGrafanaAuth(
        config.hist_node, f"https://127.0.0.1:{config.grafana_port}"
    )
    yield auth
    auth.close()


@pytest.fixture(scope="session")
def histdmz01_grafana(config: Config) -> Generator[IncusExecGrafanaAuth, None, None]:
    """Probed from inside the container, not over the network.

    The runner joins vnet-ot (`make configure-hist`), and the only rule
    admitting 443 into the DMZ is from vnet-it — so an OT-positioned client is
    refused, correctly. Reaching it over the network would mean putting the
    runner on a second zone, which is what the single-zone arrangement exists to
    avoid. The IT-to-DMZ path on 443 is proved by test_policy.sh from recon-01,
    and §7 checks reachability from an OT host the same way; what these tests
    need is the credential behaviour of the instance itself.
    """
    auth = IncusExecGrafanaAuth(
        config.hist_dmz_node, f"https://127.0.0.1:{config.grafana_port}"
    )
    yield auth
    auth.close()


# ---------------------------------------------------------------------------
# Per-test isolation
# ---------------------------------------------------------------------------


@pytest.fixture()
def unique_id() -> str:
    """A fresh identifier per test, used to tag pushed fixture data so a query
    retrieves only this test's records within the shared store (§0)."""
    return uuid.uuid4().hex
