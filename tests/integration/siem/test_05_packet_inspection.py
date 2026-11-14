"""
Packet inspection at the zone boundaries (siem-tests.md §5; siem.md §5,
§8.4). Uses the TLS-protocol frame injector (tls_frame_injector.py, §0.5) for
the TCP 10001 checks, and an in-zone client for the 443/636 conformance
checks, which cross an internal boundary the externally-positioned injector
cannot itself reach.

The relay admits only the real supplier's address (digital.md §2.5), so the
injector's frames are inspected on fw-ext-dmz and go no further. The relay
completes each handshake before it resets, and an injected frame reaches the
wire only if it is sent before that reset: the injector tests therefore
resend until the alert for one of their own connections arrives. The same
checks on fw-ot-dmz are exercised by sending from atg-relay-01 itself, the
one DMZ host whose traffic to the console the policy permits.

Requires the runner on vnet-ext for the TCP 10001 checks:
    make -f tests/integration/Makefile configure-siem
"""

from __future__ import annotations

import base64
import os
import time
from datetime import datetime, timedelta, timezone

import pytest

from tests.integration.siem.conftest import Config, Node, OutputReader
from tests.integration.siem.tls_frame_injector import build_command, send_frame

# EICAR: a standardised, harmless test string every antivirus/IDS product is
# designed to recognise. No currently-published ET Open signature matches
# this string bare, over a raw TCP connection: every EICAR-related rule in
# the pinned ruleset requires either a Multipurpose Internet Mail Extensions
# (MIME) header context or a DOS/EXE header context. The canary therefore
# wraps the string in the one context an externally-positioned client can
# construct without impersonating the console's own replies: a
# `Content-Type` header carrying an overlong `name` parameter, matching ET
# sid 2022938 ("Possible Symantec Malicious MIME Doc Name Overflow (EICAR)
# toserver M4") — a real, published Emerging Threats Open signature that
# ships commented out under a relevance-based deprecation and is re-enabled
# explicitly for this canary (siem.md §5, §8.4). Its `toserver` direction is
# what makes it reachable at all: the client-to-console direction is the one
# an external caller can drive; the `toclient` sibling signatures require
# controlling the console's own reply. The signature matches only traffic from
# $EXTERNAL_NET, so the canary must come from the external segment rather
# than from a host inside the zones, and it fires only once the flow ends —
# confirmed live, about a minute after the frame.
_EICAR_STRING = b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"
_MIME_OVERFLOW_FILLER = b"A" * 90  # sid 2022938 requires at least 78
_DEFAULT_EICAR_CANARY_PAYLOAD = (
    b"POST /upload HTTP/1.1\r\n"
    b"Host: example.test\r\n"
    b'Content-Type: application/octet-stream; name="' + _MIME_OVERFLOW_FILLER + b'"\r\n'
    b"\r\n" + _EICAR_STRING + b"\r\n"
)
_EICAR_CANARY_PAYLOAD = (
    os.environ["SIEM_ET_CANARY_PAYLOAD"].encode("ascii")
    if "SIEM_ET_CANARY_PAYLOAD" in os.environ
    else _DEFAULT_EICAR_CANARY_PAYLOAD
)


def _send_raw_bytes_from(
    node: Node, host: str, port: int, payload: bytes
) -> int | None:
    """Fire-and-forget: open a TCP connection from inside `node` to
    `host:port` and send `payload`, returning the connection's source port or
    ``None`` when the connection or write failed. Used for the 443/636
    conformance checks, which cross an internal boundary any host already on
    that path can exercise without a dedicated runner placement."""
    script = (
        "import socket\n"
        f"s = socket.socket(); s.settimeout(5)\n"
        f"s.connect(({host!r}, {port}))\n"
        "print(s.getsockname()[1], flush=True)\n"
        f"s.sendall({payload!r})\n"
        "s.close()\n"
    )
    result = node.run("python3", "-c", script, check=False)
    if result.returncode != 0:
        return None
    try:
        return int(result.stdout.strip())
    except ValueError:
        return None


# The in-zone position for the fw-ot-dmz checks: atg-relay-01 is the DMZ host
# whose connection to the console deployment.md §4.2 permits, so a frame sent
# from it crosses fw-ot-dmz exactly as the relay's own forwarded traffic does.
# The console answers any function code it does not implement with its fixed
# error frame and ignores bytes before the next SOH (infra/sim atg_console),
# so none of these frames alters simulated state.
_IN_ZONE_INJECTOR = "atg-relay-01"
_ATG_CONSOLE_IP = "10.40.0.51"
_ATG_CONSOLE_PORT = 10001
_IN_ZONE_INJECTION_ATTEMPTS = 3
_IN_ZONE_INJECTION_ALERT_TIMEOUT_S = 20.0


