# Log Server Test Plan

Test plan for the log server, `hist-01` (primary, OT zone) and `hist-dmz-01` (read-only replica, DMZ), specified in `hist.md`. One section
per behavioural area; each names the individual tests it comprises and the
requirement each targets.

---

## 0  Test harness

Tests run against real `hist-01`/`hist-dmz-01` Incus images built from
`deploy/build-scripts/`, matching the `tests/integration/fms/` convention (as
opposed to a lighter docker-compose harness) — highest fidelity, and it
exercises the actual image-build/systemd/network path rather than a proxy for
it.

The `hist-01` host's only real upstream is the FMS, which for testing is replaced by a synthetic push client. This decouples the log server's own contract — store what is
pushed, in the format specified, and replicate it — from the FMS's schedule.

**FMS push simulator** (`tests/integration/hist/stubs/fms_pusher.py`): a thin client, run from the test
runner host, offering three methods corresponding to `hist.md` §5's three
push paths:

- `push_metric(name, tank_id, value, timestamp, **labels)` — POSTs one
  VictoriaMetrics import-format JSON line to `https://hist-01:8429/api/v1/import`.
  Extra keyword arguments become labels, carrying `atg_alarm_active`'s
  `alarm="bund_leak"` label (`hist.md` §4.1).
- `push_event(event_type, **fields)` — POSTs one JSON object to
  `https://hist-01:9429/insert/jsonline`, with the envelope and type-specific
  fields of `hist.md` §4.2.
- `push_syslog(msgid, severity, **structured_data)` — sends one RFC 5424
  message, LF-terminated, over a persistent TLS connection (RFC 5425) to
  `hist-01:6514`, with the fields of `hist.md` §4.3.

All three methods validate `hist-01`'s presented certificate against
`build/tls/ca.pem`, the CA's public root (`digital.md §2.7`) — a build artefact,
not a secret, readable directly from the test runner host rather than requiring
that host to be enrolled in any node's system trust store.

**What the simulator does not establish.** It is a separate implementation of the
three push paths, and it validates against the root file where the FMS validates
against the system trust store (`fms.md` §3). Every §1–§7 assertion therefore
holds whether or not the FMS itself can deliver: a defect in the FMS's client, in
its trust, transport or framing, leaves this suite passing while nothing the FMS
pushes reaches the log server. The FMS's side is tested elsewhere: its client
against a certificate chaining to the system store root in
`fms/tests/hist_push_trust.rs`, and the deployed FMS's ATG metrics and a
syslog authentication event arriving on `hist-01` and `hist-dmz-01` in
`deploy/tests/integration/test_smoke.sh`.

There is no long-lived stub process holding state between tests, only a
client. Each test pushes its own fixture data and queries for it by a
test-local identifier (e.g. a UUID `job_id`) to avoid cross-test
interference within the same VictoriaMetrics/VictoriaLogs instance.

Because `hist-01`'s stores bind loopback-only (`hist.md` §9.1), queries
against them run via `incus exec hist-01 -- curl ...`, not directly from the
runner host. The runner joins only `vnet-ot` and has no route into the DMZ
zone either (§8), so `hist-dmz-01`'s stores are read the same way — `incus
exec hist-dmz-01 -- curl ...` — against their own plain-HTTP loopback listener
(§9.2), the one Grafana itself uses locally, rather than the TLS listener on
the zone address that `hist-01`'s replication traffic targets.

**The harness tests itself.** Two modules exercise the client and the query
transport without a deployment: `tests/integration/hist/test_00_pusher_wire_format.py`
asserts that each push method emits the wire format `hist.md` §4 specifies, and
`test_00_query_client.py` asserts that the query clients parse each store's
response shape. They are self-contained, so they run on a development machine as
well as on the deployment host, and are addressable separately as
`make -C tests/integration test-hist-harness`.

Their value is in what they separate. Every §1–§7 assertion is made through this
client, so a defect in the client presents as a failure in whichever module used
it — a push that was never correctly formed reads as a log server that failed to
store it. Testing the harness independently means a failure in §1–§7 can be
attributed to the deployment.

---

## 1  Metric ingestion

Test module: `tests/integration/hist/test_01_ingestion_metrics.py`

Targets `hist.md` §4.1 and §5 point 1.

**Reading back a freshly pushed record.** `hist.md` §9.3 leaves
`-search.latencyOffset` at the VictoriaMetrics default of 30 seconds, so an
instant query is evaluated at `now - 30s` and a record is not visible to one for
that long, however promptly it was stored. Every assertion in this section and in
§2, §3 and §4 that pushes a record and queries it back therefore goes through
`poll.poll_until_count`, whose default timeout must remain above 30 seconds.
Reducing it produces failures that are indistinguishable from ingestion being
broken: the store holds the record, with the right labels and value, and the
query returns nothing.

### `metric_push_is_queryable_on_hist01`

Push one `atg_tank_volume_litres` sample with a known value and timestamp via
`push_metric`. Query VictoriaMetrics on `hist-01` (`incus exec` + local
`/api/v1/query`) for that metric and assert the returned value and
`tank_id="01"` label match.

### `all_three_atg_metrics_are_ingested`

Push one sample each of `atg_tank_volume_litres`, `atg_tank_temperature_celsius`,
and `atg_alarm_active`. Assert all three are queryable with correct labels.
Confirms the metric name/label contract in §4.1's table, not the FMS's
decision about *when* to write them (that is `fms.md`'s concern).

### `malformed_import_line_is_rejected_without_crashing_vmagent`

POST a line that is not valid VictoriaMetrics import format to `:8429` over
HTTPS. Assert vmagent remains reachable and correctly-behaved for a subsequent
valid push (i.e. does not crash or wedge on bad input).

vmagent answers 2xx to such a line and records the rejection only in its log —
`skipping json line ... cannot parse json line`. The status code therefore
carries no information about whether the line was understood, and the surviving
assertion is the one that matters: the bad line neither stopped the service nor
corrupted the stream behind it. An earlier version of this test expected a 4xx,
which was a mistake about the product rather than about the deployment
(confirmed against vmagent 1.148.0).

### `vmagent_presents_ca_signed_certificate`

Fetch the TLS certificate vmagent presents on `:8429`. Assert its issuer is the
RavensPort internal CA and its subject alternative name is `hist-01` — confirms
`hist.md` §9.1/§10.2's certificate installation, independently of whether a push
happens to succeed (a client that skips verification would not otherwise notice
a wrong or expired certificate).

### `plain_http_import_endpoint_is_not_served`

Attempt the same POST as `metric_push_is_queryable_on_hist01` against
`http://hist-01:8429/api/v1/import` (no TLS). The connection is not refused or
reset: Go's net/http (vmagent's HTTP stack) detects the plaintext request on
its TLS listener and answers a complete HTTP/1.0 400 response — "Client sent
an HTTP request to an HTTPS server." — rather than dropping the connection
(confirmed against vmagent 1.148.0). Assert the response status is 400 —
vmagent has no unencrypted fallback listener that accepts data alongside the
TLS one (`hist.md` §9.1).

