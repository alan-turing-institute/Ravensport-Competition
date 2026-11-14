# Security Monitoring Test Plan

Test plan for the security-monitoring system specified in `siem.md`: the
Wazuh manager on `siem-01` (IT zone), the Wazuh agent on each of the twelve
monitored hosts, and Suricata on the three firewall nodes. One section
per behavioural area; each names the individual tests it comprises and the
requirement each targets.

---

## 0  Test harness

### 0.1  Two locations

| Location | Needs a deployment | Covers |
|---|---|---|
| `deploy/tests/unit/` | No | The rendered artefacts and the build script: schema, agent-key and credential-hash derivation, least-knowledge, artefact pinning, host monitoring configuration (§10) |
| `tests/integration/siem/` | Yes — the full topology | The running system: agents, decoding, rule evaluation, host and network data, packet inspection, outputs, retention, deployment and certificates, file integrity and inventory (§1–§9) |

Because detection depends on activity across every host and zone, the
integration suite runs against the full deployed topology rather than
`siem-01` in isolation, with a scenario in progress for the tests that assert
on traffic the scenario itself produces (§4, §5).

### 0.2  Management-interface client

A thin client (`tests/integration/siem/conftest.py`), run on `siem-01` itself
through `incus exec`, addressing `siem-01` by hostname on TCP 55000 and
validating its presented certificate against the CA's public root
(`build/ca/ca.pem`, `digital.md §2.7`), which the host's system trust store
holds — the same convention as the directory and log-server clients. It wraps the endpoints
the tests need: agent connection state, rule and decoder listing. It cannot
retrieve alerts (`siem.md §6`): tests that need alert content read
`alerts.json`/`archives.json` directly (§0.3).

### 0.3  Reading the outputs

`alerts.json`, `archives.json`, `alerts.log`, and the management interface's
own state are read via `incus exec siem-01`, matching the convention already
used for loopback-bound or otherwise not-directly-routable services in the
log-server and reconciliation suites. This requires no runner zone
membership and reaches state the management interface itself cannot serve.

### 0.4  Triggering events on a monitored host

A deliberate event on a monitored host — a failed authentication, a service
restart — is produced with `incus exec <host>`, not over the network. This
holds for every monitored host regardless of zone, so §1's and §3's
per-host assertions need no runner placement either.

An event is triggered only once the host's agent has completed the reload
the product performs once after its first connection, when it applies the
shared configuration the manager pushes: a journal line written while the
log collector restarts is never read. The agent restarts its daemons as soon
as it has written the pushed file, so the reload is complete once that file
exists on the host and was written at least 30 seconds ago, by the host's
own clock.

Every monitored host reports continuously, so a test matches the record of
the event it triggered, never any record from the host: the line it wrote
(a failed authentication carries a unique source port), systemd's record of
the unit it restarted or reloaded, or a line written by the restarted
process under its own name and new process identifier.

### 0.5  The TLS-protocol frame injector

`tests/integration/siem/tls_frame_injector.py`, a client implementing the
command framing and checksum of `sim/tls-protocol.md` §2 and §4, run from
the test runner positioned on `vnet-ext`. Unlike everything else in this
plan, the boundary-inspection tests of §5 must exercise the real network
path Suricata inspects, so this is the one component that cannot be
substituted with `incus exec`. The injector constructs both conforming
frames (to confirm they pass unflagged) and frames outside the permitted
set: a set command, and a function code other than `i101`/`i201`/
`i205`. It is a stateless client, not a stub: each test opens its own
connection to `atg-relay-01:10001` and constructs its own frame.

The relay resets any connection not from the supplier's address as soon as
the handshake completes (`digital.md §2.5`), so an injected frame reaches
the wire only if it is sent before that reset arrives, and sometimes it is
not. The injector reports each connection's source port, and a test waits
for the alert on its own connection, resending up to three times, rather
than assuming a single send was inspected.

