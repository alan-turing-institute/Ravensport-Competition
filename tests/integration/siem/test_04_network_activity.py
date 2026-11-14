"""
Network activity data tests (siem-tests.md §4; siem.md §4, §8.2).

These assert on traffic the running scenario itself produces (§0.1): a
scenario must be in progress against the deployed topology for the positive
cases here to have anything to observe.
"""

from __future__ import annotations

import time

import pytest

from tests.integration.siem.conftest import Config, Node, OutputReader
from tests.integration.siem.hosts import (
    CROSS_ZONE_CONNECTION_EVENTS,
    CROSS_ZONE_CONNECTION_TRIGGERS,
    CROSS_ZONE_CONVERSATIONS,
    CROSS_ZONE_DESTINATION_IPS,
    DMZ_ZONE_HOSTS,
    FLOW_RECORD_TRIGGERS,
    INTRA_ZONE_CONNECTION_TRIGGERS,
    INTRA_ZONE_CONVERSATIONS,
    INTRA_ZONE_PEER_IPS,
    MONITORED_HOSTS,
    OT_ZONE_HOSTS,
)
from tests.integration.siem.tls_frame_injector import build_command, send_frame

_CONNECTION_FIELD_CANDIDATES = {
    "source_address": ("src_ip", "source_ip", "srcip"),
    "destination_address": ("dest_ip", "destination_ip", "dstip", "dstip"),
    "source_port": ("src_port", "source_port", "srcport"),
    "destination_port": ("dest_port", "destination_port", "dstport"),
    # ulogd's JSON output (siem.md §4.1, §8.2) nests the IANA protocol number
    # under "ip", and carries direction as the nftables log prefix
    # ("conn-in: "/"conn-out: ") rather than a discrete field.
    "protocol": ("protocol", "proto", "ip.protocol"),
    "direction": ("direction", "oob.prefix"),
    "interface": ("interface", "oob.in", "oob.out"),
}

_ABSENT_FIELD_CANDIDATES = ("bytes", "duration", "packets", "process", "pid", "comm")

# The packaged `netstat listening ports` full_command scan is periodic at
# 360s (siem.md §4.3, _wazuh-agent.sh.j2), not event-driven. The test's own
# listener must outlive the wait below, or the port closes before any scan
# can observe it — the failure mode a shorter listener lifetime produced.
_PORT_SCAN_INTERVAL_S = 360
_PORT_SCAN_WAIT_S = _PORT_SCAN_INTERVAL_S + 40
_PORT_SCAN_LISTENER_LIFETIME_S = _PORT_SCAN_WAIT_S + 20

# Suricata writes a flow record once a closed TCP flow's 60 s timeout has
# elapsed and its flow manager next sweeps; the agent then forwards it.
_FLOW_RECORD_WAIT_S = 180

# An NTP exchange is two-way, so Suricata keeps it as an established UDP flow
# and writes its record only after a 300 s timeout. chrony queries corp-it
# continuously (hosts.py), but the other hosts start polling only once they
# have booted, after the firewalls: measured live, the first exchange crossed
# the firewalls 77 s after Suricata started and its record reached
# archives.json 378 s after, forwarding by the agent taking under a second.
# This age allows for hosts that boot more slowly; the fixture stops waiting
# as soon as the records exist, so it costs time only on a newly started
# deployment.
_UDP_FLOW_RECORD_AGE_S = 480


def _first_present(record: dict, candidates: tuple[str, ...]):
    """Look up each candidate key in turn, returning the first present value.
    A dotted key (e.g. "ip.protocol") descends into nested objects — the
    manager's JSON decoder (siem.md §4.1) reproduces ulogd's own nesting
    (`ip.*`, `oob.*`) rather than flattening it to top-level keys."""
    data = record.get("data", record)
    for key in candidates:
        node = data
        for part in key.split("."):
            if not isinstance(node, dict) or part not in node:
                node = None
                break
            node = node[part]
        if node is not None:
            return node
    return None


def _mentions(record: dict, needle: str) -> bool:
    return needle in str(record)


