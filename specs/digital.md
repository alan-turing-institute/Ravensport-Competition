# Digital

Build plan for the maximally-realistic digital world: the software and systems deployed by Ravensport's IT/OT team on their own hosts. Proprietary appliances
(ATG console, dispenser, RFID reader) are part of the simulated physical world, described separately in [sim.md](sim.md).

See [`docs/testbed.md`](../docs/testbed.md) for how this document relates to narrative.md and sim.md.

The testbed is constrained to open-source / open-licence software. This document therefore specifies each host by *component class* and by the *interfaces* it must present at the sim<->digital boundary and on the OT network, not by a fixed vendor product. Where a concrete product is named in a "Testbed realisation" row, it is an example of an acceptable open-source choice; substitutions are permitted provided the boundary protocols and data models in §3 and §4 are preserved. The deployment substrate (VMs, containers, or a mix) and the orchestrator that manages it are not prescribed — see §10.

**Contents:**
1. [Network architecture](#network-architecture)
2. [Host inventory](#host-inventory)
3. [Sim<->digital boundary protocols](#boundary-protocols)
4. [Application data flows](#application-data-flows)
5. [Identity & authentication](#identity-authentication)
6. [Logging & retention](#logging-retention)
7. [Product choices](#product-choices)
8. [Decisions](#decisions)
9. [Deviations from real-world practice](#deviations)
10. [Testbed orchestration](#testbed-orchestration)

---

## 1. Network architecture<a id='network-architecture'></a>

OT / DMZ / IT zones, segmentation, firewall posture, NTP. (Per [narrative](narrative.md)
§4.5, §5.)

![Network diagram](diagrams/network.png)

*(Source: `diagrams/network.mmd`. Run `make` or visit [mermaid.live](https://mermaid.live/) to re-render.)*

### 1.1 Zones

Four zones, modelled on the Purdue reference architecture as adapted for
a small port site:

- **OT (Level 2/3)** — dock control room and dock-side equipment. Hosts:
  ATG console, FMS, primary log-server, HMI.
- **DMZ (Level 3.5)** — controlled bridge between OT, IT, and External.
  Hosts: the read-only log-server replica, the ATG data relay, and the Secure Shell (SSH) bastion.
- **IT (Level 4)** — corporate network. Hosts: reconciliation workstation,
  administration workstation, the identity and time server, and the security monitoring server.
- **External** — the public internet. In the testbed, represented by
  `vnet-ext` hosting the fuel supplier actor (see sim.md §5.3).

### 1.2 Segmentation and firewall posture

Default deny between all zones; allow-lists are per-service, per-host, per-port.

Within a zone there is no restriction: hosts on the same zone bridge reach each other on any port, because the firewall instances sit at zone boundaries and there is nothing between hosts inside one. 

Allow inbound to the DMZ:
- **OT → DMZ**: log-server push to the replica (one direction); NTP and identity (LDAP over TLS) transit traffic from OT hosts en route to `corp-it` on the IT zone (hop 1 of the two-hop path); security monitoring agent traffic from OT hosts en route to `siem-01` (hop 1 of the same path).
- **IT → DMZ**: reconciliation user's read-only queries against the log-server replica (HTTPS); SSH from `admin-01` to `dmz-bastion-01` only.
- **External → DMZ**: TCP 10001 from the supplier IP only, to `atg-relay-01`. All other inbound from External is denied.

Allow outbound from the DMZ:
- **DMZ → OT**: TCP 10001 from `atg-relay-01` to the ATG console IP only; SSH from `dmz-bastion-01` to `fms-01`, `hist-01`, and `hmi-01` only.
- **DMZ → IT**: NTP and identity (LDAP over TLS) traffic to `corp-it`, and security monitoring agent traffic to `siem-01` (hop 2 of the two-hop path; also covers DMZ-local hosts reaching either directly).


Firewall class: an open-source stateful firewall product, deployed as three
instances (OT<->DMZ, DMZ<->IT, External<->DMZ). Candidates include
**pfSense CE** `[free]` and **OPNsense** `[free]` — production-grade BSD
firewalls providing stateful rules, logging, and a realistic "enterprise
firewall" artefact. Industrial firewalls like Fortinet FortiGate Rugged or
Cisco ISA-3000 are more typical at bigger ports but are `[paid]` and are
therefore not chosen.

### 1.3 Time (NTP)

Per narrative §5, clocks on the FMS and the log-server must be within 1 s.
NTP is hosted on `corp-it` in the IT zone (see §2.7), against which every
digital-world host runs a chrony client. OT hosts query it via
the two-hop path vnet-ot → `fw-ot-dmz` → vnet-dmz → `fw-dmz-it` → vnet-it.
DMZ hosts (including the log-server replica) query it directly via
vnet-dmz → `fw-dmz-it` → vnet-it. Any open-source NTP daemon is acceptable;
**chrony** on Linux `[free]` is a reasonable default.

The ATG console, as all sim-side appliances, keeps its own clock and is not
a network time client; per narrative §5, timestamps in its reports may differ
from FMS and log-server records by up to five minutes.

---

## 2. Host inventory<a id='host-inventory'></a>

Per host: role, OS, software stack, services, users. Covers FMS,
log-server (primary), HMI, log-server replica, ATG data relay, SSH bastion,
reconciliation workstation, administration workstation. Sealed appliances (ATG console, dispenser,
RFID reader) are sim-side and are covered in sim.md; digital.md only
meets them at the boundary protocols in §3.

> Convention: each host below lists **Zone / OS / Software / Interfaces /
> Users**. "Interfaces" means network and sim<->digital boundary
> interfaces. Full protocol detail is in §3; full data flows in §4.

### 2.1 Fuel Management System (FMS) — `fms-01`

- **Zone**: OT
- **Role**: authentication, authorisation lifecycle, transaction
  logging, preset control for the tug dispenser, transcription entry for
  inbound fills, and wetstock reconciliation. The operational heart of the dock.
- **Real-world product**: at a port, the two common families are
  **Gilbarco Veeder-Root Passport** (forecourt-style) and **OPW Phoenix
  / FSC3000** (fuel site controller). Both are `[paid]` appliances with
  no community edition. **Fuelsoft** and **Triscan** are the UK/EU
  equivalents, also `[paid]`.
- **Testbed realisation**: build a realistic FMS as a custom open-source
  service backed by an open-source relational database, whose **external
  interfaces and data model** mirror a real product — Tank Level System
  (TLS) polling northbound, preset and pulse I/O to the dispenser,
  append-only transaction log with the fields a real FMS carries (job ID,
  authorisation ID, operator, counterparty, fuel grade, preset volume,
  start/end totaliser, ATG delta cross-check, BDN/ticket number,
  timestamps). The specific web framework and database product are not
  prescribed. Implementations that deploy as a self-contained compiled
  binary are preferred; stacks that require an interpreted-language
  runtime on the host (e.g. Python, Node.js) are not preferred for OT
  hosts, matching the closed-binary deployment posture of commercial FMS
  products.
- **OS**: any current open-source Linux distribution `[free]`.
- **Interfaces**:
  - ATG polling to the ATG console appliance (Tank Level System (TLS) protocol — see §3).
  - Dispenser control: preset + pulse counting, solenoid enable — see §3.
  - Vessel RFID reader ingest: TCP connection to the networked reader on `vnet-ot` — see §3.
  - Log-server push (see §2.2).
  - HMI: HTTPS UI.
- **Users**: admin; systems administrator (SSH, §4.6).

### 2.2 Log-server (primary) — `hist-01`

- **Zone**: OT
- **Role**: time-series store for ATG readings and alarms; event log for
  authorisations, transactions, logins, config changes. 3-year retention
  aligned to MARPOL (narrative §5).
- **Real-world product**: **OSIsoft/AVEVA PI System** is the dominant
  enterprise historian `[paid]`; **GE Proficy Historian** `[paid]`;
  **Canary Labs** `[paid]`. For smaller sites, **Ignition** by Inductive
  Automation `[trial]` ships a tag historian and is a common testbed
  choice on the trial licence. None of these are open-source.
- **Testbed realisation**: **VictoriaMetrics** + **VictoriaLogs** + **Grafana**
  `[free]`, with **vmagent** and **vlagent** as the ingestion/replication front-end
  (`host_specs/hist/hist.md §2`). Atypical for port deployments but unavoidable under the
  open-source constraint. Recorded as a deviation in §9.
- **OS**: any current open-source Linux distribution `[free]`.
- **Interfaces**:
  - Ingest from FMS (event stream), over TLS (§2.7).
  - Push to DMZ Log Server replica (see §2.4), over TLS (§2.7).
  - NTP client (connecting to chrony running on `corp-it`).
  - Serves read-only HTTPS to the OT zone (Grafana listens on port 443; a human
    browses this directly, e.g. an admin from the `hmi-01` workstation — it is not
    consumed programmatically by the HMI application. See `host_specs/hist/hist.md §7`).
- **Users**: admin; systems administrator (SSH, §4.6).

### 2.3 Human-Machine Interface (HMI) — `hmi-01`

- **Zone**: OT
- **Role**: dock operator's single pane — ATG state, FMS transactions,
  authorisation queue, alarms.
- **Real-world product**: at this scale the HMI is typically **Ignition
  Perspective** (browser-based) `[trial]` or **Wonderware/AVEVA InTouch**
  `[paid]`. Neither is open-source.
- **Testbed realisation**: a small bespoke browser-based web UI on top of
  the FMS and log-server read APIs `[free]`. The HMI carries no business
  logic beyond presentation; authorisation and audit live in the FMS.
- **OS**: any current open-source Linux distribution `[free]` running a
  modern browser. Recorded as a deviation in §9 (real OT workstations run
  Windows).
- **Interfaces**:
  - HTTPS to FMS and log-server
  - operator RFID reader: TCP connection to the networked reader on `vnet-ot` for operator login — see §3 (and §9 deviation).
- **Users**: dock operator (user/pass + RFID); systems administrator (SSH, §4.6).

### 2.4 Log-server replica (DMZ) — `hist-dmz-01`

- **Zone**: DMZ
- **Role**: read-only mirror of `hist-01`, queried by the reconciliation
  user on IT. Divergence between this and the primary is itself a
  detection signal (narrative §4.5).
- **Real-world product**: same product as the primary log-server, in
  replica mode. Replication is by continuous push from `hist-01`'s vmagent and
  vlagent instances (`host_specs/hist/hist.md §6`); `hist-dmz-01` runs no ingestion agent
  of its own. `[free]` matches the primary.
- **OS**: matches the primary.
- **Interfaces**:
  - receives metric and event pushes from `hist-01`'s vmagent/vlagent, over TLS
    (§2.7).
  - serves read-only HTTPS to IT (Grafana listens on port 443, queried by the
    reconciliation user).
  - NTP client (connecting to chrony running on `corp-it`, direct one-hop via
    `fw-dmz-it`).
- **Users**: reconciliation user (Grafana's built-in user store).

### 2.5 Automatic Tank Gauge (ATG) data relay — `atg-relay-01`

- **Zone**: DMZ
- **Role**: thin TCP proxy accepting inbound connections on TCP 10001
  from the fuel supplier's monitoring platform. Validates the source IP
  against a whitelist and forwards the byte stream to the ATG console on
  `vnet-ot`. No protocol parsing — Veeder-Root Tank Level System (TLS) serial bytes are
  forwarded transparently.
- **Real-world product**: an open-source TCP proxy supporting stream-mode
  forwarding and source-IP access control lists (ACLs). Candidates include
  **HAProxy** `[free]` in stream (TCP) proxy mode and **NGINX** `[free]`
  with the `stream` module; either matches the supplier source IP and
  rejects non-matching connections. No SSL termination — the proprietary
  Tank Level System (TLS) serial protocol has no transport encryption and
  the proxy forwards opaque bytes.
- **OS**: any current open-source Linux distribution `[free]`.
- **Interfaces**:
  - inbound TCP 10001 from `vnet-ext`
  - outbound TCP 10001 to ATG console IP on `vnet-ot` (via `fw-ot-dmz`).
- **Access control**: source IP whitelist only. The Veeder-Root Tank Level System (TLS)
  serial protocol has no authentication; the whitelist is the sole
  barrier.
- **Users**: none.

### 2.6 Reconciliation workstation — `recon-01`

- **Zone**: IT
- **Role**: end-of-period audit of log-server replica vs. paper BDNs and
  delivery tickets.
- **Real-world product**: a workstation that can access the log replica datastores.
- **OS**: any current open-source Linux distribution `[free]`.
- **Software**: a graphical desktop environment, a remote desktop service, a terminal emulator, an audit client, a clipboard tool, and a modern browser. Full specification: `host_specs/recon/recon.md`.
- **Audit path**: via a terminal. The audit client retrieves the period's record through the visualisation tool's datasource proxy and pipes it onto the clipboard. 
A browser is installed, because an office workstation has one, but is not on the audit path. 

  The audit's footprint on the log-server replica is a sequence of datasource-proxy requests rather than a dashboard loading, so an observer on `hist-dmz-01` can distinguish reconciliation work from a person reading the dashboard. Additionally the authentication credential is presented on the command line, so it comes to rest in the user account's shell history on the reconciliation workstation. This is acknowledged as a potential attack path (`host_specs/recon/recon.md` §7.2).
- **Records held**: no full copy of the period's records. The retrieval is piped to the clipboard and no export is written to disk.
  The workstation holds the auditor's own conclusions: `narrative.md` §3 has a summary of each audit recorded here — the period, whether the records agreed, and the document numbers of any that did not.
- **Remote access**: the workstation in the IT zone is reached over Remote Desktop
  Protocol from the sim host. This is how the reconciliation user works at
  the machine, and in the testbed it is the sole channel between the
  reconciliation user actor and the workstation.
- **Users**: reconciliation user, holding two distinct credentials — one for the workstation, validated against the directory on `corp-it`, and one for the log-server replica (§5.1) datastore. The workstation itself stores neither.

### 2.7 Supporting infrastructure

- **Identity provider (IdP) and NTP (IT)**: `corp-it` in the IT zone hosts
  both the open-source LDAP directory (operator and local admin accounts) and
  the NTP daemon. Consolidating both on a single IT host reflects the decision
  that identity and time services are corporate IT infrastructure shared across
  facilities, not per-facility OT services. The directory serves LDAP over TLS
  (LDAPS) on port 636 only, using a CA-signed leaf certificate (see the
  internal certificate authority entry below); every directory bind is
  therefore encrypted in transit. NTP remains
  plain Network Time Protocol on
  port 123 (`host_specs/corp-it/corp-it.md §4`). LDAP and NTP traffic from OT hosts
  transits `fw-ot-dmz` and `fw-dmz-it`; see §1.2 and §1.3.
  The initial deployment uses **OpenLDAP (slapd)** `[free]`; **FreeIPA**
  `[free]` is the planned upgrade target, with **Samba AD-DC** `[free]` as an
  alternative. **chrony** `[free]` is the NTP daemon.

- **Internal certificate authority (CA)**: a single offline root CA that
  signs TLS leaf certificates for `fms-01`, `hmi-01`'s HAProxy front end, 
  `corp-it`'s LDAP directory, `siem-01`'s security-monitoring management 
  interface, `recon-01`'s remote-desktop service, and
  every network-facing service on `hist-01` and `hist-dmz-01` — Grafana,
  vmagent, and vlagent on `hist-01`; Grafana, VictoriaMetrics, and VictoriaLogs
  on `hist-dmz-01` — so that each trusts a common root instead of presenting
  a self-signed certificate. One keypair per host is reused across every
  service on it (`host_specs/hist/hist.md §9.2`). The
  sim-host's HAProxy dock-operator backend trusts that same root —
  validating hmi-01's certificate with `verify required` instead of `verify
  none` — but is a TLS client on that path, not a server, and so holds no
  certificate of its own. The CA is not a running service — no online
  issuance, Automatic Certificate Management Environment (ACME),
  Online Certificate Status Protocol (OCSP), or renewal endpoint.
  It runs once at build time
  under `deploy/build/ca/`, reusing the input-hash caching pattern used for
  host images (`deployment.md §6.1`). Every node — not only these — trusts
  the CA root in its own system certificate store (`deployment-config.md
  §4`), the same posture a real internal CA's root has across a corporate
  fleet, independent of whether that node makes a CA-dependent TLS
  connection today. A host that is not part of the asset holds none of this
  material — no leaf certificate, no root certificate, and no knowledge of
  the CA or its keys — and stands towards the fleet exactly as any external
  machine does. All keys are RSA-2048. The root certificate validity is
  10 years and leaf certificates are valid for 1 year. Leaf certificates
  carry DNS Subject Alternative Names (SANs) matching each host's hostname
  only — no IP SAN - so TLS clients must validate peers by hostname, not
  IP address. A connecting peer must therefore resolve the target host via
  its local `/etc/hosts` store, rather than connecting by IP address.
  The root private key is
  generated on the build host and never projected into any deployed
  instance; only the signed leaf certificates and the CA's public root
  certificate (`ca.pem`) are baked into images. See §8 decision 7.

### 2.8 Security monitoring — `siem-01`

- **Zone**: IT
- **Role**: collects log and network activity data from every digital-world
  host, evaluates it against detection rules, and retains the resulting
  alerts alongside the raw records. Full specification:
  `host_specs/siem/siem.md`.
- **Real-world product**: **Splunk**, **Microsoft Sentinel**, **IBM QRadar**,
  and **HPE ArcSight** are the products a port of this scale would run; all
  are `[paid]`. Passive OT network monitoring appliances (**Nozomi
  Networks**, **Claroty**, **Dragos**) are likewise `[paid]`.
- **Testbed realisation**: **Wazuh** manager `[free]`, with a Wazuh agent on
  every monitored host and **Suricata** `[free]` at the three zone
  boundaries. Wazuh's storage and search components are specified as an
  extension but are not deployed (`host_specs/siem/siem.md §9`); alerts and raw
  records are consumed from files. Deviations recorded in §9.
- **OS**: any current open-source Linux distribution `[free]`.
- **Interfaces**:
  - inbound TCP 1514 from every monitored host's agent (OT hosts via the
    two-hop path, DMZ hosts via `fw-dmz-it`, IT hosts intra-zone).
  - management interface on TCP 55000 (HTTPS).
  - NTP client (connecting to chrony running on `corp-it`).
- **Users**: admin.

### 2.9 Administration workstation — `admin-01`

- **Zone**: IT
- **Role**: workstation used by the systems administrator to maintain the port-managed systems on the dock network (§4.6).
- **OS**: any current open-source Linux distribution `[free]`.
- **Software**: terminal, OpenSSH client and server, and standard Linux administration tools.
- **Interfaces**:
  - inbound SSH from the sim host on `vnet-it` (Remote access, below).
  - outbound SSH to `dmz-bastion-01`; the onward connection to an OT host passes through the bastion.
- **Credentials**: the administrator's private SSH key remains on this workstation.
  OpenSSH `ProxyJump` carries the end-to-end SSH connection through the bastion, so no
  reusable OT credential is stored on `dmz-bastion-01`.
- **Workstation login**: the systems administrator signs in against the directory on
  `corp-it`, consistent with other corporate workstations.
- **Remote access**: the workstation is reached over SSH from the sim host, with the systems administrator's directory password. This is how the systems administrator works at the machine, and in the testbed it is the sole channel between the systems administrator actor and the workstation. The workstation's software is terminal-based, so its sign-in is a terminal session.
- **Users**: systems administrator.

### 2.10 SSH bastion — `dmz-bastion-01`

- **Zone**: DMZ
- **Role**: SSH bastion between the systems administrator's workstation on IT and
  the port-managed Linux systems on OT.
- **Real-world product**: **OpenSSH** `[free]`.
- **OS**: any current open-source Linux distribution `[free]`.
- **Interfaces**:
  - inbound SSH from `admin-01` on `vnet-it` (via `fw-dmz-it`).
  - outbound SSH to `fms-01`, `hist-01`, and `hmi-01` on `vnet-ot`
    (via `fw-ot-dmz`).
- **Authentication**: the systems administrator uses an SSH key held on `admin-01`.
  OpenSSH `ProxyJump` authenticates the administrator to the bastion and carries the
  separately authenticated connection to the selected OT host.
- **Shell**: none. The administration account's shell is `/usr/sbin/nologin`, so the bastion only relays connections (§4.6).
- **Users**: systems administrator.

The bastion is a separate host because it is part of the port's systems-administration
path, while `atg-relay-01` supports the supplier's monitoring service.
This follows the guidance cited in §8 decision 8, which recommends dedicated hardware for remote access servers, and lets the two operational functions be maintained independently.

---

## 3. Protocols at the sim<->digital boundary<a id='boundary-protocols'></a>

The digital world meets the sim exclusively through these protocols.
No digital component reads sim state directly.

| Signal | Protocol | Port / transport | Direction | Notes |
|---|---|---|---|---|
| ATG level, temp, volume, alarms | Veeder-Root Tank Level System (TLS) serial-over-TCP | TCP 10001 | ATG console → `fms-01` | Function codes i101, i201, i205 |
| Dispenser preset + enable | Modbus TCP | TCP 502 | `fms-01` → dispenser | Registers 0x0001–0x0002 |
| Dispenser totaliser + status | Modbus TCP | TCP 502 | Dispenser → `fms-01` | Registers 0x0003–0x0007 |
| Vessel RFID card number | Card-number stream over TCP | TCP 9001 to reader on `vnet-ot` | RFID reader → `fms-01` | Networked reader; `fms-01` connects over its OT NIC. 26-bit card number; no return channel. |
| Operator RFID card number | Card-number stream over TCP | TCP 9002 to reader on `vnet-ot` | Operator RFID reader → `hmi-01` | Networked reader; `hmi-01` connects over its OT NIC. 26-bit card number; no return channel. Deviation: §9. |
| ESD contact | Hardwired dry contact | — | ESD button → pumps/valves | Not on network |
| Overfill interlock | Hardwired dry contact | — | Probe → fill cabinet socket | Not on network |

All sim-side appliances appear on `vnet-ot` (the orchestrator's OT
virtual network).

---

## 4. Application-level data flows<a id='application-data-flows'></a>

### 4.1 Job creation lifecycle

1. Dock operator logs into HMI (user/pass validated against the
   LDAP directory on `corp-it` in the IT zone, reached over the
   two-hop firewall path of §1.2; RFID card number validated against the FMS
   internal card table).
2. Operator submits job request: type (fill or dispense), counterparty,
   grade, volume. FMS checks that no job is in progress. A dispense request
   is refused unless a current ATG reading puts the tank volume strictly
   above the cavitation threshold of ~10% of capacity (narrative §5); if no
   current reading can be obtained the request is refused rather than
   decided on a stale one. Rejection creates no job. The check does not
   apply to a fill request, which an unavailable reading does not block.
   The mechanism is `host_specs/fms.md §5.1`.
3. FMS creates a job record: job ID, operator identity, the authenticated
   session the request was made under, timestamp, job parameters, and the ATG
   volume at open, which is null for a fill opened without a reading. The job is
   immediately active.
4. FMS pushes a `job_opened` event to the log-server.

### 4.2 Dispense transaction lifecycle (tank → tug)

1. Tug engineer presents vessel RFID tag at the dispenser reader.
   FMS receives the Wiegand card number and validates it against the
   vessel the job names: the card must be active and enrolled to that
   vessel (§5.3). **This is a blocking gate** — the operator
   cannot proceed until the RFID is validated.
2. Operator sets the preset volume and requests dispense start via the HMI.
   The FMS refuses the start unless a current ATG reading puts both the tank
   volume and the projected closing volume (the current volume less the
   preset) strictly above the cavitation threshold; the mechanism is
   `host_specs/fms.md §5.1`. If rejected,
   the job remains active, no Modbus write occurs and no transaction-open
   event is emitted; the operator cancels the refused job so it no longer blocks
   the next one, and the tug departs unserved. The FMS records the cancellation
   and pushes a `job_cancelled` event to the log-server. After a
   successful check, FMS transitions the job to running,
   records the start instant (`opened_at`), which the HMI displays and the
   dock operator writes on the BDN as its time of commencement (`narrative.md`
   §2.2), records the temperature-compensated ATG volume at transaction open, and
   pushes a transaction-open event to the log-server carrying the authorisation
   and the preset volume. On its next cycle, the Modbus background task writes
   the preset (register 0x0001) and enable (register 0x0002). No transaction
   record exists yet; it is written at step 4.
3. Dispenser runs. FMS polls the transaction totaliser
   (registers 0x0005–0x0006) at regular intervals and records intermediate
   readings.
4. Dispenser auto-stops when its transaction totaliser
   (registers 0x0005–0x0006) reaches the preset. FMS reads the final totaliser
   and takes an on-demand ATG
   reading — flow has stopped, so the level is static and the reading is accurate
   for the ±1% cross-check (narrative.md §5). Records the transaction metering
   record: start/end totaliser, volume dispensed (totaliser delta ÷ K-factor),
   ATG level at close, timestamps, operator ID, job ID. The job is not yet
   closed: it awaits the BDN entry (step 6).
5. FMS pushes transaction event record to the log-server.
6. Tug engineer signs paper BDN (sim physical action). Dock
   operator enters BDN number into FMS. FMS appends a BDN linkage record
   referencing the transaction (append-only) and closes the job; pushes the
   event to the log-server.

### 4.3 Fill transaction lifecycle (road tanker → tank)

1. Road tanker arrives. If an ATG reading is available, the dock operator
   checks the level on the HMI to confirm sufficient space in the tank for
   the incoming delivery, then submits a fill job (counterparty, grade,
   volume). Job creation follows §4.1 steps 2–4; an unavailable ATG reading
   does not block the fill and the FMS records a null opening volume.
2. Road tanker driver plugs overfill cable into fill cabinet socket (sim
   physical action — hardwired interlock becomes live).
3. Fill proceeds under the road tanker's own pump. FMS monitors ATG
   level continuously.
4. Fill completes (tanker pump stops at tanker meter preset, or overfill
   interlock trips at the configured level, 95% by default:
   `process.overfill_interlock_pct`, `deployment/deployment-config.md` §3).
   Tanker driver unplugs overfill cable.
5. Tanker's onboard meter generates a DeliveryTicket object (sim).
   Driver and operator sign it (sim physical actions).
6. Operator transcribes delivery ticket fields into FMS: ticket number,
   the time the delivery started, delivered volume, temperature, density,
   temperature-corrected volume, supplier, tanker registration. FMS records
   transaction close: job ID, operator ID, transcribed fields, ATG
   level at close (an on-demand ATG reading; the pump has stopped, so the level
   is static), timestamps. The transfer's start is the transcribed time, not
   the creation of the fill job: the FMS does not observe the tanker's pump.
7. FMS pushes transaction event record to the log-server.

### 4.4 Supplier monitoring data flow

The fuel supplier's monitoring platform connects inbound to
`atg-relay-01` on TCP 10001. `atg-relay-01` validates the source IP and
forwards the connection to the ATG console on `vnet-ot`. The supplier
platform issues standard Veeder-Root Tank Level System (TLS) function code requests:

- **i201** (in-tank inventory report) — reads current level, volume, and
  temperature.
- **i205** (in-tank alarm report) — reads active alarm flags. The
  supplier uses alarm state to gate dispatch decisions: no delivery is
  scheduled if a critical alarm (overfill, leak) is active.

The supplier polls on a configurable interval, 15 minutes by default
(`fuel_supplier.poll_interval_s`, `deployment/deployment-config.md` §3). A
compressed scenario divides the poll interval by the compression factor
(`narrative.md` §6, deviations from real-world practice). When
the reported volume falls below the supplier's reorder threshold and no
critical alarm is active, the supplier schedules a road tanker delivery. It
polls at every interval, including while a delivery is pending, and schedules
no further delivery until the pending one has ended (`sim.md` §5.3).
No data flows from the supplier into the dock's systems — the relay
accepts no inbound write commands; the chosen stream proxy passes bytes
bidirectionally but `atg-relay-01` is positioned as read-only by
operational convention and the supplier's polling behaviour. The Tank Level System (TLS)
serial protocol has no authentication; the IP whitelist on
`atg-relay-01` is the sole access control.

### 4.5 Log-server ingestion and replica sync

- **ATG tag history:** FMS polls the ATG console every 30 seconds in operation, and pushes level, temperature, and volume samples to `hist-01`'s time-series store (VictoriaMetrics, via vmagent, over TLS, §2.7). A compressed scenario divides the poll interval by the compression factor (`narrative.md` §6, deviations from real-world practice). Alarm events are written as discrete sample changes on dedicated tags.
- **Transaction and authorisation events:** FMS pushes structured event
  records to `hist-01`'s event store (VictoriaLogs, via vlagent's JSON-line
  ingestion, over TLS — §2.7) at each lifecycle step (open, intermediate, close,
  BDN/ticket link).
- **Login and card-table events:** FMS pushes login attempts (success and
  failure) and RFID card table mutations to `hist-01`'s event store as syslog
  (RFC 5425, via vlagent's TLS syslog listener — §2.7).
- **Replica sync:** `hist-01`'s vmagent and vlagent instances each hold a second
  remote-write target pointed at the corresponding service on `hist-dmz-01`, over TLS
  (§2.7), so every metric or event ingested on `hist-01` is written to both stores as
  part of the same ingest operation — continuous, push-based replication with no
  separate batch or snapshot step. `hist-dmz-01` runs no ingestion agent of its own. The
  reconciliation user queries `hist-dmz-01`'s Grafana for **log records only** —
  transaction records and login/config-change events, both held in the event
  store; no direct access to OT. The metric store is replicated and provisioned
  as a datasource on the replica, but the end-of-period audit does not read it: the
  tank volumes each transaction opened and closed at are carried on the
  transaction events themselves, so every check the audit performs is reachable
  from the event store and the paper artefacts. Full detail:
  `host_specs/hist/hist.md §5`–`§6`.

### 4.6 OT system administration

The systems administrator starts an SSH session on `admin-01` and reaches a named OT
host through `dmz-bastion-01` using OpenSSH `ProxyJump`. These are two separately
authenticated SSH connections: `admin-01` to the bastion, then `admin-01` to the OT
host through the bastion's TCP forwarding. The administrator's private key remains on
`admin-01`; agent forwarding is not required, and the bastion holds no reusable OT
credential.

Only `fms-01`, `hist-01`, and `hmi-01`, the managed hosts, are administered this way. They are
port-managed Linux systems, unlike the sealed ATG console, dispenser, and RFID readers.
Each uses a local systems-administration account authenticated by SSH key, with `sudo`
for maintenance that requires elevated privileges.
That account, and the matching account on the bastion, takes the systems administrator's username (`identities.systems_administrator.username`, `deployment/deployment-config.md` §3), because `admin-01`'s client configuration and each host's build must agree on it.
The account's password is locked, and its `sudoers` rule carries `NOPASSWD`, so the SSH key held on `admin-01` is the only credential between the systems administrator and root on these hosts.
Hosts administered by key commonly run `sudo` this way; the default user of Ubuntu's cloud images is one example.
The choice departs from the Center for Internet Security (CIS) Linux benchmarks, which recommend a password for privilege escalation.
Firewall policy admits SSH only from
`admin-01` to `dmz-bastion-01`, and from `dmz-bastion-01` to those three hosts. There is
no general route for IT hosts to initiate connections to OT.

**SSH server settings.** The four SSH servers on the administration path are configured as below. Every other setting keeps the distribution's default.

| Setting | `admin-01` | `dmz-bastion-01` | `fms-01`, `hist-01`, `hmi-01` |
|---|---|---|---|
| Authentication | directory password | SSH key only | SSH key only |
| Root login | refused | refused | refused |
| Accounts admitted | the systems administrator | the administration account | the administration account |
| Authorised keys file | none (password sign-in) | `/etc/ssh/authorized_keys/%u`, owned by root | `/etc/ssh/authorized_keys/%u`, owned by root |
| Interactive shell | yes | none (`/usr/sbin/nologin`) | yes |
| TCP forwarding | none | local only (`AllowTcpForwarding local`), to port 22 on `fms-01`, `hist-01` and `hmi-01` (`PermitOpen`) | none |
| Agent and X11 forwarding | none | none | none |

Sign-in to the bastion and the managed hosts is by key only, and every server refuses root login. `admin-01` accepts the directory password, because that is the systems administrator's workstation credential (`narrative.md` §4.6). The bastion account's shell is `/usr/sbin/nologin`, so the bastion only relays connections, and its forwarding admits only the destinations that `ProxyJump` needs.
The authorised keys sit under `/etc`, owned by root, so only root can change them, and the file integrity monitoring of `host_specs/siem/siem.md` §2.1 reports any change.

**Names and keys.** `admin-01` resolves `dmz-bastion-01`. With `ProxyJump`, the bastion opens each onward connection by name, so it resolves `fms-01`, `hist-01` and `hmi-01`, and `PermitOpen` lists those names. `admin-01` holds the host keys of the bastion and the managed hosts in a system-wide `known_hosts`, and its client configuration supplies the jump and the account name. Key and host-key handling is in `deployment/deployment-config.md` §4 and §6.

---

## 5. Identity & authentication<a id='identity-authentication'></a>

### 5.1 Directories

- **IT directory** — authoritative for dock operator and local admin accounts.
  Runs on `corp-it` in the IT zone; OT hosts authenticate via an LDAP simple
  bind over TLS (LDAPS), over the two-hop path vnet-ot → `fw-ot-dmz` →
  `fw-dmz-it` → `corp-it`, addressed by hostname rather than IP so that the
  certificate's hostname SAN validates. See §2.7 for the chosen open-source
  product.
- **Log-server replica** — the reconciliation user authenticates directly
  against the log-server's own built-in user store on `hist-dmz-01`; no
  separate IT identity provider.
- **Reconciliation workstation** — the reconciliation user signs in to
  `recon-01` against the directory on `corp-it`, by the same directory the dock
  operator's HMI credential is validated against. This is a separate credential
  from the log-server one above, with a separate password: the workstation and
  the replica are different systems, and compromising one must not yield the
  other. The workstation holds no verifier — no local account, no shadow entry —
  so a credential cannot be taken from it (`host_specs/recon/recon.md §7.1`).
  The bind is over LDAPS, as the FMS's is, so the password is encrypted in
  transit across the IT zone.
- **Administration workstation** — the systems administrator signs in to
  `admin-01` against the directory on `corp-it`. SSH authentication to the bastion and
  managed OT hosts uses public keys held separately on the workstation (§4.6).

### 5.2 Per-role authentication

| Role | Factors | Validated by |
|---|---|---|
| Dock operator (HMI login) | User/pass (IT directory on `corp-it`) + RFID card number (FMS card table) | FMS (two independent lookups) |
| Reconciliation user (workstation login) | User/pass | Directory on `corp-it` (`recon-01` holds no account) |
| Reconciliation user (audit) | User/pass | Log-server replica (built-in user store) |
| Systems administrator (workstation login) | User/pass | Directory on `corp-it` |
| Systems administrator (bastion and OT hosts) | SSH public key | Local OpenSSH account on each managed host; private key retained on `admin-01` |
| Systems administrator (elevated privileges on OT hosts) | None beyond the SSH key | `sudo` with `NOPASSWD` on each managed host (§4.6) |

### 5.3 RFID card management

The FMS maintains an internal card table that maps each RFID card number to its holder: an operator's card to their IT 
directory username, and a vessel tag to the vessel's International Maritime Organization (IMO) number. 
The card table is also the digital world's record of the port's tugs. A vessel is known to the FMS through the enrolled 
tag that names it, and a dispense job names its vessel the same way, so the vessel RFID gate (§4.2) accepts only a 
tag enrolled to the job's vessel. There is no separate vessel register. Card enrolment and revocation are FMS admin 
operations. On a fresh deployment, the FMS also enrols a configured bootstrap set of cards at startup (the operators' 
cards and the tugs' vessel tags), skipping any already present. 

### 5.4 Session handling

- FMS sessions: HTTPS with server-side session tokens; in operation, sessions expire 30 minutes after login. A compressed scenario divides the session lifetime by the compression factor (`host_specs/fms.md` §4, authentication and session management).
- Log-server replica: read-only HTTPS. A browser sign-in gets a session from the log server's own session layer; the end-of-period audit does not. The audit workflow authenticates each request from the command line and keeps no session, so no credential-bearing session artefact accumulates on the workstation (`host_specs/recon/recon.md` §7.2).

---

## 6. Logging & retention<a id='logging-retention'></a>

### 6.1 What lands where

| Data | Store | Retention |
|---|---|---|
| ATG level, temperature, volume (time-series) | Log-server `hist-01` (+ replica) | 3 years (MARPOL) |
| ATG alarm events | Log-server `hist-01` (+ replica) | 3 years |
| Transaction event records (open, close, BDN/ticket link) | Log-server `hist-01` (+ replica) | 3 years |
| Job records | FMS RDBMS (append-only) | 3 years |
| User sessions and login events | FMS RDBMS (append-only) **and** log-server `hist-01` (+ replica, as syslog) | 3 years |
| FMS card table changes | FMS RDBMS (append-only) **and** log-server `hist-01` (+ replica, as syslog) | 3 years |

Transaction event records, login events, and card table changes are all written to
the log-server (not only to the FMS RDBMS) so the reconciliation user can query them
via the DMZ replica, consistent with the log-server's role in §2.2. The FMS RDBMS
remains the primary, authoritative record for job/session/card state; the log-server
copy is a second, append-only event representation for replica-based audit. Full
detail, including the syslog event schema: `host_specs/hist/hist.md §4`.

The two are written independently and are not held in a shared transaction. The FMS
writes its own row first and forwards the log-server copy afterwards, retrying with
increasing delay; no operational path waits on that delivery, and a copy that cannot
be delivered is eventually dropped (`host_specs/fms.md §5.4`). The FMS RDBMS record is
therefore enforced and the log-server copy is best-effort — complete in normal
operation, but not guaranteed under a log-server outage. This is narrative §5's
resolution of "Records are complete and retained" against "The asset is available
during operating hours" in favour of availability.

### 6.2 Append-only guarantee (FMS RDBMS)

The FMS transaction log, job log, and session log tables are protected
by database-level triggers that block UPDATE and DELETE. Corrections are
new rows referencing the original record ID; there are no in-place edits
(narrative §5). The trigger is part of the schema migration and is
verified in integration tests.

### 6.3 Replica integrity signal

Divergence between `hist-01` and `hist-dmz-01` is itself a detection
signal (narrative §4.5). The reconciliation user's end-of-period audit implicitly
tests replica integrity by cross-checking replica records against paper
documents.

Divergence between the FMS RDBMS and the log-server is a weaker signal of the same
kind, because §6.1 gives it a benign cause: a log-server outage long enough to exhaust
the FMS's retries drops the copy, leaving a transaction the FMS holds and the
log-server does not. That is the same observable an attacker produces by suppressing
an event. Separating the two requires evidence beyond the divergence itself — whether
the gap spans a contiguous period affecting every event class, which an outage
produces and selective suppression does not.

### 6.4 Security monitoring data

Alerts, and the raw records they derive from, are held on `siem-01` and
retained indefinitely. Full specification: `host_specs/siem/siem.md §6`–`§7`.

---

## 7. Product/implementation choices<a id='product-choices'></a>

Consolidated from §2 and §8. One row per host. All chosen software must
be open-source; named products are example open-source candidates and
may be substituted for any open-source equivalent that preserves the
interfaces in §3 and §4.

| Host | Zone | OS class | Component class | Example open-source choice |
|---|---|---|---|---|
| `fms-01` | OT | Open-source Linux | Custom open-source service + open-source RDBMS | No specific product committed; compiled-binary deployment preferred; see §8 decision 1 |
| `hist-01` | OT | Open-source Linux | Time-series store + log store + visualisation | VictoriaMetrics + VictoriaLogs + vmagent + vlagent + Grafana; see §8 decision 2 and `host_specs/hist/hist.md` |
| `hmi-01` | OT | Open-source Linux | Browser-based bespoke web UI | Modern browser over FMS / log-server read APIs; see §8 decision 3 |
| `hist-dmz-01` | DMZ | Open-source Linux | Log-server in replica mode | Same product as `hist-01`, without vmagent/vlagent (pure receiver) |
| `recon-01` | IT | Open-source Linux | Workstation: desktop + remote desktop service + browser | Xfce + xrdp + Firefox Extended Support Release + sssd against `corp-it`; see `host_specs/recon/recon.md` |
| `corp-it` | IT | Open-source Linux | LDAP directory + NTP daemon | OpenLDAP (slapd) + chrony; FreeIPA planned; see `host_specs/corp-it/corp-it.md` |
| `siem-01` | IT | Open-source Linux | Log collection, correlation and alerting | Wazuh manager, with Wazuh agents on every monitored host; see `host_specs/siem/siem.md` |
| `admin-01` | IT | Open-source Linux | Administration workstation | OpenSSH client and standard Linux administration tools; see §2.9 |
| `atg-relay-01` | DMZ | Open-source Linux | Open-source TCP stream proxy | HAProxy (stream mode) or NGINX (stream module); see §8 decision 6 |
| `dmz-bastion-01` | DMZ | Open-source Linux | SSH bastion | OpenSSH server; see §2.10 and §8 decision 8 |
| `fw-ot-dmz` | OT/DMZ | — | Open-source stateful firewall + boundary packet inspection | pfSense CE or OPNsense (`full` profile, as a VM); generic Linux/nftables stand-in on `ci-minimal` — see §8 decision 5 and §9 deviation 9. Suricata for packet inspection (`host_specs/siem/siem.md §5`) |
| `fw-dmz-it` | DMZ/IT | — | Open-source stateful firewall + boundary packet inspection | Same as `fw-ot-dmz` |
| `fw-ext-dmz` | External/DMZ | — | Open-source stateful firewall + boundary packet inspection | Same as `fw-ot-dmz` |

---

## 8. Decisions (resolved)<a id='decisions'></a>

Where problems highlighted gaps in narrative.md, these have been addressed upstream.

1. **FMS build strategy.**
   - *Problem:* no realistic FMS product (Gilbarco Passport, OPW Phoenix/FSC3000) is available under an open licence.
   - *Options:* procure a commercial appliance `[paid]` — ruled out by the open-source rule; build a custom open-source service whose interfaces and data model mirror a commercial product.
   - *Decision:* custom open-source service backed by an open-source RDBMS. Realism is in the protocols and data model, not the vendor product. The web framework, database product, and implementation language are not prescribed. A compiled-binary deployment is preferred so that no interpreted-language runtime (Python, Node.js) is introduced on OT hosts — this matches the closed-binary deployment posture of commercial FMS products.

2. **Log-server product.**
   - *Problem:* need a port-realistic time-series store, structured event/audit log, and visualisation tool under an open licence.
   - *Options:* OSIsoft/AVEVA PI System or GE Proficy `[paid]` — ruled out; Ignition by Inductive Automation `[trial]` — ruled out (trial mode is not an open licence); open-source time-series store + open-source log store + open-source visualisation tool `[free]`.
   - *Decision:* **VictoriaMetrics** (time-series store) + **VictoriaLogs** (structured event/audit log) + **Grafana** (visualisation), with **vmagent** and **vlagent** as the ingestion and cross-instance replication front-end. VictoriaMetrics alone is a poor fit for the rich, mostly free-text/UUID transaction and audit records the log-server's role requires (§2.2, §6.1) — forcing them into metric labels would create unbounded cardinality — so VictoriaLogs is paired alongside it for that data class. Full specification: `host_specs/hist/hist.md`. Atypical for port deployments; deviation recorded in §9.

3. **HMI product.**
   - *Problem:* need an operator HMI that can present ATG state, FMS transactions, and alarms.
   - *Options:* Ignition Perspective `[trial]` or AVEVA InTouch `[paid]` — both ruled out by the open-source rule; bespoke browser-based web UI over FMS and log-server read APIs `[free]`.
   - *Decision:* bespoke browser-based web UI. The HMI carries no business logic beyond presentation; authorisation and audit live in the FMS. Deviation from commercial-HMI practice recorded in §9.

4. **Workstation OS.**
   - *Problem:* OT workstations need an OS; Windows 10 LTSC is the real-world norm but is `[paid]`.
   - *Options:* Windows 10 LTSC `[paid]` — ruled out; open-source Linux distribution + modern browser `[free]`.
   - *Decision:* open-source Linux distribution + modern browser. The FMS and HMI are browser-based, so no Windows-specific dependency exists. Deviation recorded in §9.

5. **Firewall product.**
   - *Problem:* need zone firewalls for the OT<->DMZ, DMZ<->IT, and External<->DMZ boundaries.
   - *Options:* industrial firewalls (Fortinet FortiGate Rugged, Cisco ISA-3000) `[paid]` — ruled out; open-source stateful firewall (pfSense CE, OPNsense) `[free]`.
   - *Decision:* an open-source stateful firewall product, three instances, one per boundary. pfSense CE and OPNsense are both acceptable candidates. Deviation from industrial-firewall practice recorded in §9. Both candidates are FreeBSD-based and have no container form, so the `ci-minimal` profile substitutes a generic Linux/nftables firewall instead; this container-portability deviation is recorded separately in §9 deviation 9.

6. **ATG data relay product.**
   - *Problem:* need a TCP proxy in the DMZ to accept inbound supplier connections on TCP 10001 and forward them to the ATG console, with source IP whitelisting as the sole access control.
   - *Options:* open-source TCP stream proxy (HAProxy in stream mode, NGINX with the stream module) `[free]`; custom proxy `[free]`.
   - *Decision:* an open-source TCP stream proxy. HAProxy and NGINX are both acceptable candidates; the IP whitelist is expressed as a native ACL. Adds an authentic infrastructure component to the DMZ without custom code.

7. **Internal certificate authority (CA) for TLS trust.**
   - *Problem:* TLS-terminating components must not self-sign their own
     certificate, otherwise peers are required to accept these with `verify
     none` / `danger_accept_invalid_certs` rather than validating trust
     against a common root. Real-world fidelity requires a proper trust chain terminating in an internal root certificate recognised by all peers.
   - *Options:* a live internal CA service (for example step-ca, HashiCorp
     Vault PKI) issuing certificates over ACME at runtime `[free]` —
     would introduce a new host and a running service the narrative does not
     describe; an offline, build-time-only CA built from existing OpenSSL
     tooling, reusing the input-hash caching pattern already used for image
     builds, with no new host or running service `[free]`.
   - *Decision:* offline, build-time-only CA (§2.7). RSA-2048 keys; root
     certificate validity 10 years, leaf certificate validity 1 year. Leaf certificates carry hostname DNS
     SANs; components validate peers by hostname. The root
     private key never leaves the build host. FMS and HMI both treat a missing or unreadable certificate at the
     configured path as a startup failure, as do vmagent, vlagent, VictoriaMetrics, and
     VictoriaLogs on `hist-01`/`hist-dmz-01` (`host_specs/hist/hist.md §9`), and
     `slapd` on `corp-it` (`host_specs/corp-it/corp-it.md §5`) — none of
     these components has a self-signed fallback.
   - *Sub-problem: deterministic key material.* The root and leaf private
     keys must be reproducible from `world.yaml` alone, on the same grounds
     as every other secret in the pipeline (`deployment-config.md §6`): an
     attacker with `world.yaml` can already regenerate every other
     credential, and the render pipeline's "identical inputs, byte-identical
     artefacts" guarantee (`deployment.md §9.1`) should hold here too. This
     is unusually difficult for RSA specifically — OpenSSL and Python's
     `cryptography` library both deliberately refuse to accept a caller-supplied
     random source for key generation, precisely to prevent the class of
     predictable-key vulnerability a seedable RSA implementation reintroduces.
   - *Options:* a custom RSA key-generation implementation `[free]` — too
     much bespoke cryptographic code for a testbed to carry safely; **PyCryptodome**'s
     `RSA.generate(bits, randfunc=...)`, which accepts exactly this kind of
     substitution (a documented, intended use of that parameter) `[free]`.
   - *Decision:* PyCryptodome generates the deterministic keys in `render.py`
     (a per-key seed via HMAC-SHA256 of `crypto.password_salt_seed`, the same
     construction already used for every other seeded secret, expanded into a
     counter-mode SHA-256 byte stream for `randfunc`); `openssl` continues to
     own all X.509 structure — self-signing the root and signing each leaf CSR
     — consuming those keys rather than generating its own. Determinism
     extends to the certificate bytes, not only the keys: `openssl ca` (not
     the `req -x509` / `x509 -req` shortcuts, neither of which accept an
     explicit validity window) signs with a pinned `notBefore`/`notAfter` and
     a deterministic serial, both derived from `world.yaml` the same way the
     keys are. The `notBefore` parameter in `world.yaml` is the valid-from
     date for all certificates - mapped as `crypto.ca_not_before` — a
     required field which must be updated at least every year, given the 1-year
     leaf certificate validity. A stale value would generate expired leaf
     certificates; `render.py` refuses to render at
     all if `ca_not_before` plus the leaf validity period has already
     elapsed (`deployment-config.md §5`).
   - *Note: the sim-host dock-operator backend's Server Name Indicator (`sni`) directive.* Without an
     explicit `sni str(hmi-01)` on
     the backend `server` line, this HAProxy build's `verify required` +
     `verifyhost hmi-01` silently rejects the connection (a 503 with no
     application-level error) against a certificate that is otherwise
     valid — the same chain, the same leaf, verified successfully by
     both `openssl verify -x509_strict -purpose sslserver` and a live TLS
     handshake performed independently of HAProxy. `hmi-01` presents only one
     certificate regardless of what SNI it receives, so this is not a
     certificate-selection requirement; it is specific to how this HAProxy
     build resolves the peer name `verifyhost` checks against. sim-network.md
     §5, §6 and `host_specs/hmi.md` §9.3 record the directive; the static
     build-time checks (`openssl verify -x509_strict`) could not have caught
     its absence, since the certificate itself was never at fault — only a
     live handshake through the real component chain surfaced it, which is
     why that reproduction exists as its own test tier.

8. **Systems-administration path.**
   - *Problem:* the systems administrator needs SSH access from the IT network to the
     port-managed Linux systems on OT.
   - *Options:* permit direct SSH from IT to OT; add SSH to the supplier-facing ATG
     relay; use a separate SSH bastion in the DMZ.
   - *Real-world basis:* [Configuring and Managing Remote Access for Industrial Control Systems](https://www.cisa.gov/sites/default/files/2023-01/RP_Managing_Remote_Access_S508NC.pdf), guidance from United States and United Kingdom government agencies that the Cybersecurity and Infrastructure Security Agency (CISA) hosts, recommends dedicated hardware and a separate network segment for remote access servers (pp. 36, 38 and 39).
     [National Institute of Standards and Technology (NIST) Special Publication 1800-10](https://doi.org/10.6028/NIST.SP.1800-10) demonstrates a bastion providing remote desktop and SSH access to control-system hosts.
     Both support a dedicated host on the administration path.
   - *Decision:* `admin-01` is the systems administrator's workstation on IT and
     `dmz-bastion-01` is a small OpenSSH bastion in the DMZ. OpenSSH `ProxyJump`
     carries SSH from the workstation through the bastion to `fms-01`, `hist-01`, or
     `hmi-01`; the private key remains on the workstation. `atg-relay-01` remains a
     separate supplier-facing service. This models an ordinary small-site administration
     path without introducing a privileged-access management platform.

## 9. Deviations from real-world practice<a id='deviations'></a>

1. **OT workstation OS: open-source Linux instead of Windows.**
   - *Real world:* OT operator workstations at ports typically run
     Windows 10/11 LTSC or Windows Server (Thin Client). Most commercial
     FMS and SCADA HMI products ship as Windows applications or require
     Internet Explorer / Edge compatibility modes.
   - *Testbed concession:* open-source Linux + a modern browser, because
     the open-source rule excludes Windows licences and the FMS and HMI
     we build are browser-based.
   - *Impact:* Windows-specific attack paths (NTLM relay, WMI lateral
     movement, PrintSpooler) are not present on these hosts.

2. **FMS: custom open-source service instead of a commercial product.**
   - *Real world:* ports buy Gilbarco Veeder-Root Passport, OPW
     Phoenix/FSC3000, Triscan, or similar. These are closed,
     Weights-and-Measures-certified appliances with their own internal
     databases and proprietary protocols.
   - *Testbed concession:* custom open-source build, because no realistic
     FMS product is available under an open licence.
   - *Impact:* proprietary FMS vulnerabilities (known CVEs in Passport,
     for example) are not present; our FMS may have different
     vulnerability classes.

3. **Log-server: open-source time-series/log stack instead of a SCADA historian.**
   - *Real world:* production historians at ports run OSIsoft/AVEVA PI
     System, GE Proficy, or Ignition. These are integrated tag historians
     with native SCADA semantics (tag browsers, alarm journals, transaction
     groups).
   - *Testbed concession:* VictoriaMetrics and VictoriaLogs paired with
     Grafana (`host_specs/hist/hist.md`). This exercises the same network protocols and
     event flows but lacks the SCADA-product look-and-feel and some
     product-specific features (e.g. alarm acknowledgement workflows
     built into the historian).
   - *Impact:* historian-product-specific attack paths (e.g. CVEs in
     PI Vision, Ignition Gateway scripting) are not present; the
     open-source stack has its own vulnerability surface.

4. **Identity: open-source LDAP directory instead of Active Directory.**
   - *Real world:* ports of any size on Windows infrastructure use
     Microsoft Active Directory (on-premise) or Azure AD / Entra ID, with
     Kerberos as the primary authentication protocol. Open-source
     directories (OpenLDAP, FreeIPA, Samba AD-DC) are more common in
     academic and Linux environments than in port OT.
   - *Testbed concession:* open source, avoiding a Windows Server licence.
     Every directory consumer (`host_specs/fms.md §3`–§4, `host_specs/recon/recon.md §7.1` and §2.9) authenticates by an LDAP simple bind over TLS; no Kerberos service is deployed and no firewall rule admits one.
   - *Impact:* AD-specific attack paths (Pass-the-Hash, DCSync, Golden
     Ticket) and Kerberos ticket attacks (Kerberoasting, AS-REP roasting)
     are not present; credential attacks against the directory are limited
     to the bind password itself.

5. **Firewall: open-source stateful firewall instead of an industrial firewall.**
   - *Real world:* larger ports use Fortinet FortiGate Rugged, Cisco
     ISA-3000, or Tofino/Hirschmann for OT zone firewalls. Open-source
     stateful firewalls (pfSense CE, OPNsense) are used at small sites
     but are unusual at a modernised port with a full DMZ architecture.
   - *Testbed concession:* an open-source stateful firewall product is
     production-grade enough for the segmentation model and is required
     by the open-source rule.
   - *Impact:* vendor-specific industrial-firewall features and their
     management interfaces are absent. Signature-based detection and
     protocol inspection at the boundaries are provided separately by
     Suricata (`host_specs/siem/siem.md §5`).

6. **FMS<->dispenser protocol: Modbus TCP instead of native RS-485 proprietary.**
   - *Real world:* the FMS PC and dispenser are connected by RS-485
     serial and speak a vendor-proprietary protocol natively — OPW
     Petro-Net for OPW equipment, Gilbarco DART for Gilbarco equipment.
     Pulse output from the flow meter runs as a discrete hardware wire
     back to the FMS in parallel with the dispenser's own preset counter.
   - *Testbed concession:* Petro-Net and DART are unpublished. We use
     Modbus TCP — a documented real-world integration pattern used by
     gateway converters in the industry — with the FMS writing preset /
     enable registers and the dispenser emulator exposing totaliser and
     status registers.
   - *Impact:* RS-485 bus-level attack paths and Petro-Net/DART framing
     exploits are not present. See [`docs/testbed.md`](../docs/testbed.md),
     "Design problems", for the full rationale.

7. **Infrastructure: virtualised hosts instead of physical hardware.**
   - *Real world:* OT equipment runs on dedicated physical hosts;
     virtualisation of OT workloads is still uncommon at port level.
   - *Testbed concession:* every digital-world host is a virtualised
     execution environment — a virtual machine, a container, or a mix —
     managed by an open-source orchestrator. The specific orchestrator
     is not prescribed (see §10). This gives a reproducible, snapshottable
     environment on a single physical host.
   - *Impact:* hypervisor- and container-runtime-level attacks are not in
     scope (they are not part of the port threat model anyway); some
     hardware-dependent timing behaviour will differ. Containers in
     particular share the host kernel, which removes some kernel-level
     isolation that a real OT deployment would have between hosts.

8. **Operator login RFID reader modelled as a networked OT reader.**
   - *Real world:* a workstation login reader is normally a USB
     keyboard-wedge or USB-CCID device attached directly to the HMI PC,
     with no network presence.
   - *Testbed concession:* the operator reader is modelled like the vessel
     reader — a networked RFID reader exposing the card number as a TCP
     service on `vnet-ot` at its own OT address — so every sim<->digital
     signal crosses the documented boundary uniformly. The vessel/dispenser
     reader is realistically a networked reader, so no deviation applies to
     it.
   - *Impact:* the operator reader appears as a distinct host on the OT
     network; a scan would find it where a real USB-wedge reader would be
     invisible. Scenarios that fingerprint the login reader's attachment
     cannot be reproduced.

9. **Firewall backend on `ci-minimal`: generic Linux/nftables instead of pfSense CE/OPNsense.**
   - *Real world / testbed target:* §8 decision 5 specifies pfSense CE or
     OPNsense for the three zone firewalls.
   - *Testbed concession:* both products are FreeBSD-based and have no
     container form — Incus containers share the host's Linux kernel, so
     neither can run as a container under any configuration. The
     `ci-minimal` profile (`general-deployment.md` §1.1) runs every node as
     a container specifically so the release profile is deployable on
     hosts without nested virtualisation, so the fw nodes there run a
     generic open-source Linux firewall (nftables) enforcing the same
     `firewall_rules` policy (`deployment.md` §4) rather than the specified
     product. The `full` profile, where fw nodes run as virtual machines,
     is not subject to this constraint and should deploy pfSense CE or
     OPNsense as specified; see `deployment.md` §3 for current build-script
     status.
   - *Impact:* firewall-product-specific attack surface (the pfSense/OPNsense
     web GUI, package ecosystem, and their CVEs) is present only under
     `full`; a `ci-minimal` deployment exercises the segmentation policy
     and stateful-forwarding behaviour but not the product itself.

10. **Security monitoring: Wazuh instead of a commercial SIEM.**
    - *Real world:* ports of this scale run Splunk, Microsoft Sentinel,
      IBM QRadar, or HPE ArcSight.
    - *Testbed concession:* Wazuh, because no commercial SIEM is available
      under an open licence.
    - *Impact:* product-specific detection content and attack surface are not
      present; Wazuh has its own vulnerability surface, and its rule language
      and decoder set differ from any commercial product's.

11. **No passive OT network monitoring appliance.**
    - *Real world:* a modernised port deploys a passive OT monitoring
      appliance (Nozomi Networks, Claroty, Dragos) fed by a mirror port on the
      OT switch, giving protocol-level visibility of Modbus and tank-gauge
      traffic within the OT zone.
    - *Testbed concession:* no such appliance. Network visibility inside a
      zone is limited to the connection events each host reports about itself
      (`host_specs/siem/siem.md §4.1`); packet contents are examined only where
      traffic crosses a zone boundary.
    - *Impact:* an attack confined to traffic within the OT zone that does not
      alter host-visible behaviour is not detectable, and a compromised host
      can suppress the record of its own intra-zone activity. The sensor
      extension in `host_specs/siem/siem.md §4.5` would close this.

12. **Certificate authority: offline build-time OpenSSL CA instead of ADCS
    or a vendor PKI.**
    - *Real world:* ports running Windows-integrated identity typically
      issue internal TLS certificates from Active Directory Certificate
      Services, tied to the AD deployment; ports on vendor SCADA/FMS
      platforms may instead receive certificates through the vendor's own
      commissioning tooling.
    - *Testbed concession:* a standalone, offline, build-time-only CA (§2.7,
      §8 decision 7), consistent with the open-source LDAP
      substitution already recorded in deviation 4. No live CA service,
      ACME, or OCSP endpoint is deployed.
    - *Impact:* certificate issuance is fixed at build time; there is no
      live enrolment, renewal, or revocation workflow to attack or defend,
      so CA-service-specific attack paths (for example enrolment-service
      escalation against a live ADCS deployment) are not present.

13. **Compressed scenarios: shortened intervals in the digital world's configuration.**
    - *Real world:* a port's systems run at intervals set for real time. In operation, the FMS polls the ATG console every 30 seconds and the dispenser every 5 seconds, and an FMS session lasts 30 minutes. The HMI's challenge timeout is 30 seconds. The security monitoring's agent on each monitored host checks the host's files every 12 hours, reports its inventory every hour and reports its listening-port inventory every 360 seconds.
    - *Testbed concession:* a compressed scenario shortens every interval and duration in the port's operation by one factor, the compression factor (`narrative.md` §6, deviations from real-world practice, item 3). In the digital world, the intervals and durations in the port's operation are the schedules on which a system samples or bounds the port's activity. `render.py` divides by the compression factor the FMS's two poll intervals and session lifetime, the HMI's challenge timeout and the three schedules of the security monitoring's agents (`deployment/deployment-config.md` §4, projection targets). Retries, back-offs, input and output time limits, and processing and network delays belong to the software and networks, and keep their values.
    - *Impact:* the digital world's configured intervals differ from those of a real deployment. Input and output time limits keep their values, so a system takes longer to detect a fault, relative to the port's shortened activity. The FMS, for example, counts a poll cycle of the ATG console as failed when its network phase exceeds 15 seconds, at any compression factor (`host_specs/fms.md` §5.1, ATG poller).

14. **FMS: a running dispense cannot be cancelled.**
    - *Real world:* a fuel management system lets an operator stop a running
      fuelling and stores the partial transaction (the International Forecourt
      Standards Forum's (IFSF) Standard Forecourt Protocol, Part 3-01 §2.1.8,
      `TERMINATE_FP`; Part 3-27 §14.24, `TerminateFuelling`).
    - *Testbed concession:* the FMS refuses to cancel a dispense once fuel has
      moved (`fms.md` §7.4, jobs). The dock operator stops a running dispense
      only with the emergency shutdown (ESD) button, and the narrative
      describes no other stop.
    - *Impact:* no partial transfer is ever recorded, and no attack can stop a
      running dispense through the FMS.

---

## 10. Testbed orchestration<a id='testbed-orchestration'></a>

The digital world runs on a single physical host under an open-source
orchestrator. The orchestrator manages the digital-world hosts as virtual
machines, containers, or a mix — the chosen substrate is an
implementation decision, not a constraint imposed by this document. The
choice must satisfy the following requirements:

- **Network isolation.** Four isolated virtual networks must be presented
  to the orchestrated hosts, mapping directly to the zone architecture:
  `vnet-ot`, `vnet-dmz`, `vnet-it`, `vnet-ext`. Open-source firewall
  instances bridge the networks per §1.2.
- **Multi-interface hosts.** The sim host attaches to three virtual
  networks: five interfaces on `vnet-ot` (ATG console, dispenser, vessel
  RFID reader, operator RFID reader, and the dock operator actor, at
  distinct addresses), one on `vnet-ext` (fuel supplier actor), and two on
  `vnet-it` (reconciliation user and systems administrator actors). See sim.md §5.3 (fuel supplier) and [`docs/testbed.md`](../docs/testbed.md), "Sim appliance network presence".
- **Reproducibility.** The orchestrator must support deterministic
  startup of the full topology and snapshotting of host state.
  `competition/episodes.md` §14 (reproducibility) states what the competition's seeds reproduce, the random draws, and what varies between two runs of one seed, the times at which events happen.
- **Open licence.** The orchestrator itself must be open-source.