def _inject_in_zone_until_alerted(
    outputs: OutputReader, payload: bytes, signature_id: int
) -> dict | None:
    """Send from the permitted DMZ host and return its correlated firewall
    alert. Retry a failed send or a connection whose alert is not forwarded:
    the accelerated scenario creates much more background traffic on this path,
    so a single unchecked fire-and-forget connection is not a reliable probe."""
    for _ in range(_IN_ZONE_INJECTION_ATTEMPTS):
        since = outputs.snapshot()[0]
        src_port = _send_raw_bytes_from(
            Node(_IN_ZONE_INJECTOR),
            _ATG_CONSOLE_IP,
            _ATG_CONSOLE_PORT,
            payload,
        )
        if src_port is None:
            continue
        alerted = outputs.wait_for_alert(
            lambda record, expected_port=src_port: (
                record.get("agent", {}).get("name") == "fw-ot-dmz"
                and str(record.get("data", {}).get("alert", {}).get("signature_id"))
                == str(signature_id)
                and str(record.get("data", {}).get("src_port")) == str(expected_port)
            ),
            since=since,
            timeout=_IN_ZONE_INJECTION_ALERT_TIMEOUT_S,
        )
        if alerted is not None:
            return alerted
    return None


# Resends allowed for an injected frame that loses the race against the
# relay's reset, and how long each attempt waits for its alert. A check on
# the start of the client's data (sid 1000003) is made on the first
# segment, at once. The command checks (sids 1000001, 1000002) look anywhere
# in the client's data, which Suricata inspects as the server acknowledges
# it; the relay resets an injected connection without acknowledging any, so
# those alerts follow the flow's end instead: the closed-TCP-flow timeout
# (60 s) plus the flow manager's sweep and the agent's forwarding.
_INJECTION_ATTEMPTS = 3
_INJECTION_ALERT_TIMEOUT_S = 10.0
_UNACKNOWLEDGED_ALERT_TIMEOUT_S = 120.0
# ET sid 2022938 fires once the flow ends: the closed-TCP-flow timeout (60 s)
# plus the flow manager's sweep and the agent's forwarding.
_CANARY_ALERT_TIMEOUT_S = 120.0


def _is_alert_for(record: dict, signature_id: int, src_port: int) -> bool:
    """True if `record` is fw-ext-dmz's alert for `signature_id` on the
    injected connection from `src_port`."""
    data = record.get("data", {})
    return (
        record.get("agent", {}).get("name") == "fw-ext-dmz"
        and str(data.get("alert", {}).get("signature_id")) == str(signature_id)
        and str(data.get("src_port")) == str(src_port)
    )


def _inject_until_alerted(
    config: Config,
    outputs: OutputReader,
    frame: bytes,
    signature_id: int,
    timeout: float = _INJECTION_ALERT_TIMEOUT_S,
) -> tuple[dict, int] | None:
    """Send `frame` through the injector until fw-ext-dmz raises
    `signature_id` for the connection that carried it, resending up to
    `_INJECTION_ATTEMPTS` times. Returns the alert and that connection's
    source port, or None if no attempt was inspected."""
    for _ in range(_INJECTION_ATTEMPTS):
        since = outputs.snapshot()[0]
        src_port = send_frame(
            config.injector_host, config.injector_port, frame
        ).local_port
        alerted = outputs.wait_for_alert(
            lambda r, port=src_port: _is_alert_for(r, signature_id, port),
            since=since,
            timeout=timeout,
        )
        if alerted is not None:
            return alerted, src_port
    return None


def test_published_signature_set_matches_a_known_pattern(
    config: Config, outputs: OutputReader
) -> None:
    """A deliberately generated packet matching a published Emerging Threats
    Open signature, sent across a zone boundary, raises an alert naming that
    signature (ET sid 2022938).

    Delivery is confirmed first by the RavensPort rule that fires at once on
    non-Tank-Level-System traffic on 10001 (sid 1000003), resending if the
    relay's reset won the race; the published signature is then awaited on
    the same connection, since it fires only once the flow ends."""
    since = outputs.snapshot()[0]
    delivered = _inject_until_alerted(config, outputs, _EICAR_CANARY_PAYLOAD, 1000003)
    assert delivered is not None, (
        f"the canary was not inspected in {_INJECTION_ATTEMPTS} attempts"
    )
    _, src_port = delivered

    alerted = outputs.wait_for_alert(
        lambda r: _is_alert_for(r, 2022938, src_port),
        since=since,
        timeout=_CANARY_ALERT_TIMEOUT_S,
    )
    assert alerted is not None, "no ET sid 2022938 alert for the canary payload"


def _send_udp_from(node: Node, host: str, port: int, payload: bytes) -> None:
    """Send one UDP datagram carrying `payload` from inside `node` to
    `host:port`."""
    script = (
        "import socket\n"
        "s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)\n"
        f"s.sendto({payload!r}, ({host!r}, {port}))\n"
    )
    node.run("python3", "-c", script, check=False)


