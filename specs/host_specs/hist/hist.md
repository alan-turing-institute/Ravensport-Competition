# Log Server Implementation Specification

## 1  Role and scope

The log server runs as two instances: `hist-01`, the primary historian in the OT zone,
and `hist-dmz-01`, its read-only replica in the DMZ. Together they are the time-series
store for Automatic Tank Gauge (ATG) readings and alarms, and the structured event log
for job/transaction lifecycle events, operator authentication events, and RFID card
table changes. Retention is 3 years on both hosts, aligned to the MARPOL Convention
record-keeping period carried on the paper Bunker Delivery Notes (BDNs) and delivery
tickets the log server mirrors (narrative.md §5).

`hist-01` is the sole ingestion point: the Fuel Management System (FMS) on `fms-01`
pushes every reading and event to it. `hist-01` in turn pushes a continuous replica of
everything it holds to `hist-dmz-01`, which the reconciliation user queries for the
end-of-period audit (narrative.md §3). No component other than `hist-01` writes to
`hist-dmz-01`, and no component queries either host for anything beyond read access.
Divergence between the two is itself a detection signal (digital.md §6.3).

The authoritative description of the log server's role in the environment is
`digital.md §2.2` (primary) and `§2.4` (replica), `§4.5` (ingestion and replica sync),
and `§6` (logging and retention). This document is the authoritative specification for
the log server's software stack, ingestion protocols, data model, replication
mechanism, and deployment. The FMS's log-server push behaviour is described from the
FMS's side in `fms.md §5.4`; that section must be kept consistent with the ingestion
protocols specified here (§5).

---

## 2  Software stack

Five components, common to both hosts except where noted:

| Component | Role | Runs on |
|---|---|---|
| VictoriaMetrics | Single-node time-series store for ATG readings and alarms | Both |
| VictoriaLogs | Single-node structured log store for job/transaction/authentication/config-change events | Both |
| vmagent | Ingestion front-end for metrics; writes to the local VictoriaMetrics and, on `hist-01` only, fans out to `hist-dmz-01` | `hist-01` only |
| vlagent | Ingestion front-end for events, accepting both JSON-line and syslog protocols; same fan-out role as vmagent | `hist-01` only |
| Grafana | Queried directly by a human: an admin's dashboard on `hist-01`, and on `hist-dmz-01` both a dashboard and the datasource proxy the reconciliation user's audit retrieves through — not consumed programmatically by any other component (§7) | Both |

All five are open source — VictoriaMetrics, VictoriaLogs, vmagent, and vlagent under
Apache 2.0, Grafana OSS under AGPLv3 — satisfying the open-licence rule.

Grafana additionally loads the VictoriaLogs datasource plugin, without which it has no
means of querying VictoriaLogs (§7): VictoriaMetrics is read through Grafana's built-in
Prometheus datasource because it implements that API, whereas VictoriaLogs' query API
has no built-in equivalent. The plugin is not a sixth component of the stack — it runs
inside Grafana rather than as a service of its own — but it is pinned, fetched, and
verified alongside the five that are (§2.1), and it is Apache 2.0.

### 2.1  Pinned versions

Versions are pinned rather than resolved at build time. A `latest` lookup would make
the image non-reproducible, and against GitHub it is subject to the unauthenticated
API rate limit. `<arch>` is `amd64` or `arm64`, determined from the build host's own
architecture, since the artefacts are fetched there rather than inside the build
container (§2.2); the installed filesystem layout is identical on both, so
architecture affects only the artefact filename.

| Component | Version | Artefact | Published at |
|---|---|---|---|
| VictoriaMetrics | 1.148.0 | `victoria-metrics-linux-<arch>-v1.148.0.tar.gz` | `github.com/VictoriaMetrics/VictoriaMetrics`, tag `v1.148.0` |
| vmagent | 1.148.0 | `vmutils-linux-<arch>-v1.148.0.tar.gz` | as above |
| VictoriaLogs | 1.52.0 | `victoria-logs-linux-<arch>-v1.52.0.tar.gz` | `github.com/VictoriaMetrics/VictoriaLogs`, tag `v1.52.0` |
| vlagent | 1.52.0 | `vlutils-linux-<arch>-v1.52.0.tar.gz` | as above |
| Grafana OSS | 12.4.6 | `grafana_12.4.6_<arch>.deb` | `dl.grafana.com/oss/release/` |
| VictoriaLogs datasource plugin | 0.30.1 | `victoriametrics-logs-datasource-v0.30.1.tar.gz` | `github.com/VictoriaMetrics/victorialogs-datasource`, tag `v0.30.1` |

The plugin is on a version line of its own, unrelated to the VictoriaLogs release it
queries. Its archive carries the backend binaries for every platform, so its name
omits `<arch>`; the remaining five are architecture-specific.

Every artefact name is derived from its component, architecture, and pinned version
alone, so no artefact needs a second literal to locate it. The plugin carries one for
a different purpose — verification rather than location — for the reason given below.

The two agent tarballs carry more than the agent: `vmutils` also contains `vmalert`,
`vmalert-tool`, `vmauth`, `vmbackup`, `vmrestore`, and `vmctl`, and `vlutils` also
contains `vlogscli`. Only `vmagent-prod` and `vlagent-prod` are unpacked (§10.2);
installing the remainder would place software on the host that no part of this
specification uses.

Checksums are published in three different forms, and the fetch step in §10.2 verifies
every artefact before it enters an image. The VictoriaMetrics projects publish one
`<asset-basename>_checksums.txt` file per release asset, listing the tarball and each
binary within it as `<digest>  <filename>`. Grafana publishes a `<package>.sha256`
file containing the bare digest with no filename beside it. The plugin publishes
`<asset-basename>_checksums_tar.gz.txt`, whose single entry names the file as
`dist/<artefact>` rather than by the artefact's own name.

The plugin's published digest is SHA-1, which is not an acceptable basis for deciding
that a downloaded artefact is the published one: chosen-prefix collisions against
SHA-1 have been demonstrated since 2019. Its SHA-256 is pinned instead in `deploy/build-scripts/_artefacts.sh.j2`, and is
the digest of record for that artefact:

```
victoriametrics-logs-datasource-v0.30.1.tar.gz
sha256  328e4fd2057de057e3646fdcfcefd43e62005b99b9677489fa64c77c26395946
```

Changing the plugin version requires this literal to be updated in the same edit. The
fetch fails and the build stops if it is not, so the two cannot drift apart unnoticed.

