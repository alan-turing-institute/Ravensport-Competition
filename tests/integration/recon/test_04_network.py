"""
Name resolution and containment (`recon-tests.md` §6), targeting `recon.md` §3.

**Neither firewall policy nor intra-zone reachability is asserted here**, and
both omissions are deliberate.

Firewall policy is enumerated fleet-wide by
`deploy/tests/integration/test_policy.sh`, which already covers this host more
thoroughly than a per-host suite would: the permitted flow to `hist-dmz-01:443`,
the wrong-port cases, the wrong-destination case, ICMP across both boundaries,
and the blocked reverse direction. `test_internet_egress.sh` covers outbound
internet, and `test_policy.sh` §15 the sim-host egress guard. Keeping it there
gives a rule change one set of tests to update rather than one per host, and
that suite's `blocked()` helper distinguishes a firewall DROP from a destination
that is merely down — a distinction a simple connect cannot make, and without
which a negative assertion passes when the target is stopped.

Intra-zone reachability — that hosts on one zone bridge reach each other without
restriction — is a property of every zone rather than of this workstation, so it
belongs in a fleet-wide test and not here. `digital.md` §1.2 states it; nothing
asserts it yet.

What remains is what is a property of this image.

Run on the VM: python -m pytest tests/integration/recon/test_04_network.py
"""

from __future__ import annotations

from tests.integration.recon.conftest import Node


def test_resolves_only_the_hosts_it_connects_to(recon: Node) -> None:
    """`/etc/hosts` carries loopback, this host, `hist-dmz-01` — the name the
    audit uses — `corp-it` — the name the directory bind uses (`recon.md` §3,
    §7.1.1) — and `siem-01` — the name the security monitoring agent forwards
    to (§3.3) — and nothing else (`recon-tests.md` §6).

    This is containment rather than function: a hosts file naming the wider
    topology hands an attacker on this workstation a map of the environment at no
    cost. It is why the fleet renders a minimal per-node file instead of a shared
    one, and it is a property of this image rather than of the network, which is
    why it survives here while the reachability assertions do not."""
    hosts = recon.read("/etc/hosts")
    names = {
        field
        for line in hosts.splitlines()
        if line.strip() and not line.startswith("#")
        for field in line.split()[1:]
    }
    unexpected = names - {"localhost", "recon-01", "hist-dmz-01", "corp-it", "siem-01"}
    assert not unexpected, (
        f"/etc/hosts names hosts this workstation has no business resolving: "
        f"{sorted(unexpected)}"
    )


def _listening_sockets(recon: Node) -> dict[int, str]:
    """Ports listening on a non-loopback address, mapped to the address.

    Loopback binds are unreachable from `vnet-it` and from the sim, so they are
    not a channel in the sense `recon.md` §11.1 means. A wildcard bind (`*`,
    `0.0.0.0`, `::`) is not loopback: it accepts on every interface.
    """
    found: dict[int, str] = {}
    for line in recon.stdout("ss", "-tlnH").splitlines():
        fields = line.split()
        if len(fields) < 4:
            continue
        local = fields[3]
        address, _, port = local.rpartition(":")
        if not port.isdigit():
            continue
        # Strip a zone identifier (`127.0.0.53%lo`) and brackets round an
        # IPv6 literal (`[::1]`) before classifying.
        bare = address.split("%")[0].strip("[]")
        if bare.startswith("127.") or bare == "::1":
            continue
        found[int(port)] = local
    return found


def test_only_the_remote_desktop_service_listens(recon: Node, rdp_port: int) -> None:
    """The set of non-loopback listening TCP sockets is exactly
    `{3389}` (`recon-tests.md` §6).

    Targets `recon.md` §11.1: the remote desktop session is the only channel
    between this workstation and the actor, which holds only while nothing else
    accepts a connection.

    **An enumeration, not a probe for named services.** A test looking for a
    secure shell daemon would pass on a host running a file transfer service
    instead; §11.1 needs there to be no second channel, not that one particular
    one is missing.

    **A deliberate addition fails this, which is the intent** — §11.1 records
    that a remote-access service here is a separate and legitimate decision, so
    the change becomes visible in review. sssd accepts on a Unix socket and the
    §3.3 monitoring agent connects outbound, so neither disturbs it.
    """
    listening = _listening_sockets(recon)
    assert set(listening) == {rdp_port}, (
        f"non-loopback listeners are {dict(sorted(listening.items()))}, "
        f"expected only {{{rdp_port}}}. Every extra entry is a second channel "
        "between this workstation and anything that can reach the zone "
        "(`recon.md` §11.1)."
    )