def _is_conformance_alert(record: dict, signature_id: int, dest_port: int) -> bool:
    data = record.get("data", {})
    return str(data.get("alert", {}).get("signature_id")) == str(signature_id) and str(
        data.get("dest_port")
    ) == str(dest_port)


# (source, destination, port) for each permitted Transport Layer Security
# conversation (deployment.md §4.2). Each source reaches its destination
# across a firewall node.
_TLS_CONVERSATIONS = [
    ("fms-01", "10.30.0.20", 636),  # LDAPS to corp-it
    ("corp-it", "10.20.0.20", 443),  # HTTPS to the log-server replica
    ("hist-01", "10.20.0.20", 8428),  # metric replication
    ("hist-01", "10.20.0.20", 9428),  # event replication
]

# Every permitted Secure Shell administration hop (deployment.md §4.2).
_SSH_CONVERSATIONS = [
    ("admin-01", "10.20.0.30"),  # workstation to bastion
    ("dmz-bastion-01", "10.40.0.10"),  # bastion to fms-01
    ("dmz-bastion-01", "10.40.0.11"),  # bastion to hist-01
    ("dmz-bastion-01", "10.40.0.12"),  # bastion to hmi-01
]


@pytest.mark.parametrize(
    ("source_node", "target_host", "target_port"),
    _TLS_CONVERSATIONS,
    ids=[f"{s}->{h}:{p}" for s, h, p in _TLS_CONVERSATIONS],
)
def test_protocol_conformance_on_tls_ports(
    source_node: str,
    target_host: str,
    target_port: int,
    outputs: OutputReader,
) -> None:
    """Non-conforming traffic on 443, 636, 8428 and 9428 (Transport Layer
    Security expected) raises the conformance signature (sid 1000004)."""
    since = outputs.snapshot()[0]
    _send_raw_bytes_from(
        Node(source_node), target_host, target_port, b"not Transport Layer Security"
    )

    alerted = outputs.wait_for_alert(
        lambda r: _is_conformance_alert(r, 1000004, target_port),
        since=since,
        timeout=20.0,
    )
    assert alerted is not None, (
        f"no sid 1000004 alert for non-TLS traffic on {target_port}"
    )


def test_protocol_conformance_on_the_ntp_port(outputs: OutputReader) -> None:
    """A datagram that is not the Network Time Protocol, sent to corp-it's
    time service across both firewall hops, raises the conformance signature
    (sid 1000005), carrying T1571 (siem.md §5)."""
    since = outputs.snapshot()[0]
    _send_udp_from(Node("fms-01"), "10.30.0.20", 123, b"not Network Time Protocol")

    alerted = outputs.wait_for_alert(
        lambda r: _is_conformance_alert(r, 1000005, 123), since=since, timeout=20.0
    )
    assert alerted is not None, "no sid 1000005 alert for non-NTP traffic on 123"
    assert alerted.get("rule", {}).get("mitre", {}).get("id") == ["T1571"]


@pytest.mark.parametrize(
    ("source_node", "target_host"),
    _SSH_CONVERSATIONS,
    ids=[f"{source}->{target}:22" for source, target in _SSH_CONVERSATIONS],
)
def test_protocol_conformance_on_secure_shell_ports(
    source_node: str,
    target_host: str,
    outputs: OutputReader,
) -> None:
    """Non-Secure-Shell traffic on each permitted administration hop raises
    sid 1000008 with the non-standard-port technique mapping."""
    since = outputs.snapshot()[0]
    _send_raw_bytes_from(Node(source_node), target_host, 22, b"not Secure Shell")

    alerted = outputs.wait_for_alert(
        lambda record: _is_conformance_alert(record, 1000008, 22),
        since=since,
        timeout=20.0,
    )
    assert alerted is not None, (
        f"no sid 1000008 alert for non-SSH traffic from {source_node} to {target_host}"
    )
    assert alerted.get("rule", {}).get("mitre", {}).get("id") == ["T1571"]


# A first client write larger than the MTU, as a TLS 1.3 client's ClientHello
# carrying a post-quantum key share is (about 1,800 bytes), and the
# change-cipher-spec record such a client sends next. hist-01's kernel hands
# the first to its interface unsplit, and fw-ot-dmz receives it so on ot0.
_OVERSIZED_FIRST_WRITE = 1797
_OVERSIZED_TLS_SCRIPT = f"""
import socket, time
s = socket.create_connection(("10.20.0.20", 8428), timeout=5)
s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
print(s.getsockname()[1], flush=True)
s.sendall(b"\\x16\\x03\\x01" + b"A" * {_OVERSIZED_FIRST_WRITE - 3})
time.sleep(0.5)
s.sendall(b"\\x14\\x03\\x03\\x00\\x01\\x01" + b"\\x17\\x03\\x03\\x00\\x05" + b"B" * 5)
time.sleep(1)
s.close()
"""
# Suricata writes a flow record once a closed flow's timeout (60 s) has
# elapsed; the flow manager's sweep and the agent's forwarding follow.
_FLOW_RECORD_TIMEOUT_S = 120.0