The VictoriaLogs datasource is the only plugin either image carries. Grafana installs
four applications of its own at first start unless prevented —
`grafana-exploretraces-app`, `grafana-lokiexplore-app`, `grafana-metricsdrilldown-app`
and `grafana-pyroscope-app` — fetched from `grafana.com` at whatever version it
resolves. That is incompatible with this section on three counts: the versions are not
pinned and the artefacts not checksummed, so identical inputs can produce different
images; the fetch reintroduces the dependency on the plugin catalogue that §10.2
unpacks the VictoriaLogs plugin from an archive to avoid; and the applications fall
outside the image's input hash, so a change in what the catalogue serves rebuilds
nothing and is reported nowhere. None of the four has a backend in this deployment in
any case — there is no Loki, no Pyroscope and no tracing.

`grafana.ini` therefore sets `[plugins] preinstall_disabled = true` (§9). An empty
`preinstall` list does not achieve this: Grafana ships that setting empty in its own
`defaults.ini` and installs the four regardless, the list being defaulted in its code
rather than in its configuration. Because that behaviour is a product internal and may
change with a version, the build scripts assert the outcome rather than the setting,
failing if the plugin directory holds anything beyond the pinned datasource (§10.2).

The plugin archive carries a `MANIFEST.txt` signed by VictoriaMetrics, so Grafana
loads it without an unsigned-plugin allowance and none appears in `grafana.ini` (§9).
Grafana records the signature level as `commercial`. That describes who signed the
plugin and under which Grafana signing agreement; it says nothing about the licence,
which is Apache 2.0 as recorded above. The manifest sets no `rootUrls` restriction, so
the signature remains valid at whichever address each instance serves.

Each VictoriaMetrics and VictoriaLogs release also carries `-cluster`, `-enterprise`,
and `-enterprise-cluster` variants at the same version. The element that matters is
`-enterprise`: those builds are separately licensed and would breach the open-licence
rule. The `-cluster` build is Apache 2.0 like the unsuffixed one and is excluded on
different grounds — it is the distributed topology of `vminsert`, `vmselect`, and
`vmstorage`, where §2 specifies the single-node store. Grafana's `/oss/release/` path
serves the AGPLv3 build; the enterprise package is published under a separate path at
the same version.

### 2.2  Artefact cache

Artefacts are downloaded on the build host into `deploy/.artefact-cache/`, verified,
and pushed into the build container. They are not downloaded inside the container:
the cache would be defeated, the transfer repeated on every image build, and the
verification would move inside the environment being provisioned rather than
preceding it. The cache is excluded from version control.

The published digest is retrieved once, at first download, and retained beside the
artefact, so that a later build verifies the cached copy without a network round trip
and a rebuild succeeds offline. This detects a truncated, corrupted, or locally
altered cache entry. It does not detect a compromised upstream release: that would
require the digest itself to be pinned in this document, which is a stronger
guarantee than the two-year support horizon of §2.1 warrants and is recorded here as
a deliberate limit rather than an oversight.

The VictoriaLogs datasource plugin is the one exception, and by necessity rather than
preference: its publisher offers only a SHA-1 digest, so §2.1 pins its SHA-256 in the
document. For that artefact alone, a substituted upstream release is detected.

Each cached artefact is part of the input hash that governs selective rebuild, so a
version change in §2.1 rebuilds both images.

**Grafana 12.4 rather than 13.x.** 12.4 is the final minor of the 12 series and so
carries Grafana's fifteen-month support window, ending 24 May 2027 — longer than
either 13.0 (9 January 2027) or 13.1 (20 March 2027). It also precedes the two 13.0
changes that would otherwise reach this deployment: the removal of the
`grafana-server` command in favour of `grafana server`, and the migration of
dashboards and folders to unified storage. Upgrading a historian on a fifteen-month
cycle is the realistic posture for a port's operational-technology team; adopting a
major release published weeks earlier is not.

`hist-dmz-01` runs no ingestion agent. It is a pure receiver for vmagent's and
vlagent's remote-write pushes from `hist-01`, and a query target for Grafana and the
reconciliation user — consistent with the constraint that only `hist-01` may push into
the DMZ (`digital.md §1.2`).

---

## 3  Network interfaces

### 3.1  `hist-01` (OT, 10.40.0.11)

| Layer | Endpoint | Protocol | Purpose |
|---|---|---|---|
| vmagent — inbound | `10.40.0.11:8429` | HTTPS (VictoriaMetrics import format) | ATG metric pushes from `fms-01` |
| vlagent — inbound (structured) | `10.40.0.11:9429` | HTTPS (JSON-line) | Job/transaction/BDN event pushes from `fms-01` |
| vlagent — inbound (syslog) | `10.40.0.11:6514` | Syslog / TLS (RFC 5425) | Login and card-table event pushes from `fms-01` |
| Grafana — inbound | `0.0.0.0:443` | HTTPS | Browser access for an OT-zone admin (human; not consumed by the HMI application — see §7) |
| OpenSSH server — inbound | `10.40.0.11:22` | SSH | Systems administration from `dmz-bastion-01` |
| VictoriaMetrics — local only | `127.0.0.1:8428` | HTTP | vmagent's local remote-write target; Grafana's metrics datasource |
| VictoriaLogs — local only | `127.0.0.1:9428` | HTTP | vlagent's local remote-write target; Grafana's logs datasource |
| vmagent → `hist-dmz-01` | `hist-dmz-01:8428` | HTTPS | Metric replication (§6) |
| vlagent → `hist-dmz-01` | `hist-dmz-01:9428` | HTTPS | Event replication (§6) |
| NTP client | `10.30.0.20:123`, via `fw-ot-dmz` → `fw-dmz-it` (two-hop) | NTP / UDP | Time sync to chrony on `corp-it` (digital.md §1.3) |

The OpenSSH server authenticates the systems-administration account using the administrator's SSH key, with the settings of `digital.md §4.6`.
The account uses `sudo` for maintenance that requires elevated privileges, and its `sudoers` rule carries `NOPASSWD` (`digital.md §4.6`).

Every inbound and outbound path above that crosses the loopback boundary is a TLS
connection, each presenting a certificate signed by the RavensPort internal
certificate authority (`digital.md §2.7`, `§8` decision 7): vmagent and vlagent
present `hist-01`'s certificate to `fms-01` on their inbound listeners, and to
`hist-dmz-01` when acting as clients on the replication leg (§6). Local-only traffic
to the loopback stores is unaffected — no boundary is crossed and no certificate is
presented. `hist-01` has a `resolve: [hist-dmz-01]` entry (`deployment.md §5.2`) so
that the replication targets above can be addressed, and validated, by hostname
rather than IP address, consistent with every other TLS client in the deployment.

The inbound rows above are addressed by IP because the table documents `hist-01`'s
own listening interface, the same convention every host's interface table in this
repository uses regardless of TLS — it is not the string a client connects with.
`fms-01` reaches vmagent and vlagent via the hostname `hist-01` (`fms.md §3`, §6),
which its own `resolve:` entry resolves to this address; TLS then validates the
certificate's SAN against that hostname, never against the IP the connection
happened to resolve to. The `hist-dmz-01` rows are written as a hostname instead
because those describe `hist-01` acting as a client, where the hostname is both the
configured value (§6) and the thing validation depends on.

