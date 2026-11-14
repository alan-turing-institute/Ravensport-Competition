# HMI Implementation Specification

## 1  Role and scope

The Human-Machine Interface (HMI) runs on `hmi-01` in the OT network zone. It is a thin
HTTP presentation proxy: it serves the HMI HTTP API to the dock operator (or, in the
environment, to the dock operator actor in the sim), translates each request into one or more
calls to the FMS, and returns the result. It holds no business logic and no persistent
state beyond the in-memory pending-challenge map described in §4.

The authoritative description of the HMI's role in the environment is `digital.md §2.3`.

---

## 2  Technology

Rust + Axum. The integration-test mock at `infra/sim/tests/mock_digital/hmi.rs` is the
reference implementation and the starting point for the real service. The principal
changes required are:

- Replace Tokio channel calls (`FmsCommand`) with real HTTPS requests to the FMS.
- Replace the `LoopbackHandle` RFID source with a live TCP connection to the operator
  RFID reader (§5).
- Make the binary standalone and configurable via environment variables (§6).

---

## 3  Network interfaces

HAProxy runs on `hmi-01` alongside the HMI binary, following the same pattern as
`sim-host` (sim-network.md §1). It owns the host's network interface, terminates TLS
on port 443, and forwards plain HTTP to the HMI binary on `127.0.0.1:8080`. The HMI
binary has no listener-side TLS: it never presents a certificate. It does, however,
terminate TLS as a client on its outbound leg to the FMS (below).

| Layer | Endpoint | Protocol | Purpose |
|-------|----------|----------|---------|
| HAProxy — external | `0.0.0.0:443` | HTTPS | Requests from the dock operator |
| OpenSSH server — inbound | `10.40.0.12:22` | SSH | Systems administration from `dmz-bastion-01` |
| HMI binary — internal | `127.0.0.1:8080` | HTTP | Forwarded requests from local HAProxy |
| HMI binary → FMS | `fms-01:443` | HTTPS | All FMS API calls |
| HMI binary → RFID | `10.40.0.54:9002` | TCP | Operator RFID reader card stream |

The HAProxy frontend presents a certificate signed by the RavensPort internal
certificate authority (CA), and the HMI binary's HTTPS client to the FMS trusts
that same CA as its sole root — no built-in public root store, and no
`danger_accept_invalid_certs` fallback (digital.md §2.7, §8 decision 7).
`fms-01`'s certificate carries a DNS Subject Alternative Name of `fms-01` only, so
the HMI binary addresses it by hostname, not by IP.

The OpenSSH server authenticates the systems-administration account using the administrator's SSH key, with the settings of `digital.md §4.6`.
The account uses `sudo` for maintenance that requires elevated privileges, and its `sudoers` rule carries `NOPASSWD` (`digital.md §4.6`).

---

## 4  Authentication bridge

The dock operator performs a two-phase login:

1. `POST /api/auth/login` — submits username and password.
2. `GET /api/auth/challenge/{challenge_id}` — long-polls until the operator presents
   their RFID card at the reader.

The real FMS accepts a single call: `POST /api/v1/auth/login` with username, password,
and RFID card number together.

The HMI bridges these by maintaining an in-memory map of pending challenges:

| Phase | HMI action |
|-------|-----------|
| Login request received | Generate a UUID challenge ID. Store `challenge_id → {username, password}` with a time to live of `HMI_CHALLENGE_TIMEOUT_SECS` (§6, service configuration). Return `202 { "challenge_id": "..." }` immediately. |
| Challenge poll received | Block on the operator RFID TCP stream until a card number (decimal integer + newline) is received. Retrieve the stored credentials, call `POST /api/v1/auth/login` on the FMS with all three fields, and return the FMS session token. Discard the challenge entry. |

On challenge timeout (`HMI_CHALLENGE_TIMEOUT_SECS` with no card presented) return `408 { "error":
"challenge_expired" }`.

The challenge timeout is 30 seconds in operation. A compressed scenario divides it by the compression factor (`narrative.md` §6, deviations from real-world practice).

---

## 5  Operator RFID interface

The HMI maintains a single persistent TCP connection to the operator RFID reader at
`10.40.0.54:9002`. The reader delivers one card presentation as a decimal integer
followed by a newline (`\n`). The HMI reads one such line per challenge resolution and
discards it after use. No return channel is required.

The connection is owned by a background reader task that is started at boot,
independently of the HTTP listener: it connects (retrying), reads card frames
continuously, and reconnects on loss, delivering each card to the challenge handler.
This ordering is required. The reader stream is server-push, so the sim presents a card
only once the HMI is subscribed to the reader (sim.md §4.3–4.4); the HMI must therefore
attach proactively at startup rather than lazily on the first challenge. Reading
continuously also detects a dropped or half-open connection (for example, the HAProxy
frontend accepting while its backend is momentarily down) and re-establishes it, so the
subscription does not silently lapse.