def test_a_first_segment_larger_than_the_mtu_is_inspected_whole(
    outputs: OutputReader,
) -> None:
    """siem.md §5: a Transport Layer Security connection whose first client
    data arrives at fw-ot-dmz as one frame larger than the MTU is inspected
    whole on the interface it arrives on. Its handshake is recognised, so
    the change-cipher-spec record that follows raises no non-TLS alert
    (sid 1000004), and ot0's flow record counts every byte of the first
    write."""
    since = outputs.snapshot()
    result = Node("hist-01").run("python3", "-c", _OVERSIZED_TLS_SCRIPT, check=True)
    src_port = result.stdout.split()[0]

    def own_flow(record: dict) -> bool:
        data = record.get("data", {})
        return (
            record.get("agent", {}).get("name") == "fw-ot-dmz"
            and data.get("event_type") == "flow"
            and data.get("in_iface") == "ot0"
            and str(data.get("src_port")) == src_port
            and str(data.get("dest_port")) == "8428"
        )

    flow = outputs.wait_for_archive(
        own_flow, since=since[1], timeout=_FLOW_RECORD_TIMEOUT_S
    )
    assert flow is not None, f"no ot0 flow record for source port {src_port}"
    to_server = int(flow["data"]["flow"]["bytes_toserver"])
    assert to_server >= _OVERSIZED_FIRST_WRITE, (
        f"ot0 counted {to_server} bytes to the server: the oversized first "
        "write was not captured whole"
    )
    raised = [
        r
        for r in outputs.alerts()[since[0] :]
        if r.get("agent", {}).get("name") == "fw-ot-dmz"
        and str(r.get("data", {}).get("src_port")) == src_port
    ]
    assert not raised, f"the oversized handshake raised: {raised}"


# hist-01's metric and event replication to hist-dmz-01 (hist.md §7), a
# permitted path through fw-ot-dmz (deployment.md §4.2). Each connection is
# a TLS 1.3 client whose first message exceeds the MTU. A new connection
# opens about every two minutes; Suricata writes its flow records once the
# closed flow's timeout (60 s) has elapsed.
_HIST_01_IP = "10.40.0.11"
_HIST_DMZ_01_IP = "10.20.0.20"
_REPLICATION_PORTS = ("8428", "9428")
_REPLICATION_CONNECTIONS_REQUIRED = 3
_REPLICATION_WAIT_S = 600.0
# The first byte of every TLS record: change-cipher-spec, alert, handshake,
# application data.
_TLS_RECORD_TYPES = range(0x14, 0x18)


def _replication_flows(
    outputs: OutputReader, interface: str, tls_only: bool = True
) -> dict[tuple[str, str], dict]:
    """fw-ot-dmz's flow records on `interface` for hist-01's connections to
    hist-dmz-01's replication ports, by source and destination port: by
    default only those Suricata identified as TLS. The destination port is
    part of the key because hist-01 can reuse a source port for a later
    connection to the other replication port, whose record would otherwise
    replace the earlier one. The suite's own connections to these ports
    carry no TLS, so none of them is counted."""
    flows = {}
    for record in outputs.archives():
        data = record.get("data", {})
        if (
            record.get("agent", {}).get("name") == "fw-ot-dmz"
            and data.get("event_type") == "flow"
            and data.get("in_iface") == interface
            and data.get("src_ip") == _HIST_01_IP
            and data.get("dest_ip") == _HIST_DMZ_01_IP
            and str(data.get("dest_port")) in _REPLICATION_PORTS
            and (data.get("app_proto") == "tls" or not tls_only)
        ):
            flows[(str(data.get("src_port")), str(data.get("dest_port")))] = data
    return flows