### 3.2  `hist-dmz-01` (DMZ, 10.20.0.20)

| Layer | Endpoint | Protocol | Purpose |
|---|---|---|---|
| VictoriaMetrics — inbound | `10.20.0.20:8428` | HTTPS | Metric replication from `hist-01`'s vmagent |
| VictoriaLogs — inbound | `10.20.0.20:9428` | HTTPS | Event replication from `hist-01`'s vlagent |
| VictoriaMetrics — local only | `127.0.0.1:18428` | HTTP | Grafana's metrics datasource |
| VictoriaLogs — local only | `127.0.0.1:19428` | HTTP | Grafana's logs datasource |
| Grafana — inbound | `0.0.0.0:443` | HTTPS | Reconciliation user's end-of-period audit (`recon-01`, via `fw-dmz-it`) |
| NTP client | `10.30.0.20:123`, direct via `fw-dmz-it` (one-hop) | NTP / UDP | Time sync to chrony on `corp-it` (digital.md §1.3) |

Each store binds two listeners rather than one (§9.2): the zone address, TLS-only,
on its standard port, for `hist-01`'s replication traffic, and a second,
plain-HTTP loopback listener on a dedicated port for Grafana's own local
queries — the same split `hist-01`'s stores have between their (single) loopback
listener and the agents fronting them. The second listener needs a port of its
own rather than sharing the first's: `0.0.0.0` and `127.0.0.1` cannot both be
bound on the same port, so one store now serving two callers with different
security postures — a cross-zone write that must be encrypted, and a same-host
read that crosses no boundary at all — needs two ports as well as two addresses.

`hist-dmz-01` presents no inbound ingestion surface reachable from any host other than
`hist-01`; the firewall permits only that one flow into the DMZ for log-server traffic
(`digital.md §1.2`, `deployment.md §4.2`).

---

## 4  Data model

Every field below is a direct translation of a value the FMS already holds
(`fms.md §8`); the log server introduces no new facts, only a second, time-series/log-
oriented representation of them. The one class of series not derived from the FMS is
`hist-01`'s own operational telemetry, collected under §6.1 and held on that host
alone.

### 4.1  VictoriaMetrics — ATG time series

Source: `fms.md §5.1` (ATG poller) and `§8.6` (`atg_readings` table). One push per poll cycle of `FMS_ATG_POLL_INTERVAL`.

| Metric | Type | Labels | Value | Source field |
|---|---|---|---|---|
| `atg_tank_volume_litres` | gauge | `tank_id="01"` | `volume_l` | `atg_readings.volume_l` |
| `atg_tank_temperature_celsius` | gauge | `tank_id="01"` | `temperature_c` | `atg_readings.temperature_c` |
| `atg_alarm_active` | gauge (0/1) | `tank_id="01"`, `alarm="bund_leak"` | 1 on latch, 0 on clear | `atg_readings.alarm_flags` transition (i101 code `030301`), per `fms.md §5.1` |

`atg_alarm_active` is written only on transition, not every cycle (`fms.md §5.1`), so
its series is sparse. Grafana panels must use "last observed value" interpolation, not
linear interpolation, to render it as a step chart. Labels are low-cardinality by
construction (`tank_id` is always `"01"` in this scenario) — kept for realism and
forward-compatibility with a multi-tank site, not because cardinality demands it.

### 4.2  VictoriaLogs — structured domain events (JSON-line)

Source: `fms.md §8.3` (`jobs`) and `§8.4` (`transactions`). One event per lifecycle
step described in `digital.md §4.1`–`§4.3`.

Common envelope on every record:

| Field | Description |
|---|---|
| `_time` | VictoriaLogs' reserved timestamp field |
| `_msg` | Short human-readable summary, e.g. `"dispense transaction closed"` |
| `event_type` | `job_opened` \| `transaction_open` \| `transaction_closed` \| `bdn_linked` \| `job_cancelled` |
| `job_id` | UUID, matches FMS `jobs.job_id` |
| `type` | `dispense` \| `fill` |
| `operator` | Username |
| `session_id` | UUID, matches FMS `jobs.session_id` and the `session_id` carried on the corresponding `LOGIN` event (§4.3) |
| `counterparty` | Vessel IMO number or tanker registration |
| `grade` | Fuel grade |

`session_id` is the field that joins this store's two classes of record. It is
projected from the job onto every event belonging to it, so any bunkering event
can be traced to the `LOGIN` that authorised it without a second lookup.
`operator` names a user; `session_id` establishes that a specific job was created
under an authenticated session, which is what narrative §5's "only authenticated
transactions occur" asserts and what the end-of-period audit reads it for (§7).

**The same value is held under two different field names.** It is `session_id`
here and `fms@99999.session_id` on the `LOGIN` event, because §4.3's
structured-data parameters are stored under their identifier. Any reader
correlating the two classes — a dashboard, a query, or the audit — must address
both names; one alone matches a single class and returns nothing from the other.

Type-specific fields:

| `event_type` | Additional fields | FMS source |
|---|---|---|
| `job_opened` | `atg_volume_at_open_l`, `created_at` | `jobs` row at creation (`digital.md §4.1` step 4) |
| `transaction_open` (dispense only) | `authorisation_id`, `preset_volume_l`, `atg_volume_at_open_l`, `opened_at` | Dispense start (`digital.md §4.2` step 2) |
| `transaction_closed` (dispense) | `transaction_id`, `authorisation_id`, `preset_volume_l`, `start_totaliser_pulses`, `end_totaliser_pulses`, `volume_dispensed_l`, `temperature_c`, `density_kg_per_l`, `temp_corrected_volume_l`, `atg_volume_at_open_l`, `atg_volume_at_close_l`, `opened_at`, `closed_at` | `transactions` dispense-close row (`fms.md §8.4`) |
| `transaction_closed` (fill) | `transaction_id`, `ticket_number`, `delivered_volume_l`, `temperature_c`, `density_kg_per_l`, `temp_corrected_volume_l`, `supplier`, `tanker_registration`, `atg_volume_at_open_l`, `atg_volume_at_close_l`, `opened_at`, `closed_at` | `transactions` fill-close row |
| `bdn_linked` | `transaction_id`, `references_transaction_id`, `bdn_number` | `transactions` BDN-linkage row (`fms.md §7.4`, `POST .../bdn`) |
| `job_cancelled` | `cancelled_by`, `cancelled_session_id`, `cancelled_at` | `job_cancellations` row (`fms.md §8.5`, `DELETE /api/v1/jobs/{job_id}`) |