Because the connection is maintained off the critical path, the HTTP listener binds and
serves immediately whether or not the reader is reachable at boot: `/health` and the
FMS-facing endpoints are always available, and a challenge simply blocks (up to its TTL)
until the reader connection is live and a card arrives.

---

## 6  Service configuration

All configuration is supplied via environment variables:

| Variable | Description | Default |
|----------|-------------|---------|
| `HMI_LISTEN_ADDR` | Loopback address and port HAProxy forwards to | `127.0.0.1:8080` |
| `HMI_FMS_URL` | Base URL of the FMS | `https://fms-01` |
| `HMI_CA_CERT_PATH` | Path to the RavensPort internal CA root certificate (PEM), the sole trust root for the FMS HTTPS client | `/etc/hmi/tls/ca.pem` |
| `HMI_OPERATOR_RFID_ADDR` | Address of the operator RFID reader | `10.40.0.54:9002` |
| `HMI_CHALLENGE_TIMEOUT_SECS` | Challenge TTL in seconds | `30` |

The binary reads `/etc/hmi/hmi.env` via the systemd `EnvironmentFile` directive.

---

## 7  HMI HTTP API

All endpoints return `Content-Type: application/json`. Error responses take one of
two shapes. An error the HMI itself raises carries a fixed token,
`{ "error": "<snake_case_code>" }`. An error forwarded from the FMS carries the FMS's
body unchanged: `{ "error": "<description>" }`, or
`{ "error": "<description>", "code": "<code>" }` where the FMS tagged the refusal
with a machine-readable code (`fms.md §7`).

Authenticated endpoints require
`Authorization: Bearer <token>`; the HMI extracts the token and forwards it to the
FMS — token validation is entirely FMS-side.

**Upstream error forwarding.** The HMI is a proxy with no business logic (§1),
so for any request it forwards to the FMS it returns the FMS's status code and
response body unchanged rather than re-mapping them onto its own. A meaningful
FMS rejection — for example the 422 raised by the cavitation guard, or a 409
conflict — therefore reaches the caller intact, carrying the FMS's reason and,
where the FMS set one, its machine-readable `code` (`fms.md §7`). A caller
distinguishing one refusal from another decides on that code, not on the
description. Only
failures that arise in the HMI itself (a missing bearer token, an unread
operator card, an expired login challenge, a job identifier that is not a UUID)
produce an HMI-originated status. A
failure to reach the FMS at all is reported as `502 Bad Gateway`
(`{ "error": "upstream_unreachable" }`); an unparseable FMS reply as `502`
(`{ "error": "invalid_upstream_response" }`). Every error response is written to
the HMI's log with its reason (digital.md §6.4).

### 7.0  Health

#### `GET /health`
Response `200`: `{ "status": "ok" }`  
No authentication required. Intended for startup probes and process supervisors.

### 7.1  Authentication

#### `POST /api/auth/login`
Request: `{ "username": "string", "password": "string" }`  
Response `202`: `{ "challenge_id": "uuid" }`  
Response `401`: `{ "error": "invalid_credentials" }`

#### `DELETE /api/auth/logout`
Invalidates the FMS session. The HMI does not inspect the FMS's status, so an
already-expired or otherwise invalid session still logs out cleanly.  
Response `204`: no body.  
Response `401`: no bearer token on the request.  
Response `502`: the FMS could not be reached.

#### `GET /api/auth/challenge/{challenge_id}`
Blocks until the operator RFID card is presented at the reader (up to `HMI_CHALLENGE_TIMEOUT_SECS`).  
Response `200`: `{ "token": "string", "expires_at": "ISO8601" }`  
Response `401`: `{ "error": "rfid_mismatch" }`  
Response `408`: `{ "error": "challenge_expired" }`

### 7.2  Tank level

#### `GET /api/tanks/{tank_id}/level`
The HMI proxies this from the FMS, which holds the latest ATG reading.  
Response `200`: `{ "tank_id": "string", "level_mm": number, "volume_litres": number, "ullage_litres": number, "temperature_c": number, "timestamp": "ISO8601" }`  
Response `503`: no ATG reading is available yet (`fms.md §7.3`).

### 7.3  Jobs