@pytest.mark.parametrize(
    ("host", "peer"),
    INTRA_ZONE_CONVERSATIONS,
    ids=[f"{h}->{p}" for h, p in INTRA_ZONE_CONVERSATIONS],
)
def test_connection_event_for_every_conversation_including_the_four_within_a_zone(
    host: str, peer: str, outputs: OutputReader
) -> None:
    """With a scenario in progress, archives.json carries a connection event
    for every conversation siem.md §4.1 lists, including the four that reach
    no firewall."""
    peer_ip = INTRA_ZONE_PEER_IPS[peer]

    def _matches(r: dict) -> bool:
        return r.get("agent", {}).get("name") == host and peer_ip in (
            _first_present(r, ("src_ip",)),
            _first_present(r, ("dest_ip",)),
        )

    trigger = INTRA_ZONE_CONNECTION_TRIGGERS.get((host, peer))
    if trigger:
        since = outputs.snapshot()[1]
        Node(host).run(*trigger, check=False)
        outputs.wait_for_archive(_matches, since=since, timeout=20.0)

    matching = [r for r in outputs.archives() if _matches(r)]
    assert matching, f"no connection event found for {host} -> {peer} ({peer_ip})"


def test_connection_event_carries_the_specified_fields_and_nothing_more(
    outputs: OutputReader,
) -> None:
    """A sampled connection event carries the local host's identity and
    interface, direction, and the five-tuple, and carries no volume,
    duration, or originating process."""
    candidates = [
        r
        for r in outputs.archives()
        if r.get("location") == "/var/log/ulog/connections.json"
        and all(
            _first_present(r, keys) is not None
            for keys in (
                _CONNECTION_FIELD_CANDIDATES["source_address"],
                _CONNECTION_FIELD_CANDIDATES["destination_address"],
            )
        )
    ]
    assert candidates, "no record resembling a connection event was found"
    sample = candidates[0]

    for field, keys in _CONNECTION_FIELD_CANDIDATES.items():
        assert _first_present(sample, keys) is not None, f"missing {field}"

    data = sample.get("data", sample)
    for absent in _ABSENT_FIELD_CANDIDATES:
        assert absent not in data, f"connection event unexpectedly carries {absent!r}"


@pytest.mark.parametrize(
    ("host", "peer_ip", "peer_port", "proto"),
    CROSS_ZONE_CONNECTION_EVENTS,
    ids=[f"{h}->{ip}:{p}/{pr}" for h, ip, p, pr in CROSS_ZONE_CONNECTION_EVENTS],
)
def test_connection_event_for_every_cross_zone_conversation(
    host: str, peer_ip: str, peer_port: int, proto: str, outputs: OutputReader
) -> None:
    """A connection event reaches archives.json from at least one of a
    cross-zone conversation's own two real endpoints — the same mechanism
    already confirmed for the four intra-zone conversations, exercised here
    for the paths that also cross a firewall and so additionally produce
    the flow record of §4.2 (checked separately, below). A conversation
    crossing two hops is exercised once, from its own real endpoint, not
    once per hop — see hosts.py."""

    def _matches(r: dict) -> bool:
        return (
            r.get("agent", {}).get("name") == host
            and _first_present(r, ("dest_ip",)) == peer_ip
            and str(_first_present(r, ("dest_port",))) == str(peer_port)
        )

    trigger = CROSS_ZONE_CONNECTION_TRIGGERS.get((host, peer_ip, peer_port))
    if trigger:
        since = outputs.snapshot()[1]
        Node(host).run(*trigger, check=False)
        outputs.wait_for_archive(_matches, since=since, timeout=20.0)

    matching = [r for r in outputs.archives() if _matches(r)]
    assert matching, (
        f"no connection event found for {host} -> {peer_ip}:{peer_port}/{proto}"
    )


