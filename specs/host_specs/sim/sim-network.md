# Sim host

Specification for the `sim-host` subsystem: the single container that runs
the simulator behind an HAProxy reverse proxy. It covers the network
topology and the sim<->digital boundary on the wire, and the container's
deployment — image, build, process supervision, and hardening.

The simulator (`sim`) binds only the loopback interface; an HAProxy reverse
proxy in the same container owns every network interface and zone address
and bridges between the zone endpoints and the simulator's loopback ports.

See [sim.md](sim.md) §7 and [digital.md](digital.md) §3 for the boundary
protocols realised on the wire.

---

## 1. Topology

The `sim-host` container runs two processes:

- **`sim`** — the simulator. Binds every server and originates every client
  on the **loopback interface (`127.0.0.1`) only**; it holds no zone
  addresses and has no knowledge of the network topology.
- **HAProxy** — owns the container's network interfaces and every
  zone-visible address, terminates Transport Layer Security where the
  protocol requires it, and maps between the zone endpoints and the
  simulator's loopback ports.

Both run as services managed by the container's init; their restart behaviour
differs (§7).

**One interface per managed address.** Each address the proxy presents is a
separate interface — no address aliasing — so each simulated entity has a
distinct interface, address, and Media Access Control (MAC) address and is
indistinguishable from an independent host (the MAC addresses carry no
vendor-authentic Organisationally Unique Identifier — the known limitation
recorded in [`docs/testbed.md`](../../../docs/testbed.md), "Sim appliance
network presence"). The five `vnet-ot` interfaces share one subnet on a
single host, so they use **macvlan**: each gets its own MAC with per-MAC
receive filtering — the primitive for one host presenting several
independent devices on one segment. Per-MAC filtering delivers a frame only
to the interface owning its destination MAC, so each address must resolve to
its own interface's MAC; §2 covers the address-resolution settings that
enforce this.
The two `vnet-it` interfaces share one subnet in the same way, so they also use **macvlan**.
The single-interface zone, `vnet-ext`, uses plain **bridged** attachment.

| Interface | Virtual network | Address | NIC type | Entity |
|---|---|---|---|---|
| `ot-atg`         | `vnet-ot`  | 10.40.0.51 | macvlan | ATG console |
| `ot-disp`        | `vnet-ot`  | 10.40.0.52 | macvlan | Dispenser |
| `ot-vessel-rfid` | `vnet-ot`  | 10.40.0.53 | macvlan | Vessel RFID reader |
| `ot-op-rfid`     | `vnet-ot`  | 10.40.0.54 | macvlan | Operator RFID reader |
| `ot-operator`    | `vnet-ot`  | 10.40.0.55 | macvlan | Dock operator actor |
| `ext-supplier`   | `vnet-ext` | 10.10.0.10 | bridged | Fuel supplier actor |
| `it-recon-user`  | `vnet-it`  | 10.30.0.11 | macvlan | Reconciliation user actor |
| `it-sysadmin`    | `vnet-it`  | 10.30.0.12 | macvlan | Systems administrator actor |

Each appliance interface serves only the single port HAProxy binds on it,
so a connection to any other port finds nothing listening. The
outbound-only interfaces (`ot-operator`, `ext-supplier`, `it-recon-user`, `it-sysadmin`) bind
nothing inbound and drop any inbound connection at a host-local packet
filter. Cross-zone traffic (for example the DMZ→OT path to the ATG console)
transits the zone firewalls; intra-zone traffic is switched on the zone
bridge.

The container is multi-homed across the three virtual networks but is never
a router between them: the same host-local packet filter carries a **forward
chain that defaults to drop**, so no traffic is forwarded between zones. This
complements the disabling of Internet Protocol version 4 (IPv4) forwarding
described in §2.

---

## 2. Addressing and routing

The container's interfaces carry no orchestrator-assigned addresses. A
start-up networking step (§7) assigns each address and its routing from a
single per-interface list, so an interface and its routing are always
defined together — a new entity is one list entry.

The OT interfaces share one subnet, so destination-only routing cannot tell
which interface a packet sourced from a given OT address should leave by; it
would pick one route for the whole subnet. Each interface therefore gets a
**source-based policy routing** table: traffic from that interface's address
(`ip rule add from <addr> lookup <table>`) uses a table holding the on-link
subnet route and a default via the zone's firewall gateway, so every flow
leaves through its own interface with its own MAC. This is applied
uniformly to every interface, so adding a new entity to any zone cannot
silently misroute through a sibling.

Source policy routing governs which interface a flow leaves by; Address
Resolution Protocol (ARP) behaviour governs which interface a peer sends to.
Under the kernel defaults the container answers ARP for any local address out
of any interface and sources its announcements arbitrarily, so a peer may cache
a service address against a sibling interface's MAC; per-MAC filtering (§1) then
delivers that traffic to an interface that discards it. Each interface therefore
sets `arp_ignore = 1` (answer only for the receiving interface's own address)
and `arp_announce = 2` (source announcements from that address), so every
address resolves to its own interface's MAC.

Zone firewall gateways: `vnet-ot` 10.40.0.2, `vnet-ext` 10.10.0.2,
`vnet-it` 10.30.0.2.

The container originates and terminates all of its own traffic and never
routes for another host. Each interface's address installs an on-link route
to its zone's subnet in the main routing table, so were IPv4 forwarding left
enabled the kernel could route directly between `vnet-ext`, `vnet-ot`, and
`vnet-it`, bypassing the zone firewalls. Forwarding is therefore disabled at
start-up (`net.ipv4.ip_forward = 0`), set before any interface is brought up.
Disabling forwarding and the forward-chain drop (§1) are independent
controls: either alone prevents the container bridging the zones, and
together the non-router property holds at both the routing layer and the
packet filter.

Reverse-path filtering is disabled (`net.ipv4.conf.*.rp_filter = 0`). The check
validates an inbound packet's source against the main routing table, but each
interface's route back to an off-link peer lives in its per-source policy table,
not the main table; with filtering enabled it would drop legitimate inbound
flows whose source is off-link, such as a firewall-transited DMZ→OT connection
to the ATG console. Forwarding is disabled and the forward chain drops (§1), so
the non-router property does not rely on reverse-path filtering.

---

## 3. The simulator's loopback surface

The complete set of ports `sim` binds on `127.0.0.1` — the only network
surface the simulator itself presents.

| Port | Role | Bound by |
|---|---|---|
| 10001 | ATG console — Tank Level System over TCP | server |
| 10502 | Dispenser — Modbus TCP | server |
| 19001 | Vessel RFID reader — card number stream | server |
| 19002 | Operator RFID reader — card number stream | server |
| 20001 | Fuel supplier egress | client → proxy |
| 20002 | Dock operator egress | client → proxy |
| 20003 | Reconciliation user egress | client → proxy |
| 20004 | Systems administrator egress | client → proxy |

---

## 4. Inbound flows (digital world → sim)

The sim is the server. HAProxy listens on the entity's zone address and
forwards transparently to the sim's loopback port. The distinct frontend
address is what makes each entity appear to be a separate device.

| Entity | Proxy frontend | → sim loopback | Mode |
|---|---|---|---|
| ATG console | 10.40.0.51:10001 | 127.0.0.1:10001 | TCP |
| Dispenser | 10.40.0.52:502 | 127.0.0.1:10502 | TCP |
| Vessel RFID reader | 10.40.0.53:9001 | 127.0.0.1:19001 | TCP |
| Operator RFID reader | 10.40.0.54:9002 | 127.0.0.1:19002 | TCP |

None of the inbound protocols is encrypted, so the proxy performs no
Transport Layer Security termination on these paths.

The two RFID readers are networked readers: each exposes its card-number
stream as a TCP service at its own OT address, and `fms-01` and `hmi-01`
connect over their existing OT interfaces and read the stream directly
(sim.md §4.3–4.4, digital.md §3). The operator reader would realistically
be a USB device with no network presence; modelling it as a networked
reader is a deviation (digital.md §9).

---

## 5. Outbound flows (sim → digital world)

The sim is the client. It connects to a loopback egress port; HAProxy dials
the real digital target and **binds the connection's source to the entity's
zone address** (the `source` directive on the backend `server` line; §2's
policy routing then carries it out the matching interface). The target sees
the connection arriving from a distinct device on the correct network.

| Entity | sim → loopback | Proxy → target | Source address | Mode |
|---|---|---|---|---|
| Fuel supplier | 127.0.0.1:20001 | atg-relay-01:10001 (10.20.0.10) | 10.10.0.10 (`vnet-ext`) | TCP |
| Dock operator | 127.0.0.1:20002 | hmi-01:443 (10.40.0.12) | 10.40.0.55 (`vnet-ot`) | HTTP → HTTPS |
| Reconciliation user | 127.0.0.1:20003 | recon-01:3389 (10.30.0.10) | 10.30.0.11 (`vnet-it`) | TCP |
| Systems administrator | 127.0.0.1:20004 | admin-01:22 (10.30.0.40) | 10.30.0.12 (`vnet-it`) | TCP |

`recon-01` (10.30.0.10) is a separate digital workstation — the
reconciliation user's Remote Desktop Protocol target (digital.md §2.6) —
distinct from the sim's `it-recon-user` source address (10.30.0.11).
`admin-01` (10.30.0.40) is likewise a separate digital workstation, the systems administrator's Secure Shell (SSH) target (digital.md §2.9), distinct from the sim's `it-sysadmin` source address (10.30.0.12).

**Transport Layer Security.** The dock operator path is the only one that
crosses an encrypted boundary: the sim issues plain HTTP on loopback and
HAProxy originates HTTPS to `hmi-01` (digital.md §2.3), validating its
certificate against the RavensPort internal certificate authority's root and
its hostname (`ssl verify required ca-file ... sni str(hmi-01) verifyhost
hmi-01`) rather than accepting an unverified certificate (digital.md §2.7,
§8 decision 7). The `sni` directive is not optional here even though
`hmi-01` presents only one certificate regardless of what SNI it receives:
observed directly on a live deployment, this HAProxy build's `verifyhost`
silently fails without it, against a certificate that both `openssl verify
-x509_strict` and a direct TLS handshake accept without complaint.
sim-host trusts that root but holds no certificate of its own: it is a TLS
client on this path, never a server.
The reconciliation user's Remote Desktop Protocol connection and the systems administrator's SSH connection carry their own end-to-end encryption and are forwarded as opaque TCP.
The fuel supplier's Tank Level
System protocol is unencrypted.

---

## 6. HAProxy configuration

HAProxy runs as a standard TCP reverse proxy with production defaults, not
special rate-limiting or connection throttling. Connect timeouts are short,
but inactivity timeouts are generous and paired with TCP keepalives, because
OT connections are long-lived and mostly idle — the FMS holds an open
connection to an RFID reader and sees bytes only when a card is tapped, and
ATG polling is periodic; short timeouts would tear idle connections down.
The thread count auto-scales to the available CPUs. Inbound backends
(pointing to the sim's loopback ports) carry HAProxy's default health check
so that HAProxy's service state reflects whether the sim is running. Outbound
backends (pointing to digital-world hosts) carry no health check: the
deployment script guarantees that each target is reachable before sim-host
starts, so an initial check would add only a race condition. Each public
address and port maps to the matching loopback listener (§4); each egress
frontend source-binds to the entity's zone address (§5); the dock operator
frontend runs in HTTP mode and originates Transport Layer Security to
`hmi-01`, validating its certificate against the RavensPort internal
certificate authority's root and its hostname (`ssl verify required ca-file
... sni str(hmi-01) verifyhost hmi-01`), rather than accepting an unverified
certificate (digital.md §2.7, §8 decision 7; §5 above records why the `sni`
directive is required). The concrete configuration is a deployment
artefact.

---

## 7. Image, build, and supervision

The `sim-host` runs only as a container; there is no virtual-machine
variant. The image is a minimal Alpine Linux base carrying the two binaries
and their service definitions:

- the `sim` binary, cross-compiled to a static musl target;
- HAProxy, from the Alpine package;
- OpenRC service definitions;
- the RavensPort internal CA's root certificate, for HAProxy to validate
  `hmi-01` on the dock-operator path (§6; digital.md §2.7);
- an empty directory, `/var/lib/sim/state`, as the mount point for the sim's
  checkpoint (below).

OpenRC (over the busybox init) is the container's init and manages, in
order: a **networking** step that plumbs the interface addresses and policy
routing (§2); the **HAProxy** service, started after networking so its
public binds resolve; and the **sim** service, started after HAProxy so
the proxy is listening before the sim's actors attempt their first outbound
connections. A service that stops is surfaced through OpenRC's service state.

The **sim** service is deliberately not supervised for respawn: on a crash the
process is left dead and OpenRC reports the service as `crashed` rather than
restarting it. A respawn would reset the physical model (the tank refills to its
start volume) and continue generating traffic, burying the panic that stopped the
run behind a fresh, healthy-looking sim. Leaving it stopped preserves the failure
for post-mortem inspection and for the deployment dashboard. Networking and
HAProxy remain supervised. Restart is a manual action or a redeploy. A crash
writes no checkpoint, so a restart after one is a cold start (`sim.md` §9).

**Stopping the sim.** The service's stop sends `SIGTERM`, on which the sim
comes to rest and writes its checkpoint (`sim.md` §9.1). Coming to rest can
take as long as a fill or an audit session, so the service allows up to one
hour between `SIGTERM` and `SIGKILL`; the service stops as soon as the sim
exits. A sim killed at the end of that hour has
written no checkpoint. The checkpoint is written to `/run/sim/checkpoint.json`
on the `/run` tmpfs, in a directory the service creates for the sim's user
before starting it. The tmpfs is lost when the container stops, so the
deployment copies the checkpoint out first (`deployment/deployment.md` §8.1).

**Starting from a checkpoint.** The sim reads `/var/lib/sim/state/checkpoint.json`
at start, and `/var/lib/sim/state/reseed.json` if present (`sim.md` §9.3). The
deployment supplies that directory as a read-only mount; without the mount the
directory is empty and the sim cold-starts.

The sim's dock-operator actor logs in at the Human-Machine Interface (HMI) as soon as the sim starts. The HMI forwards the login to the FMS, which verifies the password with a bind against the directory on `corp-it`. The deployment script therefore starts sim-host only after the FMS, the HMI and the directory have each passed a readiness probe (`deployment/deployment.md` §8, deployment sequence).

The `sim` binary of the operational image reads **no configuration file at runtime** other than an optional, read-only state checkpoint (above). Every parameter, among them the loopback binds, the logical-clock mode, the tank parameters, the seeds and the start state, is fixed at compile time in the Rust binary. In the competition profile (`deployment/general-deployment.md` §1, profiles), `sim-host` runs from its competition image (`competition/episodes.md` §3.4, competition image). A deployment of the competition profile attaches the directory that holds the start-up input, the file `startup.json`, read-only at `/var/lib/sim/startup`, which only the competition image holds (`deployment/deployment.md` §8, deployment sequence). A deployment attaches either the start-up input or the checkpoint's mount directory, never both, and the two share no path (`deployment/deployment.md` §10, snapshot sets). Before any entity starts, the competition image's entry point reads only the seed of the tug arrival generator and the time of the first audit from the start-up input (`competition/episodes.md` §4.3, start-up input). The physical constants and RFID card numbers shared with the digital world, and the scenario parameters, are projected from `world.yaml` into the committed crate input `infra/sim/sim-constants.toml`. The crate's `build.rs` compiles that file in, so that `world.yaml` is their single source. The values that the sim shares with the digital world include the tank capacity, the flow-meter K-factor, and the operator and vessel card numbers. The values that the sim shares with the digital world also include each vessel's International Maritime Organization (IMO) number and name, and the FMS session lifetime. The scenario parameters are the flow rates, the overfill interlock level, the fuel supplier's ordering values, each tug's mean time away, the refuel volume range, and the reconciliation audit's period, settling delay and boundary margin. Every value that a compressed scenario scales reaches the binary through `sim-constants.toml` as well, at its value in operation, and scaled in a compressed scenario (`sim.md` §2.2, compressed scenarios; `deployment/deployment-config.md` §4.1, sim projection). In the operational image, the remaining parameters, including the sim-only seeds and the start state (`sim-impl.md`, "Start state"), are literals.
The internal certificate authority's root, by which the reconciliation user verifies
`recon-01`'s remote-desktop certificate, is compiled in the same way, from a
committed copy beside `sim-constants.toml`. The deployed binary is
self-contained and its constants cannot drift from `world.yaml`. The sim writes
its standard output and error to `/var/log/sim.log` on the writable tmpfs (§8)
rather than the container console, which start-stop-daemon does not retain; both
streams share the one file so a crash — the Rust panic on stderr and the
orchestrator's shutdown `cause=` on stdout — is captured whole. Because the
service is not respawned (above), the file is preserved for inspection rather
than overwritten by a fresh run. HAProxy keeps its output on the container console: its
foreground logging is voluminous and unsuited to the small in-memory tmpfs. The
image carries no syslog daemon.

---

## 8. Hardening

Within the constraints of an Alpine base:

- **Minimal package set.** The image carries only HAProxy, the sim binary,
  OpenRC, and the networking tooling they need — no package-manager state,
  no compilers, no remote-access daemon. Fewer binaries mean fewer tools
  available to an attacker who lands in the container.
- **Unprivileged service users.** The sim and HAProxy each run as their own
  non-root account, so compromising one process does not grant control over
  the other or the rest of the container.
- **Capability minimisation.** The only elevated capability held by a
  running process is `CAP_NET_BIND_SERVICE` on HAProxy, to bind the
  privileged Modbus port 502; the sim binds only loopback ports and holds
  none. `CAP_NET_ADMIN`, needed to plumb the interfaces and policy routing,
  is used only by the networking step at start-up, before either listener
  accepts.
- **Read-only root filesystem.** The root filesystem is immutable, with only
  the few runtime paths the processes need left writable, so an attacker
  cannot persist across a restart and the configuration cannot drift from
  the image. The checkpoint is written to the `/run` tmpfs and read from a
  read-only mount (§7), so it adds no writable persistent path.
- **Unprivileged container.** The container runs unprivileged: its root user
  maps to an unprivileged host user and it controls only its own network
  namespace, so even a full compromise inside the container cannot act on
  the host.