`transaction_open` is emitted for dispenses only. A fill has no operator-initiated start to record — it proceeds under the road tanker's own pump (`digital.md §4.3` steps 2–4) and the FMS learns of it only through the ATG level and the transcribed delivery ticket. 
Its `opened_at` on `transaction_closed` is accordingly the start time printed on the ticket and transcribed at close (`fms.md` §7) — the same instant the paper ticket carries as `delivery_commenced_at` (`sim.md` §8.3). 
It carries no `transaction_id`, because no `transactions` row exists
yet: the row is written in full when the preset volume is reached (`fms.md §5.2`).
Correlating an open with its close is therefore by `job_id`, and `authorisation_id` is
the field common to both.

It carries `preset_volume_l`, which the dispense `transaction_closed` repeats, so
the audit's reconciliation of preset against delivered volume can be made from the
close alone. An open with no corresponding `transaction_closed` is the record of a
dispense that began and did not complete, and is not recoverable from the other
events.

`job_cancelled` names who cancelled the job under fields of its own. The envelope's
`operator` and `session_id` describe the job's creation, and a job may be cancelled
by another user or under a later session. `cancelled_session_id` joins the cancel to
its `LOGIN` as `session_id` joins the creation. Only an `active` job can be cancelled
(`fms.md §7.4`), so a `job_cancelled` follows a `job_opened` and never a
`transaction_open`.

**The two ATG volumes are temperature-compensated**, being the `tc_volume_l`
figure `i201` returns rather than the raw volume (`fms.md §5.1`, `§8.4`;
`digital.md §4.2` step 2). Stating it here matters because the audit's ±1% check
compares the ATG delta against the meter: with the ATG side already corrected,
only the meter side needs correcting, and an audit that corrected both would
apply the adjustment twice. Under the initial 
assumption that the temperature
is a constant 15 °C, the adjustment is trivial: the ATG 
console returns
`TC Volume = Volume`(`tls-protocol.md §7`).

`temperature_c`, `density_kg_per_l` and `temp_corrected_volume_l` are the
observed fuel temperature, the density at 15 °C, and the meter volume corrected
to that reference. They mirror the paper artefact (`sim.md §8.1`, `§8.2`), which
is what lets the audit compare them rather than take the FMS's arithmetic on
trust. MARPOL Annex VI Appendix V requires the density on a bunker delivery
note; it does not require the temperature (`sim.md §8.4`). Carrying the inputs alongside the corrected
figure is deliberate: the correction is then checkable rather than merely
reported.

### 4.3  VictoriaLogs — security and audit events (syslog, RFC 5424)

Source: FMS authentication (`fms.md §4`) and card table mutation (`fms.md §7.5`,
`§8.2`). Envelope: `APP-NAME=fms`, `HOSTNAME=fms-01`, facility user (1), structured-data ID
`fms@99999` (an illustrative placeholder, not IANA-registered — this is a synthetic
environment).

| MSGID | Severity | MSG | Structured data | Trigger |
|---|---|---|---|---|
| `LOGIN` | informational (6) | `login` | `username`, `role`, `session_id`, `source_address`, `result="success"` | Successful login (`fms.md §4`) |
| `LOGIN_FAILED` | warning (4) | `login failed` | `username`, `role_attempted`, `source_address`, `reason="invalid_credentials"\|"invalid_rfid"\|"missing_rfid"` | Failed admin hash comparison or LDAP bind; an inactive or unknown RFID card; or an operator login supplying no card number (`fms.md §4`) |
| `CARD-CHANGE` | notice (5) | `card change` | `card_id`, `card_number`, `label`, `action="enrol"\|"revoke"` | Card table mutation (`fms.md §8.2`) |

**Every message carries a MSG.** RFC 5424 permits it to be absent, but
VictoriaLogs requires a message field and substitutes its own placeholder text —
a reference to its documentation — when one is missing. Without a MSG, every
authentication and card-change record in the historian would display that
placeholder as its message in Grafana, to both the administrator reading the
audit trail and the reconciliation user looking for tampering (§7). The MSG is
the event in words, matching the `_msg` convention §4.2 already uses for
JSON-line events, so the two stores read consistently. The detail stays in the
structured data, which is where it can be queried; the MSG is not a second copy
of it.

**Structured-data parameters are stored under their identifier.** VictoriaLogs
prefixes each parameter with the structured-data identifier, so `username`
arrives as `fms@99999.username` and is queried by that name. `MSGID`, severity,
facility, `APP-NAME` and `HOSTNAME` are not prefixed — they become `msg_id`,
`severity` (numeric, as a string), `facility` with `facility_keyword`,
`app_name` and `hostname`. This governs how a dashboard or a reconciliation
query addresses these events, not only how the tests do.

`session_id` is the one parameter also carried by the §4.2 events, where it is
unprefixed. It is the field the two classes correlate on, and the prefixing rule
means it is addressed by a different name on each side.

`reason` on `LOGIN_FAILED` distinguishes the independent checks in `fms.md §4` even
though the FMS's HTTP response stays a generic `401` — the extra granularity exists only
in the audit trail, which is the realistic pattern (an application's security log is
routinely more detailed than its API error response).

The three values are not interchangeable to a reader looking for intent.
`invalid_credentials` is a failed password, whether against the admin hash or the LDAP
bind. `invalid_rfid` is a card that was presented and rejected — revoked, or never
enrolled. `missing_rfid` is an operator login supplying no card number at all, rejected
before any lookup occurs: a client that never held a card, rather than one holding a
card it should not.

### 4.4  What is deliberately not modelled

Job records, session rows, and card table rows remain the FMS's own PostgreSQL tables
(`fms.md §8.1`–`§8.3`) as the primary, authoritative store; the log server holds a
second, append-only *event* representation of the same facts for the reasons in §1
(replica-based audit, reconciliation). The two are not kept in lock-step by a shared
transaction — the FMS writes its own row first, then pushes the corresponding event —
so a divergence between the FMS's local record and the log server's event log is
possible and is itself a weaker-but-real analogue of the replica-divergence detection
signal in §1. Two causes produce it. An FMS crash between the two writes is the narrow
one. The routine one is a log-server outage lasting long enough to fill the FMS's
256-event queue, at which point the FMS drops events rather than stalling operations
(`fms.md §5.4`, `narrative.md §5`) — so a gap here is expected behaviour under an
outage, not evidence of tampering on its own. `digital.md §6.3` sets out what
distinguishes the two.

---

## 5  Ingestion (`fms-01` → `hist-01`)

Three push paths, one per data class (§4), all originating from the FMS and landing
on `hist-01` (§3.1):

1. **ATG metrics.** The FMS's ATG poller (`fms.md §5.1`) POSTs one VictoriaMetrics
   import-format JSON line per metric to vmagent's import endpoint,
   `https://10.40.0.11:8429/api/v1/import`, once per poll cycle:
   ```
   {"metric":{"__name__":"atg_tank_volume_litres","tank_id":"01"},"values":[42350.0],"timestamps":[1752761525000]}
   ```