#### `POST /api/jobs/refuel`
Request: `{ "vessel_id": "string", "product": "string", "requested_volume_litres": number }`  
`vessel_id` is the vessel's seven-digit International Maritime Organization (IMO) number,
as written on the BDN; the FMS matches it against the vessel tag's card label (`fms.md §5.3`).  
Response `201`: `{ "job_id": "uuid", "status": "pending_vessel_rfid" }`  
Response `409`: a job is already in progress (`fms.md §7.4`); that job must reach a
closed or cancelled state before another can be created.  
Response `422`: current ATG-reported tank volume at or below the cavitation threshold
(`fms.md §5.1`); no job is created.  
Response `503`: the cavitation check cannot be evaluated because the on-demand ATG read
failed; no job is created.

#### `POST /api/jobs/tank-fill`
Request: `{ "tanker_registration": "string", "product": "string", "delivery_volume_litres": number }`  
Response `201`: `{ "job_id": "uuid", "status": "open" }`  
Response `409`: a job is already in progress (`fms.md §7.4`).

#### `GET /api/jobs/current`
The current job; there is only ever one. Response `404` when the job slot is empty. A
closed or cancelled job continues to occupy the slot and is returned with status
`complete` or `cancelled`, respectively (§8.2).  
Response `200`: `{ "job_id": "uuid", "type": "refuel"|"tank-fill", "status": "string", "opened_at": "string"|null }`

Status values and their translation from FMS status fields are defined in §8.2.
`opened_at` is the FMS's, passed through unchanged (`fms.md §7.4`): the instant the
dispense started, which the dock operator writes on the BDN as the time of
commencement (`narrative.md` §2.2).

#### `POST /api/jobs/{job_id}/dispenser/start`
Valid only when job status is `rfid_validated`.  
The HMI forwards the start request to the FMS.

Request: `{ "preset_volume_litres": number }`  
Response `200`: `{ "job_id": "uuid", "status": "dispensing" }`  
Response `409`: the vessel RFID card has not been validated against the job, or the
job is not in the active state; the FMS's message is forwarded unchanged
(`fms.md §7.4`).  
Response `422`: ATG-reported tank volume or projected closing volume at or below the
cavitation threshold, or preset volume outside the range the dispenser register can
represent; the job does not enter the dispensing state.  
Response `503`: the cavitation check cannot be evaluated because the on-demand ATG read
failed; the job does not enter the dispensing state.

#### `GET /api/jobs/current/dispenser/totaliser`
The HMI proxies current totaliser data from the FMS, which polls the
  dispenser Modbus registers.
Response `200`: `{ "job_id": "uuid", "dispensed_litres": number, "preset_litres": number, "status": "dispensing"|"complete" }`

#### `PATCH /api/jobs/{job_id}`
Request: `{ "bdn_number": "string" }`  
Response `200`: `{ "job_id": "uuid", "bdn_number": "string" }`

#### `DELETE /api/jobs/{job_id}`
Cancels the job, forwarding the FMS's decision (`fms.md §7.4`). Lets the
operator abandon a job whose start the FMS has refused, freeing the single job
slot.  
Response `204`: no body.  
Response `400`: `{ "error": "invalid_job_id" }`; the job identifier is not a UUID,
and the cancel is refused before anything is forwarded to the FMS.  
Response `404`: the job is not the current job — either no such job, or one the FMS
no longer holds in its single job slot (`fms.md §7.4`).  
Response `409`: the job is not active, so the FMS will not cancel it
(`fms.md §7.4`); the FMS's message is forwarded unchanged.

### 7.4  Delivery tickets

#### `POST /api/delivery-tickets`
Request: `{ "job_id": "uuid", "ticket_number": "string", "delivery_commenced_at": "ISO8601", "delivered_volume_litres": number, "temperature_c": number, "density_kg_per_l": number, "temp_corrected_volume_litres": number, "supplier": "string", "tanker_registration": "string" }`  
Response `201`: `{ "ticket_id": "uuid", "job_id": "uuid", "recorded_at": "ISO8601" }`

Every field is transcribed from the road tanker's printed ticket and forwarded
unchanged to the FMS's `POST /api/v1/jobs/{job_id}/close` (`fms.md` §7), with the
two volumes renamed to the FMS's `_l` form. `delivery_commenced_at` is the start
time printed on the ticket, which the FMS records as the transfer's `opened_at`;
a ticket without it is refused before anything is forwarded.

---

## 8  FMS API mapping

For each HMI endpoint, the FMS call it makes and any field translation required.

