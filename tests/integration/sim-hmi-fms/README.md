# `sim-hmi-fms` — three-way integration test

This crate runs the **real sim**, the **real HMI**, and the **real FMS** together
in one process and drives a full fuel-delivery cycle across the documented
sim<->digital APIs. It is the analogue of the sim's own `full_sim.rs`, but with
the mocked digital world (`mock_digital/{fms,hmi,relay}`) replaced by the actual
HMI and FMS code.

It deliberately does **not** test the container or network topology. Its purpose
is to verify that the three hosts' public APIs agree — payload shapes, status
translation, and the protocols at the boundary — which no other test does, since
each host is otherwise tested only against its own hand-written stub of its
neighbour.

## What is real and what is stubbed

Real (the system under test):

- The sim: tank physics, ATG console (Veeder-Root TLS), dispenser (Modbus TCP),
  RFID readers (serial-over-TCP), and the actors that play the humans (dock
  operator, tug engineer, road-tanker driver, fuel supplier).
- The HMI: its real HTTP API, FMS client, and operator-RFID reader.
- The FMS: its real HTTP API, job state machine, append-only Postgres schema and
  triggers, and the ATG / Modbus / vessel-RFID / log-pusher background tasks.

Stubbed or embedded (infrastructure a port's IT/OT team would not itself write):

- **PostgreSQL** — a real server via `postgresql_embedded` (no Docker; downloads
  a binary on first run, then caches it under the user's home directory).
- **Corp-IT directory** — a minimal bind-only LDAP server (`ldap3_proto`).
- **Log-server** — an HTTP sink that records the FMS's lifecycle events; it is the
  test's observation surface.
- **TLS / HAProxy** — omitted; HMI<->FMS speak plain HTTP. In deployment HAProxy
  terminates TLS and binds source IPs.

## Running

Requires `cargo` and (on first run only) network access to fetch the embedded
PostgreSQL binary. No Docker daemon is needed, so it runs on the macOS dev box as
well as in the `ravensport` Lima VM.

```
cargo test -p sim-hmi-fms --test full_cycle -- --nocapture
```

The embedded-PostgreSQL setup timeout is raised from the crate default of five
seconds to sixty. The default governs `initdb` and `pg_ctl start`, which can
exceed five seconds on a loaded machine (the test compiles and runs
concurrently) and otherwise fail with `DatabaseInitializationError("deadline has
elapsed")`.

## What the test exercises

The run drives a complete cycle over the real APIs: operator login (two-phase
password + RFID), a tug refuel job (RFID-gated dispense metered over Modbus, BDN
linked), and the tank-fill that the reorder control law triggers once the dispense
drains the tank below threshold. The terminating assertion is the control law: the
ATG level the FMS recorded when the fill opened must be at or below the reorder
threshold.

## Startup ordering

The digital world (FMS then HMI) is brought up **before** the sim is built. This
mirrors a real deployment — the FMS and HMI are running when the plant begins
operating — and removes a startup race: the sim's dock-operator attempts its HMI
login the instant it is spawned, and its retry budget (a few backoffs on the
time-compressed sim clock) is far shorter than HMI startup, so the HMI must
already be listening. The FMS's background pollers reconnect with backoff, so the
FMS tolerates the sim's ATG console and dispenser not being bound until the sim
is built.

## Notes

- Time runs on `WallTimeMultiple(300)`, which keeps the sim and the real
  (real-time) FMS/HMI on one scaled clock while compressing the run. Because
  completion is now read from the dispenser's status register rather than a
  volume threshold, the green path no longer depends on a pulse landing on an
  exact boundary, so it is robust to the sim/real-time interleaving.
