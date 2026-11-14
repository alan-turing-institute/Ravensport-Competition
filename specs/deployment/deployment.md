# Deployment

Specification for the Incus-based deployment that realises the digital-world architecture described in `digital.md`. All digital-world nodes and the simulated-physical world container run as Incus instances on a single physical host.

Nodes are deployed from pre-built images. Each node's complete software stack — packages, services, configuration, and seeded datastores — is assembled into a named Incus image before any instance starts. No provisioning occurs at instance start time. A deployed instance is byte-identical to its image and cannot drift through incremental changes or post-deploy pushes.

**Contents:**
1. [Physical host requirements](#host-requirements)
2. [Network topology](#network-topology)
3. [Node inventory](#node-inventory)
4. [Firewall architecture](#firewall-architecture)
5. [Render pipeline](#render-pipeline)
6. [Image build](#image-build)
7. [Deployment profiles](#profiles)
8. [Deployment sequence](#deployment-sequence)
9. [Reproducibility](#reproducibility)

---

## 1. Physical host requirements<a id='host-requirements'></a>

The deployment is tested on Ubuntu 24.04 with Incus. Requirements:

- **Kernel**: Linux 5.15 or later (required for macvlan in unprivileged containers).
- **Virtualisation**: KVM/QEMU for the `full` profile (virtual machine nodes). Not required for `ci-minimal` (all containers).
- **dnsmasq-base**: required by Incus to create managed networks. Install with `sudo apt install -y dnsmasq-base`.
- **kernel.keys.maxkeys**: at least 2000. The kernel default (200) is exhausted when more than ten containers run simultaneously, causing silent network setup failures. Set permanently in `/etc/sysctl.d/90-incus.conf`. The deployment script checks this value at startup and aborts if it is insufficient.
- **Rust toolchain and musl cross-compilation target**: `cargo` at the Rust release pinned in the repository-root `rust-toolchain.toml`, and the `<arch>-unknown-linux-musl` target for that release, required to compile the static `sim` binary during `sim-host` image build. The FMS and HMI binaries link against glibc and do not require the musl target.

---

## 2. Network topology<a id='network-topology'></a>

### 2.1 Networks

Four Incus-managed zone networks, all isolated.

| Network | CIDR | Purpose |
|---|---|---|
| `vnet-ext` | 10.10.0.0/24 | External zone: fuel supplier actor ATG polling origination. |
| `vnet-dmz` | 10.20.0.0/24 | DMZ zone: ATG relay, Secure Shell (SSH) bastion, and log-server replica. |
| `vnet-it` | 10.30.0.0/24 | IT zone: corporate identity provider, NTP, reconciliation workstation, administration workstation, security monitoring. |
| `vnet-ot` | 10.40.0.0/24 | OT zone: all OT services and simulated appliances. |

All networks carry `ipv4.address=none` and `ipv4.nat=false`. The Incus host holds no gateway address on any zone subnet and is excluded from the forwarding path; every cross-zone packet must transit a firewall node. All networks use `dns.mode=none`; nodes resolve names from a per-node `/etc/hosts` file baked into their image (§5.2).

The deployed environment contains no internet-facing or Network Address Translation (NAT)-enabled network. Packages and build-time dependencies are installed into images on the build host using temporary networks created and destroyed during the image build (§6.1); no deployed instance requires or has outbound internet access at any point.

### 2.2 Addressing

The addresses below reflect what was assigned during implementation. They were not derived from a deliberate addressing scheme and carry no architectural significance; they could be changed without affecting any design decision, though other specification documents reference them.
The rows for `dmz-bastion-01`, `admin-01` and the sim's systems administrator actor are planned addresses, assigned in the specification ahead of implementation.

| Node | Zone(s) | Address(es) |
|---|---|---|
| `fw-ext-dmz` | vnet-ext / vnet-dmz | 10.10.0.2 / 10.20.0.2 |
| `fw-dmz-it` | vnet-dmz / vnet-it | 10.20.0.3 / 10.30.0.2 |
| `fw-ot-dmz` | vnet-ot / vnet-dmz | 10.40.0.2 / 10.20.0.4 |
| `atg-relay-01` | vnet-dmz | 10.20.0.10 |
| `hist-dmz-01` | vnet-dmz | 10.20.0.20 |
| `dmz-bastion-01` | vnet-dmz | 10.20.0.30 |
| `corp-it` | vnet-it | 10.30.0.20 |
| `recon-01` | vnet-it | 10.30.0.10 |
| `siem-01` | vnet-it | 10.30.0.30 |
| `admin-01` | vnet-it | 10.30.0.40 |
| `fms-01` | vnet-ot | 10.40.0.10 |
| `hist-01` | vnet-ot | 10.40.0.11 |
| `hmi-01` | vnet-ot | 10.40.0.12 |
| `sim-host` — ATG console | vnet-ot | 10.40.0.51 |
| `sim-host` — dispenser | vnet-ot | 10.40.0.52 |
| `sim-host` — vessel RFID reader | vnet-ot | 10.40.0.53 |
| `sim-host` — operator RFID reader | vnet-ot | 10.40.0.54 |
| `sim-host` — dock operator actor | vnet-ot | 10.40.0.55 |
| `sim-host` — fuel supplier actor | vnet-ext | 10.10.0.10 |
| `sim-host` — reconciliation user actor | vnet-it | 10.30.0.11 |
| `sim-host` — systems administrator actor | vnet-it | 10.30.0.12 |

Firewall nodes that share the DMZ segment hold additional static routes to reach zones they do not directly connect to; see §4.3.

---

## 3. Node inventory<a id='node-inventory'></a>

In the `full` profile, nodes marked VM run as virtual machines. In `ci-minimal`, all nodes run as containers regardless of the type shown here (§7.2).

| Node | Type (full) | CPU | Memory | Build script | Status |
|---|---|---|---|---|---|
| `fw-ext-dmz` | VM | 2 | 2 GB | `fw-zone` | Deployed as generic Linux/nftables stand-in in both profiles; `full`-profile pfSense CE/OPNsense not yet implemented (`digital.md` §9 deviation 9). Resources sized for Suricata (`host_specs/siem/siem.md` §5), which is not yet installed |
| `fw-dmz-it` | VM | 2 | 2 GB | `fw-zone` | Same as `fw-ext-dmz` |
| `fw-ot-dmz` | VM | 2 | 2 GB | `fw-zone` | Same as `fw-ext-dmz` |
| `atg-relay-01` | Container | — | — | `atg-relay-01` | Deployed |
| `dmz-bastion-01` | Container | — | — | `dmz-bastion-01` | Specified — build script not yet implemented |
| `corp-it` | Container | — | — | `corp-it` | Deployed |
| `recon-01` | Container | — | — | `recon-01` | Deployed |
| `siem-01` | VM | 2 | 2 GB | `siem-01` | Deployed|
| `admin-01` | Container | — | — | `admin-01` | Specified — build script not yet implemented |
| `fms-01` | VM | 2 | 2 GB | `fms-01` | Deployed |
| `hist-dmz-01` | VM | 2 | 4 GB | `hist-dmz-01` | Deployed |
| `hist-01` | VM | 2 | 4 GB | `hist-01` | Deployed |
| `hmi-01` | VM | 2 | 4 GB | `hmi-01` | Deployed |
| `sim-host` | Container | 2 | 512 MB | `build-sim-image.sh` | Deployed |

All per-node images use Ubuntu 24.04 as the base. `sim-host` uses a custom Alpine Linux image built by `deploy/scripts/build-sim-image.sh`; see `specs/host_specs/sim/sim-network.md` for its full specification.

---

## 4. Firewall architecture<a id='firewall-architecture'></a>

### 4.1 Node placement

Three firewall nodes, one per zone boundary. Each node connects to exactly two zone networks. Zone nodes use the firewall node's zone NIC address as their default gateway.

| Node | Boundary | External gateway | OT gateway | DMZ gateway | IT gateway |
|---|---|---|---|---|---|
| `fw-ext-dmz` | External ↔ DMZ | 10.10.0.2 | — | 10.20.0.2 | — |
| `fw-dmz-it` | DMZ ↔ IT | — | — | 10.20.0.3 | 10.30.0.2 |
| `fw-ot-dmz` | OT ↔ DMZ | — | 10.40.0.2 | 10.20.0.4 | — |

### 4.2 Forward policy

Default deny on all firewall `forward` chains. Return traffic for an established connection is permitted automatically. The table below lists permitted connection initiations.

| From | To | Destination | Port | Protocol | Purpose |
|---|---|---|---|---|---|
| `vnet-ext` | `vnet-dmz` | `atg-relay-01` (10.20.0.10) | 10001 | TCP | Fuel supplier ATG poll |
| `vnet-dmz` (`atg-relay-01`, 10.20.0.10, only) | `vnet-ot` | ATG console (10.40.0.51) | 10001 | TCP | ATG relay forwarding to sim |
| `vnet-it` (`admin-01`, 10.30.0.40, only) | `vnet-dmz` | `dmz-bastion-01` (10.20.0.30) | 22 | TCP | Systems-administration SSH to bastion |
| `vnet-dmz` (`dmz-bastion-01`, 10.20.0.30, only) | `vnet-ot` | `fms-01` (10.40.0.10) | 22 | TCP | Bastioned SSH administration |
| `vnet-dmz` (`dmz-bastion-01`, 10.20.0.30, only) | `vnet-ot` | `hist-01` (10.40.0.11) | 22 | TCP | Bastioned SSH administration |
| `vnet-dmz` (`dmz-bastion-01`, 10.20.0.30, only) | `vnet-ot` | `hmi-01` (10.40.0.12) | 22 | TCP | Bastioned SSH administration |
| `vnet-it` | `vnet-dmz` | `hist-dmz-01` (10.20.0.20) | 443 | TCP | Reconciliation user HTTPS to log-server replica |
| `vnet-ot` | `vnet-dmz` | `hist-dmz-01` (10.20.0.20) | 8428 | TCP | Log-server metric replication push, `hist-01` vmagent → `hist-dmz-01` VictoriaMetrics |
| `vnet-ot` | `vnet-dmz` | `hist-dmz-01` (10.20.0.20) | 9428 | TCP | Log-server event replication push, `hist-01` vlagent → `hist-dmz-01` VictoriaLogs |
| `vnet-ot` | `vnet-dmz` | `corp-it` (10.30.0.20) | 123 | UDP | NTP (OT → DMZ) |
| `vnet-dmz` | `vnet-it` | `corp-it` (10.30.0.20) | 123 | UDP | NTP (DMZ → IT) |
| `vnet-ot` | `vnet-dmz` | `corp-it` (10.30.0.20) | 636 | TCP | LDAP over TLS, hop 1 |
| `vnet-dmz` | `vnet-it` | `corp-it` (10.30.0.20) | 636 | TCP | LDAP over TLS, hop 2 |
| `vnet-ot` | `vnet-dmz` | `siem-01` (10.30.0.30) | 1514 | TCP | Security monitoring agent traffic, hop 1 |
| `vnet-dmz` | `vnet-it` | `siem-01` (10.30.0.30) | 1514 | TCP | Security monitoring agent traffic, hop 2 |

All other flows are blocked. Intra-zone traffic — for example `fms-01` connecting to the RFID reader addresses on `vnet-ot`, or the dock operator actor connecting to `hmi-01` on `vnet-ot` — does not traverse a firewall forward chain.

### 4.3 Two-hop transit paths

No single firewall node spans `vnet-ot` and `vnet-it` directly. Authentication traffic from OT hosts to `corp-it`, and security monitoring agent traffic from OT hosts to `siem-01`, traverse two hops, with the DMZ segment as the handoff:

```
OT host → fw-ot-dmz → (vnet-dmz) → fw-dmz-it → vnet-it → corp-it
```

`fw-ot-dmz` holds a static route for `vnet-it` (10.30.0.0/24) via `fw-dmz-it`'s DMZ address (10.20.0.3). `fw-dmz-it` holds a static route for `vnet-ot` (10.40.0.0/24) via `fw-ot-dmz`'s DMZ address (10.20.0.4). Each hop is a distinct entry in the `firewall_rules` table.

The External↔OT path operates differently: the fuel supplier on `vnet-ext` reaches the ATG console via `atg-relay-01` as an application-level relay (two separate TCP connections) rather than a routed single-hop path. No inter-firewall static routes are required because `atg-relay-01` itself is the intermediary.

SSH administration uses the same application-level separation. `admin-01` opens an
SSH connection to `dmz-bastion-01`; OpenSSH `ProxyJump` then carries a second,
end-to-end authenticated SSH connection from `admin-01` to one named OT host. The
bastion therefore requires routes to both IT and OT, but does not route arbitrary
packets between them or hold an OT private key.

### 4.4 nftables configuration

Each firewall node runs nftables. The `firewall_rules` table in `topology.yaml` is the single source of truth for policy; `render.py` resolves destination node names to IP addresses and distributes rules to the correct firewall node based on the zone networks it owns. A rule may also name a source, as a node or an IPv4 address; a source node resolves to its address on the rule's `from` network, and the rule then admits that address alone rather than any host on the network. The `forward` chain policy is `drop`. The `input` chain permits SSH, ICMP echo, `established`/`related`, and loopback. The `output` chain policy is `accept`.

---

## 5. Render pipeline<a id='render-pipeline'></a>

### 5.1 Input files

The render pipeline reads `deploy/topology.yaml`, `deploy/world.yaml`, the override files of §7, and the build-script templates. An override file patches the node definitions of `deploy/topology.yaml` for one profile, and an additional override file may patch them further (§7.3), so that one topology serves every deployment. An additional override file may also set the compression factor, the one factor by which a compressed scenario shortens every interval and duration in the port's operation and raises every rate of fuel flow (§7.3). Before it renders, `render.py` applies the patches. From a node's build-script template, `render.py` renders the shell script that builds the node's image (§6.1).

**`deploy/topology.yaml`** is the single source of truth for networks, node definitions, NIC assignments, addressing, and firewall policy. Generated artefacts in `build/` must not be edited directly.

**`deploy/world.yaml`** holds the non-network world facts that more than one component must agree on: operator and administrator identities, RFID card numbers, LDAP configuration, database parameters, process constants, and intervals. The release profiles (`full`, `ci-minimal`) render it unchanged. A test profile may override specific facts through a `world:` section in its override file, deep-merged before validation, to keep a test runnable (`general-deployment.md` §1.2) — the `attack` profile compresses `intervals.session_timeout_s` this way (§7.3). Its structure, projection targets, secrets handling, determinism requirements, and this override mechanism are specified in `specs/deployment/deployment-config.md`.

`world.yaml` is a build-host authoring artefact and is never copied into any deployed instance. Each component receives only a generated projection of its own relevant slice.

### 5.2 render.py

`deploy/scripts/render.py` reads both input files and produces `build/`. Its outputs:

- **`build/env/<node>.env`** — rendered environment file per node, carrying the node's application configuration: database connection URL, administrator credential hash, LDAP connection parameters, service addresses, and shared process constants from `world.yaml`.
- **`build/hosts/<node>`** — per-node `/etc/hosts` containing loopback entries, the node's own hostname, and the hostnames listed in the node's `resolve:` field in `topology.yaml`. No node receives another node's entries unless explicitly listed.
- **`build/haproxy/<node>.cfg`** — HAProxy configuration for nodes that run HAProxy (`atg-relay-01`, `sim-host`).
- **`build/network-config/<node>.yaml`** — netplan v2 format static network configuration per node: interface addresses, default gateway, and additional static routes. Consumed by the node's image build script, which converts it to systemd-networkd `.network` unit files.
- **`build/nftables/<node>.nft`** — nftables ruleset, produced for every node. On firewall nodes it carries the `firewall_rules` policy, with source and destination node names resolved to IP addresses at render time; on every node it carries the connection-logging rule of `host_specs/siem/siem.md §4.1`.
- **`build/ldif/<node>.ldif`** — LDAP directory seed file for `corp-it`. Operator entries use `{SSHA}` password encoding with per-identity salts derived deterministically from `world.yaml`'s `crypto.password_salt_seed`.
- **`build/siem/<node>.keys`** — security monitoring agent key file. Each monitored node receives only its own key; `siem-01` receives the complete set. Keys are derived deterministically from `world.yaml`'s `crypto.password_salt_seed` combined with the node name, so no enrolment step runs at instance start.
- **`build/ssh/<node>/`**: SSH key material and client files per node. Each SSH server (`admin-01`, `dmz-bastion-01`, `fms-01`, `hist-01`, `hmi-01`) receives its host keypair. The bastion and the managed hosts receive the administration account's `authorized_keys`. `admin-01` also receives the systems administrator's private key, `ssh_known_hosts` and the client configuration (`deployment-config.md §4`, §6).
- **`infra/sim/sim-constants.toml`** — sim process constants (`tank_capacity_l`, `k_factor`) and RFID card numbers projected from `world.yaml`. Written to this committed path alongside the sim crate. The sim crate's `build.rs` reads it via `CARGO_MANIFEST_DIR` and emits a `pub const` block at compile time, so the sim binary carries world-derived constants without a runtime configuration file; see `specs/deployment/deployment-config.md §4` for the full detail.
- **`build/sim/sim-constants.toml`**: in a compressed scenario, the same constants scaled by the compression factor (`deployment-config.md` §4.1, sim projection).
- **`build/node-build/<node>.sh`** — rendered image build script per node, from the template that the node's `build_script` names. `render.py` looks for the template in `deploy/build-scripts/`, and then in the template directory that an additional override file adds (§7.3).
- **`build/incus-commands.sh`** — shell script that creates all Incus networks and instances and attaches their NICs. Does not start instances; the deployment script controls startup order (§8).
- **`build/teardown-commands.sh`** — shell script that destroys all instances and networks.
- **`build/manifest.json`** — records the rendered profile, node list, and image map for the test suite.
- **`build/inputs/`** — copies of the inputs of the render: `world.yaml`, `topology.yaml` and `overrides/<profile>.yaml`. A snapshot set records them as the inputs its instances were rendered from (§10.2).

Each render replaces the contents of `build/` except `build/ca/` and `build/tls/`, which `build-ca.sh` maintains. The snapshot sets are kept outside `build/`, in `deploy/snapshots/` (§10), so that neither a render nor `make clean`, which removes `build/`, deletes them.

`build/inputs/` is a record, not a rendered artefact: it holds a copy of `world.yaml`, with its plaintext credentials, and no image build copies anything from it. The rule that no plaintext credential reaches a rendered artefact (`deployment-config.md` §6) therefore applies to every other part of `build/`, and its unit test excludes this directory. The snapshot sets, which hold the same copies, are outside `build/`.

Validation performed by `render.py` before rendering:
- duplicate IP addresses across nodes;
- NIC definitions referencing unknown networks;
- firewall rules whose `dst` field is neither a valid IP address nor a known node name.

`world.yaml` validation is specified in `specs/deployment/deployment-config.md §5`.

### 5.3 Invocation

```
make install                        # create the Python virtualenv once
make render                         # render build/ for PROFILE=ci-minimal, WORLD=world.yaml (defaults)
make render PROFILE=full            # render for the full profile
make render WORLD=<path>            # render a scenario's world.yaml instead of the default
make render EXTRA_OVERRIDE=<path>   # also apply an additional override file (§7.3)
make up                             # render, build images, and deploy
make down                           # destroy all instances and networks (§8.3)
```

The `render.py` script itself takes `--profile`, `--topology`, `--world`, and `--extra-override` arguments; `make render` forwards the `PROFILE`, `WORLD`, and `EXTRA_OVERRIDE` Makefile variables to `--profile`, `--world`, and `--extra-override` respectively.

---

## 6. Image build<a id='image-build'></a>

Each node is packaged as a named Incus image before deployment. All packages, service binaries, configuration, and seeded datastore state are applied at image-build time. No provisioning occurs when an instance starts, and no deployed instance has internet access (§2.1, §8).

### 6.1 Per-node images

Each node has a rendered build script at `build/node-build/<node>.sh`. When run, the script:

1. Computes a SHA-256 hash of its own content and all inputs from `build/` it uses. If an existing published image carries a matching `ravensport.input-hash` property, the build is skipped with the message "inputs unchanged, skipping". Only nodes whose inputs have changed are rebuilt.
2. Creates a throwaway Incus container from the Ubuntu 24.04 cloud base image, attached to a temporary NAT bridge created for the build. This bridge provides outbound internet access for package installation and is destroyed when the build exits.
3. Installs all required packages via `apt-get`.
4. Configures the node's services: the FMS or HMI binary and its systemd unit; the OpenLDAP directory seeded from the rendered LDIF; the PostgreSQL cluster; HAProxy; or the nftables ruleset, as appropriate.
5. Copies the rendered artefacts from `build/` into the container: the env file, `/etc/hosts`, HAProxy configuration, and nftables rules.
6. Converts the rendered netplan YAML (`build/network-config/<node>.yaml`) to systemd-networkd `.network` unit files and enables `systemd-networkd`, so that static network configuration is applied from first boot without a Dynamic Host Configuration Protocol step.
7. Removes the Ubuntu services that connect to servers outside the port, since no port host updates itself or contacts an external service (narrative §4). The script purges the `unattended-upgrades` package and masks `apt-daily.timer`, `apt-daily-upgrade.timer`, `motd-news.timer` and `ua-timer.timer`. It also installs a `systemd-resolved` drop-in that sets `FallbackDNS=` to empty. Nodes have no DNS server configured (§2.1); with the fallback list empty, each resolves names from `/etc/hosts` alone and sends no queries to public resolvers.
8. Purges cloud-init: removes the `cloud-init` package and deletes `/var/lib/cloud` and `/etc/cloud`, so that no build-container run residue survives into the image and the cloud-init tree cannot be reconstructed at first boot. Incus re-injects a minimal inert seed directory under `/var/lib/cloud/seed/` at instance creation time — containing only the instance's own hostname and identifier — but since the package is absent it is never processed.
9. Clears the logs written during the build, so that each host enters service with no history except the record of its package installation, as a host set up by its department before being put into service would (narrative §4). The script stops `rsyslog` and moves the journal to `/run` (`journalctl --relinquish-var`), so that the build container's shutdown is not written to the image, and deletes the journal files. It removes the log directory of the purged `unattended-upgrades` package. It recreates every other file under `/var/log` empty, with its owner and mode, except the package installation record: `/var/log/apt/`, `dpkg.log`, `alternatives.log` and `bootstrap.log`. This is the last command run in the container.
10. Stops the container and publishes it as a named Incus image (for example `ravensport/fms-01:latest`), stamped with the computed `ravensport.input-hash`.

```
make build-node-images    # build all per-node images
```

### 6.2 sim-host image

`sim-host` uses a separate build script, `deploy/scripts/build-sim-image.sh`, producing a minimal Alpine Linux image. The same selective rebuild hash check applies. The script:

1. Compiles the `sim` binary as a static musl target (`cargo build --release --target <arch>-unknown-linux-musl`).
2. Creates a throwaway Alpine container on a temporary NAT bridge.
3. Bootstraps a minimal rootfs using `apk --root`, installing only HAProxy, OpenRC, `iproute2-minimal`, `nftables`, and the Alpine base userland — no package-manager state, compilers, or remote-access tools.
4. Installs the sim binary, HAProxy configuration, networking script, nftables egress filter, and OpenRC service definitions, and creates the empty mount point `/var/lib/sim/state` for the sim's checkpoint (§8.1).
5. Stops the container and publishes it as `ravensport/sim:latest`.

See `specs/host_specs/sim/sim-network.md` for the full specification of the operational image's contents, supervision model, and hardening, and `episodes.md` §3.4 (competition image) for the competition image.

```
make build-sim-image      # build the sim-host image
make images               # build all images (sim-host and per-node)
make build                # render and build all images
```

---

## 7. Deployment profiles<a id='profiles'></a>

Profiles are defined as override files in `deploy/overrides/`; `render.py`
discovers them from that directory. `full` and `ci-minimal` are the release
profiles (`general-deployment.md` §1.1); `attack` is a test profile
(`general-deployment.md` §1.2) — the release deployment plus the minimum extra
needed to keep a test runnable.

### 7.1 full

The primary deployment profile, used on a Linux host with KVM. Node types are as declared in `topology.yaml`.

### 7.2 ci-minimal

Used on hosts without KVM, including Apple Silicon machines. Every node builds the same image as under the `full` profile but runs as a container (`force_container: true`).

**Privilege policy.** Containers run unprivileged by default: each container's root user is mapped, through a user namespace, to an unprivileged host user, so a process that is root inside a container holds no privilege on the host or over any other container. This is the isolation boundary on the container profiles; it is weaker than the `full` profile's per-VM kernel, because all containers share the host kernel. No container runs in privileged mode, and the policy admits no exception. The firewall nodes require none: packet filtering, connection logging and packet capture for inspection each operate within the container's own network namespace. Capabilities or host-facility access that would let one container observe another via the shared kernel — host audit, the kernel log, kernel-wide tracing — are never granted on the container profiles; the authentic path for host-kernel telemetry is a virtual-machine-backed host on the `full` profile (`host_specs/siem/siem.md §4.4`).

### 7.3 Additional override file

An additional override file, named by `EXTRA_OVERRIDE` (§5.3), applies after the profile's override file and combines with either profile. The additional override file may patch node definitions and add a directory of build-script templates, which `render.py` searches after `deploy/build-scripts/` (§5.2). The additional override file may also set the name of the image that a node is built as and created from (§6.1, §8). An additional override file may set the compression factor of a compressed scenario (`narrative.md` §6, deviations from real-world practice). `render.py` reads the compression factor from the additional override file that `EXTRA_OVERRIDE` names. A timing's or a rate's value in operation is its value at a compression factor of 1. When no additional override file applies, or the file sets no compression factor, `render.py` uses a compression factor of 1, so that every value keeps its value in operation. An additional override file may also set values that a node's build-script template reads. A profile's override file, `deploy/overrides/full.yaml` or `deploy/overrides/ci-minimal.yaml`, sets only how the nodes are built and run (§7.1, §7.2), and `render.py` refuses one that sets the compression factor or a flow timeout.

### 7.4 attack

A test profile for the attack-chain live suite (`attack_chains/attack-chains-tests.md`). It carries `ci-minimal`'s topology (every node a container; `fms-01` privileged) and a single `world:` override (`deployment-config.md`): `intervals.session_timeout_s` is compressed from the realistic 1800 s to 60 s. The capture variant of the operator-lock attack works by sniffing the dock operator's LDAP bind off the wire, and that bind crosses the wire only when the operator re-authenticates — once per session lifetime. At 30 minutes a capturable bind is far too rare to test against; at 60 s the operator re-authenticates about once a minute, so the capture succeeds within a bounded window. The release profiles keep the realistic 1800 s. Deploy with `make up PROFILE=attack`.

**This profile exists only to make the tests fast; it is not a precondition of the attacks.** The attack binaries must, and do, run robustly against the release profiles unchanged: the capture stages that wait for a periodic event — the operator re-authentication, the fuel supplier's poll — bound their wait with a window whose *default* is sized for the realistic release-profile cadence (tens of minutes), and each returns the instant it captures. Against a release profile an attack therefore waits patiently for the next real event and then proceeds; against the `attack` profile the compressed cadence delivers that event within seconds to about a minute, so the same binary finishes quickly. The profile shortens the wait, never the attack's capability. (The time-compressed simulator scenario the false-feed and replay tests also rely on — a faster supplier poll — is likewise a test-speed convenience, not a precondition of those attacks; see `attack_chains/attack-chains-tests.md` §9.)

---

## 8. Deployment sequence<a id='deployment-sequence'></a>

After images are built (`make build`), deployment is performed by `deploy/scripts/deploy.sh` via `make deploy` or `make up`:

1. **Create networks and instances.** `build/incus-commands.sh` creates the four zone networks, creates all instances from their named images with their NICs attached, and starts all instances except `sim-host`.
2. **Wait for the FMS.** The deployment script polls `https://127.0.0.1/api/v1/health` from within `fms-01` at five-second intervals for up to 120 seconds. The FMS must respond `{"status":"ok"}` before the sequence proceeds.
3. **Wait for the HMI.** The deployment script polls `https://10.40.0.12/health` from within `fms-01` (over `vnet-ot`) at five-second intervals for up to 120 seconds.
4. **Wait for the directory.** The deployment script attempts a TLS connection from within `fms-01` to port 636 on `corp-it`, where the directory serves LDAP over TLS, at five-second intervals for up to 120 seconds. Every attempt takes the same two-hop path through `fw-ot-dmz` and `fw-dmz-it` (§4.3) as the FMS's bind for a dock-operator login (`host_specs/fms.md` §4, authentication and session management). The TLS handshake must complete before the sequence proceeds.
5. **Start `sim-host`.** Once the three probes pass, `sim-host` is started. In the competition profile, the deployment script first attaches the simulator's start-up input to the stopped `sim-host` as the read-only disk device `startup-input` at `/var/lib/sim/startup` (`general-deployment.md` §1.3, competition profile). Only the competition image of `sim-host` holds that directory. The sim's dock-operator actor connects to the HMI immediately on startup; the readiness probes guarantee the full authentication chain is available on the first connection attempt.

No deployed instance has outbound internet access at any point. `build/incus-commands.sh` attaches instances only to the isolated zone networks defined in §2.1; there is no NAT bridge in the deployed environment.

### 8.1 Stopping and starting `sim-host` with its state<a id='sim-state'></a>

The sim's state is carried across a stop in a checkpoint (`sim.md` §9). The checkpoint describes the physical world beneath the digital world's records at the moment of the stop, so it is kept with the snapshots of the other instances taken at the same stop and restored with them. A checkpoint restored beneath different records reproduces the contradictions it exists to prevent.

**Stop.** `make snapshot` (§10.2) stops `sim-host` first, as follows:

1. `incus exec sim-host -- rc-service sim stop`. The sim comes to rest and writes `/run/sim/checkpoint.json`; this can take up to an hour (`host_specs/sim/sim-network.md` §7).
2. Confirm that the service is `stopped`, not `crashed`, and that the file exists. A sim that crashed or was killed has written no checkpoint, and the stop fails.
3. `incus file pull sim-host/run/sim/checkpoint.json snapshots/<label>/checkpoint.json`. `/run` is a tmpfs, so the file must be copied out before the container stops.
4. `incus stop sim-host`.

`sim-host` is stopped before the instances whose records it produces, so that the digital world records every transfer the sim completes while coming to rest.

**Restore.** A snapshot set is restored, and `sim-host` started from its checkpoint, by

```
make -C deploy restore SNAPSHOT=<label> [SEED=<n>]
```

which reads only the snapshot set: its instance list comes from the set's rendered manifest, `snapshots/<label>/rendered/manifest.json` (§10.2), and neither `build/`, `world.yaml` nor `PROFILE` is read. It first confirms that the set is complete and that no instance is running. It fails, changing nothing, if `snapshots/<label>/` or any other set is an incomplete set (§10.2), since a restore replaces the instance state an incomplete set is completed from, if it lacks `checkpoint.json`, `stopped-at` or any of its rendered files, if any instance lacks the snapshot `<label>`, or if any instance is running; a running instance belongs to a restored run that `make -C deploy stop` (§8.2) ends first. It also fails, changing nothing, for a set taken from a deployment of the competition profile (§10, snapshot sets). It then records `<label>` as the deployed set (§10.2) and:

1. Restores every instance to its snapshot `<label>` (`incus snapshot restore`). If `SNAPSHOT` is the path of a snapshot file rather than a label, the target first imports the file (§10.4) and then continues with the label recorded in it.
2. Holds and sets the host clock (§8.2): disables the host's NTP client and, on a Lima virtual machine, the guest agent's control of the time; reads the stop time from `snapshots/<label>/stopped-at`; and sets the clock to that time plus five seconds.
3. Empties the set's mount directory, `snapshots/<label>/mount/`, and copies `checkpoint.json` into it. The mount directory holds only what the sim reads at start, so that nothing else in the set, such as the render inputs (§10.2), reaches `sim-host`. The stored checkpoint in `snapshots/<label>/` is never modified, so every restore of a snapshot starts from the same state.
4. If `SEED` is given, writes `reseed.json` into the mount directory with `tug_arrival_seed` set to it (`host_specs/sim/sim-impl.md`, "Stopping and warm start"). Without `SEED` no reseed file is written, and the run repeats the snapshot's own continuation exactly (`sim.md` §9.3); since step 3 empties the directory, a reseed file from an earlier run is not read again.
5. Attaches the mount directory to `sim-host` as a read-only disk device:

   ```
   incus config device add sim-host sim-state disk \
       source=$PWD/snapshots/<label>/mount \
       path=/var/lib/sim/state readonly=true
   ```

6. Starts every instance except `sim-host`, in the reverse of the stop order of §10.2, and waits for the FMS, the HMI and the directory, as in steps 2 to 4 of §8.
7. Checks the time against the checkpoint: reads `written_at` from `snapshots/<label>/checkpoint.json` and compares it with the host clock. If more than five minutes have passed since `written_at`, the sim would refuse the checkpoint (`sim.md` §9.3), so the target fails at this point without starting `sim-host`, reporting both times and stating that the restore took too long. The sim's own check remains in place behind this one.
8. Starts `sim-host`, waits up to 60 seconds for the sim to create `/run/sim/checkpoint-loaded`, which it does once it has read and accepted the checkpoint, and then removes the device, so that a later restart does not reuse the checkpoint. If the marker does not appear the target fails and prints the end of `/var/log/sim.log`, which records why the checkpoint was refused; a refused checkpoint is a startup fault.
9. Confirms that the sim is still running 30 seconds after it accepted the checkpoint: `rc-service sim status` must report `started`. The sim acts at once on every due time that has passed (`sim.md` §9.3), and retries a failed connection for seven seconds before it stops (`host_specs/sim/sim-impl.md`, retry policy), so 30 seconds exceeds the time a fault in its first connections takes to stop it, such as a fuel supplier poll due at the start (§10.2). If the sim has stopped, the target fails and prints the end of `/var/log/sim.log`; a sim that stops at the start of a restored run is a startup fault.
10. Appends a line to the set's run log, `snapshots/<label>/runs.log`, giving the real time at which the restore began, read before step 2 sets the clock, and the seed: `<UTC time> seed=<n>`, or `seed=none` without `SEED`. A restore that fails records nothing.

The run log is the record of each run the set has started: the set and the seed reproduce it, since every restore of a set starts from the same checkpoint and a run is reproduced by restoring the same snapshot with the same `SEED`. The files in the mount directory are world-readable, so the sim's unprivileged user can read them through the container's user namespace without an identity shift. Without the device the mount point is empty and the sim cold-starts, as in the deployment sequence above.

### 8.2 Host clock for a restored run<a id='restored-run-clock'></a>

Every instance takes its time from the deployment host's clock: a container reads the host's kernel clock directly, and `corp-it`, the environment's NTP server, is a container on both profiles and serves that clock to every host that synchronises with it. A restored run continues from the history in its snapshots without a gap only if the host clock reads the snapshot time when the instances start, and nothing changes it during the run. The sim refuses a checkpoint written more than five minutes before it starts (`sim.md` §9.3), so a restore that does not set the clock fails before `sim-host` starts (§8.1, step 7).

**Recording the stop time.** When the last instance of a snapshot set has stopped, `make snapshot` (§10.2) writes the current UTC time, in RFC 3339, to `snapshots/<label>/stopped-at`. Every record in the set is earlier than this time.

**Holding the clock.** Before the clock is set, and for the whole run, nothing on the host may set it:

- The host's NTP client is disabled with `timedatectl set-ntp false`, which stops `systemd-timesyncd` on Ubuntu.
- On a Lima virtual machine, the Lima guest agent, which sets the virtual machine's clock from the Mac host's, is denied the capability to set the time by a systemd drop-in, `/etc/systemd/system/lima-guestagent.service.d/no-set-time.conf`, followed by `systemctl daemon-reload` and `systemctl restart lima-guestagent`:

  ```
  [Service]
  CapabilityBoundingSet=~CAP_SYS_TIME
  ```

  Disabling the NTP client alone is not sufficient: the guest agent can set the clock as often as every 10 seconds.

Before it changes either, `make restore` records the hold in `snapshots/clock-held`, which holds the real time at which the hold began. A restored run is in progress while the record exists: a restore that succeeds leaves it, and so does a restore that fails after holding the clock.

**Setting the clock.** With every instance stopped and the instance snapshots restored, `make restore` (§8.1, step 2) sets the clock to the time in `stopped-at` plus five seconds (`date -u -s`). Instances are never running when the clock is stepped: a step while they run is logged by chrony on every host (`Forward time jump detected!`, then `Can't synchronise: no selectable sources` until it resynchronises about two minutes later). A step made before they start leaves no such entry. Every chronyd in the containers runs with `-x` and does not control the clock, and `corp-it` serves the host clock with no upstream source, so the environment's own NTP service does not reveal the shift.

The sim's checkpoint is written before the rest of the set stops (§8.1), so the sim starts later than its checkpoint by the remainder of the stop sequence, the five seconds, and the start sequence up to `sim-host`. That total must stay within the sim's five-minute limit; `make restore` checks it before starting `sim-host` (§8.1, step 7).

**Ending a restored run.** The clock is returned to real time only with every instance stopped. `make -C deploy stop` stops the instances, removes the drop-in, restarts the guest agent, and re-enables the NTP client with `timedatectl set-ntp true`, which steps the clock to real time; once every step has succeeded, it removes `snapshots/clock-held`. A return made while instances run produces the chrony entries above on every host. The target acts only while the clock is held: without `snapshots/clock-held`, no restored environment is running, and it fails, changing nothing, with a message stating that `make stop` applies only to an environment restored from a snapshot set. A deployment created by `make deploy` is therefore never stopped by it, and its sim is never force-stopped.

The run's state is not kept: the next restore replaces every instance's root filesystem with the set's. `make stop` therefore force-stops `sim-host` (`incus stop --force`) without bringing the sim to rest, and stops the remaining instances of the deployed set (§10.2) in the stop order of §10.2, giving each 120 seconds to shut down cleanly (`incus stop --timeout 120`). An instance that has not stopped by then is force-stopped, and the target names it in a warning. A snapshot set is never taken with a forced stop (§10.2).

**Profiles.** The procedure has been exercised on `ci-minimal`: with the clock set 780 seconds behind real time, it held for 20 minutes, the restored records continued from the snapshot without a gap, and chrony inside the environment recorded nothing. On `full`, the virtual machine nodes have their own kernel clocks, which take the host's time at boot and then synchronise with `corp-it`; the same procedure applies, but it is not yet verified on that profile.

### 8.3 Removing the deployment<a id='teardown'></a>

```
make -C deploy down [FORCE=1]
```

runs the teardown commands of the deployed instances (§10.2), which delete every RavensPort instance and network. Deleting an instance deletes its Incus snapshots, after which a snapshot set can be restored only from its snapshot file (§10.3); deleting the instances of a restored run leaves the host clock held (§8.2). Without `FORCE=1`, the target therefore fails, changing nothing, if:

- a restored run is in progress, that is, `snapshots/clock-held` exists; `make -C deploy stop` ends the run and keeps the instances and their snapshots;
- any RavensPort instance holds an Incus snapshot, or any instance exists while a set is incomplete (§10.2), whose checkpoint may be held only on `sim-host`; the target names each set and states whether its snapshot file exists in `snapshots/`, or that the set is incomplete, giving the command that completes it;
- Incus cannot be queried, so that the snapshots cannot be listed.

With `FORCE=1`, the target deletes the instances and their snapshots and then, if the clock is held, returns it to real time as `make stop` does. The step to real time is made after every instance has been deleted, so that no host records it. The set directories in `snapshots/`, with their checkpoints and run logs, are kept.

`make deploy` fails while the host clock is held, since the instances it starts would record the snapshot time; `make up` makes the same check before it builds the images.

---

## 9. Reproducibility<a id='reproducibility'></a>

A deployed environment must be reconstructible to an identical starting configuration.

`episodes.md` §14 (reproducibility) states what the competition's seeds reproduce, the random draws, and what varies between two runs of one seed, the times at which events happen.

### 9.1 Deterministic build<a id='deterministic-build'></a>

The same inputs to `render.py` and to each image build script must produce byte-identical artefacts. This is maintained by:

- **Stable render output**: all generated files use sorted keys, fixed entry ordering (operators and vessels in declared order, not dictionary-iteration order), and fixed numeric formatting.
- **Deterministic credential hashing**: argon2id salts for `FMS_ADMIN_PASSWORD_HASH`, SSHA salts for LDAP operator entries, PBKDF2-HMAC-SHA256 salts for the Grafana user rows on `hist-01` and `hist-dmz-01`, and the security monitoring agent keys are all derived from `world.yaml`'s `crypto.password_salt_seed` combined with the username or node name, not from a random generator at render time, so each value is identical across renders for the same inputs.
- **Committed `infra/sim/sim-constants.toml`**: `render.py` writes the sim's projected constants, at their values in operation, to this path on every render, unconditionally. Cargo's `rerun-if-changed` directive on the file means the binary is recompiled whenever it changes. An edit to `world.yaml` that affects the sim's constants must be followed by a re-render and a commit of the updated `infra/sim/sim-constants.toml`; `deploy/tests/unit/test_sim_constants_agreement.py` fails if the committed file has drifted from `world.yaml`.

---

## 10. Snapshot sets<a id='snapshot-sets'></a>

A snapshot set captures a stopped environment after it has run for a period, so that a restored run can start from that operational history (`narrative.md` §3, planned maintenance windows). A set exists in two forms: a **local set**, made of an Incus snapshot of every instance with the sim's checkpoint and stop time, and the rendered files and render inputs its instances came from, beside it; and a **snapshot file**, a single file that carries a set to another machine. A set restores from its own contents alone: no restore renders, and none reads the local `world.yaml`, topology, profile or `build/`. Most users first meet RavensPort by restoring a snapshot file someone else produced.

Local sets and snapshot files are kept in `deploy/snapshots/`, which git ignores. Paths beginning `snapshots/` are relative to `deploy/`, as are those beginning `build/`.

**Competition deployments.** A deployment of the competition profile takes no snapshot set and is never restored from one (`general-deployment.md` §1.3, competition profile). `make snapshot` refuses such a deployment, and `make restore` refuses a set taken from one, each changing nothing. Each deployment attaches to `sim-host` only the simulator state that it uses: a deployment of the competition profile attaches the start-up input (§8), and a restored run attaches the checkpoint's mount directory (§8.1). The two share no path. The start-up input, the file `startup.json`, is attached as the disk device `startup-input` at `/var/lib/sim/startup`, which only the competition image of `sim-host` holds. The checkpoint's mount directory is attached as the disk device `sim-state` at `/var/lib/sim/state`, by `make restore` alone, until the simulator has accepted the checkpoint (§8.1).

**Recorded parameters.** A snapshot set's checkpoint records the parameters that decide the simulator's behaviour (`host_specs/sim/sim-impl.md`, "Checkpoint document"). No restore compares them. The simulator compares the checkpoint's parameters with its own as it starts, after `make restore` has restored the instances, and refuses a checkpoint whose parameters differ, as a start-up fault (§8.1, step 8). The restore brings back the `sim` binary of `sim-host` with the rest of the set, so a set always starts under the simulator that wrote it, and its checkpoint is accepted. A set is taken again only to run a simulator whose parameters differ from those of the simulator that wrote it.

### 10.1 Producing history

The producer deploys the environment (`make -C deploy up`) and lets it run in real time for the length of history wanted. Nothing is run inside a digital-world instance while it does: every command run inside an instance is recorded in that host's logs and by the security monitoring, and so becomes part of the history. The deployment's own readiness probes (§8) are the only exception.

### 10.2 Taking a local set

```
make -C deploy snapshot LABEL=<label>
```

stops the environment in the order a planned maintenance window shuts the port down, then snapshots it. The target fails, changing nothing, if `<label>` is not made of letters, digits, `.`, `_` and `-`, beginning with a letter or digit. If `snapshots/<label>/` is an incomplete set, the target completes it (below). Otherwise it fails, changing nothing, if `snapshots/<label>/` already exists, if any instance already holds an Incus snapshot `<label>`, which would be taken for one of the set's, if the deployment's rendered files or render inputs are missing, or if any instance is not running. The target also fails, changing nothing, on a deployment of the competition profile. It then:

1. Creates `snapshots/<label>/` holding the file `incomplete`, which marks the set as incomplete until step 8 and records each instance's Incus `last_used_at`, the time of its last start. Copies the rendered files the instances were created from into `snapshots/<label>/rendered/`: `manifest.json`, which lists the instances and networks; `incus-commands.sh`, whose network commands an import runs; and `teardown-commands.sh`. It copies the inputs they were rendered from (§5.2) into `snapshots/<label>/inputs/`. The files come from the deployed set if the instances were restored from one, and from `build/` otherwise.
2. Stops `sim-host` and saves its checkpoint to `snapshots/<label>/checkpoint.json` (§8.1).
3. Waits 60 seconds, so that the FMS forwards its last records to the log server (`narrative.md` §5, temporal and procedural 4).
4. Stops the remaining instances with `incus stop`, which shuts each down cleanly, in this order: the adversary host; the administration workstation, then the SSH bastion; the HMI and the reconciliation workstation; the FMS; the ATG data relay; the primary log server, then the replica; the corporate IT host; the security monitoring manager; the firewalls. The adversary host and then the systems-administration path stop before the hosts they reach, the applications stop before the stores they write to, the security monitoring manager stops after every agent that reports to it, and the firewalls, which carry agent traffic between zones, stop last. The firewalls' own stops are therefore not recorded by the security monitoring. The order is listed in `deploy/scripts/snapshot-lib.sh` (`STOP_ORDER`); an instance added to the topology is placed there by the same rules, and the target fails for an instance the list does not place. Every stop is a clean stop with no time limit: a forced stop discards the data an instance's stores have not yet written, and the set would capture that loss.
5. Writes the current UTC time to `snapshots/<label>/stopped-at` (§8.2).
6. Confirms that the set can be restored. A restore sets the host clock to the stop time plus five seconds (§8.2), and the sim refuses a checkpoint written more than five minutes before it starts (`sim.md` §9.3). If the stop time plus five seconds is more than five minutes after the checkpoint's `written_at`, no restore of the set can start the sim, and the target fails.
7. Creates the Incus snapshot `<label>` of every instance (`incus snapshot create`).
8. Removes `snapshots/<label>/incomplete`.

**A supplier poll due at the start of a restored run.** Once the set is complete, the target warns, without failing, if the fuel supplier's next poll recorded in the checkpoint (`fuel_supplier.next_poll_at`) is due less than 60 seconds after the time a restore sets the clock to, the stop time plus five seconds, or before that time. A restore starts `sim-host` after `atg-relay-01`, whose health check has by then marked the ATG console's address down, and the relay passes no connection until its check has passed twice, ten seconds apart. A poll due by then is made as soon as the sim starts (`sim.md` §9.3), is refused at the relay, and exhausts its retries (`host_specs/sim/sim-impl.md`, retry policy), so the sim stops within seconds of every restore of the set; the restore reports it (§8.1, step 9). On `ci-minimal` the sim started about nine seconds after the clock was set, and the supplier stops retrying seven seconds after its first attempt, so a poll due less than about 22 seconds after that time stops the sim; the 60 seconds allow for a slower restore. Polls fall due whether or not an order is pending (`sim.md` §5.3), so the check applies equally while a tanker is awaited. The set is kept, since the history it holds cannot be taken again, and the warning advises against exporting it.

**Failure.** A failure before the sim's checkpoint is confirmed on `sim-host` (§8.1, step 2) removes `snapshots/<label>/`: the sim has written no checkpoint, so the set holds nothing that could be completed. From that confirmation the checkpoint is the only record of the sim's state, so a later failure, including an interruption of the target, keeps the set with its `incomplete` file, and the target names the command that completes it. Incus snapshots already created are kept: each was taken after every instance had stopped. A restore (§8.1) and an export (§10.3) refuse an incomplete set.

**Completing a set.** Once the cause of the failure is removed, `make -C deploy snapshot LABEL=<label>` completes an incomplete set. It takes the instance list from the set's `rendered/manifest.json`, not from `build/`, and repeats steps 2 to 8, omitting what is already done: it copies the checkpoint from `sim-host` only if the set does not hold it, stops `sim-host` and the remaining instances only if they are running, waits 60 seconds only if an instance other than `sim-host` remains to be stopped, keeps a stop time already recorded, and creates only the missing Incus snapshots. A completion after a failure in step 4 records the stop time once it has stopped the remaining instances, which continue to record until then, so it passes step 6 only if it is made within the limit that step states.

The target fails, changing nothing, keeping the set and stating that it cannot be completed, if an instance of the set no longer exists, if `sim-host` is running and its sim service is not stopped, if the set lacks the checkpoint and `sim-host` no longer holds it, if any instance is running once the stop time has been recorded, or if any instance's `last_used_at` differs from the one recorded in `incomplete`, which shows that the instance has been started since the set was begun, even if it has since been stopped; and it fails at step 6 in the same way. In each case the set cannot hold a consistent state: the environment has run since the checkpoint, the sim's state or an instance has been lost, or no restore could start the sim. Such a set is discarded by deleting `snapshots/<label>/` and the Incus snapshots `<label>` already created.

A set is restored locally by `make -C deploy restore SNAPSHOT=<label>` (§8.1). `make -C deploy down` deletes the instances and with them their snapshots, so it refuses while any instance holds one unless `FORCE=1` is given (§8.3); a set that is to survive it must first be exported.

**The deployed set.** A restore or an import records the label of the set in `snapshots/deployed-set`. While it is recorded, the instances are the set's, and `make snapshot`, `make stop` and `make down` take the instance list and the teardown commands from the set's `rendered/` directory rather than from `build/`, which a later render may have changed. `make down` removes the record once it has removed the instances, and `make deploy` removes it before it creates instances from `build/`.

### 10.3 Snapshot files

```
make -C deploy export-snapshot LABEL=<label>
```

writes the local set `<label>` to one file, `snapshots/ravensport-snapshot-<label>-<profile>-<arch>.tar`, where `<profile>` is the profile in the set's rendered manifest (§7) and `<arch>` the host architecture as `uname -m` reports it (`aarch64` or `x86_64`). An incomplete set (§10.2) is refused. The repository is on the directory Lima mounts from the host, so the file is available on the host for distribution. Each instance is exported from its snapshot, not its current state: the snapshot is copied to a temporary instance, which is exported with `incus export --instance-only --compression zstd` in the storage-independent format and then deleted. Incus gives each network interface of a copy a new media access control (MAC) address, so before the export the temporary instance's `volatile.<interface>.hwaddr` keys are set to the snapshot's; the imported instances then keep the producer's MAC addresses. The file is an uncompressed tar archive containing:

| Member | Content |
|---|---|
| `manifest.json` | The fields listed below |
| `instances/<instance>.tar.zst` | Each instance's export |
| `checkpoint.json` | The sim's checkpoint |
| `stopped-at` | The stop time |
| `rendered/` | The set's rendered files (§10.2) |
| `inputs/` | The set's render inputs (§10.2) |

The manifest is a JSON object with these fields:

| Field | Content |
|---|---|
| `format_version` | The snapshot file format version, `2` |
| `label` | The set's label |
| `profile` | The deployment profile |
| `arch` | The host architecture |
| `commit` | The RavensPort commit the set was produced from |
| `incus_version` | The Incus server version |
| `stopped_at` | The content of `stopped-at` |
| `instances` | For each instance, an object with `name`; `archive`, the member `instances/<name>.tar.zst`; `sha256`, the archive's SHA-256 digest; and `disk_usage_bytes`, the uncompressed size of the archive |
| `files` | For each file under `rendered/` and `inputs/`, an object with `path`, the member's path, and `sha256`, its SHA-256 digest |
| `checkpoint_sha256` | The SHA-256 digest of `checkpoint.json` |
| `stopped_at_sha256` | The SHA-256 digest of `stopped-at` |

An instance's disk usage is the uncompressed size of its export, which an import writes back to the storage pool. Incus reports no disk usage for an instance on a `btrfs` storage pool without quota groups, so the usage is not taken from Incus.

The render inputs are a record, which no restore reads: with the commit, they let the set's instances be rebuilt exactly. The recorded `world.yaml` carries `crypto.password_salt_seed`, from which the internal certification authority's private key, the SSH keys and every credential hash are derived (`deployment-config.md` §6), so anyone holding the file can derive them. For the committed `world.yaml` this discloses nothing that the repository does not; a producer who uses a different seed distributes it with every file.

### 10.4 Importing a snapshot file

```
make -C deploy restore SNAPSHOT=<path to snapshot file>
```

imports the file and then restores it as §8.1 specifies. The file may be anywhere on the mounted directory; how it was obtained is outside the deployment, which never downloads (`general-deployment.md` §1.1). Before changing anything, the target refuses the file if:

- its format version is not one the target reads;
- its architecture differs from the host's;
- its label is not a valid label (§10.2), any instance entry has a name other than lower-case letters, digits and `-` or an archive other than `instances/<name>.tar.zst`, or any `files` entry has a path outside `rendered/` and `inputs/`;
- it lacks any of the rendered files of §10.2;
- any member's SHA-256 digest differs from the manifest's, which detects a truncated or corrupted file;
- its instance entries name other instances than its rendered manifest;
- any RavensPort instance already exists (`make -C deploy down` removes them, with `FORCE=1` if they hold snapshot sets, §8.3);
- the storage pool's free space is less than the instances' recorded disk usage;
- `snapshots/<label>/` already holds a set whose `checkpoint.json` differs from the file's, which is a different set with the same label.

It then runs the file's `rendered/incus-commands.sh` up to its instance commands, which creates the zone networks as the producer rendered them, without creating instances; records the set as the deployed set (§10.2); imports each instance (`incus import`); copies `checkpoint.json`, `stopped-at`, `rendered/` and `inputs/` to `snapshots/<label>/`; and creates the snapshot `<label>` of every imported instance, so that later restores of the set are local and do not read the file again. Nothing is rendered and no image is built: each instance carries its own root filesystem, configured from the producer's `world.yaml`, and the sim binary in `sim-host`, which carries the producer's constants, is the one that wrote the checkpoint. The profile is the file's; the local `world.yaml`, topology, `PROFILE` and `build/` are not read. Only the snapshot scripts come from the checkout, and the format version (§10.3) is what they check the file against: if the file's commit differs from the checkout's, the target warns and continues. It fails if any network an imported instance names does not exist after the networks are created.

The digests detect damage, not tampering. A snapshot file contains the root filesystem of every RavensPort instance as its producer built it, and restoring the file runs those instances on the deployment host, so a user restores only files from a producer they trust; establishing that trust is part of obtaining the file.

**Profiles.** Local sets and export have been exercised on `ci-minimal` (§8.2, §10.5); import has not yet been exercised on a deployment of either profile. On `full`, virtual machine instances export as disk images, so the files are larger.

### 10.5 Tests

`deploy/tests/unit/test_snapshot_sets.py` and `deploy/tests/unit/test_snapshot_scripts.py`, run by `make -C deploy test-unit`, need no deployment. The first checks that every node of the topology has a place in the stop order and that a node with none is refused, and that an import refuses, changing nothing, a file of another format version or architecture, a file with a corrupted member, a malformed instance or file entry, missing rendered files, or instance entries that differ from its rendered manifest, a host on which an instance already exists, and a storage pool that is too small. The second runs the snapshot targets in a copy of `deploy/` against a stub `incus` (`deploy/tests/unit/stub_incus.py`), which holds the instances, snapshots, devices and networks as state, and stub `sudo`, `systemctl`, `sleep` and `make` commands, which record their arguments and change nothing on the host. Every stub records its commands in one log, and the tests assert the order of the steps as well as the end state:

- `make snapshot` stops the sim first, then the remaining instances in the stop order, and snapshots no instance before every instance has stopped; it copies the rendered files and render inputs into the set, from the deployed set when there is one; it refuses an invalid or existing label, a label an instance already holds as a snapshot, a deployment without recorded render inputs and a partly stopped environment; a sim that did not come to rest leaves no set; a failure once the checkpoint is confirmed keeps the set, marked incomplete, and the target run again completes it from the set's own instance list, without stopping the sim or any instance a second time, changing a recorded stop time or creating a snapshot twice, after which the set restores; it refuses, changing nothing, to complete a set whose sim has been started, an instance of which has been started since the stop time, started and stopped again since the set was begun, or deleted, or whose checkpoint has been lost; it refuses a set that no restore could start, before any instance is snapshotted; and it warns, keeping the set, when the fuel supplier's next poll is due less than 60 seconds after a restore's clock time or before it, and not when the poll is later, whether or not a tanker is due.
- A render records its inputs in `build/inputs/`.
- `make restore` restores every instance before it holds and sets the clock, sets the clock before any instance starts, starts the instances in the reverse of the stop order and `sim-host` last, attaches the mount directory read-only, and removes the device once the sim has accepted the checkpoint; it writes a reseed file only when `SEED` is given, and a later restore without `SEED` leaves none; it records each run it starts in the run log, and none that fails; it refuses each precondition of §8.1, including another set being incomplete, a checkpoint the sim would refuse, a checkpoint the sim does not accept, and a sim that stops within 30 seconds of accepting its checkpoint; it takes its instance list from the set, not `build/`, and records the deployed set; and after it, `make stop` and `make down` use the set's rendered files, and `make down` removes the record.
- `make snapshot` refuses a deployment of the competition profile, and `make restore` refuses a set taken from one, or a snapshot file of such a set before importing it, each changing nothing. The scripts recognise such a deployment by the image that the competition's additional override file gives `sim-host`, and the start-up input and the checkpoint's mount directory share no path.
- `make stop` fails, changing nothing, when the clock is not held; otherwise it stops every instance before it releases the clock, and force-stops an instance that does not stop within 120 seconds; the clock is recorded as held from the restore until the run ends.
- `make down` fails, changing nothing, while a restored run is in progress, while an instance holds a snapshot set or any instance exists while a set is incomplete, including one no instance holds a snapshot of, naming each set and whether it has been exported or is incomplete, and when Incus cannot be queried; otherwise it tears the deployment down. With `FORCE=1` it removes the instances, then releases a held clock, and keeps the set directories. `make deploy` fails while the clock is held.
- `make export-snapshot` writes the file of §10.3 with correct digests and disk usage, and refuses an incomplete set; a file exported on one host restores on another that has never rendered, without running `make`; and an import warns when the commits differ, refuses a different set with the same label, and fails when an imported instance names a network the topology does not create.
- The paths, file names and start gap the scripts use, and the checkpoint's record of the fuel supplier's next poll, agree with the sim's source.

`deploy/tests/integration/test_snapshot.sh`, run by `make -C deploy test-snapshot`, takes a set from a running deployment, exports it, restores it twice, the second time with `SEED`, and stops each restored run. It checks what the stubs cannot show: that the set and the file carry the rendered files and render inputs, that the sim writes a checkpoint it accepts on restore, that the read-only device mounts in the unprivileged `sim-host` container, that every export has a non-zero size and carries its snapshot's MAC addresses, that an instance imported from an export keeps them, that the host clock is held near the stop time and recorded as held during a restored run and returned to time synchronisation afterwards, and that Incus lists an instance's snapshots in the form `make down` reads. The test stops the environment, steps the host clock, and can take up to an hour while the sim comes to rest, so `make test-integration` does not run it. It deletes its set, its file and the imported instance, restores the deployed-set record it found, and leaves the instances running from the set's state, with the sim cold-started.