**Runner access to `vnet-ext`.** The zone bridges carry no host address
(`deployment.md §2.1`), so a test process on the Incus host has no route to
`atg-relay-01` until placed on a zone. `make -f tests/integration/Makefile
configure-siem` places the runner on `vnet-ext` as `RUNNER_HOST`, following
the same pattern as `configure-hist` and `configure-fms`. This is the only
suite in the repository that needs a presence on the external zone; no
other test client originates traffic from there.

### 0.6  Decoder and rule checks in isolation

`wazuh-logtest`, run via `incus exec siem-01`, decodes a supplied line and
reports which rule (if any) matched, without needing a live agent
connection or generated traffic. Used for the decoder and rule assertions
of §2 that concern the ruleset itself rather than an end-to-end delivery
path.

---

## 1  Agent enrolment and channel

Targets `siem.md §2.1`, `§8.1`, `§8.5`.

### `all_twelve_agents_report_connected`

Every monitored host's agent shows as connected on the management
interface, including `siem-01`'s own self-monitoring agent.

### `deliberate_event_reaches_both_outputs_correctly_attributed`

Parametrised across all twelve monitored hosts. A deliberate event triggered
on the host (§0.4) appears in `archives.json` within seconds, attributed to
that host's agent identity; where the event matches a rule, it also appears
in `alerts.json` with the same attribution. Both records are matched on the
event's own logged line, not on any record from the host, since every host
reports continuously.

### `agent_channel_uses_the_pre_shared_key_not_a_certificate`

The agent-to-manager connection on TCP 1514 authenticates with the
per-agent key of `siem.md §2.1`/`§8.5`, not a certificate: connecting with a
wrong or absent key is refused, and no certificate exchange is observable
on this channel — distinct from the management interface, which does
present one (§8).

### `agents_reconnect_after_a_full_teardown_and_redeploy`

Following `make down` and `make up`, every agent reconnects and reports as
connected without any manual re-enrolment step, since keys are derived
deterministically at build time (`siem.md §8.5`) rather than issued at
first contact. Alert and archive data do not survive: the stores begin
empty on a rebuilt environment (§7).

---

## 2  Decoding and rule evaluation

Targets `siem.md §2.2`, `§8.4`.

### `wazuh_logtest_decodes_every_input_format`

Parametrised over a line in each format the manager receives (`siem.md §3`,
§4), copied from the deployed hosts: a systemd journal entry, a secure shell
authentication failure, a `sudo` invocation, PostgreSQL's background,
session-start, session-end and authentication-failure lines, a line from the
directory client's domain log, a Suricata `eve.json` alert, and a
connection-log record. Each reaches the decoder for its format, which
extracts the fields the line carries; for PostgreSQL's session lines, the
database user and the database. Only the decoding phase of `wazuh-logtest`'s
output is read: the pre-decoding phase prints named fields for every line,
decoded or not.

### `postgresql_password_failure_carries_password_guessing`

A PostgreSQL password failure line, decoded by `wazuh-logtest`, raises the
RavensPort rule rather than the manager's generic authentication-failure
rule, naming the database user and carrying T1110.001 (`siem.md §3`).

### `repeated_postgresql_password_failures_carry_brute_force`

Eight PostgreSQL password failures for one database user, fed to one
`wazuh-logtest` session, raise the RavensPort brute-force rule on the
eighth, carrying T1110; seven raise only the single-failure rule
(`siem.md §3`).

### `attck_technique_is_populated_for_any_mapped_rule`

An alert from a rule with no ATT&CK mapping at all carries no technique. An
alert from a rule carrying a mapping does carry one — this includes many
rules already included in the manager's own packaged ruleset, not only a
boundary-inspection rule (§5) or a rule from the imported ICS matrix.

### `att_ck_for_ics_matrix_is_present`

At least one ATT&CK for Industrial Control Systems (ICS) identifier is
directly queryable in the manager's technique database (`siem.md §2.2`,
`§8.4`), confirming the imported bundle is loaded and parsed into the
technique/reference relation, not merely present as an unparsed file.
Whether the rule loader resolves an imported technique is checked end to
end in §5.

### `every_tactic_carries_a_reference`