def test_connection_event_for_inbound_external_supplier_traffic(
    config: Config, outputs: OutputReader
) -> None:
    """The one cross-zone conversation whose monitored participant is the
    destination rather than the initiator: atg-relay-01's own inbound
    connection events include the external zone's traffic on its relay
    port (deployment.md §4.2, `fw-ext-dmz` hop).

    Driven by one conforming poll from the runner on vnet-ext rather than
    left to the sim's fuel supplier, which polls at 15-minute intervals and
    so cannot be relied on within the test's own window."""

    def _matches(r: dict) -> bool:
        return (
            r.get("agent", {}).get("name") == "atg-relay-01"
            and _first_present(r, ("oob.prefix",)) == "conn-in: "
            and str(_first_present(r, ("dest_port",))) == "10001"
        )

    since = outputs.snapshot()[1]
    send_frame(config.injector_host, config.injector_port, build_command("i20100"))
    outputs.wait_for_archive(_matches, since=since, timeout=20.0)

    matching = [r for r in outputs.archives() if _matches(r)]
    assert matching, (
        "no inbound connection event on atg-relay-01:10001 from the external zone"
    )


@pytest.mark.parametrize(
    ("zone_hosts", "peer_ip", "peer_port", "zone_name"),
    [
        (OT_ZONE_HOSTS, "10.30.0.20", 123, "OT"),
        (DMZ_ZONE_HOSTS, "10.30.0.20", 123, "DMZ"),
        (OT_ZONE_HOSTS, "10.30.0.30", 1514, "OT"),
        (DMZ_ZONE_HOSTS, "10.30.0.30", 1514, "DMZ"),
    ],
    ids=[
        "ntp-from-ot",
        "ntp-from-dmz",
        "agent-channel-from-ot",
        "agent-channel-from-dmz",
    ],
)
def test_connection_event_for_corp_it_and_siem_01_from_every_zone(
    zone_hosts: list[str],
    peer_ip: str,
    peer_port: int,
    zone_name: str,
    outputs: OutputReader,
) -> None:
    """corp-it's NTP service and siem-01's agent channel are each reached
    continuously from every host in both the OT and DMZ zones; any one host
    from the zone demonstrates the path deployment.md §4.2 describes for
    it."""
    matching = [
        r
        for r in outputs.archives()
        if r.get("agent", {}).get("name") in zone_hosts
        and _first_present(r, ("dest_ip",)) == peer_ip
        and str(_first_present(r, ("dest_port",))) == str(peer_port)
    ]
    assert matching, (
        f"no connection event from any {zone_name}-zone host to {peer_ip}:{peer_port}"
    )


def _is_flow_record(
    r: dict, firewall_node: str, dst: str, port: int, proto: str
) -> bool:
    data = r.get("data", {})
    return (
        r.get("agent", {}).get("name") == firewall_node
        and data.get("event_type") == "flow"
        and data.get("dest_ip") == CROSS_ZONE_DESTINATION_IPS[dst]
        and str(data.get("dest_port")) == str(port)
        and str(data.get("proto", "")).lower() == proto
    )


def _suricata_uptime_s(firewall_node: str) -> int:
    """Seconds since Suricata's main process started on `firewall_node`."""
    processes = Node(firewall_node).stdout("ps", "-eo", "etimes=,comm=")
    for line in processes.splitlines():
        elapsed, _, name = line.strip().partition(" ")
        if name.strip() == "Suricata-Main":
            return int(elapsed)
    raise AssertionError(f"Suricata is not running on {firewall_node}")


@pytest.fixture(scope="module")
def triggered_flow_records(config: Config, outputs: OutputReader) -> None:
    """Open a fresh connection on every cross-zone path whose own traffic
    produces no flow record within the test window (hosts.py), then wait
    once, across all of them, for Suricata's flow timeout to elapse and the
    records to reach archives.json — rather than once per parametrised
    row. On a deployment younger than the NTP rows' timeout, the wait
    extends only as far as needed for those records to exist."""
    since = outputs.snapshot()[1]
    send_frame(config.injector_host, config.injector_port, build_command("i20100"))
    for host, command in FLOW_RECORD_TRIGGERS:
        Node(host).run(*command, check=False)

    # The triggered rows' records are new, so only records written after the
    # triggers count. The NTP rows need no trigger, and their records may
    # predate this fixture, so every archived record counts for them.
    triggered = [row for row in CROSS_ZONE_CONVERSATIONS if row[2] != 123]
    ntp = [row for row in CROSS_ZONE_CONVERSATIONS if row[2] == 123]
    ntp_wait_s = max(_UDP_FLOW_RECORD_AGE_S - _suricata_uptime_s(row[0]) for row in ntp)
    deadline = time.monotonic() + max(_FLOW_RECORD_WAIT_S, ntp_wait_s)
    while (triggered or ntp) and time.monotonic() < deadline:
        archives = outputs.archives()
        triggered = [
            row
            for row in triggered
            if not any(_is_flow_record(r, *row) for r in archives[since:])
        ]
        ntp = [
            row for row in ntp if not any(_is_flow_record(r, *row) for r in archives)
        ]
        if triggered or ntp:
            time.sleep(5.0)