---

## 2  Structured event ingestion

Test module: `tests/integration/hist/test_02_ingestion_events.py`

Targets `hist.md` §4.2 and §5 point 2.

### `job_opened_event_fields_land_verbatim`

Push a `job_opened` event with all fields from §4.2's table via
`push_event`. Query VictoriaLogs on `hist-01` and assert every field is
present with the pushed value, plus `_time` and `_msg`.

### `transaction_closed_dispense_fields_land_verbatim`

As above, for the dispense variant of `transaction_closed` (its distinct
field set from the fill variant).

### `transaction_closed_fill_fields_land_verbatim`

As above, for the fill variant.

### `bdn_linked_event_fields_land_verbatim`

As above, for `bdn_linked`.

### `job_cancelled_event_fields_land_verbatim`

As above, for `job_cancelled`.

### `vlagent_jsonline_presents_ca_signed_certificate`

Fetch the TLS certificate vlagent's JSON-line listener presents on `:9429`.
Assert its issuer is the RavensPort internal CA and its subject alternative
name is `hist-01` — the JSON-line listener and the syslog listener (§3) are
independent TLS server configurations within the same vlagent process
(`-tls`/`-tlsCertFile`/`-tlsKeyFile` vs. `-syslog.tls`/`-syslog.tlsCertFile`/
`-syslog.tlsKeyFile`, `hist.md` §9.1), so one can be misconfigured while the
other works; this is the counterpart to §1's vmagent test and §3's syslog
test, completing coverage of every ingestion listener.

### `plain_http_jsonline_endpoint_is_not_served`

Attempt the same POST as `job_opened_event_fields_land_verbatim` against
`http://hist-01:9429/insert/jsonline` (no TLS). As with §1's
`plain_http_import_endpoint_is_not_served`, the connection is not refused or
reset: vlagent's HTTP stack answers a complete HTTP/1.0 400 response rather
than dropping the connection (confirmed against vlagent 1.52.0). Assert the
response status is 400 — vlagent has no unencrypted fallback listener that
accepts data on this port either (`hist.md` §9.1).

### `session_id_joins_a_bunkering_event_to_its_login`

Spans §4.2 and §4.3, and is placed here because it is the common envelope's
`session_id` that makes the join possible.

Push a `LOGIN` syslog message carrying a known `session_id` via `push_syslog`,
then a `job_opened` event carrying the same value via `push_event`. Query
VictoriaLogs for that `session_id` and assert both records return — the
bunkering event by its envelope field, the login by `fms@99999.session_id`,
which §4.3's prefixing rule places under the structured-data identifier.

The two classes are addressed by different field names for the same value, so a
query that finds only one of them passes a naive assertion. Assert both, and
assert the prefixed form explicitly: this is the join the end-of-period audit's
"only authenticated transactions occur" check depends on (`hist.md` §7).

---

## 3  Security and audit event ingestion

Test module: `tests/integration/hist/test_03_ingestion_syslog.py`

Targets `hist.md` §4.3 and §5 point 3.

**Field names, confirmed against vlagent 1.52.0.** `MSGID`
becomes `msg_id`, severity becomes `severity` as a numeric string, and the MSG
becomes `_msg`. Structured-data parameters are **prefixed with the
structured-data identifier**: `username` is stored and queried as
`fms@99999.username`. A selector naming the bare parameter matches nothing, and
the field name must be quoted in LogsQL because `@` and `.` are otherwise read as
syntax. This is the same for a Grafana query as for a test.

