"""
Security monitoring topology tests (siem-tests.md §10), targeting siem.md §8.1.

siem-01 sits in the IT zone at 10.30.0.30. Every monitored node's `resolve:`
list gains siem-01, and two firewall_rules entries admit agent traffic on TCP
1514 across the two-hop OT→DMZ→IT path, matching the existing pattern for
LDAPS transit to corp-it (deployment.md §4.3, test_topology.py's
transit-rule tests).

Run with: pytest tests/unit -q
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # deploy/
sys.path.insert(0, str(ROOT / "scripts"))
from render import load_yaml  # noqa: E402

_MONITORED_NODES = (
    "fms-01",
    "hmi-01",
    "hist-01",
    "hist-dmz-01",
    "atg-relay-01",
    "dmz-bastion-01",
    "corp-it",
    "recon-01",
    "admin-01",
    "fw-ext-dmz",
    "fw-dmz-it",
    "fw-ot-dmz",
)


def _topology() -> dict:
    return load_yaml(ROOT / "topology.yaml")


def test_siem_01_is_a_node_in_the_it_zone():
    t = _topology()
    siem = t["nodes"].get("siem-01")
    assert siem is not None, "siem-01 missing from topology.yaml"
    nic = siem["nics"][0]
    assert nic["network"] == "vnet-it"
    assert nic["ipv4"] == "10.30.0.30"


def test_every_monitored_node_resolves_siem_01():
    """Every monitored node's resolve: list must gain siem-01
    (siem-tests.md §10) so its agent can address the manager by name if it ever
    needs to, matching the fleet-wide minimal-resolve convention."""
    t = _topology()
    for name in _MONITORED_NODES:
        node = t["nodes"].get(name)
        assert node is not None, f"{name} missing from topology.yaml"
        assert "siem-01" in node.get("resolve", []), (
            f"{name}'s resolve: list does not include siem-01"
        )


def test_agent_firewall_rules_admit_tcp_1514_both_hops():
    """siem.md §8.1: two firewall_rules entries, one per hop of the
    OT→DMZ→IT transit path, both destined for siem-01 on TCP 1514."""
    t = _topology()
    rules = [
        r
        for r in t.get("firewall_rules", [])
        if r.get("dst") == "siem-01" and r.get("port") == 1514
    ]
    hops = {(r["from"], r["to"], r["proto"]) for r in rules}
    assert ("vnet-ot", "vnet-dmz", "tcp") in hops, (
        "missing OT→DMZ (hop 1) firewall rule for siem-01:1514/tcp"
    )
    assert ("vnet-dmz", "vnet-it", "tcp") in hops, (
        "missing DMZ→IT (hop 2) firewall rule for siem-01:1514/tcp"
    )


def test_agent_firewall_rules_render_on_correct_fw_node():
    """The two hops must appear in fw-ot-dmz's and fw-dmz-it's rendered
    nftables, with siem-01's IP as the destination — the rendered
    counterpart to the source-rule assertion above."""
    import subprocess

    out = subprocess.run(
        [sys.executable, "scripts/render.py", "--profile", "full"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode == 0, out.stderr
    build = ROOT / "build"

    siem_ip = _topology()["nodes"]["siem-01"]["nics"][0]["ipv4"]

    fw_ot_dmz = (build / "nftables" / "fw-ot-dmz.nft").read_text()
    assert siem_ip in fw_ot_dmz, "siem-01 IP missing from fw-ot-dmz nftables"
    assert 'iifname "ot0" oifname "dmz0"' in fw_ot_dmz
    assert "tcp dport 1514" in fw_ot_dmz, "fw-ot-dmz missing tcp dport 1514 for siem-01"

    fw_dmz_it = (build / "nftables" / "fw-dmz-it.nft").read_text()
    assert siem_ip in fw_dmz_it, "siem-01 IP missing from fw-dmz-it nftables"
    assert 'iifname "dmz0" oifname "it0"' in fw_dmz_it
    assert "tcp dport 1514" in fw_dmz_it, "fw-dmz-it missing tcp dport 1514 for siem-01"


def test_dmz_side_hosts_have_a_route_to_the_it_zone():
    """siem.md §8.1: the firewall rules permit the agent-traffic path but do
    not create the routes it uses. DMZ-positioned hosts need an explicit
    route to vnet-it via fw-dmz-it, or their agent traffic to siem-01 never
    leaves the host — the same gotcha the two-hop LDAPS path already
    required a route for on the firewall nodes (test_fw_nodes_have_transit_routes_in_network_config)."""
    t = _topology()
    for name in ("atg-relay-01", "fw-ext-dmz"):
        node = t["nodes"][name]
        # Checked across every NIC, not just the first: fw-ext-dmz carries
        # this route on its vnet-dmz NIC (nics[1]), not its vnet-ext one
        # (nics[0]) — the same placement fw-ot-dmz's analogous route uses.
        routes = [r for nic in node["nics"] for r in nic.get("routes", [])]
        assert any(r["to"] == "10.30.0.0/24" for r in routes), (
            f"{name} has no route to vnet-it (10.30.0.0/24); its agent "
            "traffic to siem-01 cannot reach the second hop"
        )
