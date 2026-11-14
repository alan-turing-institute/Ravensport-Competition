# FMS Implementation Specification

## 1  Role and scope

The Fuel Management System (FMS) runs on `fms-01` in the OT network zone. It is the
operational heart of the dock: it authenticates operators, manages the job lifecycle for
both dispense and fill operations, controls the tug dispenser over Modbus TCP, polls the
Automatic Tank Gauge (ATG) console, validates vessel RFID cards, and maintains the
append-only transaction log. All business logic lives here; the HMI is a thin
presentation proxy with no logic of its own.

The authoritative description of the FMS's role in the environment is `digital.md §2.1`
and `digital.md §4`.

This document is the authoritative specification for the FMS HTTP API. The HMI specification, in `hmi.md`, must be consistent with this document in its description of the HMI interface with the FMS. The sim
integration tests use an in-process channel-based stand-in for the FMS
(`infra/sim/tests/mock_digital/fms.rs`) rather than a real HTTP service; that is not a
reference for the API contract but must also be consistent with this document.

---

## 2  Technology

Rust + Axum. The FMS is a self-contained compiled binary backed by a local PostgreSQL
database. It terminates Transport Layer Security (TLS) directly using rustls (no reverse proxy in front of it), runs
four concurrent background tasks alongside the HTTP server, and requires no interpreted
runtime on the host. It also constructs RFC 5424 syslog messages directly over a TCP
socket for security-relevant audit events (§4, §7.5), rather than depending on an
external syslog crate or the host's own syslog service — consistent with the
self-contained-binary design.

---

## 3  Network interfaces

The FMS binary owns its own TLS termination. Unlike the HMI (`hmi-01`), no HAProxy
instance sits in front of it; the binary binds directly to port 443 using a rustls
listener. A TLS certificate signed by the RavensPort internal certificate authority is
a hard startup dependency: the deployment pipeline provisions it into
`/etc/fms/tls` at build time (§9.2; digital.md §2.7, §8 decision 7), and the FMS has
no self-sign fallback — it fails to start if the certificate or key is missing or
unreadable.

| Layer | Endpoint | Protocol | Purpose |
|-------|----------|----------|---------|
| FMS binary — inbound | `0.0.0.0:443` | HTTPS | Requests from the HMI |
| OpenSSH server — inbound | `10.40.0.10:22` | SSH | Systems administration from `dmz-bastion-01` |
| FMS binary → ATG console | `10.40.0.51:10001` | TCP (serial) | Veeder-Root Tank Level System polling |
| FMS binary → dispenser | `10.40.0.52:502` | Modbus TCP | Preset, enable, and totaliser registers |
| FMS binary → vessel RFID reader | `10.40.0.53:9001` | TCP | Vessel RFID card stream |
| FMS binary → LDAP directory | `10.30.0.20:636` | LDAPS | Operator credential bind (two-hop via firewalls) |
| FMS binary → vmagent (metrics) | `10.40.0.11:8429` | HTTPS (VictoriaMetrics import format) | ATG reading push (§5.1) |
| FMS binary → vlagent (structured events) | `10.40.0.11:9429` | HTTPS (JSON-line) | Job/transaction/BDN event push (§5.4) |
| FMS binary → vlagent (syslog) | `10.40.0.11:6514` | Syslog / TLS (RFC 5425) | Login and card-table event push (§4, §7.5) |

All addresses except the LDAP directory are on `vnet-ot`, including the log server
(`hist-01`). The LDAP directory is on `vnet-it` and is reached via `fw-ot-dmz` →
`fw-dmz-it` (topology.yaml firewall rules permit TCP/636 on this path), addressed
by hostname rather than IP so that `corp-it`'s certificate's hostname SAN
validates (`host_specs/corp-it/corp-it.md §5`).
Full detail on the log server's ingestion protocols is `host_specs/hist/hist.md §5`.

The OpenSSH server authenticates the systems-administration account using the administrator's SSH key, with the settings of `digital.md §4.6`.
The account uses `sudo` for maintenance that requires elevated privileges, and its `sudoers` rule carries `NOPASSWD` (`digital.md §4.6`).

All three pushes to `hist-01` are TLS connections: the FMS binary is a client here,
not a server, so it validates `hist-01`'s certificate rather than presenting one of
its own. Validation is against the CA root already installed in every node's system
trust store (`digital.md §2.7`). A validation failure is retried under the same
backoff policy as an unreachable host (§5.4), so it carries no operational
consequence beyond a delayed push — but it is recorded at WARN in the FMS's own
logs with rustls's rejection reason (expired certificate, hostname mismatch,
untrusted issuer), distinct from the message logged for a connection refused or
timed out. Without that distinction, a rotated or compromised `hist-01` certificate
and a `hist-01` outage would read identically in the FMS's logs, and an operator or
the reconciliation user's end-of-period audit would have no way to tell a certificate
problem from a network one.

---

## 4  Authentication and session management

Two roles are supported.

**Admin.** The local admin account is verified by argon2id hash comparison, with no LDAP
lookup and no RFID requirement. The hash is stored in the environment file and generated
at provisioning time using the `argon2` CLI.

**Operator.** Two independent checks must both succeed:

1. An LDAP simple bind, over TLS (LDAPS), against
   `uid={username},ou=people,dc=ravensport,dc=local` on the configured LDAP URI. A
   successful bind proves the password.
2. Card table lookup: the supplied RFID card number must appear in the card table with
   `active = true` and a `label` equal to the username (§8.2). A vessel tag, or a card
   enrolled to another operator, fails this check even when active.

On success, a UUID session token is returned. Active sessions are held in an in-memory map keyed by token string. Each session carries a `session_id`, `username`, `role`, and `expires_at` timestamp, set at login to the login time plus the configured session timeout (default 30 minutes). A compressed scenario divides the session timeout by the compression factor (`narrative.md` §6, deviations from real-world practice). The expiry is checked on each request and the session is evicted on access if expired.

