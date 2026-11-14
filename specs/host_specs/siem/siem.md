# Security monitoring

## 1  Purpose and scope

Centralised security monitoring across the port's digital systems is part of the
modernised-port operating context (`narrative.md §4.7`, `§6`); this document specifies
that system.

A Security Information and Event Management (SIEM) system performs four
functions: it **collects** records  from every host to one place, **interprets** each
record by converting unstructured text into named fields, **evaluates** the interpreted
records against a set of detection rules, and **retains** the result. Typically they collect service starts and failures, authentication attempts, configuration changes, connections refused, etc.

Twelve hosts are monitored: `fms-01`, `hmi-01`, `hist-01`, `hist-dmz-01`,
`atg-relay-01`, `dmz-bastion-01`, `corp-it`, `recon-01`, `admin-01`, and the three
firewall nodes. `sim-host` is not monitored.

The system is deployed on the digital-world networks as any other service is. Agents (deployed on each host)
reach the manager (a `siem-01` host in IT) over the zone networks and through the zone firewalls; nothing passes
through the orchestration host.

Rules that reason over a time window spanning several hosts **depend on those hosts
agreeing on the time**. Every monitored host is a client of the site time source and is
synchronised to within one second (`digital.md §1.3`).

---

## 2  Components

Two components are deployed. Both are Wazuh, licensed under version 2 of the GNU General
Public Licence. Suricata, used at the zone boundaries (§5), carries the same licence.

### 2.1  Agent — on every monitored host

Collects and forwards. Performs no analysis.

- **Log collection** — reads the systemd journal and named log files, forwarding each new
  line (§3).
- **File integrity monitoring** — records a cryptographic hash of every file under the
  product's default configured paths (`/etc`, `/usr/bin`, `/usr/sbin`, `/bin`, `/sbin`,
  `/boot`), with its ownership and permissions, at start and every 12 hours, and reports
  any file added, deleted, or changed in content, ownership or permissions.
- **Inventory** — reports installed packages, listening ports, running processes and
  network interfaces, at start and every hour. No other category — hardware, operating
  system, users, groups, services — is reported.
- **Listening-port inventory**: reports the ports the host listens on, and the process behind each, every 360 seconds (§4.3, listening-port inventory).

The intervals of file integrity monitoring (12 hours), the inventory (1 hour) and the listening-port inventory (360 seconds) are values in operation (`sim.md` §2.2, compressed scenarios). A compressed scenario divides each by the compression factor (`narrative.md` §6, deviations from real-world practice). `world.yaml` holds the three values in operation as `intervals.file_integrity_interval_h`, `intervals.inventory_interval_h` and `intervals.listening_port_interval_s` (§8.5, `world.yaml`).