2. **Job/transaction events.** The FMS's log-server pusher (`fms.md §5.4`) POSTs one
   JSON object per line to vlagent's JSON-line endpoint,
   `https://10.40.0.11:9429/insert/jsonline`, at each lifecycle step (`digital.md
   §4.1`–`§4.3`), carrying the fields in §4.2.

3. **Login and card-table events.** The FMS sends an RFC 5424 syslog message,
   LF-terminated, over a persistent TLS connection (RFC 5425) to vlagent's syslog
   listener, `10.40.0.11:6514/tcp`, on every login attempt and every card table
   mutation, carrying the fields in §4.3.

All three paths present `hist-01`'s CA-signed certificate to the FMS and are
validated by it against the CA root already in `fms-01`'s system trust store
(digital.md §2.7); none falls back to an unauthenticated transport. Port 6514 is the
IANA-registered syslog-over-TLS port (RFC 5425), distinct from the plain-TCP port
514 the FMS used before the internal certificate authority was available.

Paths 1 and 2 are specified on the FMS's side in `fms.md §5.4`, and their target
configuration in `fms.md §6` (`FMS_VMAGENT_URL`, `FMS_VLAGENT_JSONLINE_URL`).

Path 3 is specified in `fms.md §5.4` and `§6`
(`FMS_VLAGENT_SYSLOG_HOST`/`FMS_VLAGENT_SYSLOG_PORT`). The FMS authentication handling, documented in `fms.md §4`, emits `LOGIN` and `LOGIN_FAILED` and the card endpoints (`fms.md §7.5`)
emit `CARD-CHANGE`. A rejected login is recorded here and nowhere else — the `sessions`
table holds successful sessions only and the HTTP response is a generic `401` — so the
`reason` carried on `LOGIN_FAILED` is the only place the four rejection paths are
distinguishable.

---

## 6  Replication (`hist-01` → `hist-dmz-01`)

vmagent and vlagent on `hist-01` are each configured with two `-remoteWrite.url`
targets: their own host's local VictoriaMetrics/VictoriaLogs instance, and the
corresponding service on `hist-dmz-01`. Every sample or event ingested on `hist-01` is
therefore written to both stores as part of the same ingest operation — continuous,
push-based replication with no separate batch or snapshot step, satisfying `digital.md
§4.5`'s replication requirement uniformly across metrics, structured domain events,
and syslog-sourced audit events. vlagent normalises syslog input into VictoriaLogs'
native format before forwarding, so `hist-dmz-01` needs no syslog listener of its own
(§2).

The local target stays plain HTTP — no zone boundary is crossed, and nothing outside
`hist-01` can reach it (§3.1). The `hist-dmz-01` target is HTTPS, addressed by
hostname rather than IP so that the certificate's hostname SAN validates (§3.1):

```
# vmagent
-remoteWrite.url=http://127.0.0.1:8428/api/v1/write
-remoteWrite.url=https://hist-dmz-01:8428/api/v1/write

# vlagent
-remoteWrite.url=http://127.0.0.1:9428/insert/native
-remoteWrite.url=https://hist-dmz-01:9428/insert/native
```

Neither agent needs a `-remoteWrite.tlsCAFile` flag for the `hist-dmz-01` target: both
verify the presented certificate against the system trust store by default, which
already holds the CA root on every node (digital.md §2.7), the same default behaviour
`fms-01`'s pushes into `hist-01` rely on (§5). `hist-dmz-01`'s VictoriaMetrics and
VictoriaLogs terminate TLS directly on their own listeners (`-tls`, `-tlsCertFile`,
`-tlsKeyFile`), presenting the same CA-signed certificate `hist-dmz-01`'s Grafana
instance uses (§7, §9.2) — one certificate per host, reused across every
network-facing service on it.

Each `-remoteWrite.url` target gets its own on-disk buffer
(`-remoteWrite.tmpDataPath`), so a transient interruption reaching `hist-dmz-01` (a
firewall blip, a `hist-dmz-01` restart) does not cause data loss — buffered data is
delivered once the target is reachable again. This is what keeps `digital.md §6.3`'s
divergence signal meaningful rather than noisy: a routine, short-lived gap resolves
itself automatically, so a *persisting* divergence is what should be treated as
suspicious.

Both agents mask the remote-write target's address in their own metrics —
`-remoteWrite.showURL` defaults to false — so the `url` label on each agent's own
`/metrics` output reads `1:secret-url` and `2:secret-url`, the ordinals following the
order of the two `-remoteWrite.url` flags above. The masking is retained rather than
lifted: the agents' metrics ports bind every address so that `fms-01` can push to them
(§9.1), and unmasking would publish `hist-dmz-01`'s address to every host in the
operational-technology zone.

### 6.1  Self-monitoring collection

`hist-01` collects its own operational telemetry, which is what §7.1's operations
dashboard displays. Each of the four components exposes a Prometheus-format
`/metrics` endpoint, and VictoriaMetrics scrapes all four over loopback from a
configuration at `/etc/victoria-metrics/scrape.yml`:

```
127.0.0.1:8428   victoria-metrics
127.0.0.1:8429   vmagent
127.0.0.1:9428   victoria-logs
127.0.0.1:9429   vlagent
```

One scrape job per component, named for it, so that the `job` label distinguishes
the source of every collected series. All four are needed rather than the stores
alone: uptime and disk usage are exposed by the stores, but ingestion counts and
remote-write queue depth are exposed by the agents. The scrape interval is the
upstream default of 60 seconds, stated in the file rather than left implicit
because it sets the storage cost of §8's retention; nothing §7.1 displays resolves
faster.

**These series are the one exception to §6's replication, and are held on `hist-01`
alone.** They are the operational-technology host's internal telemetry, and they do
not cross into the demilitarised zone. The exception is structural rather than
configured: the scrape runs on `victoria-metrics`, which stores what it collects
locally and originates no remote-write. Placing it on `vmagent` instead would send
every scraped sample to both of §6's targets, the second being `hist-dmz-01`.
Suppressing that would require a per-target relabelling rule, whose failure mode is
the telemetry reaching the replica silently; there is no such failure mode here.

---

## 7  Query and serving interfaces (Grafana)

Grafana is human-facing only; it is not consumed programmatically by the HMI or any
other component. Both routes into it are a person at a keyboard: the provisioned
dashboards (§7.1), and, on `hist-dmz-01`, the datasource proxy through which the
reconciliation user's end-of-period audit retrieves the period's records from a terminal
rather than from the dashboard (`recon.md` §6.1).