A login event row is written to the `sessions` table (append-only) on every successful
login regardless of role. The in-memory map is the authoritative source for active
tokens; the database row is the audit record.

On every login attempt, successful or not, the FMS additionally emits an RFC 5424
syslog message to the log server's vlagent syslog listener (`host_specs/hist/hist.md
§4.3`, §5.4): `LOGIN` on success, carrying `username`, `role`, `session_id`,
`source_address` and `result="success"`; `LOGIN_FAILED` on failure, carrying
`username`, `role_attempted`, `source_address`, and a `reason` of `"invalid_credentials"` (LDAP bind or admin hash comparison failed),
`"invalid_rfid"` (card table lookup returned no active card enrolled to that username) or `"missing_rfid"` (an
operator login supplied no card number, so no lookup was performed) — a distinction not
exposed in the HTTP response, which remains a generic `401`. Failed attempts are not written to the
`sessions` table, which remains a record of successful sessions only; they are recorded
by the syslog event on the log server and, like every rejection, in the FMS journal
(§7).

A directory the FMS cannot reach produces the same outcome as a password the directory
rejects: the bind fails, the response is the generic `401`, and the syslog event carries
`"invalid_credentials"`. Neither the response nor the event separates them, so that a
caller learns neither which accounts exist nor whether the directory is reachable. The
distinction is drawn in the FMS journal alone (§7), where a failure to reach the
directory, or to complete a bind against it, is recorded at warning level with the
configured LDAP URI, and a rejected password is not. Without it, an untrusted or expired
certificate, a name that does not resolve, or a dropped route to `corp-it` would present
exactly as every operator mistyping their password.

`source_address` is the address of the peer that made the request, without the ephemeral
port. Without it, an operator mistyping a password at the dock and an attacker trying
credentials from elsewhere on the network produce identical records, and the audit trail
is the only place either is recorded at all. Operator logins reach the FMS through the
HMI's HAProxy frontend (`hmi.md §9.3`), so those carry `hmi-01`'s address rather than
the workstation's; a login from any other source is what the field exists to make
visible.

---

## 5  Background tasks

Four tasks run concurrently with the HTTP server for the lifetime of the process.

### 5.1  ATG poller

Connects to the ATG console (Veeder-Root Tank Level System serial-over-TCP) on each
poll cycle, issues function codes `i201` (in-tank inventory), `i205` (in-tank status), and
`i101` (system status report), and updates a shared in-memory reading. The reading is the
source used by `GET /api/v1/tanks/{tank_id}/level`. Each cycle also:

- Writes one row to the `atg_readings` table (volume, temperature, alarm flags, timestamp).
- Sends an `atg_reading` event to the log-server pusher's metrics queue (§5.4).
- Sends an `atg_alarm` event when the bund liquid-sensor fuel alarm (the `i101` system
  status entry `030301`) transitions from inactive to active since the previous cycle, and
  one `atg_alarm` event for each tank alarm reported by `i205` (none arise in the testbed).

The poller reconnects on each cycle (a new TCP connection per poll). On failure the cycle
is skipped with a warning log and the previous in-memory reading is retained. The cycle's
network phase is bounded by a fifteen-second timeout; a cycle that does not complete within
it is a failed cycle, so a console that accepts the connection and then goes silent cannot
stall the poller.

Default poll interval: 30 seconds. A compressed scenario divides the poll interval by the compression factor (`narrative.md` §6, deviations from real-world practice).

The shared in-memory reading serves the periodic uses (the `/level` API, the `atg_readings`
series, alarm-edge detection). Transaction boundaries are not among them: both the open and
the close of a dispense or fill issue their own on-demand `i201` read for the
`atg_volume_at_open_l` / `atg_volume_at_close_l` fields (§5.2, §7.4), rather than initially
reading the cache, which would be up to one poll interval stale. At close this is essential:
flow has just stopped, so a cached reading still reflects the higher level from earlier in the
drain or fill and can inflate the meter-versus-ATG delta past the ±1 % reconciliation tolerance
the cross-check depends on (narrative.md §5). At open the level is static before flow starts.

Each on-demand read is transaction-scoped: it does not update the shared reading, write an
`atg_readings` row or emit a log event, so the periodic series keeps its uniform cadence for
the rate-of-change checks. A read fails if:

- it does not complete within a five-second timeout;
- its temperature-compensated volume is not a finite, non-negative number;
- its temperature is not a finite number (the dispense close records the temperature from
  the same reading, §5.2).

The periodic reading is held to the same rules, so a fallback to the cache never substitutes
a value the fresh read would have refused. If a fill opening or closing read fails, the
operation continues and records null. If a dispense-closing read fails, the transaction
falls back to the cached reading provided it is no more than two poll intervals old,
logging a warning rather than blocking; with no sufficiently fresh cached reading the
record is null.

The cavitation threshold is a volume in litres calculated as
`FMS_CAVITATION_THRESHOLD_PCT` of `FMS_TANK_CAPACITY_L` (§6). Every cavitation
comparison uses the temperature-compensated volume (`tc_volume_l`) returned by `i201`;
the ATG level in millimetres is not used.

| Point | Reading | Rule | Failure outcome |
|-------|---------|------|-----------------|
| Dispense creation | Fresh on-demand `i201` reading | Current volume must be strictly above the threshold | Reject the request; create no job or `job_opened` event |
| Dispense start | Fresh on-demand `i201` reading | Current volume and projected closing volume (`current volume - preset`) must be strictly above the threshold | Reject the request; leave the job `active` and emit no `transaction_open` event or Modbus write |

The creation and start checks do not use the cached reading as a fallback. Job creation
does not authorise fuel flow, and the projected closing volume is checked at start even
if a preset was supplied when the job was created.

The cavitation checks do not apply to fill jobs. An unavailable ATG reading does not
block a fill or its closure; the recorded opening or closing volume may be null (§8.4).

The physical model does not represent post-flow settling (turbulence, foam), so the on-demand
read is exact the instant flow stops; a real automatic tank gauge would impose a short quiet
period before recording a delivery-end volume. Recorded as a divergence.

### 5.2  Modbus poller

Monitors the dispenser during an active dispense job. On each cycle the poller checks
whether a dispense job is in the `Running` state; if so, it connects to the dispenser
over Modbus TCP and:

1. On the first cycle for a given job: reads the cumulative totaliser (registers
   `0x0003`–`0x0004`) to record the start reading, then writes the preset volume to
   register `0x0001` (litres × 10, 16-bit) and asserts pump enable on register `0x0002`.
2. On subsequent cycles: reads the transaction totaliser (registers `0x0005`–`0x0006`)
   and updates the in-memory `current_dispensed_l` value on the current job. This value
   is served by `GET /api/v1/jobs/{job_id}/totaliser`.
3. When the transaction totaliser reaches the preset volume: reads the final cumulative
   totaliser, writes pump enable to 0, issues an on-demand `i201` read for the close volume
   (§5.1), inserts one complete immutable transaction row, transitions the job to
   `AwaitingBdn` in memory (not `Closed` — the job remains live until the BDN is entered),
   and sends a `transaction_closed` event to the log-server pusher's event queue (§5.4).

Default poll interval: 5 seconds. A compressed scenario divides the poll interval by the compression factor (`narrative.md` §6, deviations from real-world practice).

**Where the dispense row's measurement fields come from.** A dispense has no
transcription route — the operator enters only the BDN number, via
`PATCH /api/v1/jobs/{job_id}` — so the three measurement columns of `§8.4` are
populated by the FMS rather than received:

- `temperature_c` — from the same on-demand `i201` read taken for the close
  volume in step 3. The port has one temperature sensor (`sim.md` §8.4.1), so
  this is the same value the operator sees on the HMI and writes onto the BDN.
- `density_kg_per_l` — `FMS_DENSITY_KG_PER_L` (§6), the reference density of the
  configured grade, `FMS_GRADE`, at 15 °C. It is a property of the product rather than a
  measurement, taken from the certificate of quality in a real deployment.
- `temp_corrected_volume_l` — `volume_dispensed_l` corrected to 15 °C using the
  two above.

Carrying the inputs as well as the corrected figure lets the end-of-period audit
recompute the correction rather than accept it (`hist.md` §4.2).

### 5.3  Vessel RFID reader

Maintains a single persistent TCP connection to the vessel RFID reader. Card numbers
arrive as decimal integers framed by `\r\n`. On each received card number the task:

1. If no dispense job is in the `Active` or `Running` state, discards the card number.
2. Otherwise looks up the card number in the `card_table` (most recent row per card
   number) and requires `active = true` and a `label` equal to the job's
   `counterparty`, the vessel's IMO number (§8.2, §8.3).
3. If both hold, sets `rfid_validated = true` on that job. Inactive or unknown card
   numbers, and cards enrolled to another vessel or to an operator, are logged and
   discarded.

On connection loss the task reconnects with a fixed 2-second delay before retrying.

### 5.4  Log-server pusher

Drains three in-memory queues of `LogEvent` values, one per target on `hist-01`, and
forwards each event to the target its variant selects:

- **`AtgReading` / `AtgAlarm`** (from §5.1) — serialised to VictoriaMetrics
  import-format JSON lines and POSTed over HTTPS to vmagent's import endpoint
  (`FMS_VMAGENT_URL`). Both are metrics rather than log records
  (`host_specs/hist/hist.md §4.1`):
  a reading becomes one line per metric per poll cycle, an alarm one line on
  transition only, so its series is sparse by design.
- **`JobOpened` / `TransactionOpen` / `TransactionClosed` / `BdnLinked` /
  `JobCancelled`** (from the job/transaction endpoints, §7.4, and the Modbus poller,
  §5.2) — serialised to a
  JSON-line object and POSTed over HTTPS to vlagent's JSON-line endpoint
  (`FMS_VLAGENT_JSONLINE_URL`). `TransactionOpen` is emitted by the dispense-start
  endpoint only; a fill has no equivalent operator-initiated start (`digital.md §4.3`).
- **`Login` / `LoginFailed` / `CardChange`** (from §4 and §7.5) — formatted as an
  LF-terminated RFC 5424 syslog message and written to a persistent TLS connection
  (RFC 5425) to vlagent's syslog listener
  (`FMS_VLAGENT_SYSLOG_HOST`/`FMS_VLAGENT_SYSLOG_PORT`).

The full field mapping for each variant is `host_specs/hist/hist.md §4`. All three paths
share the same retry policy: exponential backoff starting at 500 ms and doubling to a
cap of 30 seconds, retried without an attempt limit. The syslog connection is held open
by the pusher task and reconnected with a fixed delay on failure, the same pattern
already used by the vessel RFID reader task (§5.3); a message that cannot be written
while the connection is down is retried once it is re-established, giving this path the
same delivery guarantee as the other two.

**One queue per target, 256 events each.** The three classes do not share a queue. A
shared queue couples them: the pusher delivers one event at a time, so an event whose
target is unreachable holds up every event behind it, including those whose own target
is healthy. That coupling is invisible while vmagent and vlagent fail together on
`hist-01`, but vlagent's syslog listener on port 6514 can fail independently of its
JSON-line endpoint on 9429, and a single undeliverable login message would then stop
ATG metrics and transaction events reaching a log server that is up. Within one
target's queue the same blocking is harmless, because an event behind an undeliverable
one is bound for the same place and is equally undeliverable.

**No operational path waits on delivery.** Producers enqueue without blocking: when a
queue is full the oldest event in it is discarded rather than back-pressured onto the
operational path. The ATG poller (§5.1), the Modbus poller (§5.2) and the job endpoints
(§7.4) must never stall on telemetry, and the authoritative record is the durable ledger
(§8), not this stream.

Retrying without a limit and dropping only at the queue means a transient log-server
failure loses nothing — the pusher waits it out — while a sustained one degrades to
loss at the rate the queue fills, without ever slowing fuel movement. Every drop is
recorded at WARN so the loss is visible in the FMS's own logs. This is narrative §5's
"Temporal & procedural" condition 4, and `digital.md §6.1` and `§6.3` describe what it
means for the audit and for divergence as a detection signal.

**A full queue discards its oldest event, not the incoming one.** Under an outage
longer than a queue can absorb, the records nearest the present are the ones an audit
needs, so the gap in the log server's records falls at the start of the outage rather
than at its end. Discarding the oldest event also ensures that an outage that results
in a gap in the log is traceable, as closely as possible, to the time at which the
outage occurred, since the first log event after the outage will be missing.

At the default `FMS_ATG_POLL_INTERVAL` of 30 seconds the ATG poller
fills its 256 slots in a little over two hours, which is the order of outage the FMS
absorbs with no loss at all; job and transaction events accrue at the rate of bunkering
activity, and login events at the rate of operator sign-ins.

Discarding the oldest is a decision rather than a property of the queue type: a bounded
channel rejects the incoming event instead, so the queue is a bounded double-ended
structure from which the producer evicts at the front. The reconciliation user needs to
know which of the two holds in order to read a gap correctly (`digital.md §6.3`), so it
is specified here rather than left to the implementation.

---

## 6  Service configuration

All configuration is supplied via environment variables. The binary reads
`/etc/fms/fms.env` via the systemd `EnvironmentFile` directive; a second
`EnvironmentFile=-/etc/fms/test-overrides.env` is loaded if present (used in
integration tests).

| Variable | Description | Default |
|----------|-------------|---------|
| `FMS_DATABASE_URL` | PostgreSQL connection URL | *(required)* |
| `FMS_LISTEN_PORT` | Port the binary binds to | `443` |
| `FMS_TLS_CERT` | Path to the TLS certificate (PEM) | `/etc/fms/tls/cert.pem` |
| `FMS_TLS_KEY` | Path to the TLS private key (PEM) | `/etc/fms/tls/key.pem` |
| `FMS_ADMIN_USERNAME` | Local admin username | `fms-admin` |
| `FMS_ADMIN_PASSWORD_HASH` | argon2id hash of the local admin password | *(required)* |
| `FMS_LDAP_URI` | LDAPS server URI, addressed by hostname (`ldaps://corp-it/`) so the certificate's hostname SAN validates | *(required)* |
| `FMS_LDAP_BASE_DN` | LDAP base distinguished name | `dc=ravensport,dc=local` |
| `FMS_SEED_CARDS` | RFID cards to enrol at startup if absent from the card table, as `number:label` entries joined by commas; each label is the card's holder (§8.2) | *(none)* |
| `FMS_ATG_HOST` | ATG console hostname or IP | *(required)* |
| `FMS_ATG_PORT` | ATG console TCP port | `10001` |
| `FMS_ATG_POLL_INTERVAL` | ATG poll interval in seconds | `30` |
| `FMS_DISPENSER_HOST` | Dispenser hostname or IP | *(required)* |
| `FMS_DISPENSER_PORT` | Dispenser Modbus TCP port | `502` |
| `FMS_MODBUS_POLL_INTERVAL` | Modbus poll interval in seconds | `5` |
| `FMS_VESSEL_RFID_HOST` | Vessel RFID reader hostname or IP | *(required)* |
| `FMS_VESSEL_RFID_TCP_PORT` | Vessel RFID reader TCP port | `9001` |
| `FMS_VMAGENT_URL` | vmagent import endpoint for ATG metrics | `https://hist-01:8429/api/v1/import` |
| `FMS_VLAGENT_JSONLINE_URL` | vlagent JSON-line endpoint for domain events | `https://hist-01:9429/insert/jsonline` |
| `FMS_VLAGENT_SYSLOG_HOST` | vlagent syslog listener host | `hist-01` |
| `FMS_VLAGENT_SYSLOG_PORT` | vlagent syslog listener port (TLS) | `6514` |
| `FMS_SESSION_TIMEOUT` | Session lifetime in seconds from login (§4) | `1800` |
| `FMS_TANK_CAPACITY_L` | Tank capacity in litres (cavitation guard) | `50000` |
| `FMS_CAVITATION_THRESHOLD_PCT` | Cavitation threshold as a percentage of capacity | `10` |
| `FMS_K_FACTOR` | Dispenser flow meter K-factor (pulses per litre) | `100` |
| `FMS_GRADE` | The fuel grade the tank holds; a job for any other grade is refused (§7.4) | `MGO` |
| `FMS_DENSITY_KG_PER_L` | Reference density of `FMS_GRADE` at 15 °C (§5.2) | `0.840` |