Every tactic in the manager's technique database, the imported ICS tactics
included, has a reference entry (`siem.md §8.4`). The manager loads the
whole matrix at every start, and a tactic without one stops that load,
leaving every technique not yet loaded unresolved.

### `sequence_rule_fires_on_repeated_failures_within_the_window`

Repeated authentication failures from one source within the configured
window produce a single alert distinct from any of the individual
failures (`siem.md §2.2` illustrates the general capability with "e.g.
five", not a literal count; the manager's own stock rule requires eight).
One fewer than the threshold produces none. The rule's 120-second window
and its 60-second suppression period after firing persist across runs, so
the test first waits, only as long as needed, until neither holds anything
from an earlier run.

---

## 3  Host log data

Targets `siem.md §3`, `§8.2`.

### `journal_sources_are_collected_on_every_monitored_host`

A unit start/stop, an authentication event, and a network configuration
change on a monitored host each reach `archives.json`. The Fuel Management
System's and Human-Machine Interface's own process output, carried via the
journal, is included.

### `named_log_files_are_collected_where_specified`

The file inputs of `siem.md §3` each reach `archives.json` when written to.
They are PostgreSQL on `fms-01`, the directory client's domain log on `recon-01` and `admin-01`, and Suricata's event file on the firewall nodes.

The host audit itself — every path found outside the
journal on each deployed host, and the collect-or-not decision made for
it — is a pull-request artefact, not a suite deliverable; nothing here
checks for its existence.

### `no_input_is_collected_twice`

For a host with both a journal source and a file source that could overlap
(for example a service whose stdout also lands in the file the audit
found), one event produces exactly one record, not two under different
decoders.

---

## 4  Network activity data

Targets `siem.md §4`, `§8.2`.

### `connection_event_for_every_conversation_including_the_four_within_a_zone`

With a scenario in progress, `archives.json` carries a connection event for
every conversation `siem.md §4.1` lists, including the four that reach no
firewall: the Fuel Management System to the tank-gauge console, to the
dispenser, and to the vessel card reader, and the Human-Machine Interface
to the operator card reader. The dispenser conversation occurs only during
a dispense, which follows a tug arrival the simulator draws at a mean rate
of one per hour, so the test opens that connection itself rather than
waiting for one.

### `connection_event_carries_the_specified_fields_and_nothing_more`

A sampled connection event carries the local host's identity and interface,
direction, and the five-tuple, and carries no volume, duration, or
originating process — confirming the coverage limit of `siem.md §4.4` is a
property of the record, not an omission from this test.

### `connection_event_for_every_cross_zone_conversation`

Every cross-zone path `deployment.md §4.2` permits also produces a
connection event — the same coverage confirmed above for the four
intra-zone conversations, checked here for the paths that also cross a
firewall. Exercised at each conversation's own real endpoint, once per
conversation rather than once per firewall hop: a firewall node's own
connection-event log carries only traffic it makes or accepts itself,
never what it merely forwards. The test opens its own connection for the IT
zone's HTTPS path to the log-server replica, exercised from `corp-it`, since
nothing drives it on a predictable cadence, and for the two replication paths from
`hist-01`, whose connections are held open rather than re-established per
event.

### `connection_event_for_inbound_external_supplier_traffic`

The one cross-zone conversation whose monitored participant is the
destination rather than the initiator: the relay's own inbound connection
events include the external zone's traffic on its relay port. Driven by one
conforming poll from the runner on the external segment, since the
simulator's fuel supplier polls only at its configured interval,
`fuel_supplier.poll_interval_s`, 15 minutes by default
(`deployment-config.md §3`).

### `connection_event_for_corp_it_and_siem_01_from_every_zone`

The directory's NTP service and the manager's own agent channel are each
reached continuously from every host in both the OT and DMZ zones; any one
host from a zone demonstrates the path for it.

### `flow_record_for_every_cross_zone_conversation`

For every permitted cross-zone path in `deployment.md §4.2`, the
originating firewall node's `eve.json` carries a flow record with source
and destination, duration, byte and packet counts in each direction, and
termination reason — reaching `archives.json` via that node's own agent.

Suricata writes a flow record only once a flow has ended and its timeout has
elapsed (60 seconds for a closed TCP flow), so a connection held open for
the life of the deployment — the agent channel, the log-server replication —
produces none. The test opens one fresh connection on each TCP path whose
own traffic does not already produce records, and waits once, across all of
them, for the records to arrive. NTP and the relay-to-console path need no
such connection: every host polls `corp-it` continuously, and the relay
checks the console's availability every 10 seconds (`digital.md §2.5`). An NTP exchange is
two-way, so its record is written only after Suricata's 300-second timeout
for an established UDP flow; on a deployment younger than that, the test
waits only until Suricata has been capturing long enough for the records to
exist.

### `listening_port_inventory_is_reported_and_a_new_port_alerts`

Each monitored host's listening-port inventory is reported periodically. A
new listening port opened on a host, outside the fixed service set, raises
an alert — checked on every monitored host, opened on all of them together
and waited for once, not one at a time.

---

## 5  Packet inspection at the zone boundaries

Targets `siem.md §5`, `§8.4`. Uses the injector of §0.5, and
`sim/tls-protocol.md` as the reference for what is conforming.

### `published_signature_set_matches_a_known_pattern`

A deliberately generated packet matching a published Emerging Threats Open
signature, sent across a zone boundary, raises an alert naming that
signature. Confirms the ruleset is loaded and the matching engine runs, not
only that the file is present.

The canary is the standard EICAR antivirus test string, carried in an
overlong Multipurpose Internet Mail Extensions `name` header rather than
sent bare: no signature in the pinned ruleset matches the bare string over a
plain TCP connection, and this construction matches ET sid 2022938
(`siem.md §5`, `§8.4`), a real published signature re-enabled explicitly for
this purpose. The signature matches only traffic from outside the zones, so
the canary is sent by the injector rather than from a host within them, and
it fires only once the flow ends, about a minute after the frame. The test
therefore confirms delivery first, by the immediate alert the RavensPort rule
raises for non-Tank-Level-System traffic on 10001, and then waits for ET sid
2022938 on the same connection.

### `protocol_conformance_on_tls_ports`

Non-conforming traffic on each of 443, 636, 8428 and 9428 (Transport Layer
Security expected), sent across a firewall node from a host on that path,
raises the conformance signature (`siem.md §5`).

### `protocol_conformance_on_the_ntp_port`

A datagram that is not the Network Time Protocol, sent to `corp-it`'s time
service across both firewall hops, raises the NTP conformance signature,
carrying T1571.

### `protocol_conformance_on_the_secure_shell_port`

Traffic that is not secure shell, sent to port 22 on `dmz-bastion-01` from `admin-01`, and on `fms-01` from `dmz-bastion-01`, raises the secure shell conformance signature at each boundary, carrying T1571.

### `a_first_segment_larger_than_the_mtu_is_inspected_whole`

A Transport Layer Security connection from `hist-01` to `hist-dmz-01` on
8428 whose first client write is larger than the maximum transmission unit
(MTU) — the size of a version 1.3 client's first message carrying a
post-quantum key share — reaches `fw-ot-dmz` as one frame on `ot0`. Its
handshake is recognised there, so the change-cipher-spec record that follows
raises no non-TLS alert, and the `ot0` flow record counts every byte of the
first write (`siem.md §5`).

### `log_server_replication_raises_no_non_tls_alert`

`hist-01`'s metric and event replication to `hist-dmz-01`, a permitted path
through `fw-ot-dmz` whose TLS 1.3 first message reaches the firewall larger
than the MTU, raises no non-TLS alert on either interface over the
deployment's whole run, and each connection's handshake is recognised on
`ot0`, where it arrives, as on `dmz0`. The test waits until at least three
replication connections are recorded, counted from `dmz0`, where every
segment arrives split to the MTU. The suite's own non-TLS payloads to these
ports raise the same signature deliberately; an alert on genuine traffic is
told apart by its payload, which opens with a TLS record (`siem.md §5`).

### `conforming_traffic_raises_no_conformance_alert`

A fresh Transport Layer Security connection on each of 443, 636, 8428 and
9428, and a burst of time queries to `corp-it`, raise no conformance
alert. This is a regression test in its own right: two earlier forms of
the TLS check fired on every legitimate TLS connection — one matched a
connection's first packets before protocol detection had finished, the other
checked every client segment, including the change-cipher-spec record a
TLS 1.3 client sends in its second flight.

A secure shell session from `admin-01` through `dmz-bastion-01` to each of `fms-01`, `hist-01` and `hmi-01` also raises no conformance alert.

### `benign_supplier_polling_produces_no_boundary_alert`

Repeated legitimate `i101`, `i201`, and `i205` reads, generated across many
polling cycles, produce no Tank Level System alert of any kind. This is a
regression test in its own right: the empty leading state a permitted
connection presents before its first segment must not itself be mistaken
for a disallowed frame.

The relay resets each injected connection without acknowledging its data,
so a frame reaches `fw-ext-dmz` only if it is sent before the reset arrives,
and is inspected only when the flow ends. The test waits for each
connection's flow record, written at that point, and requires at least one
connection for each read whose client sent more packets than the handshake
before checking that none of them raised an alert.

### `set_command_on_tcp_10001_is_rejected`

A set command (format letter `s`, `sim/tls-protocol.md` §2), sent to the
tank-gauge console's port, raises its own signature on `fw-ext-dmz`,
distinguishable from a benign read. The relay resets the injected
connection without acknowledging its data, so the alert follows the flow's
end (the closed-connection timeout of 60 s) rather than the frame itself.

### `disallowed_function_code_on_tcp_10001_is_rejected`

A frame using an `i`-format function code other than `i101`, `i201`, or
`i205` raises its own signature on `fw-ext-dmz`, after the flow's end for
the same reason.

### `protocol_substitution_on_tcp_10001_is_rejected`

A byte stream that is not the Tank Level System protocol at all, sent to
port 10001, raises a signature on `fw-ext-dmz` distinct from the
function-code and set-command checks above.

### `boundary_alerts_are_agent_attributed_and_decoded`

A boundary-inspection alert reaches `alerts.json` naming the firewall node
that raised it, with the underlying Suricata signature identifier preserved
in the decoded fields, and carrying the ATT&CK for ICS technique its rule
maps: a set command to the tank gauge console carries T1692.001
(Command Message), resolved to its technique and tactic names
(`siem.md §5`).
The frame is sent from `atg-relay-01` to the console, not through the frame
injector: the relay resets any connection not from the supplier's address
as soon as the handshake completes, so an injected frame reaches the wire
only if it is sent before that reset arrives.

### `protocol_conformance_carries_its_technique`

A protocol-conformance alert on a Transport Layer Security port carries
the Enterprise technique its rule maps, T1571 (Non-Standard Port), resolved
to its technique name (`siem.md §2.2`).

### `rule_sets_are_never_updated_outside_the_image_build`

No mechanism on a firewall node fetches or reloads signatures at run time:
the packaged automatic-update path is absent or disabled, and the loaded
ruleset matches what the image was built with.

### `tank_level_system_rules_hold_on_the_ot_boundary`

A set command, a disallowed function code, and a byte stream that is not
the Tank Level System protocol, each sent from `atg-relay-01` to the
console, raise their own signature on `fw-ot-dmz`. So do a set command and a
disallowed function code sent in the same segment as a permitted read, and
bytes following a permitted read that do not begin another command: the
console acts on every SOH in the client's stream (`sim/tls-protocol.md`
§2), so a command hidden behind a permitted one must be checked as if it
arrived alone. The frame injector
cannot exercise this boundary: the relay admits only the real supplier's
address (`digital.md §2.5`), so injected frames are inspected on
`fw-ext-dmz` and go no further. Each alert also carries the technique its
rule maps: T1692.001 for the set command and the disallowed function code,
T1571 for the stream that is not the protocol at all and for data outside a
command. The console answers
any function code it does not implement with its fixed error frame, so none
of these frames alters simulated state.