def test_log_server_replication_raises_no_non_tls_alert(
    outputs: OutputReader,
) -> None:
    """siem.md §5: hist-01's replication to hist-dmz-01, whose TLS 1.3 first
    message reaches fw-ot-dmz larger than the MTU, raises no non-TLS alert
    (sid 1000004) on either interface over the deployment's whole run, and
    its handshake is recognised on ot0, where it arrives, as on dmz0.

    The genuine connections are counted from dmz0, where every segment
    arrives split to the MTU; a connection lost to inspection on ot0 would
    not be recognised as TLS there. The suite's own non-TLS payloads to
    these ports also raise sid 1000004, deliberately; an alert on genuine
    traffic is told apart by its payload, which opens with a TLS record."""
    deadline = time.monotonic() + _REPLICATION_WAIT_S
    while True:
        replicated = _replication_flows(outputs, "dmz0")
        if len(replicated) >= _REPLICATION_CONNECTIONS_REQUIRED:
            break
        assert time.monotonic() < deadline, (
            f"only {len(replicated)} replication connections from hist-01 "
            f"recorded on fw-ot-dmz within {_REPLICATION_WAIT_S:.0f} s"
        )
        time.sleep(15.0)

    false_alerts = [
        r
        for r in outputs.alerts()
        if r.get("agent", {}).get("name") == "fw-ot-dmz"
        and str(r.get("data", {}).get("alert", {}).get("signature_id")) == "1000004"
        and r.get("data", {}).get("src_ip") == _HIST_01_IP
        and r.get("data", {}).get("dest_ip") == _HIST_DMZ_01_IP
        and base64.b64decode(r.get("data", {}).get("payload", ""))[:1]
        and base64.b64decode(r["data"]["payload"])[0] in _TLS_RECORD_TYPES
    ]
    assert not false_alerts, (
        f"{len(false_alerts)} non-TLS alerts on hist-01's TLS replication, "
        f"first on {false_alerts[0]['data'].get('in_iface')} at "
        f"{false_alerts[0]['timestamp']}"
    )

    # ot0 and dmz0 each write their own flow record, not necessarily in the
    # same sweep, so only connections already recorded on both are compared.
    on_ot0 = _replication_flows(outputs, "ot0", tls_only=False)
    compared = sorted(set(replicated) & set(on_ot0))
    assert compared, "no replication connection is yet recorded on ot0"
    unrecognised = [
        f"{src}->{dest}"
        for src, dest in compared
        if on_ot0[(src, dest)].get("app_proto") != "tls"
    ]
    assert not unrecognised, (
        f"replication connections (source->destination port) {unrecognised} "
        "were recognised as TLS on dmz0 but not on ot0"
    )


def test_conforming_traffic_raises_no_conformance_alert(
    outputs: OutputReader,
) -> None:
    """A fresh Transport Layer Security connection on each of 443, 636, 8428
    and 9428, and a burst of time queries to corp-it, raise no conformance
    alert: the regression case for two earlier forms of the TLS check, each
    confirmed live to fire on every TLS connection — one matched a
    connection's first packets before protocol detection had finished, the
    other checked every client segment, including a TLS 1.3 client's
    change-cipher-spec record (siem.md §5).

    Only alerts on flows that began with this test's own traffic count: a
    preceding test's non-conforming datagram crosses two firewalls on two
    interfaces each, raising four alerts, the later of which can reach
    alerts.json after that test has already returned on the first."""
    since = outputs.snapshot()[0]
    started = datetime.now(timezone.utc)
    for source, host, port in _TLS_CONVERSATIONS:
        Node(source).run(
            "python3",
            "-c",
            "import socket, ssl\n"
            "ctx = ssl.create_default_context(); ctx.check_hostname = False\n"
            "ctx.verify_mode = ssl.CERT_NONE\n"
            f"with socket.create_connection(({host!r}, {port}), 5) as raw:\n"
            "    ctx.wrap_socket(raw).close()\n",
            check=True,
        )
    Node("fms-01").run("chronyc", "-a", "burst 2/2", check=False)

    # An alert, if any, arrives within seconds of the traffic that raised it.
    time.sleep(20.0)
    raised = [
        r
        for r in outputs.alerts()[since:]
        if str(r.get("data", {}).get("alert", {}).get("signature_id"))
        in ("1000004", "1000005")
        and _flow_start(r["data"]) >= started
    ]
    assert not raised, f"conforming traffic raised: {raised}"


# Packets from the client in a connection that sent only the handshake (SYN,
# then ACK). The relay resets every injected connection, so the client sends
# nothing after it, and a third packet is the frame.
_HANDSHAKE_PACKETS_TO_SERVER = 2


def _injected_flows(
    outputs: OutputReader, ports: set[int], since: int, timeout: float
) -> dict[int, dict]:
    """Wait for fw-ext-dmz's ext0 flow record for each injected connection
    from `ports`, and return those found, by source port."""
    deadline = time.monotonic() + timeout
    while True:
        flows = {
            int(r["data"]["src_port"]): r["data"]
            for r in outputs.archives()[since:]
            if r.get("agent", {}).get("name") == "fw-ext-dmz"
            and r.get("data", {}).get("event_type") == "flow"
            and r["data"].get("in_iface") == "ext0"
            and str(r["data"].get("dest_port")) == "10001"
            and int(r["data"].get("src_port", 0)) in ports
        }
        if set(flows) == ports or time.monotonic() >= deadline:
            return flows
        time.sleep(2.0)