**The audit retrieves both classes of event held in this store** — the bunkering
events of §4.2 and the authentication and card-table events of §4.3
(`digital.md §4.5`). The two are joined on `session_id`, and §4.3's field-naming
rules are what a query addressing the syslog-shaped records depends on. The
metric store is not on the audit path; the tank volumes each transaction opened
and closed at are carried on the §4.2 events. What the audit does with each class
is specified in `sim-impl.md`.

The dock operator's HMI (`hmi.md`)
draws its current-state view entirely from the FMS's in-memory API (`fms.md §7.3`) and
does not query the log server. The interface `digital.md §2.2` describes as "serves read-only HTTPS to HMI"
refers to network reachability from the OT zone — an admin can point a browser at
`https://hist-01` from the `hmi-01` workstation, or any other OT host — not to a call
made by the HMI application itself.

- **`hist-01`**: Grafana's local user store holds a single admin account (`digital.md
  §2.2`, "Users: admin"), used for operational troubleshooting.
- **`hist-dmz-01`**: Grafana's local user store holds the reconciliation user account,
  matching `digital.md §5.1`/`§5.2` ("the reconciliation user authenticates directly
  against the log-server's own built-in user store"). Grafana's built-in
  authentication *is* that store.

Each instance holds exactly one account, and that account administers its own
instance. It is seeded both as a Grafana server administrator (`user.is_admin`)
and as an Administrator of the default organisation (`org_user.role`). Both are
required: server administrator alone leaves the account without an organisation
membership, and an account with no organisation membership cannot list the
datasources provisioned below. No second account exists on either instance to
hold those rights instead.

For `hist-dmz-01` this grants no route into `hist-01`. 

Both instances provision two datasources and one dashboard at image-build time (§7.1).
The datasources (`/etc/grafana/provisioning/datasources/`) are:

- a Prometheus-compatible datasource pointed at the local VictoriaMetrics
  (`http://127.0.0.1:8428`), since VictoriaMetrics implements the Prometheus HTTP API
  directly; `uid` is `monthly-audit-metrics`;
- a VictoriaLogs datasource pointed at the local VictoriaLogs
  (`http://127.0.0.1:9428`), using the official VictoriaLogs Grafana plugin pinned in
  §2.1, whose `type` is `victoriametrics-logs-datasource`; `uid` is
  `monthly-audit-logs`.

Both `uid`s are set explicitly in the provisioning files rather than left to Grafana,
which generates a random one when the field is absent. They are part of the interface
this host presents: the audit addresses the event-store datasource by `uid` in the
proxy path (`recon.md` §6.1) and the provisioned dashboard references both, so a
generated value would differ per build and neither address could be written down. The pair is identical on both instances, so one provisioning
file serves both images.

On `hist-01`, both stores have one listener each, so the datasources address them
at `http://127.0.0.1:8428`/`:9428` directly. On `hist-dmz-01`, each store answers
two listeners instead (§9.2): the zone address, TLS, on the standard port
(`8428`/`9428`), for `hist-01`'s replication traffic, and loopback, plain HTTP, on
a second, dedicated port — `0.0.0.0` and `127.0.0.1` cannot both be bound on the
same port, so the two callers cannot share one. These datasources therefore
address `http://127.0.0.1:18428`/`:19428`, ports reserved for this purpose alone
and not reachable from any other host.

Both terminate TLS directly on port 443 using a certificate signed by the RavensPort
internal certificate authority, generated at image-build time (§10) — the same CA that
signs `fms-01` and `hmi-01`'s leaf certificates (`digital.md §2.7`, `§8` decision 7).
The certificate carries a DNS Subject Alternative Name matching the host's hostname
only, no IP SAN: a human reaches either instance by browser, so the browsing host must
resolve the target by name — `recon-01` resolves `hist-dmz-01` and `hmi-01` resolves
`hist-01` (`deployment.md §5.2`) — the same mechanism the HMI binary's own HTTPS client
already uses to reach `fms-01`.

