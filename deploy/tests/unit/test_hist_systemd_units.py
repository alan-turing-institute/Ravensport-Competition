"""
Systemd unit tests for the log servers (hist-tests.md §8), targeting hist.md §9's
flag table and §10.1's unit shapes.

RED UNTIL C1/C2. `hist-01.sh.j2` and `hist-dmz-01.sh.j2` are skeletons: they build
a bootable image but install no log-server stack, so no unit exists to parse yet.
These tests are the contract those build scripts must satisfy, written before the
scripts that satisfy them.

Assertions are on the rendered `build/node-build/hist-*.sh`, not on the Jinja
templates, so that a value lost to template logic fails here rather than on the
VM. The binding asymmetry between the two hosts is the property most worth
guarding: it is the difference between a store reachable only by its local agent
and one reachable across a zone boundary, and nothing else in the suite checks it.

Run with: pytest tests/unit -q
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]  # deploy/

_REPLICA_HOSTNAME = "hist-dmz-01"
_RETENTION = "-retentionPeriod=3y"
_STORES = ("victoria-metrics", "victoria-logs")
_AGENTS = ("vmagent", "vlagent")
_TLS_CERT_FLAG = "-tlsCertFile=/etc/hist-tls/cert.pem"
_TLS_KEY_FLAG = "-tlsKeyFile=/etc/hist-tls/key.pem"


def _render(profile: str = "ci-minimal") -> Path:
    out = subprocess.run(
        [sys.executable, "scripts/render.py", "--profile", profile],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert out.returncode == 0, out.stderr
    return ROOT / "build"


def _units(node: str) -> dict[str, str]:
    """Map service name to the systemd configuration the build script writes for
    it, whether as a unit file or as a drop-in.

    A drop-in under `<name>.service.d/` is folded into the same key as
    `<name>.service`: Grafana's unit ships with its package, so the capability it
    needs is most naturally added as an override rather than by replacing the
    vendor unit. Keying on the service rather than the file leaves that choice to
    the build script instead of forcing one here.
    """
    build = _render()
    script = (build / "node-build" / f"{node}.sh").read_text()
    lines = script.splitlines()

    units: dict[str, str] = {}
    pattern = re.compile(r"/etc/systemd/system/([^/\s\"']+\.service)(\.d/[^\s\"']+)?")
    i = 0
    while i < len(lines):
        match = pattern.search(lines[i])
        heredoc = re.search(r"<<\s*'?([A-Za-z_]+)'?\s*$", lines[i])
        if match and heredoc:
            terminator = heredoc.group(1)
            service = match.group(1).removesuffix(".service")
            body: list[str] = []
            i += 1
            while i < len(lines) and lines[i].strip() != terminator:
                body.append(lines[i])
                i += 1
            units[service] = units.get(service, "") + "\n".join(body) + "\n"
        i += 1
    return units


def _unit(node: str, service: str) -> str:
    """One service's configuration, failing with the node and the units that were
    found. Indexing the map directly raises a bare KeyError naming only the
    service, which does not say which host it was missing from or that the build
    script writes no units at all."""
    units = _units(node)
    if service not in units:
        pytest.fail(
            f"{node}'s build script writes no {service} unit. Units found: "
            f"{sorted(units) or 'none'}. hist.md §10.1 requires one unit per "
            "component of §2's stack."
        )
    return units[service]


def _exec_start(unit_body: str) -> str:
    """The ExecStart line, joined if continued across escaped newlines."""
    joined = unit_body.replace("\\\n", " ")
    for line in joined.splitlines():
        if line.strip().startswith("ExecStart="):
            return line.strip()
    return ""


# ── Which units exist on which host ───────────────────────────────────────────


def test_hist01_runs_both_stores_and_both_agents():
    """hist.md §2: hist-01 is the ingestion point, so it runs the two stores and
    the two agents that write to them and fan out to the replica."""
    units = _units("hist-01")
    for component in (*_STORES, *_AGENTS, "grafana-server"):
        assert component in units, (
            f"hist-01 has no {component} unit; rendered units are {sorted(units)}"
        )


def test_histdmz01_runs_the_stores_but_no_agent():
    """hist.md §2: the replica is a pure receiver. An agent there would give the
    DMZ host an outbound remote-write path of its own, which is the property
    'only hist-01 may push into the DMZ' (digital.md §1.2) depends on not
    existing."""
    units = _units("hist-dmz-01")
    for component in (*_STORES, "grafana-server"):
        assert component in units, f"hist-dmz-01 has no {component} unit"
    for agent in _AGENTS:
        assert agent not in units, (
            f"hist-dmz-01 renders a {agent} unit; the replica receives rather "
            "than forwards, and must originate no remote-write of its own"
        )


# ── The binding asymmetry (hist.md §9.1 versus §9.2) ──────────────────────────


@pytest.mark.parametrize("store,port", list(zip(_STORES, ("8428", "9428"))))
def test_hist01_stores_bind_loopback_only(store: str, port: str):
    """hist-01's stores are reached solely through its local agent, so they bind
    loopback. Binding the zone address instead would expose an unauthenticated
    write API to the whole OT zone, bypassing the agent entirely."""
    exec_start = _exec_start(_unit("hist-01", store))
    assert f"-httpListenAddr=127.0.0.1:{port}" in exec_start, (
        f"hist-01 {store} does not bind 127.0.0.1:{port} (hist.md §9.1); "
        f"ExecStart is {exec_start!r}"
    )
    assert "0.0.0.0" not in exec_start, (
        f"hist-01 {store} binds a non-loopback address: {exec_start!r}"
    )


@pytest.mark.parametrize("store", _STORES)
def test_hist01_stores_stay_plain_http_on_loopback(store: str):
    """No boundary is crossed on hist-01's loopback stores and nothing outside
    hist-01 can reach them (hist.md §9.1), so — unlike vmagent/vlagent/Grafana on
    the same host — they carry no TLS flags at all."""
    exec_start = _exec_start(_unit("hist-01", store))
    assert "-tls" not in exec_start, (
        f"hist-01 {store} carries a TLS flag; hist.md §9.1 keeps its loopback "
        f"traffic plain HTTP. ExecStart is {exec_start!r}"
    )


@pytest.mark.parametrize(
    "store,zone_port,loopback_port",
    [("victoria-metrics", "8428", "18428"), ("victoria-logs", "9428", "19428")],
)
def test_histdmz01_stores_bind_loopback_plain_and_zone_address_tls(
    store: str, zone_port: str, loopback_port: str
):
    """The replica has no local agent in front of it: hist-01's agents write to it
    across the zone boundary, so its stores must bind 0.0.0.0 on the standard
    port and terminate TLS with the shared /etc/hist-tls/ keypair there — but
    they also serve Grafana's own local datasource queries (§7) on a second,
    plain-HTTP loopback listener, since that traffic crosses no boundary and the
    certificate (hostname-only SAN) could not validate against 127.0.0.1 in any
    case (hist.md §9.2). The loopback listener needs a port of its own rather
    than sharing the zone address's: 0.0.0.0 and 127.0.0.1 cannot both be bound
    on the same port. `-httpListenAddr` takes both addresses as one
    comma-separated list, and `-tls`/`-tlsCertFile`/`-tlsKeyFile` are positional
    lists matched to it — the loopback entry plain (`false`, empty path), the
    zone-address entry TLS."""
    exec_start = _exec_start(_unit("hist-dmz-01", store))
    assert (
        f"-httpListenAddr=127.0.0.1:{loopback_port},0.0.0.0:{zone_port}" in exec_start
    ), (
        f"hist-dmz-01 {store} does not bind loopback:{loopback_port} and "
        f"0.0.0.0:{zone_port} as a comma-separated list (hist.md §9.2); "
        f"ExecStart is {exec_start!r}"
    )
    assert "-tls=false,true" in exec_start, (
        f"hist-dmz-01 {store} does not set -tls=false,true (loopback plain, zone "
        f"address TLS); ExecStart is {exec_start!r}"
    )
    assert "-tlsCertFile=,/etc/hist-tls/cert.pem" in exec_start, (
        f"hist-dmz-01 {store} does not set -tlsCertFile=,/etc/hist-tls/cert.pem "
        f"(empty for the loopback listener, the shared path for the zone-address "
        f"one); ExecStart is {exec_start!r}"
    )
    assert "-tlsKeyFile=,/etc/hist-tls/key.pem" in exec_start, (
        f"hist-dmz-01 {store} does not set -tlsKeyFile=,/etc/hist-tls/key.pem: "
        f"ExecStart is {exec_start!r}"
    )


# ── Agent fan-out ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "agent,port,path",
    [("vmagent", "8428", "/api/v1/write"), ("vlagent", "9428", "/insert/native")],
)
def test_agents_write_locally_and_to_the_replica(agent: str, port: str, path: str):
    """Each agent carries two -remoteWrite.url flags: the local store (plain
    HTTP, no boundary crossed) and the replica (HTTPS, addressed by hostname
    so the certificate's hostname SAN validates, hist.md §9.1). Both are
    required — the local one is the queryable copy behind Grafana, the remote
    one is the tamper-evidence channel digital.md §6.3 depends on, and each
    URL gets its own on-disk buffer."""
    exec_start = _exec_start(_unit("hist-01", agent))
    local = f"-remoteWrite.url=http://127.0.0.1:{port}{path}"
    remote = f"-remoteWrite.url=https://{_REPLICA_HOSTNAME}:{port}{path}"

    assert local in exec_start, f"{agent} does not write to its local store: {local}"
    assert remote in exec_start, (
        f"{agent} does not replicate to https://{_REPLICA_HOSTNAME}: {remote}"
    )
    assert exec_start.count("-remoteWrite.url=") == 2, (
        f"{agent} has {exec_start.count('-remoteWrite.url=')} remote-write "
        "targets; hist.md §9.1 specifies exactly two"
    )


@pytest.mark.parametrize("agent", _AGENTS)
def test_agents_terminate_tls_on_their_inbound_listener(agent: str):
    """vmagent's import endpoint and vlagent's JSON-line endpoint both present
    the shared /etc/hist-tls/ certificate to fms-01 (hist.md §9.1,
    digital.md §2.7) rather than serving plain HTTP."""
    exec_start = _exec_start(_unit("hist-01", agent))
    for flag in (_TLS_CERT_FLAG, _TLS_KEY_FLAG):
        assert flag in exec_start, f"{agent} missing {flag}: {exec_start!r}"


def test_vlagent_accepts_syslog_on_6514_with_tls():
    """vlagent is the FMS's syslog sink as well as its JSON-line sink (hist.md
    §5), now over TLS (RFC 5425) on port 6514 rather than plain TCP on 514.
    Without the syslog listener the RFC 5424 path has no receiver."""
    exec_start = _exec_start(_unit("hist-01", "vlagent"))
    assert "-syslog.listenAddr.tcp=0.0.0.0:6514" in exec_start, (
        f"vlagent does not listen for syslog on 6514: {exec_start!r}"
    )
    assert "-syslog.tls" in exec_start, (
        f"vlagent's syslog listener is not TLS: {exec_start!r}"
    )
    assert "-syslog.tlsCertFile=/etc/hist-tls/cert.pem" in exec_start, (
        f"vlagent's syslog listener does not reference the shared certificate: {exec_start!r}"
    )
    assert "-syslog.tlsKeyFile=/etc/hist-tls/key.pem" in exec_start, (
        f"vlagent's syslog listener does not reference the shared key: {exec_start!r}"
    )


# ── Privileged ports ──────────────────────────────────────────────────────────


def test_vlagent_no_longer_needs_the_bind_service_capability():
    """vlagent's syslog listener moved from privileged port 514 to unprivileged
    6514 when it gained TLS (hist.md §5, §10.1), which removes what was its
    only claim to CAP_NET_BIND_SERVICE. Carrying the capability regardless
    would be an unnecessary privilege on a compromise of the service."""
    body = _unit("hist-01", "vlagent")
    assert "AmbientCapabilities=CAP_NET_BIND_SERVICE" not in body, (
        "vlagent.service still carries CAP_NET_BIND_SERVICE; hist.md §10.1 "
        "notes it no longer binds a privileged port"
    )


@pytest.mark.parametrize("node", ["hist-01", "hist-dmz-01"])
def test_grafana_may_bind_443(node: str):
    """Grafana terminates TLS on 443 itself (hist.md §9), which is privileged and
    so needs the same capability, on both hosts.

    Both settings are required here, unlike in the units this repository writes
    itself. Grafana's packaged unit sets `CapabilityBoundingSet=` — emptying it —
    and systemd will not place a capability in the ambient set that is absent
    from the bounding set, so the ambient line alone is inert and Grafana exits
    unable to bind. `vlagent` needs only the ambient line because its unit sets
    no bounding set and inherits the default. Observed against a live deployment
    of Grafana OSS 12.4.6, whose packaged unit is where the empty bounding set
    comes from.
    """
    body = _unit(node, "grafana-server")
    for setting in (
        "AmbientCapabilities=CAP_NET_BIND_SERVICE",
        "CapabilityBoundingSet=CAP_NET_BIND_SERVICE",
    ):
        assert setting in body, (
            f"{node} grafana-server drop-in lacks {setting!r} and cannot bind 443"
        )


# ── Service accounts ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("node", ["hist-01", "hist-dmz-01"])
def test_every_service_account_exists_and_cannot_log_in(node: str):
    """hist.md §10.2 gives each service its own unprivileged system account. A
    unit naming a `User=` the build never created fails at start with a message
    about the user rather than about the service, and one created with a real
    shell turns a compromise of any store into an interactive session.

    Grafana is not covered here: its account comes from the `.deb`, and the build
    script writes a drop-in rather than the unit that names it.
    """
    build = _render()
    script = (build / "node-build" / f"{node}.sh").read_text()

    accounts = {
        line.split("=", 1)[1].strip()
        for body in _units(node).values()
        for line in body.splitlines()
        if line.strip().startswith("User=")
    }
    assert accounts, (
        f"{node} renders no unit with a User=, so every service runs as root"
    )

    for account in sorted(accounts):
        creation = [
            line
            for line in script.splitlines()
            if "useradd" in line and account in line
        ]
        assert creation, (
            f"{node} runs a service as {account!r} but never creates that account; "
            "systemd fails the unit at start"
        )
        joined = " ".join(creation)
        assert "--system" in joined, f"{node}'s {account} is not a system account"
        assert "/usr/sbin/nologin" in joined, (
            f"{node}'s {account} has a login shell; a service account must not be "
            "able to open a session"
        )


# ── Retention ─────────────────────────────────────────────────────────────────


def test_retention_is_three_years_and_identical_on_both_hosts():
    """MARPOL record keeping requires three years, and the replica must not expire
    data the primary still holds or the divergence signal becomes noise.

    The period is a literal duplicated across two templates rather than a
    world.yaml field — it has one consumer class and so fails that file's
    inclusion criterion — which makes this test the drift guard for the
    duplication.
    """
    for node in ("hist-01", "hist-dmz-01"):
        for store in _STORES:
            exec_start = _exec_start(_unit(node, store))
            assert _RETENTION in exec_start, (
                f"{node} {store} does not set {_RETENTION}; MARPOL requires three "
                f"years of retention. ExecStart is {exec_start!r}"
            )


@pytest.mark.parametrize("node", ["hist-01", "hist-dmz-01"])
def test_stores_persist_outside_the_container_writable_layer(node: str):
    """Each store writes to its own path under /var/lib. A store left on its
    default path would place three years of records wherever the working
    directory happened to be at start."""
    for store in _STORES:
        exec_start = _exec_start(_unit(node, store))
        assert f"-storageDataPath=/var/lib/{store}" in exec_start, (
            f"{node} {store} does not set -storageDataPath=/var/lib/{store}: "
            f"{exec_start!r}"
        )