### `display_format_read_is_a_disallowed_code_not_a_set_command`

A display-format read (`I101`) sent from `atg-relay-01` to the console
raises the disallowed-function-code signature on `fw-ot-dmz`, and not the
set-command signature: only the format letters `s` and `S` denote a set
command (`sim/tls-protocol.md` §2).

### `simulator_supplier_polling_produces_no_boundary_alert`

The simulator's own supplier polls — the known-good implementation of
`sim/tls-protocol.md` — raise no alert on `fw-ext-dmz`, where the supplier
reaches the relay, or on `fw-ot-dmz`, where the relay forwards the same
conversation to the console. A forwarded poll is the relay-to-console flow
carrying data that began with a supplier flow; the relay's health checks
carry none. The supplier first polls one full poll interval
(`fuel_supplier.poll_interval_s`, 15 minutes by default) after the simulator
starts, so the test is skipped when `fw-ext-dmz` has recorded
no supplier poll yet, rather than waiting for one.

---

## 6  What the system produces

Targets `siem.md §6`.

### `alerts_json_carries_the_specified_fields`

A sampled `alerts.json` object carries rule identifier, severity,
originating agent, decoded fields, and timestamp. The ATT&CK-technique
field is present but empty for a stock-decoder alert and populated for a
mapped one (§2); the original-line field is present for a text-sourced
alert and absent for a Suricata- or connection-log-sourced one, with the
decoded fields present in its place.