Grafana's admin (`hist-01`) and reconciliation-user (`hist-dmz-01`) credentials are set
deterministically at build time from `world.yaml`'s `identities.hist_admin` and
`identities.reconciliation_user` entries (`deployment-config.md §3`), matching the
reproducibility requirement in `deployment.md §9`. Each is projected to its host's
Grafana instance as a build-time-seeded user row, hashed with Grafana's own scheme —
PBKDF2-HMAC-SHA256, 10,000 iterations, a 50-byte derived key, hex-encoded
(`EncodePassword` in Grafana's `pkg/util/encoding.go`) — using a deterministic salt
derived from `crypto.password_salt_seed` (`deployment-config.md §4`, §6).

### 7.1  Provisioned dashboards

Each instance carries one dashboard, provisioned from files at image-build time
alongside the datasources (`/etc/grafana/provisioning/dashboards/`). The files are part
of each image's input hash, so editing a dashboard rebuilds the image and the deployed
view cannot drift from the repository.

File provisioning marks a dashboard read-only in the user interface. Both accounts
remain free to create dashboards of their own beside it: the provisioner owns only the
files it manages, so a saved view and a provisioned one coexist. The provisioned
dashboard is a starting point, not the limit of what either user may do.

| Host | Dashboard | Purpose |
|---|---|---|
| `hist-dmz-01` | Bunkering operations | The month's dispense and fill jobs in time order, for the reconciliation user's audit (`digital.md §5`) |
| `hist-01` | Log server operations | Ingestion, replication and store health, for the administrator's troubleshooting |

**`hist-dmz-01` — bunkering operations.** A time-ordered table of the selected period's
job and transaction events (§4.2), with tank volume (§4.1) plotted over the same period.
Authentication and card-table events (§4.3) reach both hosts but are not on this
dashboard. Default period: last 30 days. No refresh.

**`hist-01` — log server operations.** Ingestion rate per source, remote-write queue
depth and blocks delivered for each of §6's two targets per agent, and the four
components' uptime and the two stores' disk usage against §8's retention. Its data is
§6.1's self-monitoring collection. Quantities only: no threshold, colour coding or
statement about whether a value is suspicious. Divergence between the two hosts is a
detection signal (`digital.md §6.3`), and deciding that a value indicates one belongs
to whatever implements that detection, not to a dashboard. Default period: last 6 hours.
Refresh every minute.

Panels on `atg_alarm_active` must use last-observed-value interpolation rather than
linear. §4.1 writes that series on transition only, so it is sparse by construction and
a linear interpolation would draw a ramp between two states that were each held
constant.

Both dashboards render timestamps in Universal Coordinated Time rather than the
browser's zone. The retained record is the evidence for a MARPOL-period audit (§8), and
two readers in different zones must not see different timestamps from the same row; the
administrator investigating an ingestion gap and the auditor reading the records it left
behind should also be reading one clock.

---

## 8  Retention

3-year retention on both hosts, aligned to the MARPOL record-keeping period
(narrative.md §5, digital.md §6.1):

```
-retentionPeriod=3y
```

applied identically to VictoriaMetrics and VictoriaLogs on both hosts. `hist-dmz-01`
carries the same retention as `hist-01` — for the records it holds, the replica is a
full mirror rather than a summarised or shorter-lived copy — since the reconciliation
user's end-of-period audit (narrative.md §3) must be able to reach back over the full
retained period. §6.1's self-monitoring series are the one class it does not hold at
all.

The same period applies to those self-monitoring series on `hist-01`, and they
dominate its stored volume: the FMS pushes three series (§4.1), whereas the four
scraped components between them expose on the order of a thousand. The period is not
reduced for them. Neither store expresses retention per series, and the figures are
useful for troubleshooting only over a period that includes whatever is being
investigated.

---

## 9  Service configuration

Systemd unit `ExecStart` flags, one row per component. These products are configured
natively via command-line flags (VictoriaMetrics/VictoriaLogs/vmagent/vlagent) or an
INI file (Grafana), matching real-world deployment practice for each product — not
forced into the FMS/HMI's `EnvironmentFile` pattern, which those products do not use.

### 9.1  `hist-01`

| Component | Key flags |
|---|---|
| `victoria-metrics` | `-storageDataPath=/var/lib/victoria-metrics -httpListenAddr=127.0.0.1:8428 -retentionPeriod=3y -promscrape.config=/etc/victoria-metrics/scrape.yml` |
| `vmagent` | `-httpListenAddr=0.0.0.0:8429 -tls -tlsCertFile=/etc/hist-tls/cert.pem -tlsKeyFile=/etc/hist-tls/key.pem -remoteWrite.url=http://127.0.0.1:8428/api/v1/write -remoteWrite.url=https://hist-dmz-01:8428/api/v1/write -remoteWrite.tmpDataPath=/var/lib/vmagent` |
| `victoria-logs` | `-storageDataPath=/var/lib/victoria-logs -httpListenAddr=127.0.0.1:9428 -retentionPeriod=3y` |
| `vlagent` | `-httpListenAddr=0.0.0.0:9429 -tls -tlsCertFile=/etc/hist-tls/cert.pem -tlsKeyFile=/etc/hist-tls/key.pem -syslog.listenAddr.tcp=0.0.0.0:6514 -syslog.tls -syslog.tlsCertFile=/etc/hist-tls/cert.pem -syslog.tlsKeyFile=/etc/hist-tls/key.pem -remoteWrite.url=http://127.0.0.1:9428/insert/native -remoteWrite.url=https://hist-dmz-01:9428/insert/native -remoteWrite.tmpDataPath=/var/lib/vlagent` |
| `grafana-server` | `/etc/grafana/grafana.ini`: `[server] protocol=https, http_addr=0.0.0.0, http_port=443, cert_file=/etc/hist-tls/cert.pem, cert_key=/etc/hist-tls/key.pem` |

### 9.2  `hist-dmz-01`

| Component | Key flags |
|---|---|
| `victoria-metrics` | `-storageDataPath=/var/lib/victoria-metrics -httpListenAddr=127.0.0.1:18428,0.0.0.0:8428 -retentionPeriod=3y -tls=false,true -tlsCertFile=,/etc/hist-tls/cert.pem -tlsKeyFile=,/etc/hist-tls/key.pem` |
| `victoria-logs` | `-storageDataPath=/var/lib/victoria-logs -httpListenAddr=127.0.0.1:19428,0.0.0.0:9428 -retentionPeriod=3y -tls=false,true -tlsCertFile=,/etc/hist-tls/cert.pem -tlsKeyFile=,/etc/hist-tls/key.pem` |
| `grafana-server` | Same as `hist-01`, distinct TLS keypair and admin credential (§7) |

`-httpListenAddr` takes a comma-separated list of addresses, and `-tls`/
`-tlsCertFile`/`-tlsKeyFile` are positional lists matched to it — the same
per-listener array-flag convention `vmagent`/`vlagent` use for
`-remoteWrite.url` (§6). This lets each store serve two callers with different
security postures from one process: the zone address, on the standard port
(`8428`/`9428`), is TLS-only, for `hist-01`'s replication traffic crossing into
the DMZ; loopback, on a second, dedicated port (`18428`/`19428`), stays plain
HTTP, for Grafana's own local datasource queries (§7). The two cannot share one
port: `0.0.0.0` and `127.0.0.1` cannot both be bound simultaneously on the same
port, so the loopback listener needs a port the zone-address one is not already
holding. Without it, Grafana's datasource queries (§7) would have no plain-HTTP
endpoint to reach at all.

Every TLS-terminating component on a host presents the same certificate and key —
one CA-signed keypair per host, installed once (§10.2, §10.3) at `/etc/hist-tls/`,
following the standard Debian/Ubuntu convention for a certificate shared between
independent daemons (the `ssl-cert` group's role for `/etc/ssl/private/`, e.g. for
Postfix and Dovecot sharing one certificate). `cert.pem` is world-readable (`0644`);
`key.pem` is owned `root:hist-tls` and mode `0640`, and membership in the
`hist-tls` group is granted only to the system user of a service that actually
terminates TLS with this keypair on that host — `vmagent`, `vlagent`, and
`grafana` on `hist-01`; `victoria-metrics`, `victoria-logs`, and `grafana` on
`hist-dmz-01` (§10.2, §10.3). `hist-01`'s `victoria-metrics`/`victoria-logs`
are not members: they bind loopback with no `-tls` flags of their own, so
group membership would grant private-key access no service on that host
reads, cutting against this repository's own least-knowledge principle
(`deployment-config.md` §5). One file to rotate at each annual reissuance
(§8 decision 7), not one copy per service kept in sync by hand.

`httpListenAddr` differs between hosts: `hist-01`'s VictoriaMetrics/VictoriaLogs bind
loopback only, reached solely via their local agent, so no second listener is
needed. `hist-dmz-01` has no local agent in front of it and two callers that arrive
by different routes — `hist-01`'s agents across the zone boundary, and its own
Grafana over loopback, which the datasources of §7 address as `127.0.0.1` — so it
binds both: the zone address, TLS-only, for the first; loopback, plain HTTP, for
the second. Binding the zone address alone would serve the first and fail the
second (a plain-HTTP request against a TLS-only port); binding loopback alone
would serve the second and give `hist-01` nothing to replicate into.

### 9.3  Query visibility

Every flag not listed above is left at its upstream default, including
`-search.latencyOffset`, which VictoriaMetrics sets to 30 seconds. It moves the
evaluation time of an instant query back by that interval, so that the most
recent and possibly incomplete data is not returned. A record is therefore
queryable about thirty seconds after it is stored, not immediately.

This is retained rather than tuned. A port installs the product and runs it as
shipped; an operations team that reduces a default has a reason to, and there is
none here — no dashboard in §7 and no reconciliation in `digital.md` §5 operates
on a timescale where thirty seconds matters, and the interval is far below the
poll periods of §4.1's sources.

The consequence falls on the test suite rather than the deployment: a test that
pushes a record and reads it back must wait longer than the offset, or it will
fail against a store holding that record correctly. `hist-tests.md` §1 records
the resulting bound on the polling helper.

---

## 10  Deployment

### 10.1  Systemd units

One unit per component. `vmagent` and `vlagent` depend on their local store being up.

```
[Unit]
Description=VictoriaMetrics
After=network-online.target

[Service]
User=victoria-metrics
ExecStart=/usr/local/bin/victoria-metrics -storageDataPath=/var/lib/victoria-metrics -httpListenAddr=127.0.0.1:8428 -retentionPeriod=3y -promscrape.config=/etc/victoria-metrics/scrape.yml
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

`vmagent`, `victoria-logs`, and `vlagent` follow the same shape with the flags from
§9. `-promscrape.config` appears on `hist-01` only (§6.1); `hist-dmz-01`'s unit is
otherwise identical but for the listen address of §9.2. `grafana-server.service`
needs `AmbientCapabilities=CAP_NET_BIND_SERVICE` to bind 443, matching the precedent
set in `fms.md §9.1`. No other component in §9 binds a privileged (<1024) port:
`vlagent`'s syslog listener moved from 514 to 6514 (§5) when it gained TLS, which
removes what was its only claim to that capability.

### 10.2  Image build script for `hist-01`

The image build script (`deploy/build-scripts/hist-01.sh.j2`) assembles the
`ravensport/hist-01:latest` Incus image, replacing the `ignition-node` placeholder
(`deployment.md §3`). The script:

- Fetches the six pinned artefacts of §2.1 through the cache of §2.2, before the
  input hash is computed, and installs the VictoriaMetrics, vmagent, VictoriaLogs,
  and vlagent binaries — unpacking only `victoria-metrics-prod`, `vmagent-prod`,
  `victoria-logs-prod`, and `vlagent-prod` — and Grafana OSS from its `.deb`.
- Unpacks the VictoriaLogs datasource plugin into `/var/lib/grafana/plugins/` from the
  pinned release archive, rather than through `grafana-cli plugins install`, which
  would make every image build depend on the Grafana plugin catalogue being reachable
  and unthrottled (`deployment.md §9.1`). The archive expands to a single
  `victoriametrics-logs-datasource/` directory, whose name is the plugin identifier
  that the datasource provisioning of §7 names as its `type`.
- Confirms, after the Grafana start that creates the schema, that
  `/var/lib/grafana/plugins/` holds that plugin and nothing else, and fails the build
  otherwise. Grafana's preinstall runs at first start, asynchronously, and logs a
  failure rather than refusing to serve, so neither the rendered `grafana.ini` nor a
  healthy service establishes which plugins an image carries (§2.1).
- Creates an unprivileged system user per service (`shell: /usr/sbin/nologin`) and a
  shared `hist-tls` system group, membership in which is every TLS-terminating
  service's only route to the private key below.
- Installs the CA-signed TLS certificate and key (`build/tls/hist-01/cert.pem`,
  `key.pem`, generated by `deploy/scripts/build-ca.sh`) as a single copy at
  `/etc/hist-tls/`, following the Debian/Ubuntu `ssl-cert`-group convention for a
  certificate shared between independent daemons (§9): `cert.pem` mode `0644`
  (world-readable — the certificate is public); `key.pem` owned `root:hist-tls`,
  mode `0640`. `vmagent`, `vlagent`, and `grafana` are added to `hist-tls`;
  `victoria-metrics`/`victoria-logs` are not, since their loopback listeners on
  this host carry no `-tls` flags of their own (§9) and so never read the key.
  The certificate carries a DNS
  `subjectAltName` naming the node's hostname only — no IP SAN (digital.md §2.7, §8
  decision 7) — so a peer reaching it must resolve that hostname: `hmi-01` for
  `hist-01`'s Grafana, `recon-01` for `hist-dmz-01`'s (both instances are reached by
  browser — `hist-01` from an operational-technology workstation (§7) and
  `hist-dmz-01` by the reconciliation user), `fms-01` for `hist-01`'s vmagent and
  vlagent (§5), and `hist-01` itself for the replication leg (§6).
- Installs the CA root certificate into the host's system trust store
  (`update-ca-certificates`), the same as every other node (digital.md §2.7).
- Installs the Grafana datasource provisioning files (§7) and `grafana.ini`, and sets
  the admin credential from the `world.yaml` projection (§7).
- Installs the dashboard provider and the dashboard itself (§7.1), and confirms before
  publishing that the dashboard can be retrieved from `GET /api/dashboards/uid/…`.
  Grafana logs a rejected dashboard and carries on serving, so a syntax error, an
  unreadable file, or a provider naming the wrong directory would otherwise publish an
  image that looks healthy and carries no dashboard. The check asks the HTTP API rather
  than the database: Grafana 12.4 serves dashboards from unified storage, so the table
  a provisioned dashboard occupies is a property of the storage layer and changes with
  a version, whereas retrievability is the property being asserted.
- Installs the self-monitoring scrape configuration (§6.1) at
  `/etc/victoria-metrics/scrape.yml`, owned by the `victoria-metrics` account that
  reads it. A configuration the service cannot read fails identically to one that is
  absent: VictoriaMetrics starts, the unit stays active, and nothing is collected.
- Converts `build/network-config/hist-01.yaml` to systemd-networkd `.network` units
  and enables `systemd-networkd`.
- Installs `/etc/hosts` from `build/hosts/hist-01`.
- Enables `victoria-metrics.service`, `vmagent.service`, `victoria-logs.service`,
  `vlagent.service`, and `grafana-server.service`.
- Purges `cloud-init` from the image.

### 10.3  Image build script for `hist-dmz-01`

The image build script (`deploy/build-scripts/hist-dmz-01.sh.j2`) assembles the
`ravensport/hist-dmz-01:latest` Incus image, also replacing `ignition-node`. Identical
to §10.2 except: neither agent artefact is fetched or installed, so the replica holds
no binary capable of originating a remote-write; VictoriaMetrics and VictoriaLogs
each bind a second, TLS-terminating listener on the zone address alongside their
loopback one (§9.2) and so, unlike `hist-01`'s, join the shared `hist-tls` group
themselves; the group's members here are
`victoria-metrics`, `victoria-logs`, and `grafana` — the mirror image of
`hist-01`'s `vmagent`/`vlagent`/`grafana` (no `vmagent`/`vlagent` accounts exist
to add on this host); no scrape configuration is installed and neither store is
given `-promscrape.config` (§6.1);
and Grafana's datasource provisioning, dashboard, TLS keypair, and admin credential
(the reconciliation user, §7) are distinct from `hist-01`'s.