### `login_success_event_is_ingested`

Send a `LOGIN` message (severity informational) with `username`, `role`,
`session_id`, `result="success"`. Assert it lands in VictoriaLogs with the
correct `MSGID`, severity, MSG, and structured-data fields.

### `login_failed_event_distinguishes_reason`

Send two `LOGIN_FAILED` messages, one with `reason="invalid_credentials"`
and one with `reason="invalid_rfid"`. Assert both land distinctly — this is
the granularity `hist.md` §4.3 notes the FMS's own HTTP response does not
expose.

### `card_change_event_is_ingested`

Send a `CARD-CHANGE` message for each of `action="enrol"` and
`action="revoke"`. Assert both land with `card_id`, `card_number`, `label`.

### `persistent_connection_carries_multiple_messages`

Open one TLS connection to `:6514` and send three RFC 5424 messages over it
without reconnecting. Assert all three are ingested — confirms the
"persistent TLS connection" requirement in §5 point 3, not just single-shot
delivery.

### `malformed_syslog_line_does_not_crash_vlagent`

Send a line that is not valid RFC 5424 over the same connection. Assert
vlagent's syslog listener remains reachable and a subsequent well-formed
message on the same connection is still ingested.

vlagent does not reject such a line. It falls back to RFC 3164, where the whole
text becomes the message, and stores the result with `format="rfc3164"`.
Survival is therefore what this test can assert, not rejection — the same
position as vmagent answering 2xx to an unparseable import line (§1).

### `vlagent_syslog_presents_ca_signed_certificate`

Fetch the TLS certificate vlagent's syslog listener presents on `:6514`.
Assert its issuer is the RavensPort internal CA and its subject alternative
name is `hist-01` — the same certificate vmagent presents on `:8429` (§1),
reused rather than a distinct keypair per service (`hist.md` §9.1).

### `plain_tcp_syslog_port_is_not_served`

Attempt a plain-TCP RFC 5424 connection to `hist-01:514`. Assert the
connection is refused — port 514 is no longer listened on now that the syslog
path has moved to TLS on 6514 (`hist.md` §5, §10.1).

---

## 4  Replication

**The module must leave `hist-dmz-01` serving.** Two of these tests stop and
start the replica. `incus start` returns once the instance exists, not once its
services are listening, so the controller waits until both stores and Grafana
answer before handing it back. Run alone this is invisible; in a full-suite run
it is not, because the modules execute in name order and §4 precedes §5, §6 and
§7 — each of which reads the replica. Without the wait, their assertions fail
against a host that is still booting, and the failures are reported in those
sections rather than in this one.

Test module: `tests/integration/hist/test_04_replication.py`

Targets `hist.md` §6 — the section with the most safety-relevant claim in
the specification (the divergence-detection signal in `digital.md` §6.3
depends on routine gaps resolving themselves).

### `metric_replicates_to_dmz`

Push a metric to `hist-01`. Poll `hist-dmz-01`'s VictoriaMetrics (its own
plain-HTTP loopback listener, §9.2; reached via `incus exec`, not directly
from the runner, per §0/§8) until the same sample appears, within a bounded
timeout. Assert value and labels match exactly.

### `event_replicates_to_dmz`

As above, for a JSON-line event.

### `syslog_event_replicates_to_dmz_as_native_victorialogs_record`

Send a syslog message to `hist-01`. Assert the corresponding record appears
on `hist-dmz-01`'s VictoriaLogs — confirms vlagent's syslog-to-native
normalisation before forwarding (§6), and that `hist-dmz-01` needs no
syslog listener of its own.

### `replication_survives_transient_dmz_unavailability`

The central test of this section. Stop `hist-dmz-01` (or block the
`hist-01` → `hist-dmz-01` path). While it is down, push N metric samples
and N events to `hist-01`. Restart `hist-dmz-01` (or unblock the path).
Assert all N samples and events eventually arrive, within a bounded
timeout, and that none were lost — validates the per-target on-disk buffer
(`-remoteWrite.tmpDataPath`) that `hist.md` §6 describes as what keeps a
persisting divergence meaningful rather than noisy.

### `local_ingestion_continues_during_dmz_unavailability`

While `hist-dmz-01` is unreachable, confirm pushes to `hist-01` still
succeed and are queryable locally on `hist-01` — the buffer must not
apply backpressure to local ingestion.

### `replication_targets_present_ca_signed_certificate`

Fetch the TLS certificates `hist-dmz-01`'s VictoriaMetrics (`:8428`) and
VictoriaLogs (`:9428`) present on the zone address — the listener hist-01's
agents replicate into, distinct from each store's other, plain-HTTP loopback
listener (§9.2). Assert both are issued by the RavensPort internal CA with
subject alternative name `hist-dmz-01` — the replication leg described in
`hist.md` §6 is encrypted end to end, not only the Grafana instance
`grafana_certificates_differ_between_hosts` (§7) already covers.

### `plain_http_replication_endpoints_are_not_served`