### `archives_json_holds_every_record_regardless_of_a_rule_match`

A record that matches no rule still reaches `archives.json`.

### `alerts_log_mirrors_alerts_json`

The plain-text rendering carries the same alerts as the structured file,
in the same order.

### `outputs_rotate_daily_into_dated_subdirectories`

Both files rotate on the daily boundary into a directory named for the
date, rather than truncating or being overwritten in place.

### `outputs_carry_no_record_from_the_image_build`

The deployed manager's records begin at its own first start. Every
archived record names `siem-01` as its manager, and no file under the
manager's installation directory names the container the image was built
in.

### `management_interface_does_not_serve_alerts`

Every endpoint the management interface exposes returns configuration,
agent state, rule, or inventory data; none returns alert content.

---

## 7  Retention

Targets `siem.md §7`.

### `retention_configuration_compresses_and_never_deletes`

The manager compresses each previous day's files (`monitord.compress`), and
nothing on `siem-01` deletes them: no `logrotate` configuration or scheduled
job names the alert or archive directories. The manager's own deletion
setting, `monitord.keep_log_days`, applies only to its operational log and
is raised to its maximum of 500 days (`siem.md §7`).

### `stores_begin_empty_on_a_rebuilt_environment`

A freshly built and started `siem-01` instance holds no prior alert or
archive content.