---

## 7  FMS HTTP API

All endpoints return `Content-Type: application/json`. Error responses follow
`{ "error": "<description>" }`. Authenticated endpoints require
`Authorization: Bearer <token>`. Admin endpoints additionally require the `admin` role.

Refusals a caller is expected to act on programmatically carry an additional
`code` field, `{ "error": "<description>", "code": "<code>" }`, so a consumer
decides on the code rather than on the description, which may be reworded. The
HMI forwards both fields unchanged (`hmi.md §7`). The codes:

| Code | Status | Meaning |
|------|--------|---------|
| `cavitation_threshold` | 422 | The current or projected closing volume is at or below the cavitation threshold (§5.1). |
| `atg_read_failed` | 503 | A cavitation check could not be evaluated because the on-demand ATG read failed (§5.1). |
| `no_atg_reading` | 503 | No ATG reading is available yet (§7.3). |

Every refused request is recorded in the FMS journal with its status, code and
description, whatever the endpoint.

### 7.1  Health

#### `GET /api/v1/health`
Public. No authentication required.  
Response `200`: `{ "status": "ok", "version": "<semver>" }`

### 7.2  Authentication

#### `POST /api/v1/auth/login`
Public. For the operator role, both fields are required; for the admin account, `rfid_card_number` is ignored.  
Request: `{ "username": "string", "password": "string", "rfid_card_number": number|null }`  
Response `200`: `{ "session_token": "string", "role": "admin"|"operator" }`  
Response `401`: authentication failed (credential or card number invalid).