Attempt a plain HTTP write against `http://hist-dmz-01:8428/api/v1/write` and
`http://hist-dmz-01:9428/insert/native` — the zone address, not loopback.
Assert both are refused: that listener is TLS-only (`hist.md` §9.2), so a
firewall misconfiguration that let OT traffic bypass the intended HTTPS port
would still find no working plaintext endpoint behind it. Each store's other
listener, on loopback, is deliberately plain HTTP for Grafana's own local
queries (§7) and is not what this test probes.

---

## 5  Retention configuration

Test module: `tests/integration/hist/test_05_retention.py`

Targets `hist.md` §8. A 3-year retention period cannot be exercised by
elapsed time in a test run; these tests confirm the *configuration* takes
effect, not the behaviour at the 3-year boundary.

### `victoriametrics_retention_period_is_3y_on_both_hosts`

Query each VictoriaMetrics instance's effective configuration (its
`/metrics` endpoint exposes the configured `-retentionPeriod`, or
equivalent) on `hist-01` and `hist-dmz-01`. Assert both report `3y`,
guarding against one template drifting from the other.

### `victorialogs_retention_period_is_3y_on_both_hosts`

As above, for VictoriaLogs.

---

## 6  Grafana authentication

Test module: `tests/integration/hist/test_06_grafana_auth.py`

Targets `hist.md` §7 and `digital.md` §5.1/§5.2.

### `hist01_admin_login_succeeds_with_seeded_credential`

Using the `identities.hist_admin` credential from `world.yaml`,
authenticate against `hist-01`'s Grafana. Assert success.

### `hist01_rejects_reconciliation_user_credential`

Attempt to authenticate against `hist-01`'s Grafana with the
`identities.reconciliation_user` credential. Assert failure — the two
Grafana instances have independent, non-shared user stores (§7).

### `histdmz01_reconciliation_login_succeeds_with_seeded_credential`

Using `identities.reconciliation_user`, authenticate against
`hist-dmz-01`'s Grafana. Assert success.

### `histdmz01_rejects_hist01_admin_credential`

As the mirror of the second test, on `hist-dmz-01`.

### `hist01_rejects_default_grafana_credential`

Attempt to authenticate against `hist-01`'s Grafana with `admin`/`admin`.
Assert failure. Grafana creates that account on first start, and the
seeding step of `hist.md` §10.2 renames it rather than adding a second
row; were it to add one, a well-known credential would ship in the image
and §7's "single account" would be false.

### `histdmz01_rejects_default_grafana_credential`

The mirror of the previous test, on `hist-dmz-01`.

### `hist01_user_store_holds_exactly_one_account`

As the seeded administrator, request the instance's user list. Assert it
holds exactly one row and that its login is the `identities.hist_admin`
username. This asserts §7's "single account" property directly rather
than inferring it from which credentials are rejected, and is possible
because §7 seeds the account as a server administrator.

### `histdmz01_user_store_holds_exactly_one_account`

The mirror of the previous test, on `hist-dmz-01`, against
`identities.reconciliation_user`.

### `credentials_are_deterministic_across_rebuilds`

Rebuild the `hist-01` image (or re-render its provisioning) twice from the
same `world.yaml`. Assert the seeded Grafana user's password hash is
identical both times — the reproducibility guarantee `deployment.md §9`
requires, applied to the deterministic-salt derivation `world.yaml`'s
`crypto.password_salt_seed` documents.

---

## 7  Datasources and query reachability

Test module: `tests/integration/hist/test_07_query_interfaces.py`

Targets `hist.md` §7.

### `hist01_victorialogs_plugin_is_loaded`

Enumerate the instance's plugins and assert `victoriametrics-logs-datasource` is
present with a valid signature. The provisioning tests below do not establish
this: the datasource `type` in `/api/datasources` is copied from the
provisioning file, so a datasource whose plugin Grafana rejected is still listed
and those tests still pass. Grafana refuses to load a plugin whose signature does
not verify, so a valid signature here shows both that the pinned archive arrived
intact (`hist.md` §2.1) and that no unsigned-plugin allowance is in `grafana.ini`.

### `histdmz01_victorialogs_plugin_is_loaded`

The mirror of the previous test, on `hist-dmz-01`.

### `hist01_datasources_are_provisioned`

Query `hist-01`'s Grafana `/api/datasources` as the admin user. Assert
exactly two datasources exist: a Prometheus-compatible one pointed at
`http://127.0.0.1:8428`, and a VictoriaLogs one pointed at
`http://127.0.0.1:9428`.

### `histdmz01_datasources_are_provisioned`

As above, on `hist-dmz-01` as the reconciliation user.

### `grafana_reachable_over_https_from_another_ot_host`

From `hmi-01` (or another OT host), reach `https://hist-01` and confirm a
TLS handshake with a certificate signed by the RavensPort internal CA —
confirms `hist.md` §7's "any other OT host" reachability, without asserting
the HMI application itself ever calls it (it does not; see §9 note).

### `grafana_certificates_differ_between_hosts`

Fetch the presented TLS certificate from `hist-01` and from `hist-dmz-01`.
Assert they are distinct — `hist.md` §10.2/§10.3 requires separate
keypairs, not one certificate reused across both images.

### `one_proxy_query_returns_both_classes_of_event`

Through `hist-dmz-01`'s datasource proxy as the reconciliation user, over the
`monthly-audit-logs` datasource, for a period seeded with both a bunkering event
and a `LOGIN`.