---

## 8  Deployment and certificates

Targets `siem.md §8`, `deployment.md §7.2`.

### `firewall_reachability_for_agent_traffic`

Extends `deploy/tests/integration/test_firewall.sh`/`test_policy.sh` for
TCP 1514: reachable from every zone, including the two-hop
operational-technology path, and from no other port on `siem-01`.

### `no_container_can_observe_the_shared_kernel`

On the container profiles, every monitored host and `siem-01` runs
unprivileged (`deployment.md §7.2`): its root user is not the host's root
user, and it can read neither the host kernel log nor the kernel message
device, nor host audit status, nor kernel-wide tracing. Checked on each host
separately, the firewall nodes included; a host backed by a virtual machine
has its own kernel and is not checked.

### `management_interface_presents_ca_signed_certificate`

Fetched from another host in the IT zone, the certificate `siem-01`
presents on TCP 55000 chains to the RavensPort internal certificate
authority and carries the subject alternative name `siem-01`.

### `no_self_signed_material_exists_in_the_image`

Every certificate in the manager's configuration trees
(`/var/ossec/etc`, `/var/ossec/api/configuration`) is issued by the
RavensPort internal certificate authority. Nothing self-signed was ever
generated, confirming the leaf was installed before the manager's first
start and that the enrolment service's package-generated pair is absent
(`siem.md §8.1`, `§8.6`). The manager's bundled Python runtime also carries
certificates — the interpreter's own test fixtures and several libraries'
public-root bundles — which are inert package payload that nothing
configures or presents, and are not examined.