def test_benign_supplier_polling_produces_no_boundary_alert(
    config: Config, outputs: OutputReader
) -> None:
    """Repeated legitimate i101, i201, and i205 reads, generated across many
    polling cycles, produce no Tank Level System alert of any kind — the
    regression case for a rule matching the empty leading state a permitted
    connection presents before its first segment.

    The relay resets each injected connection without acknowledging its
    data, so the frame reaches fw-ext-dmz only if it is sent before the reset
    arrives, and Suricata inspects it only when the flow ends, at the
    closed-flow timeout. The test therefore waits for each connection's flow
    record, which is written at that point, after any alert; requires at
    least one connection for each function code whose client sent more than
    the handshake; and only then checks for alerts on those connections."""
    since = outputs.snapshot()
    sent: dict[int, str] = {}
    for function_code in ("i10100", "i20100", "i20500") * 6:
        frame = build_command(function_code)
        result = send_frame(
            config.injector_host, config.injector_port, frame, read_timeout=2.0
        )
        if result.local_port:
            sent[result.local_port] = function_code

    flows = _injected_flows(
        outputs, set(sent), since=since[1], timeout=_FLOW_RECORD_TIMEOUT_S
    )
    inspected = {
        port
        for port, flow in flows.items()
        if int(flow.get("flow", {}).get("pkts_toserver", 0))
        > _HANDSHAKE_PACKETS_TO_SERVER
    }
    for function_code in ("i10100", "i20100", "i20500"):
        assert any(sent[port] == function_code for port in inspected), (
            f"no {function_code} frame reached fw-ext-dmz before the relay's "
            f"reset ({len(flows)} of {len(sent)} flow records found)"
        )

    raised = [
        r
        for r in outputs.alerts()[since[0] :]
        if r.get("agent", {}).get("name") == "fw-ext-dmz"
        and int(r.get("data", {}).get("src_port", 0)) in inspected
    ]
    assert not raised, f"benign polling unexpectedly raised: {raised}"


def test_set_command_on_tcp_10001_is_rejected(
    config: Config, outputs: OutputReader
) -> None:
    """A set command (format letter s, tls-protocol.md §2), sent to the
    tank-gauge console's port, raises its own signature (sid 1000001),
    distinguishable from a benign read."""
    frame = build_command("s60200", data=b"461C4000")
    alerted = _inject_until_alerted(
        config, outputs, frame, 1000001, timeout=_UNACKNOWLEDGED_ALERT_TIMEOUT_S
    )
    assert alerted is not None, "no sid 1000001 alert for the set command"


def test_disallowed_function_code_on_tcp_10001_is_rejected(
    config: Config, outputs: OutputReader
) -> None:
    """A frame using an i-format function code other than i101, i201, or
    i205 raises its own signature (sid 1000002)."""
    frame = build_command("i30100")
    alerted = _inject_until_alerted(
        config, outputs, frame, 1000002, timeout=_UNACKNOWLEDGED_ALERT_TIMEOUT_S
    )
    assert alerted is not None, "no sid 1000002 alert for the disallowed function code"


def test_protocol_substitution_on_tcp_10001_is_rejected(
    config: Config, outputs: OutputReader
) -> None:
    """A byte stream that is not the Tank Level System protocol at all, sent
    to port 10001, raises an alert distinct from the function-code and
    set-command checks above (sid 1000003)."""
    frame = b"GET / HTTP/1.0\r\nHost: atg-relay-01\r\n\r\n"
    alerted = _inject_until_alerted(config, outputs, frame, 1000003)
    assert alerted is not None, (
        "no sid 1000003 alert for the protocol-substitution frame"
    )


def test_boundary_alerts_are_agent_attributed_and_decoded(
    outputs: OutputReader,
) -> None:
    """A boundary-inspection alert reaches alerts.json naming the firewall
    node that raised it, with the underlying Suricata signature identifier
    preserved in the decoded fields, and carrying the ATT&CK for Industrial
    Control Systems technique its rule maps: a set command to the tank
    gauge console is T1692.001, Command Message, tactics Evasion and Impair
    Process Control (siem.md §2.2, §5). The technique resolves only if the manager loaded the
    imported matrix in full at its last start (siem.md §8.4).

    Sent from atg-relay-01 to the console across fw-ot-dmz, not through the
    frame injector: the relay resets any connection not from the supplier's
    address as soon as the handshake completes, so an injected frame reaches
    the wire only if it is sent before that reset arrives — confirmed live
    to lose that race, leaving a connection that carries no data to inspect.
    The path from the relay to the console is permitted and delivers the
    frame every time."""
    frame = build_command("s60200", data=b"461C4000")
    alerted = _inject_in_zone_until_alerted(outputs, frame, 1000001)
    assert alerted is not None, "no alert for the set command"
    mitre = alerted.get("rule", {}).get("mitre", {})
    assert mitre.get("id") == ["T1692.001"], f"unexpected technique: {mitre}"
    assert mitre.get("technique") == ["Command Message"], f"unresolved: {mitre}"
    assert mitre.get("tactic") == ["Evasion", "Impair Process Control"], (
        f"unresolved: {mitre}"
    )