#### `DELETE /api/v1/auth/logout`
Authenticated (any role). Invalidates the current session token.  
Response `204`: no body.

### 7.3  Tank level

#### `GET /api/v1/tanks/{tank_id}/level`
Authenticated. `tank_id` must be `"01"`. Returns the most recent reading held in memory
by the ATG poller (§5.1).  
Response `200`: `{ "tank_id": "string", "level_mm": number, "volume_litres": number, "ullage_litres": number, "temperature_c": number, "timestamp": "ISO8601" }`  
Response `503`: no ATG reading available yet.

### 7.4  Jobs

#### `POST /api/v1/jobs`
Authenticated (operator). Creates a new job. Only one job may be in progress at a time;
a new job is rejected while an existing job is in the `active`, `running`, or
`awaiting_bdn` state. The check and slot assignment are atomic (a single write lock) so
concurrent creates cannot both succeed. For `"dispense"` jobs, the FMS applies the
creation check in §5.1; the check does not apply to `"fill"` jobs. If a
`preset_volume` is supplied it must lie in the range the dispenser register can represent:
at most 6553.5 litres (the 16-bit "litres × 10" ceiling), and large enough to round to a
non-zero register value at the register's 0.1 L resolution (at least 0.05 litres).
Otherwise the request returns `422`.

