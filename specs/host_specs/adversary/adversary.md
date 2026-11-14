# Adversary

A plain host on the External and information-technology zones, from which attacks
against RavensPort are launched. It models nothing in `narrative.md` and is not part of the asset. The
adversary host, and the attacks run from it, belong to a separate phase of the
project, independent of the specifications that define what RavensPort is and
how it operates in the absence of an attack. Those attacks and their tooling are
described under `specs/attack_chains/`, not here.

## 1. Placement

The node has two interfaces of equal standing: `eth0` on the External zone
(`vnet-ext`) and `it0` on the information-technology (IT) zone (`vnet-it`). Each
gives the host a link-layer presence on its zone, from which attacks against the
hosts reachable there are launched.

| Property | External interface | IT interface |
|---|---|---|
| Bridge | `vnet-ext` | `vnet-it` |
| Interface name | `eth0` | `it0` |
| Address (bound per stage) | 10.10.0.20/24 | 10.30.0.50/24 |

| Property | Value |
|---|---|
| Instance type | Container, in both deployment profiles |
| Base image | `images:ubuntu/24.04/cloud`, as every managed node |
| Build script | `adversary` |

Both interfaces boot **down and addressless, with an empty routing table**. At
rest the host holds no knowledge of the wider network — no address, no gateway,
no route — only the latent link-layer wiring of its two NICs to their bridges.
That wiring (which bridge each NIC attaches to, and so which zone it is on) is
fixed at deployment and cannot be changed from within the host; the addresses in
the table above are not configured at boot but are the addresses a stage binds
when it activates a NIC. This is the physical counterpart of the attack model's
rule that an adversary knows nothing it has not been seeded or has not
discovered: a boot-time route would be knowledge of the network the adversary was
never given.

Link-layer presence on a zone is a seeded fact — `Segment(Zone)`, carrying the
interface name and the address the host binds there. A stage that requires that
fact brings the zone's NIC up and binds its address for the duration of the
stage, then removes the address and brings the NIC down again (the per-stage
activation of `stage-writing-rules.md`). Binding the address is what creates the
connected route for that zone's own subnet — the whole of the routing knowledge
that link-layer presence confers, and no more.

Reaching a host in another zone needs a route through a firewall node, and none
is pre-configured. The gateway is discovered at attack time by observation — a
sweep of the local segment for live hosts, then a forwarding probe (a TTL-limited
packet that a router answers and a plain host does not) that identifies which is
the router — and a route to an off-segment target is installed transiently by the
stage that needs it and removed when that stage ends. Both bridges are isolated,
carrying no address on the Incus host and no network address translation, so
every packet the host sends to another zone must be forwarded by a firewall node.

On the External zone the host shares the segment with the fuel supplier's
monitoring address and the External gateway `fw-ext-dmz` (10.10.0.2); on the IT
zone it has link-layer adjacency to the identity provider `corp-it`, the gateway
`fw-dmz-it` (10.30.0.2), and `recon-01`.

## 2. Contents

The `adversary` build script installs no service and the node declares no
listening port. Beyond the static network configuration and an `/etc/hosts`
holding only loopback and the host's own entry, the image carries three
utilities, installed directly by that script: the packet-capture program
`tcpdump`, the network scanner `nmap`, and the LDAP client `ldap-utils`
(`ldapsearch`/`ldapwhoami`/`ldappasswd`/`ldapmodify`). These serve the capture,
scan, and directory stages of the attack phase, standing requirements of that
phase rather than tooling an individual task binary can supply for itself. The
reconciliation-workstation session stages need no image tooling: their Remote
Desktop Protocol client is compiled into the task binary itself (an in-process
IronRDP client), so it depends only on that binary being delivered.

The host models a machine entirely outside RavensPort, and only its two NICs sit
inside RavensPort broadcast domains, as the footholds the modelled attacks use.
It therefore holds no material belonging to the asset. In particular the host
has:

- no `resolve:` entries, so no port hostname is resolvable and every
  destination must be addressed by literal address;
- no internal CA material: it neither trusts nor holds the RavensPort CA root
  (which every asset node does, `digital.md §2.7`), and carries no certificate
  or key of the fleet;
- no chrony client, so it does not read the site clock;
- no nftables ruleset, as it is not a firewall node.

The image holds every compiled attack task binary, and the catalogue
`chains.json` beside them, in `/root`. The image's build copies them from
`attack-chains/dist`, which `make up` populates before the image is built: by
building them from the generator source where it is present, or, in the public
release that ships without that source, by staging the prebuilt binaries for the
host's architecture (`attack-chains/prebuilt/<arch>/`) after verifying their
checksums.