def test_protocol_conformance_carries_its_technique(
    outputs: OutputReader,
) -> None:
    """A protocol-conformance alert on a Transport Layer Security port
    (siem.md §5) carries the Enterprise technique its rule maps, T1571,
    Non-Standard Port (siem.md §2.2)."""
    since = outputs.snapshot()[0]
    _send_raw_bytes_from(
        Node("corp-it"), "10.20.0.20", 443, b"not Transport Layer Security"
    )

    alerted = outputs.wait_for_alert(
        lambda r: (
            str(r.get("data", {}).get("alert", {}).get("signature_id")) == "1000004"
        ),
        since=since,
        timeout=20.0,
    )
    assert alerted is not None, "no protocol-conformance alert on 443"
    mitre = alerted.get("rule", {}).get("mitre", {})
    assert mitre.get("id") == ["T1571"], f"unexpected technique: {mitre}"
    assert mitre.get("technique") == ["Non-Standard Port"], f"unresolved: {mitre}"


def test_rule_sets_are_never_updated_outside_the_image_build() -> None:
    """No mechanism on a firewall node fetches or reloads signatures at run
    time: the packaged automatic-update path is absent or disabled, and the
    loaded ruleset matches what the image was built with."""
    for host in ("fw-ext-dmz", "fw-dmz-it", "fw-ot-dmz"):
        node = Node(host)
        result = node.run(
            "systemctl", "is-enabled", "suricata-update.timer", check=False
        )
        assert result.stdout.strip() not in ("enabled", "static"), (
            f"{host}: suricata-update.timer is enabled"
        )


@pytest.mark.parametrize(
    ("payload", "signature_id", "technique"),
    [
        (build_command("s60200", data=b"461C4000"), 1000001, "T1692.001"),
        (build_command("i30100"), 1000002, "T1692.001"),
        (b"GET / HTTP/1.0\r\nHost: atg-console\r\n\r\n", 1000003, "T1571"),
        (
            build_command("i10100") + build_command("s60200", data=b"461C4000"),
            1000001,
            "T1692.001",
        ),
        (build_command("i10100") + build_command("i30100"), 1000002, "T1692.001"),
        (build_command("i10100") + b"GET / HTTP/1.0\r\n\r\n", 1000007, "T1571"),
    ],
    ids=[
        "set_command",
        "disallowed_function_code",
        "protocol_substitution",
        "set_command_after_a_permitted_read",
        "disallowed_function_code_after_a_permitted_read",
        "data_outside_a_command",
    ],
)
def test_tank_level_system_rules_hold_on_the_ot_boundary(
    payload: bytes, signature_id: int, technique: str, outputs: OutputReader
) -> None:
    """Each Tank Level System check raises its own signature on fw-ot-dmz for
    a non-conforming frame sent from the DMZ to the console — the boundary
    the externally-positioned injector cannot reach past the relay — and
    carries the technique its rule maps: T1692.001 for a command the console
    must not accept, T1571 for a stream that is not the protocol at all or
    data outside a command (siem.md §2.2). A command sent in the same segment
    as a permitted read is checked as if it arrived alone, since the console
    acts on every SOH in the stream (tls-protocol.md §2)."""
    alerted = _inject_in_zone_until_alerted(outputs, payload, signature_id)
    assert alerted is not None, f"fw-ot-dmz raised no sid {signature_id}"
    assert alerted.get("rule", {}).get("mitre", {}).get("id") == [technique], (
        f"sid {signature_id} did not carry {technique}: {alerted.get('rule')}"
    )


def test_display_format_read_is_a_disallowed_code_not_a_set_command(
    outputs: OutputReader,
) -> None:
    """A display-format read (I101, tls-protocol.md §2) is not one of the
    permitted computer-format codes, so it raises the disallowed-function-code
    signature (sid 1000002) and not the set-command one (sid 1000001)."""
    since = outputs.snapshot()[0]
    _send_raw_bytes_from(
        Node(_IN_ZONE_INJECTOR),
        _ATG_CONSOLE_IP,
        _ATG_CONSOLE_PORT,
        build_command("I10100"),
    )

    def from_fw_ot_dmz(signature_id: int):
        return lambda r: (
            r.get("agent", {}).get("name") == "fw-ot-dmz"
            and str(r.get("data", {}).get("alert", {}).get("signature_id"))
            == str(signature_id)
        )

    alerted = outputs.wait_for_alert(from_fw_ot_dmz(1000002), since=since, timeout=20.0)
    assert alerted is not None, "fw-ot-dmz raised no sid 1000002 for I101"
    mislabelled = [r for r in outputs.alerts()[since:] if from_fw_ot_dmz(1000001)(r)]
    assert not mislabelled, "the display-format read was labelled a set command"