`grade` must be the configured grade, `FMS_GRADE` (§6). The grade determines the fuel's properties, including its density, flashpoint and sulphur content; of these the FMS holds only the density, `FMS_DENSITY_KG_PER_L`. The tank holds one product, so a job for any other grade is a data-entry error, and the density recorded on its transactions would belong to a different fuel. Such a request returns `422` before any other check, and no job is created.

Request: `{ "type": "dispense"|"fill", "counterparty": "string", "grade": "string", "preset_volume": number|null, "expected_volume": number|null }`  
Response `201`: `{ "job_id": "uuid", "type": "string", "status": "active" }`  
Response `409`: a job is already in progress.  
Response `422`: a grade other than `FMS_GRADE`, tank volume at or below the cavitation threshold, or preset volume outside the representable range.  
Response `503`: on-demand ATG read failed.

#### `GET /api/v1/jobs`
Authenticated (operator). Returns the current job, if any, as an array of zero or one
objects. Each object has: `job_id`, `type` (`"dispense"`|`"fill"`), `status` (see
below), `counterparty`, `grade`, `operator`, `rfid_validated`.

Job status values:

| Status | Meaning |
|--------|---------|
| `active` | Job created; no fuel flowing. For dispense jobs: awaiting RFID validation or operator has not yet started the pump. For fill jobs: awaiting close. |
| `running` | Dispense only. Start has been authorised; the Modbus task is starting or monitoring the dispenser, so the pump might not yet have asserted its running status. |
| `awaiting_bdn` | Dispense only. Metering is complete and the transaction row is recorded; awaiting the operator entering the BDN number. |
| `closed` | Job complete. |
| `cancelled` | Job abandoned. |

#### `GET /api/v1/jobs/current`
Authenticated (operator). Returns the job occupying the single job slot, or `404` if
the slot has never been filled. A job that is closed or cancelled remains in the slot
and is still returned, with that status; it no longer blocks a new job from being
created, which is what frees the slot for a successor. Returns a subset of the full
job fields sufficient for HMI status display.  
Response `200`: `{ "job_id": "uuid", "type": "dispense"|"fill", "status": "string", "rfid_validated": bool, "opened_at": "RFC 3339"|null }`  
`opened_at` is the instant the dispense started (`POST /api/v1/jobs/{job_id}/start`),
the value the job's `transaction_open` and `transaction_closed` events carry, at their
precision. It is `null` before the start and for a fill, whose `opened_at` is
transcribed from the delivery ticket at close. The dock operator writes it on the BDN
as the time of commencement (`narrative.md` §2.2).  
Response `404`: no current job.

#### `DELETE /api/v1/jobs/{job_id}`
Authenticated (operator). Cancels an `active` job: the job the dock operator abandons
after the FMS refuses its start (`digital.md §4.2`), before any fuel has moved. The
FMS records the cancellation in `job_cancellations` (§8.5), sets the job's status to
`"cancelled"` in memory, and emits a `JobCancelled` event (§5.4). The record names
the user and session that cancelled the job, which need not be those that created it.
The status check, the record and the status change are made under one hold of the
job slot, so no start or create interleaves with them.  
Response `204`: no body. A cancel of a job already cancelled also answers `204` and
records nothing further, so a cancel whose response was lost can be sent again.  
Response `404`: `job_id` is not the current job — the slot is empty, or holds a
different job. Cancellation resolves against the live job only, so a job that exists
in the `jobs` table but is no longer current also answers `404`.  
Response `409`: the job is not `active`. A running dispense, one awaiting its BDN, and
a closed job cannot be cancelled.

#### `POST /api/v1/jobs/{job_id}/rfid`
Authenticated (operator). Validates a vessel RFID card number against the active job
programmatically (the background RFID task performs the same operation autonomously when
a card is presented at the reader).  
Request: `{ "card_number": number }`  
Response `200`: no body; `rfid_validated` is now true on the job.  
Response `403`: card number not active, or not enrolled to the vessel the job names
(its `counterparty`; §5.3).  
Response `404`: job not found.

#### `POST /api/v1/jobs/{job_id}/start`
Authenticated (operator). Applies the start check in §5.1 before transitioning a
dispense job from `active` to `running`. Rejection leaves the job state unchanged. The
Modbus background task detects a successful transition on its next cycle and writes the
preset and enable registers to the dispenser. The `preset_volume` must lie in the range
the dispenser register can represent: at most 6553.5 litres, and large enough to round
to a non-zero register value at the register's 0.1 L resolution (at least 0.05 litres).
The preset is stored, checked, and written at that resolution: the value used for the
projected closing volume, recorded on the job and transaction, and carried by the
`transaction_open` event is the register-represented volume, not the raw request value.