### `enrolment_service_is_not_listening`

Agent keys are generated at render time, so the manager's enrolment
service is not enabled and nothing listens on its port, TCP 1515
(`siem.md §8.1`).

### `client_certificate_verification_is_not_enabled`

A connection to the management interface presenting no client certificate
succeeds at the transport level and is refused only by the credential of
§1's counterpart (username and password), not by certificate policy.

### `only_the_management_account_can_authenticate`

The manager's default superusers, `wazuh` and `wazuh-wui`, are refused with
their well-known passwords (HTTP 401). `rbac.db` holds the management
interface account as its only user and no role assignment for a user that
does not exist (`siem.md §8.5`).

### `agent_channel_presents_no_certificate_at_all`

Distinct from the check above: the agent-to-manager channel on TCP 1514
performs no certificate exchange, consistent with `siem.md §8.6`'s
statement that this channel is a pre-shared key, not TLS.

---

## 9  File integrity and inventory

Targets `siem.md §2.1`, `§6`.

Neither capability writes to `alerts.json` in normal running: the manager keeps
each agent's file integrity baseline and inventory in a per-agent database,
`/var/ossec/queue/db/<agent id>.db` on `siem-01`, which these tests read
directly. Each agent scans at start and the file scan's results reach the
manager by periodic synchronisation, so on a newly started deployment the
databases fill over several minutes; the tests wait only as long as needed.

### `every_agent_reports_file_integrity_and_inventory`

Parametrised across all twelve monitored hosts. The manager holds a database
for the host's agent, with a file integrity baseline and entries in each of
the four inventory categories: installed packages, listening ports, running
processes and network interfaces.

### `no_agent_reports_another_inventory_category`

Parametrised across all twelve monitored hosts. Hardware, operating system,
users, groups, services and browser extensions are not reported.

### `inventory_records_a_known_listener`

The inventory reflects the host as deployed: `corp-it`'s listening ports
include 636, with `slapd` behind it.

### `a_new_file_under_etc_raises_an_integrity_alert`

A file created under `/etc` on a monitored host raises the manager's
file-added alert, attributed to that host and naming the file. The scheduled
scan runs every 12 hours, so the test has the manager trigger an immediate
scan of that agent.

### `no_rootcheck_or_configuration_assessment_alerts`

Neither rootcheck nor configuration assessment runs on any monitored host or
on `siem-01` itself, so neither raises an alert.

---

## 10  Build-time configuration tests

Location: `deploy/tests/unit/`. Follows the `test_sim_constants_agreement.py`
pattern already established.

### `test_world_schema.py`

Extended for `identities.siem_admin`. Rejected by the strict schema if a
required field is missing.

### `test_siem_credential_hash.py`

`identities.siem_admin`'s hash is reproducible across repeated renders from
the same `world.yaml`; the deterministically derived salt reproduces the
manager's own scrypt scheme byte-for-byte (`deployment-config.md §6`), and
two different usernames with the same plaintext password produce different
hashes.

### `test_siem_admin_seed.py`

Applied to the manager's `rbac.db` schema with its two default users, the
rendered seed leaves the management interface account as the only user and
no role assignment for a removed user. Applied with the default users not yet
inserted, or with another user present, it fails and changes nothing. The
build script waits for both default users, fails if they do not appear, and
applies the seed with `sqlite3 -bail` (`siem.md §8.5`).

### `test_siem_agent_keys.py`

Every monitored node's agent key is reproducible across repeated renders,
derived from `crypto.password_salt_seed` and the node name; `siem-01`'s
projected `client.keys` carries the complete set, and each monitored node's
own projection carries only its key.

### `test_siem_least_knowledge.py`

The least-knowledge check of `deployment-config.md §5` correctly permits
`siem-01` to hold every agent key while asserting no monitored node's baked
configuration carries another node's key, the management-interface
credential, or any fact belonging to a different host.