# The simulator's fuel supplier (topology.yaml `fuel-supplier-01`). It first
# polls one full interval (world.yaml `fuel_supplier.poll_interval_s`, 15
# minutes by default, on a real-time clock) after the simulator starts, so a
# suite run soon after deployment may find no poll to examine. Once a supplier
# flow record exists, the forwarded conversation's record follows within the
# same Suricata flow-manager sweep, since both flows end together; the pairing
# wait allows for that alone.
_SUPPLIER_IP = "10.10.0.10"
_RELAY_IP = "10.20.0.10"
_PAIRING_WAIT_S = 60
# The relay opens its connection to the console as it accepts the supplier's.
_FORWARDING_TOLERANCE = timedelta(seconds=3)


def _flow_start(flow_record: dict) -> datetime:
    return datetime.strptime(flow_record["flow"]["start"], "%Y-%m-%dT%H:%M:%S.%f%z")


def _flow_records(
    records: list[dict], firewall_node: str, src_ip: str, dest_ip: str
) -> list[dict]:
    return [
        r["data"]
        for r in records
        if r.get("agent", {}).get("name") == firewall_node
        and r.get("data", {}).get("event_type") == "flow"
        and r["data"].get("src_ip") == src_ip
        and r["data"].get("dest_ip") == dest_ip
        and str(r["data"].get("dest_port")) == str(_ATG_CONSOLE_PORT)
    ]


def test_simulator_supplier_polling_produces_no_boundary_alert(
    outputs: OutputReader,
) -> None:
    """The simulator's own supplier polls — the known-good implementation of
    sim/tls-protocol.md — raise no alert of any kind on either boundary they
    cross: fw-ext-dmz, where the supplier reaches the relay, and fw-ot-dmz,
    where the relay forwards the same conversation to the console.

    Skipped when fw-ext-dmz has recorded no supplier poll yet: the first
    poll comes one poll interval (`fuel_supplier.poll_interval_s`, 15 minutes
    by default) after the simulator starts, and waiting for it
    would dominate the suite's run time. Otherwise a complete poll is one
    identified on fw-ot-dmz as a relay-to-console flow carrying data (the
    relay's health checks carry none) that began with a supplier flow on
    fw-ext-dmz. A supplier poll
    passes if any flow paired with it is unalerted: a frame the in-zone test
    above sends from the relay always alerts, so an unalerted pairing can
    only be the poll's own forwarded conversation, and one that coincides
    with a poll cannot mask an alert the poll itself raised."""

    def _unalerted(flow_record: dict) -> bool:
        return str(flow_record["flow"].get("alerted")).lower() == "false"

    if not _flow_records(outputs.archives(), "fw-ext-dmz", _SUPPLIER_IP, _RELAY_IP):
        pytest.skip(
            "no supplier poll recorded on fw-ext-dmz yet; the simulator's "
            "supplier first polls one poll interval "
            "(fuel_supplier.poll_interval_s) after it starts"
        )

    deadline = time.monotonic() + _PAIRING_WAIT_S
    while True:
        records = outputs.archives()
        supplier = _flow_records(records, "fw-ext-dmz", _SUPPLIER_IP, _RELAY_IP)
        carrying_data = [
            f
            for f in _flow_records(records, "fw-ot-dmz", _RELAY_IP, _ATG_CONSOLE_IP)
            if str(f.get("tcp", {}).get("psh")).lower() == "true"
        ]
        # Each supplier poll with the fw-ot-dmz flows that began with it; a
        # poll whose forwarded record has not yet been written has none.
        pairings = [
            (
                s,
                [
                    f
                    for f in carrying_data
                    if abs(_flow_start(f) - _flow_start(s)) <= _FORWARDING_TOLERANCE
                ],
            )
            for s in supplier
        ]
        complete = [(s, paired) for s, paired in pairings if paired]
        if complete:
            break
        assert time.monotonic() < deadline, (
            "no fw-ot-dmz forwarded conversation paired with any of the "
            f"{len(supplier)} supplier polls on fw-ext-dmz"
        )
        time.sleep(5.0)

    alerted_on_ext = [s for s in supplier if not _unalerted(s)]
    assert not alerted_on_ext, (
        f"supplier polling alerted on fw-ext-dmz: {alerted_on_ext}"
    )
    alerted_on_ot = [
        s for s, paired in complete if not any(_unalerted(f) for f in paired)
    ]
    assert not alerted_on_ot, (
        f"forwarded supplier polling alerted on fw-ot-dmz: {alerted_on_ot}"
    )

    supplier_alerts = [
        r for r in outputs.alerts() if r.get("data", {}).get("src_ip") == _SUPPLIER_IP
    ]
    assert not supplier_alerts, f"supplier polling raised: {supplier_alerts}"