Request: `{ "preset_volume": number }`  
Response `200`: no body.  
Response `404`: job not found or not a dispense job.  
Response `409`: vessel RFID not yet validated, or job not in `active` state.  
Response `422`: tank volume or projected closing volume at or below the cavitation threshold, or preset volume outside the representable range.  
Response `503`: on-demand ATG read failed.

#### `POST /api/v1/jobs/{job_id}/close`
Authenticated (operator). Closes a fill job and records the transcribed delivery ticket
fields. Issues an on-demand `i201` read for the close volume (§5.1) and creates one
transaction row in the database.  
Request: `{ "ticket_number": "string", "delivery_commenced_at": "ISO8601", "delivered_volume_l": number, "temperature_c": number, "density_kg_per_l": number, "temp_corrected_volume_l": number, "supplier": "string", "tanker_registration": "string" }`  
Response `201`: `{ "transaction_id": "uuid" }`  
Response `404`: job not found or not a fill job.  
Response `409`: fill job not in `active` state.

The three measurement fields are the road tanker's own figures, transcribed from
the delivery ticket (`sim.md` §8.1). They are the fill leg's only route from
paper into the database, and `§8.4`'s columns of the same names hold them. The
tanker measures at its own temperature, independent of the shore probe, which is
what makes the ±1% check on a fill a comparison between two measurements rather
than one (`sim.md` §8.4.1).

The reconciliation audit places a transaction in an audit window by its `opened_at` (`sim.md` §8.3). For a fill, `opened_at` is the ticket's transcribed `delivery_commenced_at` (§8.4), because the FMS does not observe the tanker's pump and creates the job before the transfer starts.

#### `POST /api/v1/jobs/{job_id}/bdn`
Authenticated (operator). Links a Bunker Delivery Note number to a completed dispense job
and closes that job. The dispense transaction is resolved from the database by `job_id`,
so this endpoint remains valid even after the job is no longer the live one. Appends one
BDN linkage row to the `transactions` table; no existing row is modified.

Validations (in order):
- `job_id` must exist and have type `"dispense"` — returns `422` for fill jobs.
- The dispense's metering transaction row must already exist (the Modbus task writes it
  when the preset is reached) — returns `409` if the dispense has not yet completed.
- No BDN may already be linked to this dispense — returns `409` on duplicate.

