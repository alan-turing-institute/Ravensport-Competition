"""Single-source-of-truth check for the sim-host network configuration.

sim-net.sh declares the sim-host interfaces and addresses by hand, and
topology.yaml declares them again for the orchestrator. They must agree —
adding or changing an entity in one without the other is a silent routing
misconfiguration. This catches that drift.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]  # deploy/
_IPV4 = r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}"


def _sim_net_nics() -> dict[str, str]:
    """name -> ipv4 from the NICS="..." list in sim-net.sh (rows of
    `name address prefix gateway table`)."""
    text = (ROOT / "sim-host" / "sim-net.sh").read_text()
    nics: dict[str, str] = {}
    in_block = False
    for line in text.splitlines():
        if line.startswith("NICS="):
            in_block = True
            continue
        if in_block:
            if line.strip() == '"':
                break
            parts = line.split()
            if len(parts) == 5 and re.fullmatch(_IPV4, parts[1]):
                nics[parts[0]] = parts[1]
    return nics


def _topology_sim_host_nics() -> dict[str, str]:
    t = yaml.safe_load((ROOT / "topology.yaml").read_text())
    return {n["name"]: n["ipv4"] for n in t["nodes"]["sim-host"]["nics"]}


def test_sim_net_nics_match_topology():
    """Every sim-host NIC name+address in sim-net.sh matches topology.yaml."""
    assert _sim_net_nics() == _topology_sim_host_nics()


def test_sim_net_disables_ip_forwarding():
    """sim-host is a multi-homed endpoint on vnet-ext, vnet-ot, and vnet-it,
    never a router between them. sim-net.sh must disable IPv4 forwarding so the
    kernel cannot bridge the three zones via their directly connected routes."""
    text = (ROOT / "sim-host" / "sim-net.sh").read_text()
    assert re.search(r"echo\s+0\s*>\s*/proc/sys/net/ipv4/ip_forward", text), (
        "sim-net.sh must disable net.ipv4.ip_forward"
    )


def test_egress_guard_drops_forwarded_traffic():
    """egress-guard.nft must carry a forward chain with a drop policy so that,
    even if IPv4 forwarding were enabled, sim-host cannot route transit traffic
    between its zones."""
    text = (ROOT / "sim-host" / "egress-guard.nft").read_text()
    m = re.search(r"chain\s+forward\s*\{.*?\}", text, re.DOTALL)
    assert m, "egress-guard.nft has no forward chain"
    body = m.group(0)
    assert re.search(r"hook\s+forward", body), "forward chain missing forward hook"
    assert "policy drop" in body, "forward chain must default to drop"