| HMI endpoint | FMS call | Notes |
|-------------|----------|-------|
| `POST /api/auth/login` | *(none — stores challenge only)* | Credentials held in memory until challenge resolved |
| `GET /api/auth/challenge/{id}` | `POST /api/v1/auth/login` | Combines stored credentials + RFID card number into single FMS call |
| `DELETE /api/auth/logout` | `DELETE /api/v1/auth/logout` | The FMS's status is not inspected; only an unreachable FMS fails the call |
| `GET /api/tanks/{id}/level` | `GET /api/v1/tanks/{id}/level` | — |
| `POST /api/jobs/refuel` | `POST /api/v1/jobs` | `vessel_id` → `counterparty`, `product` → `grade`, `type: "dispense"` |
| `POST /api/jobs/tank-fill` | `POST /api/v1/jobs` | `tanker_registration` → `counterparty`, `product` → `grade`, `type: "fill"` |
| `GET /api/jobs/current` | `GET /api/v1/jobs/current` | Status translation required — see §8.2; `opened_at` passed through |
| `POST /api/jobs/{id}/dispenser/start` | `POST /api/v1/jobs/{id}/start` | `preset_volume_litres` → `preset_volume` |
| `GET /api/jobs/current/dispenser/totaliser` | `GET /api/v1/jobs/current/totaliser` | — |
| `PATCH /api/jobs/{id}` (BDN) | `POST /api/v1/jobs/{id}/bdn` | FMS resolves the dispense transaction from the ledger; valid only for a dispense |
| `DELETE /api/jobs/{id}` | `DELETE /api/v1/jobs/{id}` | — |
| `POST /api/delivery-tickets` | `POST /api/v1/jobs/{id}/close` | Delivery ticket fields passed directly |

### 8.1  FMS gaps

Both endpoints previously missing from the FMS have been implemented:

1. **Tank level** — `GET /api/v1/tanks/{tank_id}/level`. Exposes the latest ATG reading
   held in FMS state.

2. **Dispenser totaliser** — `GET /api/v1/jobs/current/totaliser`. Exposes the
   dispensed volume tracked by the FMS Modbus background task.

The refuel and tank-fill flows are now exercisable end-to-end.

### 8.2  Job status translation

The FMS represents job state with a `status` field and a separate `rfid_validated`
boolean. The HMI translates to the status strings the dock operator expects:

| FMS `status` | FMS `rfid_validated` | HMI status |
|-------------|---------------------|------------|
| `active` | `false` | `pending_vessel_rfid` |
| `active` | `true` | `rfid_validated` |
| `running` | — | `dispensing` |
| `awaiting_bdn` | — | `awaiting_bdn` |
| `closed` | — | `complete` |
| `cancelled` | — | `cancelled` |

---

## 9  Deployment

### 9.1  Systemd unit

```
[Unit]
Description=RavensPort HMI
After=network-online.target
Wants=network-online.target

[Service]
User=hmi
EnvironmentFile=/etc/hmi/hmi.env
ExecStart=/usr/local/bin/hmi
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

### 9.2  Image build script for `hmi-01`

The image build script (`deploy/build-scripts/hmi-01.sh.j2`) assembles the `ravensport/hmi-01:latest` Incus image. The HMI binary must be compiled before the script is run (`make build-hmi`). The script:

- Installs HAProxy.
- Creates an unprivileged `hmi` system user (`shell: /usr/sbin/nologin`).
- Installs the HMI binary at `/usr/local/bin/hmi`.
- Installs `build/env/hmi-01.env` at `/etc/hmi/hmi.env`.
- Installs the CA-signed TLS certificate and key (`build/tls/hmi-01/hmi-01.pem`,
  generated by `deploy/scripts/build-ca.sh`) and installs the HAProxy configuration at
  `/etc/haproxy/haproxy.cfg` (§9.3).
- Installs the CA root certificate (`build/ca/ca.pem`) at `/etc/hmi/tls/ca.pem`, so
  the HMI binary's HTTPS client can validate `fms-01` (digital.md §8 decision 7), and
  into the host's system trust store (`update-ca-certificates`), the same as every
  other node (digital.md §2.7).
- Converts `build/network-config/hmi-01.yaml` to systemd-networkd `.network` units and
  enables `systemd-networkd`.
- Installs `/etc/hosts` from `build/hosts/hmi-01`.
- Enables `hmi.service`, `haproxy`, and `ravensport-hosts.service`.
- Purges `cloud-init` from the image.

### 9.3  HAProxy

HAProxy runs as a service on `hmi-01`, supervised by systemd, and is started before
the HMI binary so that the port 443 frontend is available as soon as the host is
reachable.

**Frontend.** Listens on `0.0.0.0:443` in HTTP mode with TLS termination. The
certificate is signed by the RavensPort internal CA (digital.md §2.7, §8 decision 7);
the sim-host HAProxy validates it against the CA root and the certificate's hostname
(`ssl verify required ca-file ... sni str(hmi-01) verifyhost hmi-01` on the
dock-operator backend — sim-network.md §5, §6).

**Backend.** Forwards plain HTTP to `127.0.0.1:8080` (the HMI binary's loopback
listener). No TLS on the backend path.

The concrete HAProxy configuration is a deployment artefact generated from the
cloud-init template.