Request: `{ "bdn_number": "string" }`  
Response `201`: `{ "transaction_id": "uuid" }` (the BDN linkage row's ID).  
Response `404`: job not found.  
Response `409`: dispense not yet recorded, or BDN already linked.  
Response `422`: job is not a dispense.

#### `GET /api/v1/jobs/current/totaliser`
Authenticated (operator). Returns the current dispensed volume for the in-flight job,
sourced from the in-memory value updated by the Modbus poller each cycle. Valid while the
current job is `running`, `awaiting_bdn`, or `closed`.  
Response `200`: `{ "job_id": "uuid", "dispensed_litres": number, "preset_litres": number, "status": "dispensing"|"complete" }`  
Response `404`: no current job, or current job is not in a dispense state.

### 7.5  Card management (admin only)

#### `POST /api/v1/cards`
Admin only. Enrols a new RFID card number to its holder: `label` is the operator's
directory username for an operator card, or the vessel's IMO number for a vessel tag (§8.2).  
Request: `{ "card_number": number, "label": "string" }`  
Response `201`: `{ "card_id": "uuid" }`

#### `GET /api/v1/cards`
Admin only. Returns all currently active cards (latest row per `card_id` where
`active = true`).  
Response `200`: array of `{ "card_id": "uuid", "card_number": number, "label": "string", "enrolled_at": "ISO8601" }`

#### `DELETE /api/v1/cards/{card_id}`
Admin only. Revokes a card by appending a `revoke` row; the card immediately becomes
inactive. The `card_id` path parameter is the UUID assigned at enrolment (not the card
number).  
Response `204`: no body.  
Response `404`: card not found.

Both endpoints additionally emit a `CARD-CHANGE` syslog datagram to the log server
(`host_specs/hist/hist.md §4.3`) after the database write succeeds, carrying `card_id`,
`card_number`, `label`, and `action` (`"enrol"` or `"revoke"`).

---

## 8  Database schema

PostgreSQL 13 or later. The schema is applied via `sqlx` migrations at startup
(`migrations/0001_initial.sql`). All tables use `gen_random_uuid()` (built-in from
PostgreSQL 13) rather than any extension.

### 8.1  `sessions`

One immutable row per login event. The in-memory session map is the authoritative source
for active tokens; this table is the audit trail.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `session_id` | `UUID` | PK, default `gen_random_uuid()` | Stable session identity |
| `username` | `TEXT` | NOT NULL | The authenticated user |
| `role` | `TEXT` | NOT NULL | `"admin"` or `"operator"` |
| `logged_in_at` | `TIMESTAMPTZ` | NOT NULL, default `now()` | Wall-clock time of successful login |

Protected by `sessions_no_update` and `sessions_no_delete` triggers (see §8.7).

### 8.2  `card_table`

Append-only event log for RFID card enrolment and revocation. It holds both operator
cards and vessel tags, distinguished by the holder their `label` names, and is the
FMS's only record of the port's vessels (`digital.md §5.3`). Two rows per card
lifecycle: one `enrol` row and, when revoked, one `revoke` row. The `card_id` UUID is
stable across both events for the same physical card. Active status is determined by the
most recent row for a given `card_id`.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `id` | `BIGSERIAL` | PK | Row sequence |
| `card_id` | `UUID` | NOT NULL | Stable identifier for the physical card |
| `card_number` | `INTEGER` | NOT NULL | 26-bit Wiegand card number |
| `label` | `TEXT` | NOT NULL | Card holder: the operator's directory username for an operator card (the value §4 login matches); the vessel's seven-digit International Maritime Organization (IMO) number for a vessel tag |
| `active` | `BOOLEAN` | NOT NULL | `true` on enrolment, `false` on revocation |
| `action` | `TEXT` | NOT NULL, CHECK IN ('enrol', 'revoke') | The event type |
| `enrolled_at` | `TIMESTAMPTZ` | NOT NULL, default `now()` | Event timestamp |

Indices: `card_table_card_number_idx` on `card_number`; `card_table_card_id_idx` on
`card_id`.

No append-only trigger: the event-log pattern is intrinsic to the design.

### 8.3  `jobs`

One immutable row per job creation. All mutable state (status, `rfid_validated`) is
held in memory only; it is not persisted to this table. The dispense transaction is
linked to its job through the `transactions.job_id` foreign key, not through any
in-memory reference on the job. The row records the job parameters and ATG volume at
the moment of creation.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `job_id` | `UUID` | PK, default `gen_random_uuid()` | Job identity |
| `type` | `TEXT` | NOT NULL, CHECK IN ('dispense', 'fill') | Job type |
| `counterparty` | `TEXT` | NOT NULL | Vessel IMO number (dispense) or tanker registration (fill) |
| `grade` | `TEXT` | NOT NULL | Fuel grade: always `FMS_GRADE` (§7.4) |
| `operator` | `TEXT` | NOT NULL | Username of the creating operator |
| `session_id` | `UUID` | NOT NULL, FK → `sessions.session_id` | The authenticated session the job was created under |
| `atg_volume_at_open_l` | `DOUBLE PRECISION` | — | Tank volume reported by the ATG when the job was created. A dispense job is created only when this value is available; a fill job may record null. |
| `created_at` | `TIMESTAMPTZ` | NOT NULL, default `now()` | Job creation timestamp |

`session_id` binds the job to the login that authorised it. `operator` alone names
a user and does not establish that any particular job was created under an
authenticated session, which is what narrative §5's "only authenticated
transactions occur" requires. The column is on `jobs` rather than on
`transactions` because the job is the operator-initiated act; a transaction
reaches its authorising session through `job_id`. A dispense-close row is written
by the Modbus background task with no request context (§5.2), so a column on
`transactions` would be null on exactly the row that records the fuel movement.

A cancellation is not a change to this row: it is a row of its own in
`job_cancellations` (§8.5).

Protected by `jobs_no_update` and `jobs_no_delete` triggers (see §8.7).

### 8.4  `transactions`

Append-only record of all transaction lifecycle events. Each event is a new row; no
existing row is ever updated. Three distinct event shapes share one table:

- **Dispense close** — written by the Modbus background task when the preset volume is
  reached. Carries `authorisation_id`, totaliser readings, and `volume_dispensed_l`.
- **Fill close** — written by `POST /api/v1/jobs/{id}/close`. Carries the transcribed
  delivery ticket fields.
- **BDN linkage** — written by `POST /api/v1/transactions/{id}/bdn`. Has only
  `references_transaction_id` and `bdn_number` set; other payload columns are null.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `transaction_id` | `UUID` | PK, default `gen_random_uuid()` | Transaction row identity |
| `job_id` | `UUID` | NOT NULL, FK → `jobs.job_id` | Parent job |
| `references_transaction_id` | `UUID` | — | Set on BDN linkage rows; references the dispense close row |
| `authorisation_id` | `UUID` | — | Authorisation UUID generated at dispense start; null on fill and BDN rows |
| `operator` | `TEXT` | — | Operator username; null on BDN linkage rows |
| `counterparty` | `TEXT` | — | Counterparty (vessel or tanker); null on BDN linkage rows |
| `grade` | `TEXT` | — | Fuel grade; null on BDN linkage rows |
| `preset_volume_l` | `DOUBLE PRECISION` | — | Operator-set dispense preset in litres, rounded to the dispenser register's 0.1 L resolution (§7.4); null on fill and BDN rows |
| `start_totaliser_pulses` | `BIGINT` | — | Cumulative totaliser at pump enable; null on fill and BDN rows. `BIGINT` holds the dispenser's full 32-bit unsigned pulse range, which a signed `INTEGER` cannot. |
| `end_totaliser_pulses` | `BIGINT` | — | Cumulative totaliser at pump stop; null on fill and BDN rows |
| `volume_dispensed_l` | `DOUBLE PRECISION` | — | Totaliser delta divided by K-factor; null on fill and BDN rows |
| `temperature_c` | `DOUBLE PRECISION` | — | Observed fuel temperature in °C at the transfer, mirrored from the paper artefact; null on BDN-linkage rows |
| `density_kg_per_l` | `DOUBLE PRECISION` | — | Fuel density at 15 °C, mirrored from the paper artefact; null on BDN-linkage rows |
| `temp_corrected_volume_l` | `DOUBLE PRECISION` | — | Meter volume corrected to 15 °C using the two columns above; null on BDN-linkage rows |
| `ticket_number` | `TEXT` | — | Delivery ticket number; null on dispense and BDN rows |
| `delivered_volume_l` | `DOUBLE PRECISION` | — | Transcribed delivered volume; null on dispense and BDN rows |
| `supplier` | `TEXT` | — | Fuel supplier name; null on dispense and BDN rows |
| `tanker_registration` | `TEXT` | — | Road tanker registration; null on dispense and BDN rows |
| `bdn_number` | `TEXT` | — | Bunker Delivery Note number; set only on BDN linkage rows |
| `atg_volume_at_open_l` | `DOUBLE PRECISION` | — | Temperature-compensated tank volume (`tc_volume_l`, §5.1) reported by the ATG when the fuel transfer began. A dispense-close record always contains this value; a fill-close record may record null. The field is not used for BDN-linkage records. |
| `atg_volume_at_close_l` | `DOUBLE PRECISION` | — | Temperature-compensated tank volume (`tc_volume_l`, §5.1) reported by the ATG when the fuel transfer ended. A failed dispense-close read may use the cached reading if it is no more than two poll intervals old, and otherwise records null; a fill-close record may record null. The field is not used for BDN-linkage records. |
| `opened_at` | `TIMESTAMPTZ` | — | When the dispense or fill started: for a dispense, the instant the FMS started it; for a fill, the transcribed `delivery_commenced_at` (§7) |
| `closed_at` | `TIMESTAMPTZ` | — | When the dispense or fill completed |
| `created_at` | `TIMESTAMPTZ` | NOT NULL, default `now()` | Row insertion timestamp |

Index: `transactions_job_id_idx` on `job_id`.

Protected by `transactions_no_update` and `transactions_no_delete` triggers (see §8.7).

### 8.5  `job_cancellations`

One immutable row per cancelled job (`DELETE /api/v1/jobs/{job_id}`, §7.4). A job's
status is held in memory only (§8.3), so without this row a cancelled job would leave
only its creation in the durable record, and would be indistinguishable from a job
whose outcome was never recorded. The row names who cancelled the job, which `jobs`
cannot: its `operator` and `session_id` are those of the creation.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `job_id` | `UUID` | PK, FK → `jobs.job_id` | The cancelled job. The key admits one cancellation per job. |
| `operator` | `TEXT` | NOT NULL | Username of the user who cancelled the job |
| `session_id` | `UUID` | NOT NULL, FK → `sessions.session_id` | The authenticated session the cancel was made under |
| `cancelled_at` | `TIMESTAMPTZ` | NOT NULL, default `now()` | When the job was cancelled |

Protected by `job_cancellations_no_update` and `job_cancellations_no_delete` triggers
(see §8.7).

### 8.6  `atg_readings`

Time-series store for raw ATG poll results. No append-only constraint; rows are written
on each successful ATG poll cycle and are not referenced by other tables. The log server
is the long-term ATG historian; this table provides a local fallback and audit buffer.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `id` | `BIGSERIAL` | PK | Row sequence |
| `volume_l` | `DOUBLE PRECISION` | NOT NULL | Tank volume in litres as reported by ATG |
| `temperature_c` | `DOUBLE PRECISION` | NOT NULL | Fuel temperature in °C |
| `alarm_flags` | `TEXT` | — | Six-character `i101` system-status body (`000000` when no alarm is active, `030301` when the bund liquid-sensor fuel alarm is latched) |
| `recorded_at` | `TIMESTAMPTZ` | NOT NULL, default `now()` | Timestamp of the poll cycle |

### 8.7  Append-only triggers

A single `deny_mutation()` trigger function (PL/pgSQL) raises an exception on any
`UPDATE` or `DELETE` operation. It is applied to `sessions`, `jobs`, `transactions` and
`job_cancellations` via eight named triggers:

```
sessions_no_update   BEFORE UPDATE ON sessions
sessions_no_delete   BEFORE DELETE ON sessions
jobs_no_update       BEFORE UPDATE ON jobs
jobs_no_delete       BEFORE DELETE ON jobs
transactions_no_update  BEFORE UPDATE ON transactions
transactions_no_delete  BEFORE DELETE ON transactions
job_cancellations_no_update  BEFORE UPDATE ON job_cancellations
job_cancellations_no_delete  BEFORE DELETE ON job_cancellations
```

Corrections to these tables are made by inserting a new row that references the original
row's primary key. There are no in-place edits (narrative §5, digital.md §6.2).

---

## 9  Deployment

### 9.1  Systemd unit

```
[Unit]
Description=Fuel Management System
After=network-online.target postgresql.service
Wants=postgresql.service

[Service]
User=fms
EnvironmentFile=/etc/fms/fms.env
EnvironmentFile=-/etc/fms/test-overrides.env
ExecStart=/usr/local/bin/fms
Restart=always
RestartSec=5
AmbientCapabilities=CAP_NET_BIND_SERVICE

[Install]
WantedBy=multi-user.target
```

`AmbientCapabilities=CAP_NET_BIND_SERVICE` allows the unprivileged `fms` system user to
bind port 443 without running as root. The service depends on `postgresql.service`;
PostgreSQL must be up before the FMS attempts its migration run.

### 9.2  Image build script for `fms-01`

The image build script (`deploy/build-scripts/fms-01.sh.j2`) assembles the `ravensport/fms-01:latest` Incus image. The FMS binary must be compiled before the script is run (`make build-fms`). The script:

- Installs PostgreSQL and argon2 system packages.
- Creates an unprivileged `fms` system user (`shell: /usr/sbin/nologin`).
- Installs the FMS binary at `/usr/local/bin/fms`.
- Installs `build/env/fms-01.env` at `/etc/fms/fms.env`. This file is rendered by
  `render.py` from `topology.yaml` and `world.yaml`; it includes the argon2id hash for
  the default admin password, generated deterministically from `world.yaml`'s
  `crypto.password_salt_seed`.
- Creates `/etc/fms/tls/` and installs the CA-signed certificate and key
  (`build/tls/fms-01/cert.pem`, `key.pem`, generated by
  `deploy/scripts/build-ca.sh`), owned by the `fms` system user.
- Also installs the CA root certificate into the host's system trust store
  (`update-ca-certificates`), the same as every other node (digital.md §2.7).
- Installs the systemd unit file at `/etc/systemd/system/fms.service`.
- Seeds PostgreSQL: starts the cluster, creates the `fms` role and `fms` database.
- Enables PostgreSQL's `log_connections` and `log_disconnections`, so its log file records
  the start and end of every session as well as every failure; the file is collected by
  the security monitoring agent (`host_specs/siem/siem.md §3`).
- Converts `build/network-config/fms-01.yaml` to systemd-networkd `.network` units and
  enables `systemd-networkd`.
- Installs `/etc/hosts` from `build/hosts/fms-01`.
- Enables `fms.service`.
- Purges `cloud-init` from the image.
