"""Shared host lists for the security-monitoring integration suite
(siem-tests.md).

Twelve hosts carry a Wazuh agent (siem.md §1); `sim-host` reports nothing about
itself (siem.md §4.4) and never appears here.
"""

from __future__ import annotations

MONITORED_HOSTS: list[str] = [
    "corp-it",
    "fms-01",
    "hmi-01",
    "hist-01",
    "hist-dmz-01",
    "recon-01",
    "admin-01",
    "atg-relay-01",
    "dmz-bastion-01",
    "fw-ext-dmz",
    "fw-dmz-it",
    "fw-ot-dmz",
]

# The three boundary nodes additionally run Suricata (siem.md §5) and forward
# its event file (§3).
FIREWALL_HOSTS: list[str] = ["fw-ext-dmz", "fw-dmz-it", "fw-ot-dmz"]

# Hosts whose file inputs siem.md §3 names, beyond the systemd journal every
# monitored host carries.
NAMED_LOG_FILE_HOSTS: dict[str, str] = {
    "fms-01": "/var/log/postgresql/",
    "recon-01": "sssd",
    "fw-ext-dmz": "/var/log/suricata/eve.json",
    "fw-dmz-it": "/var/log/suricata/eve.json",
    "fw-ot-dmz": "/var/log/suricata/eve.json",
}

# Cross-zone conversations permitted by deployment.md §4.2, one row per
# firewall_rules entry: (originating firewall node, destination, port, proto).
CROSS_ZONE_CONVERSATIONS: list[tuple[str, str, int, str]] = [
    ("fw-ext-dmz", "atg-relay-01", 10001, "tcp"),
    ("fw-ot-dmz", "atg-console", 10001, "tcp"),
    ("fw-dmz-it", "hist-dmz-01", 443, "tcp"),
    ("fw-dmz-it", "dmz-bastion-01", 22, "tcp"),
    ("fw-ot-dmz", "fms-01", 22, "tcp"),
    ("fw-ot-dmz", "hist-01", 22, "tcp"),
    ("fw-ot-dmz", "hmi-01", 22, "tcp"),
    ("fw-ot-dmz", "hist-dmz-01", 8428, "tcp"),
    ("fw-ot-dmz", "hist-dmz-01", 9428, "tcp"),
    ("fw-ot-dmz", "corp-it", 123, "udp"),
    ("fw-dmz-it", "corp-it", 123, "udp"),
    ("fw-ot-dmz", "corp-it", 636, "tcp"),
    ("fw-dmz-it", "corp-it", 636, "tcp"),
    ("fw-ot-dmz", "siem-01", 1514, "tcp"),
    ("fw-dmz-it", "siem-01", 1514, "tcp"),
]

# The destination address of each row above (topology.yaml). A flow record
# carries addresses, not hostnames.
CROSS_ZONE_DESTINATION_IPS: dict[str, str] = {
    "atg-relay-01": "10.20.0.10",
    "atg-console": "10.40.0.51",
    "hist-dmz-01": "10.20.0.20",
    "dmz-bastion-01": "10.20.0.30",
    "fms-01": "10.40.0.10",
    "hist-01": "10.40.0.11",
    "hmi-01": "10.40.0.12",
    "corp-it": "10.30.0.20",
    "siem-01": "10.30.0.30",
}


def tcp_connect_command(host: str, port: int) -> tuple[str, ...]:
    """A command opening and immediately closing one TCP connection to
    `host:port`, for use as a connection trigger inside a monitored host."""
    return (
        "python3",
        "-c",
        (
            "import socket; s = socket.socket(); s.settimeout(5); "
            f"s.connect(({host!r}, {port})); s.close()"
        ),
    )


# (host, command) pairs that open a fresh connection on each cross-zone path
# above whose natural traffic produces no flow record within a test's own
# window. Suricata writes a flow record only once a flow has ended and its
# timeout has elapsed (60 s for a closed TCP flow), so a connection held
# open for the life of the deployment — the agent channel, hist-01's
# replication — produces none until it is torn down, and the fms-01 LDAPS
# bind occurs only at operator sign-in. A connection from fms-01 to the IT
# zone crosses both fw-ot-dmz and fw-dmz-it, so one trigger serves both
# hops. The fw-ext-dmz TCP 10001 row is driven from the runner on vnet-ext
# instead (the frame injector); the relay rejects that connection by source
# address (digital.md §2.5), so it never continues to the console. The
# fw-ot-dmz TCP 10001 row needs no trigger: the relay's HAProxy health check
# opens a connection to the console every two seconds. UDP 123 needs no
# trigger either: every host's chrony polls corp-it continuously.
FLOW_RECORD_TRIGGERS: list[tuple[str, tuple[str, ...]]] = [
    ("corp-it", ("curl", "-sk", "--max-time", "5", "https://10.20.0.20:443/")),
    ("admin-01", tcp_connect_command("10.20.0.30", 22)),
    ("dmz-bastion-01", tcp_connect_command("10.40.0.10", 22)),
    ("dmz-bastion-01", tcp_connect_command("10.40.0.11", 22)),
    ("dmz-bastion-01", tcp_connect_command("10.40.0.12", 22)),
    ("hist-01", tcp_connect_command("10.20.0.20", 8428)),
    ("hist-01", tcp_connect_command("10.20.0.20", 9428)),
    ("fms-01", tcp_connect_command("10.30.0.20", 636)),
    ("fms-01", tcp_connect_command("10.30.0.30", 1514)),
]