### `test_siem_artefact_fetch.py`

Wazuh packages are installed at a pinned version with the repository
signing key held in the repository rather than fetched at build time; the
Emerging Threats Open ruleset is held in the repository at the revision
`siem.md §8.4` records, is not downloaded at build time, matches the
recorded digest, and carries sid 2022938 commented out for the build to
re-enable; the ATT&CK for ICS bundle is pinned by version and verified
against a recorded digest before use.

### `test_siem_certificate.py`

`siem-01`'s build script installs the authority-issued leaf and key as the
management interface's server certificate before the manager's first
start; sets the `https` block of `api.yaml` to present them, with
client-certificate verification off; and disables the enrolment service and
removes its package-generated pair (`siem.md §8.1`, `§8.6`). The leaf's
chain, hostname-only subject alternative name, and byte-identical output
across two renders are covered with every other leaf by
`test_ca_provisioning.py`, whose leaf list includes `siem-01`.

### `test_siem_host_monitoring.py`

Every agent's rendered configuration monitors the product's default paths on
the default schedule, reports exactly the four inventory categories of
`siem.md §2.1` (listening ports only), and disables rootcheck and
configuration assessment explicitly. `siem-01` installs the same blocks, and
its build removes each of the packaged monitoring blocks, failing if one is
missing or repeated.

### `test_siem_capture_size.py`

Every firewall's rendered build captures frames up to the largest IP packet,
in a capture ring whose blocks each hold one such frame and whose size stays
within a few megabytes per thread. The settings are written to the
`af-packet` entry the command-line interfaces inherit, and the file is
written without reordering its keys (`siem.md §5`, §8.4).

### `test_siem_attack_mapping.py`

The ATT&CK for ICS import writes a reference entry for every tactic, and
the build fails if the manager's first start reports that the matrix could
not be loaded (`siem.md §8.4`). Each RavensPort boundary signature is
selected by exactly one Wazuh rule carrying the technique the table in
`siem.md §5` names — T1692.001 for the set command and the disallowed
function code, T1571 for the five protocol-conformance checks — and the
Suricata signature's own metadata declares the same technique. The
PostgreSQL password-failure rule refines the manager's generic
authentication-failure rule, selects only lines from the RavensPort
PostgreSQL decoder, and carries T1110.001; the repeated-failure rule
correlates eight of them for one database user within 120 seconds, with a
60-second quiet period, and carries T1110 (`siem.md §3`).

### `test_siem_topology.py`

Every monitored node's `resolve:` entry in `topology.yaml` gains `siem-01`,
and the two `firewall_rules` entries for TCP 1514
match `siem.md §8.1` exactly.

---

## 11  Explicitly out of scope

- **Process attribution on connections.** `siem.md §4.4`/`§10` record this
  as deferred to virtual-machine-backed hosts; nothing here asserts a
  connection event carries a process, only that it does not (§4).
- **Intra-zone payload visibility.** A design choice (`siem.md §4.5`), not
  a gap this suite probes for.
- **Protocol conformance on TCP 1514.** No parser exists for the agent
  protocol in the deployed Suricata version (`siem.md §5`, `§10`); nothing
  here exercises it.
- **The storage and search extension (`siem.md §9`).** Not deployed;
  detection and the two output files are unaffected by its absence, and
  this suite covers only what is deployed.
- **Certificate renewal and revocation.** Leaf certificates are valid for
  one year (`digital.md §8` decision 7) and the authority issues no
  revocation mechanism; the same reasoning as the log server's test plan
  applies, and nothing here exercises either.
- **The correctness of the applications being monitored.** This suite
  confirms what the monitoring system observes and reports about the Fuel
  Management System, the Human-Machine Interface, the directory, the log
  server, and the reconciliation workstation; each of those systems' own
  correctness is covered by its own test plan.
- **Quantifying peak memory and retention volume.** The two measurements
  are one-off figures recorded against a scenario run, not a repeatable
  assertion this suite re-checks on every run.