@pytest.mark.parametrize(
    ("firewall_node", "dst", "port", "proto"),
    CROSS_ZONE_CONVERSATIONS,
    ids=[f"{n}:{d}:{p}/{pr}" for n, d, p, pr in CROSS_ZONE_CONVERSATIONS],
)
def test_flow_record_for_every_cross_zone_conversation(
    firewall_node: str,
    dst: str,
    port: int,
    proto: str,
    outputs: OutputReader,
    triggered_flow_records: None,
) -> None:
    """For every permitted cross-zone path in deployment.md §4.2, the
    originating firewall node's eve.json carries a flow record with source
    and destination, duration, byte and packet counts in each direction, and
    termination reason — reaching archives.json via that node's own agent.

    The manager's JSON decoder places every eve.json field under `data`."""
    matching = [
        r
        for r in outputs.archives()
        if _is_flow_record(r, firewall_node, dst, port, proto)
    ]
    assert matching, f"no flow record on {firewall_node} for {dst}:{port}/{proto}"
    data = matching[-1]["data"]
    assert data.get("src_ip"), "flow record missing src_ip"
    flow = data.get("flow", {})
    for field in (
        "bytes_toserver",
        "bytes_toclient",
        "pkts_toserver",
        "pkts_toclient",
        "age",
        "reason",
    ):
        assert field in flow, f"flow record missing flow.{field}"


def test_listening_port_inventory_is_reported_and_a_new_port_alerts(
    outputs: OutputReader,
) -> None:
    """Each monitored host's listening-port inventory is reported
    periodically. A new listening port opened on a host, outside the fixed
    service set, raises an alert — checked on every monitored host, not
    only one, since the mechanism is configured identically on each
    (_wazuh-agent.sh.j2) and nothing narrows it to a single host.

    Opened on every host up front, and waited for once across all of them,
    rather than one host at a time: the scan is periodic at 360s
    (siem.md §4.3), so a sequential per-host wait would multiply a
    multi-minute wait by ten."""
    since = outputs.snapshot()[0]

    # Spawned detached and backgrounded on each host in turn: each call must
    # return immediately so the shared wait below is not itself consumed by
    # any one listener's own lifetime. Every listener's own lifetime must
    # outlast the wait, or its port closes before that host's next periodic
    # scan can observe it.
    for host in MONITORED_HOSTS:
        Node(host).run(
            "bash",
            "-c",
            'nohup python3 -c "import socket,time; s=socket.socket(); '
            f"s.bind(('0.0.0.0', 47821)); s.listen(1); "
            f'time.sleep({_PORT_SCAN_LISTENER_LIFETIME_S})" '
            ">/tmp/siem_test_listener.log 2>&1 & disown; sleep 1",
            check=True,
        )
    try:
        remaining = set(MONITORED_HOSTS)
        deadline = time.monotonic() + _PORT_SCAN_WAIT_S
        while remaining and time.monotonic() < deadline:
            for record in outputs.alerts()[since:]:
                name = record.get("agent", {}).get("name")
                if name in remaining and "47821" in str(record):
                    remaining.discard(name)
            if remaining:
                time.sleep(5.0)
        assert not remaining, (
            f"no alert raised for the new listening port on: {sorted(remaining)}"
        )
    finally:
        for host in MONITORED_HOSTS:
            Node(host).run("pkill", "-f", "47821", check=False)