`hist.md` §7 has the audit retrieving both classes and `digital.md` §4.5 states
the scope. This asserts the host makes that retrievable in a single query rather
than requiring one per class — the property the audit's single retrieval per
window depends on. A filter selecting only records carrying `event_type` returns
the bunkering events alone, so the assertion is that both are present in one
response.

The expression the audit actually issues is `sim-impl.md`'s; this test asserts
the host's capability, not the actor's choice of filter.

### 7.1  Provisioned dashboards

Test module: `tests/integration/hist/test_08_dashboard_queries.py`

Targets `hist.md` §7.1. Grouped under §7 rather than given a section of its own
because §8 below is already occupied by the deployment-level tests, and these
exercise the same Grafana query path as the module above.

**This module exists because §8's drift guard structurally cannot cover it.**
`test_hist_grafana_dashboards.py` checks each dashboard's queries against a
vocabulary transcribed into the test module. Both sides are files in the
repository, so it catches a query naming a series the transcription does not hold
and cannot catch the transcription itself being wrong: were a product to rename a
series at a later pinned version, the query and the transcription would be
corrected together, agree with each other, and leave the panel rendering empty on
the deployed host. The authority for those names is the running product, so the
check has to be made where one exists.

#### `hist01_dashboard_is_retrievable`, `histdmz01_dashboard_is_retrievable`

`GET /api/dashboards/uid/…` as each host's seeded account returns the dashboard
`hist.md` §7.1 provisions, with its specified title and at least one panel.

Asked of the HTTP interface rather than of Grafana's database or its journal.
Grafana 12.4 serves dashboards from unified storage, so which table holds one is a
storage-layer detail that changes with a version; and its provisioning log lines
report on its own reconciler rather than on whether a given dashboard was accepted.
Retrievability by the account that uses it is the property, and the only one that
stays true across versions.

#### `hist01_dashboard_queries_return_data`, `histdmz01_dashboard_queries_return_data`

Read every metric query out of the **deployed** dashboard, not the rendered file,
and assert each returns at least one series from the datastore it is bound to. A
stale image therefore fails here rather than being tested through a copy it does
not contain.

An empty result is what a renamed series, a mistyped label, a label whose value is
not what was assumed, or a scrape not reaching an endpoint all produce — and it is
what the panel would show its reader with nothing else indicating a fault. All
empty panels are reported together rather than the first, so a version bump is one
edit rather than one per re-run.

The `hist-dmz-01` case pushes a tank reading first rather than relying on residual
history, so a store with no readings fails for its own reason rather than this one.

#### `hist01_scrape_reaches_all_four_components`

`vm_app_uptime_seconds` returns a series for each of `hist.md` §6.1's four scrape
jobs. Asserted separately from the panels because no panel names every job: a
configuration that had lost one would satisfy most panels, and the gap would show
only as one missing line in a legend.

#### `hist01_self_monitoring_does_not_reach_the_replica`

`hist-dmz-01` holds no `vm_app_uptime_seconds` series. §6.1 places the scrape on
`victoria-metrics`, which originates no remote-write, so there is no path by which
these series could arrive; this asserts that outcome on the deployed pair rather
than the placement that produces it, which §8's `test_hist_self_monitoring.py`
covers.

**Timing.** §6.1 scrapes every 60 seconds and `hist.md` §9.3 leaves
`-search.latencyOffset` at its 30-second default, so a scraped series can be
invisible for about 90 seconds after the process exposing it starts. The polling
bound in this module is set accordingly and is longer than §0's, which is sized for
a push.

---

## 8  Deployment-level configuration tests

Location: `deploy/tests/unit/`

Follows the `test_fms_env_contract.py` /
`test_world_schema.py` / `test_projection_map.py` pattern already
established for the FMS.

### `test_world_schema.py`

Extended in place rather than given a log-server module of its own, the schema
being one object with one set of cases.

- `world.yaml` validates against an `Identities` model extended with
  `hist_admin` and `reconciliation_user`.
- Both are rejected by the strict schema if a required field (username,
  password) is missing, following the existing `INVALID_CASES` pattern.

### `test_hist_grafana_seed.py`

`identities.hist_admin`/`identities.reconciliation_user` are consumed by
dedicated render.py code, not `PROJECTION_MAP` — password hashes never go
through that map (the FMS admin hash does not either; `PROJECTION_MAP`
carries only cross-consumer scalar facts, per `world_schema.py`'s own
comment). The test therefore targets the dedicated hashing function
directly, mirroring `compute_argon2id_hash`'s existing coverage.