# The four conversations siem.md §4.1 names that cross no firewall at all.
INTRA_ZONE_CONVERSATIONS: list[tuple[str, str]] = [
    ("fms-01", "atg-console"),
    ("fms-01", "dispenser"),
    ("fms-01", "vessel-rfid"),
    ("hmi-01", "operator-rfid"),
]

# The peer's address for each conversation above (topology.yaml's sim-host
# nics). A connection event carries addresses, not hostnames — sim-host's
# macvlan devices are never entered into any monitored host's /etc/hosts
# (build/hosts/<node>, §4.4: sim-host reports nothing about itself and holds
# no identity a monitored host would resolve) — so matching against the peer
# name itself, rather than its address, would never find a real record.
INTRA_ZONE_PEER_IPS: dict[str, str] = {
    "atg-console": "10.40.0.51",
    "dispenser": "10.40.0.52",
    "vessel-rfid": "10.40.0.53",
    "operator-rfid": "10.40.0.54",
}

# (host, peer) -> the command forcing a fresh connection, for intra-zone
# conversations the scenario drives too rarely to rely on within a test's
# own window. fms-01 opens its Modbus connection to the dispenser (TCP 502,
# dispenser-modbus-tcp-spec.md §1) only during a dispense, and dispenses
# follow tug arrivals the sim draws at a mean rate of one per hour.
INTRA_ZONE_CONNECTION_TRIGGERS: dict[tuple[str, str], tuple[str, ...]] = {
    ("fms-01", "dispenser"): tcp_connect_command("10.40.0.52", 502),
}

# Connection-event view of the same cross-zone paths CROSS_ZONE_CONVERSATIONS
# describes for flow records: one entry per real host-to-host endpoint pair,
# not one per firewall hop. A conversation crossing two hops (the OT -> DMZ ->
# IT path, deployment.md §4.2) appears once here, at its own two real
# endpoints — a firewall node's own connection-event log carries only traffic
# it makes or accepts itself, never what it merely forwards through its
# `forward` chain, so the two hop-keyed rows CROSS_ZONE_CONVERSATIONS carries
# for one such conversation collapse to a single entry here. Resolved against
# live archived records, not derived from the spec alone.
CROSS_ZONE_CONNECTION_EVENTS: list[tuple[str, str, int, str]] = [
    ("atg-relay-01", "10.40.0.51", 10001, "tcp"),  # -> atg-console
    ("fms-01", "10.30.0.20", 636, "tcp"),  # -> corp-it, LDAPS (two-hop)
    ("corp-it", "10.20.0.20", 443, "tcp"),  # -> hist-dmz-01
    ("admin-01", "10.20.0.30", 22, "tcp"),  # -> dmz-bastion-01
    ("dmz-bastion-01", "10.40.0.10", 22, "tcp"),  # -> fms-01
    ("dmz-bastion-01", "10.40.0.11", 22, "tcp"),  # -> hist-01
    ("dmz-bastion-01", "10.40.0.12", 22, "tcp"),  # -> hmi-01
    ("hist-01", "10.20.0.20", 8428, "tcp"),  # -> hist-dmz-01, metric replication
    ("hist-01", "10.20.0.20", 9428, "tcp"),  # -> hist-dmz-01, event replication
]

# (host, peer address, port) -> the command forcing a fresh connection, for entries above
# whose natural traffic cannot be relied on within a test's own window.
# Confirmed live: hist-01's vmagent/vlagent hold their replication connection
# open rather than reconnecting per event once one is established, and
# nothing on corp-it drives its own hist-dmz-01 request on a predictable
# cadence. The ATG entry needs no trigger — confirmed continuously present
# without one.
CROSS_ZONE_CONNECTION_TRIGGERS: dict[tuple[str, str, int], tuple[str, ...]] = {
    ("fms-01", "10.30.0.20", 636): tcp_connect_command("10.30.0.20", 636),
    ("corp-it", "10.20.0.20", 443): (
        "curl",
        "-sk",
        "--max-time",
        "5",
        "https://10.20.0.20:443/",
    ),
    ("admin-01", "10.20.0.30", 22): tcp_connect_command("10.20.0.30", 22),
    ("dmz-bastion-01", "10.40.0.10", 22): tcp_connect_command("10.40.0.10", 22),
    ("dmz-bastion-01", "10.40.0.11", 22): tcp_connect_command("10.40.0.11", 22),
    ("dmz-bastion-01", "10.40.0.12", 22): tcp_connect_command("10.40.0.12", 22),
    ("hist-01", "10.20.0.20", 8428): (
        "curl",
        "-sk",
        "--max-time",
        "5",
        "https://hist-dmz-01:8428/api/v1/write",
    ),
    ("hist-01", "10.20.0.20", 9428): (
        "curl",
        "-sk",
        "--max-time",
        "5",
        "https://hist-dmz-01:9428/insert/native",
    ),
}

# The two zones either side of the OT/DMZ transit (deployment.md §4.2). Every
# host in each reaches corp-it's NTP service and siem-01's agent channel
# continuously, so any one of them demonstrates the path for that zone.
OT_ZONE_HOSTS: list[str] = ["fms-01", "hmi-01", "hist-01"]
DMZ_ZONE_HOSTS: list[str] = ["atg-relay-01", "hist-dmz-01", "dmz-bastion-01"]