Neither rootcheck (the product's search for rootkit signatures and anomalous system files)
nor configuration assessment against a hardening benchmark runs. The manager applies
exactly the same monitoring to `siem-01` itself.

Forwards to the manager on port 1514 of the Transmission Control Protocol (TCP). The channel is encrypted with a pre-shared key, unique per agent, written into the host's image at build time (§8.5).

### 2.2  Manager — on `siem-01`

Receives every agent's records and performs all detection, in two stages.

**Decoding.** A decoder recognises the format of an incoming line and extracts named
fields from it.

**Rule evaluation.** Each decoded record is tested against an ordered rule set. A rule
that matches produces an alert carrying a severity from 0 to 15 and, where applicable,
the identifier of the corresponding technique in the MITRE ATT&CK catalogue of adversary
behaviour. The catalogue comprises both the Enterprise matrix and the matrix for
Industrial Control Systems (ICS); the manager ships only the Enterprise matrix, so the ICS
matrix is imported at build time (§8.4), into the same technique database and directly
queryable there. The manager's own packaged ruleset — the rules and decoders that ship
with the Wazuh installation, before any RavensPort or ICS-matrix rule is added — already
carries ATT&CK Enterprise mappings on many of its rules; the RavensPort boundary rules
extend this coverage with one technique for each RavensPort signature, drawn from the
imported ICS matrix or the Enterprise matrix (§5). A rule may also depend on preceding
records (e.g. five authentication failures from one source address within 120 seconds) which is
how a sequence of individually unremarkable events produces a single alert.

---

## 3  Host log data

**The systemd journal**, which carries unit starts, failures and restarts;
authentication through secure shell and `sudo`; network configuration; the packet-filter
connection records described in §4; and the standard output of every service run under
systemd, including the Fuel Management System and the Human-Machine Interface.

`sudo` records each command it runs in the journal, which the agent collects. A command run without `sudo` appears in no collected log.

**Named log files**, for the services that write their own files rather than using the
journal. HAProxy on `hmi-01` and `atg-relay-01`, and Grafana on `hist-01` and
`hist-dmz-01`, are covered by the journal already (§2.1) and are not listed here: each
writes its own log line through the same path systemd captures, so a file input on
either would duplicate every record collected via the journal.

| Host | File |
|---|---|
| `fms-01` | PostgreSQL, `/var/log/postgresql/` |
| `recon-01` and `admin-01` | the directory client's domain log, `/var/log/sssd/sssd_ravensport.log` |
| the three firewall nodes | Suricata's event file, `/var/log/suricata/eve.json`, carrying the flow records of §4.2 and the inspection alerts of §5 |

PostgreSQL on `fms-01` logs the start and end of every session as well as every failure
(`log_connections` and `log_disconnections`, `fms.md §9.2`); its packaged default logs
failures and errors only. A session's lines name the database user and the database,
which the decoder extracts with the severity and the message. A password failure raises a
RavensPort rule naming the database user and carrying T1110.001 (Password Guessing), as the
manager's packaged secure shell failure rules do. Eight failures for one database user within
120 seconds raise a second rule carrying T1110 (Brute Force), with the threshold, window and
60-second quiet period of the packaged secure shell brute-force rule. The manager's packaged
PostgreSQL rules expect a different log format and do not apply.

---

## 4  Network activity data

Three kinds of network data are collected, from two collection points. None involves
recording packet contents in bulk, and none requires a monitoring appliance or a mirrored
port.

### 4.1  Connection events — from every monitored host

Each monitored host logs every new network connection it makes or accepts. A
packet-filter rule matching connection establishment produces one record, written to a file
the host's agent collects (§8.2).

One record per connection, at the moment it opens:

- the local host's identity and the interface used;
- direction: inbound or outbound;
- source address and port, destination address and port, protocol.

There is no record of content, and no volume or duration, because the record is written
when the connection opens rather than when it closes.

Because every conversation in the environment has at least one monitored host as a
participant, this covers every conversation, in every zone — including those that reach
no firewall: the Fuel Management System to the tank gauge console, to the dispenser and
to the vessel card reader, and the Human-Machine Interface to the operator card reader.

### 4.2  Flow records — from the three firewall nodes

Each firewall node produces a summary record for every connection that crosses its zone boundary, written once the connection has ended and Suricata's flow timeout for it has passed:

- source and destination addresses and ports, protocol;
- start time, end time, duration;
- packets and bytes transferred in each direction;
- how the connection terminated.

Suricata's packaged flow timeouts keep a closed TCP connection, and a TCP connection that the inspection never saw open, for 60 seconds after its last packet. The packaged timeouts keep an established flow of the User Datagram Protocol (UDP), and an exchange of the Internet Control Message Protocol (ICMP), each with traffic both ways, for 300 seconds. A flow record therefore reaches the event file about a minute after a TCP connection closes, and about five minutes after the last packet of such a UDP flow or ICMP exchange. An additional override file can set shorter flow timeouts, as values that the firewall nodes' build-script template reads (`deployment.md` §7.3, additional override file).

These are richer than the connection events, carrying volume and duration, but they exist
only for traffic that crosses a zone boundary. Traffic between two hosts in the same zone
never reaches a firewall node and produces no flow record.

Records are written to Suricata's event file on the node and forwarded by that node's own
agent (§3), in the same way as any other file input.

### 4.3  Listening-port inventory — from every monitored host

Each host periodically reports the ports it is listening on and the process behind each.
The environment's service set is fixed, so a listening port appearing where none was
before is a strong indicator on its own.

### 4.4  Limitations

- **Packet contents are examined only at zone boundaries** (§5). No conversation within a
  zone is inspected, so dispenser register values and card numbers are never recorded.
- **Secure shell sessions are opaque after the key exchange.** The check of §5 confirms that traffic on 22 is secure shell. The commands in a session, and any connection forwarded through it, are encrypted and never inspected.
- **No volume or duration for conversations within a zone**, only the fact and time of
  each connection.
- **`sim-host` reports nothing about itself.** Every conversation it participates in is
  recorded by its monitored counterpart, so no conversation is lost, but only one side of
  each is observed.
- **Connection events are self-reported.** A host under an attacker's control can suppress
  records of its own activity. For conversations crossing a zone boundary this is
  detectable, because the firewall records the same connection independently and a
  discrepancy between the two is itself an indicator. For conversations within a zone
  there is no independent record and suppression would not be detectable.
- **Connection events carry no originating process.** The record is the connection's
  five-tuple and direction, not the process that opened it. Process attribution is deferred
  (§10); where it is added it requires host-level audit, which is available
  only on virtual-machine-backed hosts, because the kernel permits audit control only from
  the initial network namespace and so it cannot run in a container (§8.2).

### 4.5  Extension

A sensor host in each segment, receiving a mirrored copy of that segment's traffic, would
add payload visibility within zones and remove the self-reporting limitation above.

---

## 5  Packet inspection at the zone boundaries

The three firewall nodes examine packet contents, for traffic crossing a zone boundary
only. Three checks:

- **Known attack patterns.** Traffic is compared against a published signature set — the
  Emerging Threats Open ruleset, held in this repository at a recorded revision and
  verified against a recorded digest at build time (§8.4).
- **Protocol conformance on permitted ports.** The firewall policy
  (`deployment.md §4.2`) permits specific ports; this verifies that the traffic on each is
  the protocol that port was opened for. Carrying a different protocol over an
  already-permitted port is a common way of moving data past a firewall, and it is
  invisible to the firewall policy itself. Transport Layer Security is expected on 443,
  636, 8428 and 9428: every TLS connection opens with a handshake record, so a client
  whose first data does not begin with one is flagged. The Network Time Protocol is
  expected on UDP 123, identified by the inspection engine's own parser.
  Secure shell is expected on TCP 22, identified by the inspection engine's own parser.
  The agent channel on 1514 is not checked (§10).
- **Tank Level System protocol conformance on TCP 10001.** The wire format is documented
  in `sim/tls-protocol.md`, so traffic on this port is checked against the permitted frame
  structure and the permitted function codes — `i101`, `i201`, `i205` — with set
  commands rejected. The console acts on every start-of-header byte in a client's stream,
  so every command is checked, including one sent in the same segment as a permitted
  command; bytes after a permitted command that do not begin another are rejected as
  data outside a command. These checks inspect the client's data as the receiving side
  acknowledges it, so data that is never acknowledged — a connection the relay resets —
  is checked, and alerted on, when the connection ends. This is the port on which the external fuel supplier reaches
  the tank gauge console through `atg-relay-01`, and the only path into the environment
  from outside.

Boundary capture is per-network-namespace (`AF_PACKET`), so it observes the real traffic on
the zone interfaces whether a firewall node is a virtual machine or a container; unlike the
host-level connection logging of §4.1, it does not depend on the substrate.

A sending host's kernel hands its virtual interface segments larger than the maximum
transmission unit (MTU), and the zone bridge delivers them to the firewall unsplit; they
are split only as they leave the firewall's other interface. A Transport Layer Security
1.3 client's first message, carrying a post-quantum key share, is one such segment. The
capture therefore accepts frames up to the largest IP packet, so every segment is
inspected whole on the interface it arrives on as well as the one it leaves by (§8.4).

Each check produces an alert record naming the rule that matched, with the relevant packet
excerpt, written to the same event file as the flow records and forwarded by the node's
agent (§3, §4.2). Rule sets are fixed at image build time and are never updated at run
time (§8.3, §8.4).

The RavensPort signatures, and the manager rules that attach an ATT&CK technique to each
(§2.2):

| Signature | Detects | Manager rule | Technique | Tactic |
|---|---|---|---|---|
| 1000001 | A set command (format letter `s` or `S`) to the tank gauge console | 100101 | T1692.001 Command Message (ICS) | Evasion, Impair Process Control |
| 1000002 | A function code other than `i101`, `i201`, `i205` that is not a set command, including the display-format `I101` | 100101 | T1692.001 Command Message (ICS) | Evasion, Impair Process Control |
| 1000003 | Traffic on 10001 that is not the Tank Level System protocol | 100102 | T1571 Non-Standard Port (Enterprise) | Command and Control |
| 1000004 | Traffic on 443, 636, 8428 or 9428 that does not open with a Transport Layer Security handshake | 100102 | T1571 Non-Standard Port (Enterprise) | Command and Control |
| 1000005 | Traffic on UDP 123 that is not the Network Time Protocol | 100102 | T1571 Non-Standard Port (Enterprise) | Command and Control |
| 1000007 | Bytes on 10001 after a permitted command that do not begin another command | 100102 | T1571 Non-Standard Port (Enterprise) | Command and Control |
| 1000008 | Traffic on 22 that is not secure shell | 100102 | T1571 Non-Standard Port (Enterprise) | Command and Control |

T1692.001 is the successor MITRE names for T0855 (Unauthorized Command Message), which
the pinned ICS matrix revokes; the revoked identifier cannot be resolved. Alerts from
the published Emerging Threats signatures carry no RavensPort mapping: the manager's
packaged Suricata rule raises them without a technique.

---

## 6  What the system produces

Three outputs on `siem-01`. The first two are files, and are the interface that anything
consuming this data reads.

| Output | Contents |
|---|---|
| `/var/ossec/logs/alerts/alerts.json` | One JSON object per alert: rule identifier, severity, ATT&CK technique, originating agent, decoded fields, the original line, timestamp. The minimum severity at which an alert is written is configurable |
| `/var/ossec/logs/archives/archives.json` | Every record received, whether or not a rule matched. Requires `<logall_json>yes</logall_json>` in `ossec.conf`, which is set |
| Management interface, TCP 55000 | Configuration, agent connection state, active rules, inventory. **Does not serve alerts** |

Neither file holds the file integrity baseline or the inventory (§2.1). The manager keeps
both in a database for each agent, `/var/ossec/queue/db/<agent identifier>.db`, which the management interface
serves. Two kinds of record do reach the files: a file added, deleted or changed raises an
alert, and each inventory change — a process started, a port opened — is archived.

Both files are written as one JSON object per line. A plain-text rendering of the same
alerts is written alongside to `alerts.log`. Files are rotated daily into dated
subdirectories. Both files, and the per-agent databases, begin at the deployed manager's
own first start: the image carries no record from the manager's start during its build.

Two fields in `alerts.json` are conditional:

- **ATT&CK technique** is populated for any rule carrying a mapping — many rules in the
  manager's own packaged ruleset already carry one, and every RavensPort boundary rule
  carries one (§2.2, §5, §8.4). A rule with no mapping leaves the field empty.
- **The original line** is present for records from text inputs. For records sourced as
  JSON — Suricata's event file, and the connection log where it is read as JSON — the
  manager stores the parsed structure in the decoded fields instead and does not set the
  original line; the decoded fields are richer, but a consumer expecting the original line
  on every record will not find it.

---

## 7  Retention

Retention is indefinite. Neither file is deleted.

The manager's analysis daemon writes each day's alerts and archived records into a file in a
dated subdirectory (§6); `alerts.json` and `archives.json` are second names for the current
day's files. Its `monitord` daemon compresses each previous day's files
(`monitord.compress`, enabled in the packaged default). Nothing in the manager deletes them,
and the system `logrotate` utility plays no part. The manager's deletion setting,
`monitord.keep_log_days`, applies only to its own operational log, `ossec.log`; it is set to
its maximum of 500 days rather than the packaged 31, so that log is kept as long as the
setting allows. The environment is rebuilt periodically, so both stores begin each run
empty.

---

## 8  Deployment

### 8.1  Placement and firewall policy

`siem-01` sits in the IT zone at 10.30.0.30.

Agents connect to `siem-01` on TCP 1514. Hosts in the IT zone reach it without crossing a
firewall. Hosts in the DMZ cross `fw-dmz-it`. Hosts in the OT zone use the two-hop path
already carrying identity and time traffic (`deployment.md §4.3`). Two rules are added to
`firewall_rules`:

```
vnet-ot  → vnet-dmz : siem-01 1514/tcp    (hop 1)
vnet-dmz → vnet-it  : siem-01 1514/tcp    (hop 2, also serves DMZ hosts)
```

The firewall rules permit these paths but do not create the routes they use: hosts in the
DMZ and external segments additionally require a route to the IT zone via `fw-dmz-it` to
reach the manager.

No rule is required for agent enrolment: keys are generated at render time (§8.5), so the
manager's enrolment service is not enabled.

### 8.2  Changes on every monitored host

- The agent, its configuration, and its pre-shared key.
- An `nftables` rule logging new connections in both directions (§4.1). The rule directs
  each matching packet to a log group local to the host's network namespace (`NFLOG`), from
  which a userspace daemon (`ulogd`) writes one JSON record per connection to
  `/var/log/ulog/connections.json`, a file the agent collects. The same mechanism is used on
  every host and every deployment profile: on a container the kernel log is shared across
  containers and is not visible to a per-host collector, and a single mechanism keeps the
  record's format the same on a virtual machine. The firewall nodes already run `nftables`
  and gain the same rule for their own traffic, distinct from the `forward` chain that
  carries cross-zone policy.

The firewall nodes additionally run Suricata (§5). They carry 2 virtual CPUs and 2 GB
(`deployment.md §3`), confirmed sufficient by measurement: with
the full Emerging Threats Open ruleset loaded, no packets are dropped. Where hardware
pattern-matching acceleration is unavailable — for example an `aarch64` build, which has no
Hyperscan — the bit-state matcher (`ac-bs`) holds the ruleset in roughly 530 MB rather than
1.1 GB.

### 8.3  Operating without internet access

No deployed instance has outbound internet access (`deployment.md §2.1`). Connectivity
exists only during image build. Two consequences:

- **Vulnerability detection is disabled.** It is enabled by default and queries an
  external threat-intelligence service at startup and periodically.
- **Rule sets and signatures are never updated at run time.** They are part of the image.

### 8.4  Image build

`deploy/build-scripts/siem-01.sh.j2` assembles `ravensport/siem-01:latest`, following
`deployment.md §6` in full, including the input-hash rebuild check and the removal of
`cloud-init`. Reproducibility requires:

- Wazuh packages installed from the vendor repository at **pinned versions**, with the
  repository signing key held in this repository rather than fetched at build time.
- Suricata installed at a pinned version, with the packaged service unit masked and
  replaced by a unit reading a rendered `AF_PACKET` interface list. Its signature set is
  the Emerging Threats Open ruleset for the Suricata 7.0.3 release line, held in this
  repository rather than downloaded during the build, verified against a recorded SHA-256
  digest, and loaded beside the RavensPort boundary rules. The publisher replaces the
  ruleset daily at a single address per release line and retains no earlier revision, so
  a download cannot be pinned: a digest recorded against it matches for at most a day.
  The held revision is 11302, SHA-256
  `b5d970de888803e58b91a22b642965c190970d2d698eaab6575095cbcf96c3b3`. Moving to a later
  revision is a deliberate change to this repository, replacing the held file and the
  recorded revision and digest together, and is confirmed by the canary below. Where
  hardware pattern-matching acceleration is unavailable the bit-state matcher (`ac-bs`)
  is selected (§8.2), and the `HOME_NET` and port variables the ruleset
  references are set to the internal zones. The capture accepts frames of up to 65,549
  bytes (§5), in a ring of 128 KiB blocks; the packet pool is reduced from 1,024 to 128,
  which keeps that ring to 64 frames per capture thread. One further signature is re-enabled beyond
  the ruleset's own published defaults: sid 2022938 (`ET EXPLOIT Possible Symantec
  Malicious MIME Doc Name Overflow (EICAR) toserver M4`), which the ruleset ships
  commented out under a relevance-based deprecation. It matches the standard EICAR
  antivirus test string carried in an overlong Multipurpose Internet Mail Extensions
  `name` header, in the client-to-console direction — the one direction reachable from
  outside the console itself, and the only published signature in the pinned ruleset that
  matches the EICAR string at all over a plain TCP connection. It is the known-attack-
  pattern canary of `siem-tests.md §5`, confirming the signature set is loaded and the
  matching engine runs.
- The ATT&CK for Industrial Control Systems matrix imported into the manager's technique
  database at build time, from a pinned Structured Threat Information Expression (STIX)
  bundle verified against a recorded SHA-256 digest, because the manager ships only the
  Enterprise matrix (§2.2). Each imported tactic carries a reference entry as well as its
  tactic entry: the manager loads the whole matrix at every start, and a tactic without a
  reference stops that load, leaving every technique not yet loaded — the imported ones
  included — unresolved. The build fails if the manager's first start reports that the
  matrix could not be loaded.
- `ossec.conf` and the rule set installed from rendered artefacts. Nothing is configured
  at instance start.
- Network configuration converted from `build/network-config/siem-01.yaml`; `/etc/hosts`
  from `build/hosts/siem-01`.

### 8.5  `world.yaml`

Three additions, projected by `render.py`:

| Value | Projection |
|---|---|
| Agent pre-shared keys | One line of `client.keys` into each monitored host's image; the complete file onto `siem-01` |
| Management interface credential | Onto `siem-01`, hashed with the manager's own scheme |
| File integrity, inventory and listening-port schedules (`intervals.file_integrity_interval_h`, `intervals.inventory_interval_h`, `intervals.listening_port_interval_s`) | Into the agent configuration in each monitored host's image, `siem-01`'s own agent included, each divided by the compression factor in a compressed scenario (`deployment-config.md` §4, projection targets) |

The pre-shared keys and the management credential are derived deterministically from `crypto.password_salt_seed` combined with the
agent or user name, as the existing credential projections are
(`deployment-config.md §6`). The least-knowledge check (`deployment-config.md §5`) must
permit `siem-01` holding every agent's key while each monitored host holds only its own.

The manager creates two default superusers on its first start, `wazuh` and `wazuh-wui`,
each with its username as its password. The image build starts the manager once, waits
until both exist, renames `wazuh` to the management interface account with the projected
hash, and removes `wazuh-wui` together with its role assignment. The build fails unless the
management interface account is then the only user and no role assignment names a removed
user.

In `client.keys` the source-address field is left unrestricted (`any`): the two-hop
`OT → DMZ → IT` path and the multiple interfaces on the firewall nodes make a single source
address unreliable, and authentication rests on the per-agent key rather than the address.

### 8.6  Certificates

The agent-to-manager channel does not use certificates: it is encrypted with the
per-agent pre-shared key of §2.1. The management interface on TCP 55000 does use one.

The management interface presents a leaf certificate issued by the RavensPort internal
certificate authority (`digital.md §2.7`, `§8` decision 7), so that callers validate it
against the common root rather than accepting a self-signed certificate. `siem-01` is one
of the authority's leaf-certificate holders, alongside `fms-01` and `hmi-01`. The leaf
carries a DNS Subject Alternative Name of `siem-01`, and callers validate by hostname, not
by address.

The certificate and key are generated by the certificate-authority build (under
`build/tls/siem-01/`) and installed as the manager's application-programming-interface
server certificate — `/var/ossec/api/configuration/ssl/server.crt` and `server.key` — with
the `https` block of `/var/ossec/api/configuration/api.yaml` set to present them
(`enabled: yes`, `key: server.key`, `cert: server.crt`). Client-certificate verification is
not enabled (`use_ca: false`): callers are authenticated by the credential of §8.5, not by
a client certificate. The authority's root certificate is already installed in every node's
system trust store, `siem-01` included (`deployment-config.md §4`).

Unlike `fms-01` and `hmi-01`, which fail to start on a missing certificate
(`digital.md §8` decision 7), the Wazuh manager generates a self-signed certificate at
first start when none is present. The build therefore installs the authority-issued leaf
before the manager first runs, so no self-signed material is ever generated in the image.
The package itself also generates a self-signed certificate and key at installation, for
the enrolment service (`/var/ossec/etc/sslmanager.cert` and `sslmanager.key`). That
service is not enabled (§8.1), so the build removes both, leaving the authority-issued
leaf as the only certificate the manager holds.

The authority's private key remains on the build host, and validity dates are derived from
`crypto.ca_not_before` so that the artefact is deterministic (`deployment-config.md §6`).

---

## 9  Extension: storage and search

Two further Wazuh components — a search store and a browser interface — may be added.
They provide retrospective query across hosts and time. Detection is unaffected: all rule
evaluation is in the manager, and the output files of §6 are written whether or not these
components are present.

The cost is substantial. The store is a Java virtual machine process, raising `siem-01`
from roughly 1–2 virtual CPUs and 1–2 GB to 4 and 8 GB. It requires
`vm.max_map_count` of at least 262144 on the physical host: the setting is not namespaced
and so cannot be set per instance, following the same pattern as the existing
`kernel.keys.maxkeys` prerequisite (`deployment.md §1`). It authorises by certificate
distinguished name, so the certificate authority's naming policy becomes load-bearing
configuration. Its update check and map panels must be disabled for offline operation
(§8.3).

---

## 10  Deferred capabilities and coverage gaps

These are recorded so that the team can flag any that must not be deferred. Each
names why it is deferred, what it narrows in coverage, and what brings it back. The output
files of §6 are written regardless; nothing here disables detection already specified.

| Item | Why deferred | Coverage impact | Brought back when |
|---|---|---|---|
| **Process attribution on connections** (§4.4) | Host-level audit needs the initial network namespace, so it cannot run in a container; in-container tracing observes every container and is not per-host (§8.2) | Connection events carry the five-tuple only; activity cannot be tied to a process on the container profile | Virtual-machine-backed hosts, or a scenario requires it |
| **Intra-zone payload visibility** (§4.5) | Design choice: content is inspected only at zone boundaries (§5) | Intra-zone content — dispenser register values, card numbers — is never inspected, and intra-zone connection suppression is undetectable (§4.4) | A scenario requires payload visibility within a zone |
| **Protocol conformance on TCP 1514** (§5) | The agent channel carries the manager's own encrypted protocol, for which the deployed Suricata version has no parser | Another protocol carried over the agent port is undetected; every other permitted port is checked | A parser for the agent protocol is available |

The first two narrow what the system can detect by architecture and substrate; the third
is a coverage gap that closes once Suricata has a parser for the agent protocol.