Grafana's internal `user` table hashes passwords with PBKDF2-HMAC-SHA256
(10,000 iterations, 50-byte derived key, hex-encoded —
`EncodePassword(password, salt)` in Grafana's own `pkg/util/encoding.go`),
with the salt stored in a separate `salt` column rather than embedded in
the hash string. The tests below assume this scheme:

- the rendered artefact's PBKDF2 hash for each identity is reproducible
  across repeated renders from the same `world.yaml` (deterministic salt
  derived from `crypto.password_salt_seed` combined with the username,
  the same mechanism as `_derive_salt` already uses for argon2id/SSHA —
  no new dependency needed, since PBKDF2-HMAC-SHA256 is in Python's
  standard library);
- two different usernames with the same plaintext password produce
  different hashes (salt actually varies per identity, not a single
  fixed salt reused);
- the produced hash and salt reproduce Grafana's own
  `EncodePassword(password, salt)` output for the same inputs (10,000
  iterations, 50-byte key, hex-encoded), confirming the seeded row is
  byte-for-byte what Grafana itself would have written, not just a
  plausible-looking value.

### `test_hist_grafana_artefacts.py`

The counterpart to the hashing tests above, covering what `render.py` writes
rather than what it computes. Per host, against `build/grafana/`:

- the user seed carries exactly the hash and salt produced for that host's
  identity, and no plaintext password;
- the seed contains no `INSERT INTO user`. Grafana creates `admin`/`admin` on
  first start, and its organisation membership references that row, so the
  seeded account must rename it rather than add a second one — an insert leaves
  a well-known credential live and the membership attached to the wrong
  account;
- the seed sets `login`, `email`, `name`, `password`, `salt` and `is_admin`
  together, and writes the `org_user` role of §7;
- `grafana.ini` carries the `[server]` block of `hist.md` §9 and sets no
  `admin_user` or `admin_password`: either would have Grafana re-hash the
  credential with a random salt at startup and discard the seeded row;
- both datasources point at the local stores on loopback and are marked
  non-editable, so neither can be repointed from a browser without a rendered
  artefact changing;
- all three artefacts appear in the build script's `current_hash` list, so that
  editing one rebuilds the image.

The same module covers what the build script then does with them, since an
artefact rendered correctly and installed incorrectly fails in a different way
from one never rendered at all — Grafana falls back to its packaged defaults and
reports no error the build would notice:

- `grafana.ini` and the datasource provisioning reach the paths Grafana reads,
  and the plugin is unpacked into Grafana's plugin directory;
- the seed is applied after Grafana's first start has created the schema and
  before the service is enabled. Ordering is the whole of this step: applied
  earlier there is no `user` table to update, applied later it races a running
  instance;
- the seed does not survive into the published image. It carries a working
  credential hash and salt, so once applied it is provisioning residue
  (`deployment-config.md` §4). The container path is read from the push rather
  than fixed by the test.

### `test_hist_least_knowledge.py`

`deployment-config.md` §5 requires the least-knowledge check to run against a
node's *whole* baked configuration set rather than its env file alone. Password
hashes bypass `PROJECTION_MAP`, so no projection test covers them; this module
is that coverage, reading every rendered artefact belonging to a node:

- no identity's plaintext password reaches any node;
- `crypto.password_salt_seed` reaches no node — with a username it reproduces
  that identity's salt under all three hashing schemes at once;
- each Grafana credential — hash, salt and username — appears on its own
  instance and on no other. §6 asserts that the two instances reject each
  other's credentials at run time; this asserts they never held them;
- neither log server carries the FMS administrator's argon2id hash, an
  operator's `{SSHA}` password or username, or the LDAP base distinguished
  name: Grafana's built-in store is the only authority on these hosts (§7);
- neither carries any RFID card number. A log server receives events *about*
  card presentations over the ingestion path; the roster is not a log-server
  fact, and baking it in would let an attacker who reaches the historian
  enumerate valid cards without observing one in use.

### `test_hist_systemd_units.py`

Parses each rendered systemd unit's `ExecStart` and asserts, per `hist.md`
§9's table:

- `hist-01`: `victoria-metrics`/`victoria-logs` bind `127.0.0.1`; `vmagent`/
  `vlagent` are present, each carrying `-tls`/`-tlsCertFile`/`-tlsKeyFile` and
  two `-remoteWrite.url` targets (`http://127.0.0.1:…` local,
  `https://hist-dmz-01:…` replicated); `vlagent` additionally binds
  `0.0.0.0:6514` with `-syslog.tls`/`-syslog.tlsCertFile`/`-syslog.tlsKeyFile`.
- `hist-dmz-01`: `victoria-metrics`/`victoria-logs` bind `0.0.0.0` and each
  carries `-tls`/`-tlsCertFile`/`-tlsKeyFile`; no `vmagent`/`vlagent` units are
  rendered at all.
- `grafana-server.service` carries `AmbientCapabilities=CAP_NET_BIND_SERVICE`
  on both hosts. `vlagent.service` no longer needs it: its syslog listener
  moved from port 514 to 6514 (§3, `hist.md` §10.1).
- `-retentionPeriod=3y` is identical across both hosts' units (a literal
  duplicated across two templates, not sourced from one `world.yaml`
  field per its inclusion criterion — this test is the drift guard for
  that duplication).
- every account named by a `User=` is created by the same build script as a
  system account with `/usr/sbin/nologin`. A unit naming an account that was
  never created fails at start with a message about the user rather than the
  service; one created with a login shell turns a compromise of any store into
  an interactive session. Grafana is outside this check: its account comes from
  the package, and the build script writes a drop-in rather than the unit that
  names it.

### `test_hist_tls_material.py`

Covers the shared-keypair design of `hist.md` §9/§10.2/§10.3 — the properties
that make the ingestion, replication, and Grafana TLS tests in §1–§4 and §7
possible, distinct from what those functional tests already assert about the
running services:

- Exactly one certificate/key pair is rendered per host
  (`build/tls/hist-01/`, `build/tls/hist-dmz-01/`), and every TLS flag on that
  host's rendered units and `grafana.ini` — vmagent's and vlagent's
  `-tlsCertFile`/`-tlsKeyFile`, vlagent's `-syslog.tlsCertFile`/
  `-syslog.tlsKeyFile`, both stores' `-tlsCertFile`/`-tlsKeyFile`, and
  Grafana's `cert_file`/`cert_key` — names the identical `/etc/hist-tls/`
  path, never a per-service copy. This is the regression guard for the
  design in §9: nothing here would fail if a future edit reintroduced
  per-service copies that all happened to be byte-identical at build time
  but could drift at the next renewal.
- On the deployed image, `/etc/hist-tls/cert.pem` is mode `0644`;
  `/etc/hist-tls/key.pem` is owned `root:hist-tls` and mode `0640`.
- The `hist-tls` group's membership matches §10.2/§10.3 exactly:
  `victoria-metrics`, `vmagent`, `victoria-logs`, `vlagent`, and `grafana` on
  `hist-01`; the same three minus `vmagent`/`vlagent` on `hist-dmz-01`, which
  has no accounts of those names to add. A service account missing from the
  group is indistinguishable from a correctly-configured one until the
  private key is rotated or the permission bits are checked directly — a
  functional test pushing data today would still pass.

### `test_hist_hosts_resolve.py`

`hist-01`'s rendered `/etc/hosts` (`build/hosts/hist-01`) contains an entry
resolving `hist-dmz-01` to its DMZ address — the `resolve: [hist-dmz-01]`
topology entry `hist.md` §3.1 requires so that the replication targets in §6
can be addressed, and their certificates validated, by hostname. Without it
the `-remoteWrite.url=https://hist-dmz-01:…` targets in §9.1 fail to resolve
at all, which is a different failure from the certificate mismatch
`replication_targets_present_ca_signed_certificate` (§4) guards against, and
one none of the functional replication tests would otherwise distinguish from
`hist-dmz-01` simply being down.

### `test_hist_artefact_fetch.py`

Asserts, against both rendered build scripts, that the upstream artefacts of
`hist.md` §2.1 are pinned, verified, and cached as §2.2 requires:

- Each host fetches exactly the artefacts its services need: six for `hist-01`,
  and the same set less `vmutils` and `vlutils` for `hist-dmz-01`. The replica
  must not hold the binary for an outbound remote-write path, whether or not a
  unit is written for it — the build-time counterpart to
  `test_hist_systemd_units.py`'s assertion that no agent unit is rendered there.
  The Grafana datasource plugin is fetched by both hosts, because both provision
  the datasource it implements; its backend queries a store on Grafana's behalf
  and cannot originate a remote-write, so it is not an exception to that rule.
- Every artefact is pinned to its specified version, and no artefact is resolved
  through `/releases/latest` or `/latest/download`.
- The architecture is derived from the build host, and no download URL carries a
  hard-coded `amd64` or `arm64`. A baked architecture produces an image whose
  binaries do not run, failing at first boot rather than at build.
- Both published checksum forms are read (`_checksums.txt` and `.sha256`), a
  digest is computed, and a failed verification removes the cached copy so that
  the corruption cannot be reused by the next build.
- The plugin, whose publisher offers only a SHA-1 digest, is verified against the
  SHA-256 pinned in `hist.md` §2.1 rather than against that file, and its
  filename carries no architecture — one archive serves every platform. Both are
  asserted against the plugin's own arm of the resolver, with the architecture
  assertion also confirming that the other artefacts still resolve one, so that
  it cannot pass by the mechanism having been removed altogether.
- Nothing is downloaded inside the build container. Distribution packages
  installed there with `apt-get` are not downloads in this sense
  (`deployment.md` §6.1 step 4); a general-purpose fetch with `curl` or `wget`
  is, since it is how an artefact would re-enter the container unverified.
- The fetch precedes the `current_hash` assignment, and every fetched artefact
  appears in that hash. Without the first, `sha256sum` runs against files that do
  not exist and `set -e` aborts the first build on a clean checkout; without the
  second, a version bump re-renders the script but the build reports "inputs
  unchanged" and keeps the previous binary.
- `deploy/.artefact-cache/` is excluded from version control, asserted through
  `git check-ignore` rather than by reading `.gitignore`, so that the test
  follows however the exclusion is expressed.

### `test_hist_grafana_dashboards.py`

The provisioned dashboards of `hist.md` §7.1, and the drift guard over what their
panels may name. A dashboard fails more quietly than the other Grafana artefacts:
a panel whose datasource identifier does not resolve renders empty rather than
erroring, and a query naming a field that does not exist returns no rows rather
than a message. Neither appears in a build log, a journal, or a status code.

- The file provider is a file provider, reads the directory the build fills, is
  scoped to organisation 1 — the only one the seeded account belongs to — and
  leaves the dashboard read-only so the deployed view cannot drift from the file
  the input hash covers.
- Every panel binds to a datasource identifier its own host provisions, and each
  identifier is paired with the datasource type that serves it. Binding to the
  other host's identifier fails here rather than rendering the wrong data.
- Panel queries name only series and fields the host holds. The permitted
  vocabulary differs per host: `hist-dmz-01`'s is `hist.md` §4.1 and §4.2;
  `hist-01`'s is the self-monitoring set of §6.1, whose names come from the pinned
  products rather than from `hist.md` and are transcribed into the module.
  **That transcription cannot be validated from within the repository** — see
  §7.1.
- The `hist-dmz-01` events table excludes §4.3's syslog records, uses the query
  type that returns individual records rather than an aggregate, and carries its
  three transformations in the order the plugin requires: the projected fields
  arrive inside a `labels` map, so extraction must precede the numeric conversion
  that targets fields extraction creates.
- Litre columns are converted from the strings VictoriaLogs returns, or a column
  sorts lexicographically and places `"9.5"` after `"10.2"` while showing no sign
  of being wrong.
- The two hosts do not share a dashboard identifier, for the reason they do not
  share datasource identifiers.
- The remote-write ordinals the replication panels select match the
  `-remoteWrite.url` order in the rendered build script. Both agents mask the URL,
  so the panels address each target by position; reordering those two flags would
  otherwise leave every replication panel reporting each queue as the other one,
  rendering normally and with real values.
- Both dashboard artefacts join the input hash, are pushed into the directory the
  provider reads, and the build confirms the dashboard can be retrieved before
  publishing the image.

### `test_hist_self_monitoring.py`

The scrape configuration of `hist.md` §6.1, and the containment it provides.

- All four local endpoints are scraped, one job per component, over loopback.
  Anything less leaves a §7.1 panel rendering empty rather than failing.
- **Whichever service carries `-promscrape.config` carries no
  `-remoteWrite.url`.** This is the assertion behind §6.1's exception to §6:
  a scraper that also remote-writes forwards everything it collects to both of
  §6's targets, the second being `hist-dmz-01`. Asserting the placement rather
  than the file is the point — the placement is what provides the guarantee.
- The configuration is pushed to the path the unit reads and is owned by the
  unprivileged account that must read it. A file the service cannot read fails
  identically to one that is absent, and neither stops the unit.
- `hist-dmz-01` renders no scrape configuration and neither of its stores scrapes.
- The configuration joins the input hash, so editing what the host collects
  rebuilds the image.

### Runner access to the zone networks

The zone bridges carry no host address (`deployment.md` §2.1), so a test process
on the Incus host has no route to `hist-01` and every push fails with a connect
timeout. `make -f tests/integration/Makefile configure-hist`, which `test-hist`
depends on, places the runner on `vnet-ot` as `RUNNER_HOST`. That is the single
line `configure-fms` and `configure-corp-it` already run for the FMS and LDAP
suites, so it grants the host no reach it does not already have wherever those
suites have been used.

The runner joins that one zone and reaches no other. A host holding addresses on
several zones, or routes into them, is a path between those zones that bypasses
the firewall nodes; the arrangement here avoids becoming one. Everything on the
far side of a boundary is read through `incus exec` instead — `hist-01`'s
loopback-bound stores (§9.1) and both of `hist-dmz-01`'s services. Only the
pushes to `hist-01` cross the network, and only because the simulator must be a
real client for the §3 framing assertions to mean anything.

Reading the replica this way costs no coverage. Those tests establish that
replicated records arrived; whether the path carrying them is open is asserted
by `test_policy.sh` and `test_firewall.sh`, which probe
`hist-01 → hist-dmz-01:8428` and `:9428` directly. For the replica's Grafana it
is not a choice at all: 443 into the demilitarised zone is permitted only from
`vnet-it`, so an operational-technology-positioned runner is refused, correctly.

### `test_cloud_init_residue.py`

`hist-01.sh.j2` and `hist-dmz-01.sh.j2` purge `cloud-init` at build time. No
log-server module was added: that file's `node_build_scripts()` helper discovers
every rendered build script, so both hosts are covered by the existing tests.

---

## 9  Explicitly out of scope

- **HMI never queries the log server.** `hist.md` §7 states this as a
  clarification of `digital.md`'s "serves read-only HTTPS to HMI" wording,
  not a new behaviour. Any assertion that the HMI application makes no
  such call belongs to the HMI's own test suite, not this one.
- **Divergence *detection*.** `digital.md` §6.3 names replica divergence
  as a detection signal, but `hist.md` builds only the replication
  mechanism, not a detector. §4's tests confirm routine gaps resolve
  themselves (the precondition for that signal to be meaningful); a test
  that a monitoring component *flags* a persisting divergence belongs to
  whichever future component implements that detection.
- **Certificate renewal and expiry.** Leaf certificates are valid for one
  year (`digital.md §8` decision 7); nothing in this plan exercises what
  happens as one approaches or crosses that boundary, since a test run
  cannot elapse a year and the renewal process is a build-time action, not a
  runtime one. `plain_http_import_endpoint_is_not_served` and its siblings
  (§1–§4) confirm there is no unencrypted fallback to fall back to, which is
  the property that matters if renewal is ever missed.
- **Certificate revocation.** The CA issues no CRL and runs no OCSP responder
  (`digital.md §2.7`); there is nothing to test because the design has
  nothing to revoke against.
