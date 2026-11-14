# World configuration

Specification for `world.yaml`: the single source of truth for **non-network "world facts"** relating to the Ravensport environment. These include operator and admin identities, RFID card numbers, directory distinguished names, database parameters and physical constants. It is the world-facts counterpart to `topology.yaml`, which defines network and firewall facts.

This document covers the structure and content of `world.yaml` and how it is projected, via the render pipeline, into build artefacts for each network host.
The wire protocols, API contracts, and entity behaviours that *consume* these facts are specified in their own documents and are out of scope here.

**Contents:**
1. [Motivation](#motivation)
2. [Scope and the build-time boundary](#boundary)
3. [Structure of `world.yaml`](#structure)
4. [Projection targets](#projection)
5. [Render-time validation](#validation)
6. [Secrets](#secrets)
7. [Decisions on seeded-but-mutable tables](#open)
8. [Per-profile overrides](#profile-overrides)

---

## 1. Motivation<a id='motivation'></a>

Several facts must be held identically by more than one network component.

Examples include:
- the flow-meter K-factor governs the dispenser's auto-stop arithmetic (`dispenser-modbus-tcp-spec.md §9`) *and* the FMS's litre reconstruction (`fms.md §6`, `FMS_K_FACTOR`);
- The fuel grade (MGO) is named on the sim paper artefacts and must be accepted by the FMS on a job (`fms.md §7.4`, `FMS_GRADE`)
- the grade's reference density is the sim's `rho_ref_kg_per_L` (`sim.md §1.1`), used for the temperature correction on both paper artefacts, *and* the FMS's `FMS_DENSITY_KG_PER_L` (`fms.md §5.2`), written onto every dispense transaction row. A divergence would put a different density on the paper and in the record the end-of-period audit compares it against;
- the tank capacity sets the sim's overfill threshold (`sim-impl.md`, fuel tank) *and* the FMS's cavitation guard (`fms.md §5.1`, `FMS_TANK_CAPACITY_L` and `FMS_CAVITATION_THRESHOLD_PCT` in `fms.md §6`) *and* the ATG console's shortfall computation (`tls-protocol.md §7`);
- operator and vessel RFID numbers are presented by the sim's actors *and* validated against the FMS card table;
- LDAP distinguished names appear in the FMS bind string *and* in the directory that answers it.

To avoid duplication, `world.yaml` defines each such fact once. Every consumer reads a *generated projection* of it, never the file itself. This is the same pattern established by `topology.yaml` for addressing and firewall policy: one declarative source, projected by `render.py` into whatever form each consumer needs.

---

## 2. Scope and the build-time boundary<a id='boundary'></a>

The `world.yaml` file is an authoring artefact that resides only on the build host. It is an input to `render.py` and is never copied into any deployed instance.

An operator working inside the deployed environment must see what a real OT network would present. A real network contains no roster file mapping every identity, credential, and device parameter; that kind of god-file is a build-time convenience, not a runtime network object.

The binaries are deliberately self-contained with no Incus internet access and no runtime config fetch (`deployment.md §8`, `sim-network.md §7`), and `world.yaml` must not become a back door around that. The truth is centralised for the *authors*; it is distributed across realistic locations in the deployed network.

The line is therefore absolute: `world.yaml`, `render.py`, and everything under `build/` live on the build host and are never present in any deployed instance. What enters the environment is only each consumer's realistic slice, projected and placed as specified in §4.

---

## 3. Structure of `world.yaml`<a id='structure'></a>

The file is organised by **domain entity**, not by consumer, so that no fact appears twice. The example values below are illustrative. A value is defined under the entity it belongs to; every consumer that needs it derives it at render time.

```yaml
identities:
  admin:
    username: fms-admin                                              # [shared, credential] FMS and directory identity
    password: "<sim-only plaintext; hashed at render time, see §6>"  # [shared, credential] also reused by admin-01's local systems-administrator account
  ldap_admin:
    dn: "cn=admin,dc=ravensport,dc=local"                            # [credential] slapd rootDN; suffix equals ldap.base_dn by directory convention
    password: "<sim-only plaintext; corp-it slapd rootDN, see §6>"   # [credential]
  hist_admin:
    username: hist-admin                                             # [credential]
    password: "<sim-only plaintext; hashed at render time, see §6>"  # [credential] hist-01 Grafana
  reconciliation_user:
    username: recon-user                                             # [shared, credential]
    password: "<sim-only plaintext; hashed at render time, see §6>"  # [shared, credential] hist-dmz-01 Grafana
    workstation_password: "<sim-only plaintext; hashed at render time, see §6>"  # [shared, credential] recon-01 login; distinct from the Grafana password above
  systems_administrator:
    username: systems-admin                                          # [shared, credential]
    workstation_password: "<sim-only plaintext; hashed at render time, see §6>"  # [shared, credential] admin-01 directory login
  siem_admin:
    username: siem-admin                                             # [credential]
    password: "<sim-only plaintext; hashed at render time, see §6>"  # [credential] siem-01 management interface
  operators:
    - username: jrenard                    # [shared, credential]
      password: "<sim-only plaintext>"     # [shared, credential]
      rfid_card: 84217                     # [shared]

roster:
  refuel_volume_min_l: 500                 # [scenario] lower bound of each tug's requested volume (narrative.md §2.2; sim.md §6)
  refuel_volume_max_l: 3000                # [scenario] upper bound of each tug's requested volume, at most 6,553.5 (§5)
  vessels:
    - imo_number: 1999997                  # [shared, scenario] the vessel identifier: FMS card label and job counterparty, sim and BDN
      name: "Tug Alpha"                    # [scenario] written on the BDN
      rfid_card: 99999                     # [shared, scenario]
      mean_time_away_s: 72000              # [scenario] the tug's mean time away from the dock (sim.md §6)
    # … one entry per tug

ldap:
  base_dn: "dc=ravensport,dc=local"        # [shared]
  people_ou: "ou=people"                   # [shared]

database:
  fms:
    name: fms                              # [shared]
    role: fms                              # [shared]

process:
  tank_capacity_l: 50000                   # [shared, scenario]
  cavitation_threshold_pct: 10             # [scenario]
  overfill_interlock_pct: 95               # [scenario] level at which the hardwired overfill interlock opens (narrative.md §2.1; sim.md §1.3)
  dispenser_flow_rate_l_per_min: 300       # [scenario] the dispenser's flow rate (sim.md §1.1)
  k_factor: 100                            # [shared]
  grade: "MGO"                             # [shared, scenario] must be a fuel grade defined in narrative.md
  density_kg_per_l: 0.840                  # [shared, scenario] reference density of `grade` at 15 °C
  sulphur_pct_mm: 0.10                     # [scenario] sulphur content of `grade`, % mass/mass
  flashpoint_c: 66.0                       # [scenario] flashpoint of `grade`; a chosen value, not a measurement

fuel_supplier:
  reorder_threshold_l: 20000               # [scenario] reported volume at or below which the supplier orders (narrative.md §2.1; sim.md §5.3)
  delivery_volume_l: 18000                 # [scenario] volume each road tanker carries (narrative.md §2.1)
  lead_time_s: 21600                       # [scenario] time from the order to the tanker's arrival, "a short time after" (narrative.md §2.1)
  tanker_pump_rate_l_per_min: 600          # [scenario] the road tanker's pump rate (sim.md §1.1)
  poll_interval_s: 900                     # [scenario] time between the supplier's polls of the ATG (digital.md §4.4; sim.md §5.3)

intervals:
  atg_poll_s: 30                           # [scenario]
  modbus_poll_s: 5                         # [scenario]
  session_timeout_s: 1800                  # [shared, scenario] FMS bearer-token lifetime
  challenge_timeout_s: 30                  # [scenario] HMI window for RFID presentation after login initiation
  admin_check_interval_h: 24               # [scenario] systems administrator's routine-check cadence
  dock_operator_poll_s: 30                 # [scenario] how often the dock operator checks a job's status on the HMI (sim-impl.md, dock operator)
  file_integrity_interval_h: 12            # [scenario] how often each host's agent checks its files for changes (siem.md §2.1, the agent on every monitored host)
  inventory_interval_h: 1                  # [scenario] how often each host's agent reports its packages, ports, processes and interfaces (siem.md §2.1, the agent on every monitored host)
  listening_port_interval_s: 360           # [scenario] how often each host's agent reports the ports it listens on (siem.md §2.1, the agent on every monitored host)

audit:
  period: month                            # [scenario] the reconciliation audit's period, set by the port (narrative.md §3): month, week or day
  settling_delay_s: 21600                  # [scenario] how far into the following period each audit runs
  boundary_margin_s: 7200                  # [scenario] how far beyond each end of the period the audit also retrieves transaction records (sim-impl.md, reconciliation user)

crypto: # authored once, NOT projected to any node
  password_salt_seed: "0f1e2d3c4b5a69788796a5b4c3d2e1f0"  # [determinism] 16-byte hex; per-identity salt = derive(seed, username)
  ca_not_before: "2026-09-01"  # [determinism] notBefore for every internal-CA certificate (§6); must move forward at least annually
```

The addresses, ports, and firewall rules a component needs are *not* duplicated here — those remain mastered in `topology.yaml` and are joined in at render time. `world.yaml` defines only the facts `topology.yaml` does not already own.

The two files are kept separate because they change on different axes and carry different significance: addressing "carries no architectural significance" and "could be changed without affecting any design decision" (`deployment.md §2.2`), whereas a value like `k_factor` is behaviourally load-bearing and changing it changes what the environment *does*.

**Provenance.** Every value in `world.yaml` is a fact about the fictional asset and must trace to `narrative.md`, the authoritative source for the port and its processes. A value not derivable from the narrative — a new identity, a changed physical constant, an added fuel grade — requires an agreed addition to `narrative.md` before it is entered here. Because `world.yaml` is a convenient single file of constants, it is the most likely place for an unsourced value to enter the environment unnoticed; this rule is what keeps it honest.

**Inclusion criterion.** A fact belongs in `world.yaml` if, and only if, it falls into at least one of four categories:

- **Shared** — more than one component must agree on it, such as `process.k_factor`.
- **Determinism** — it is part of the reproducibility surface, such as `crypto.password_salt_seed` or `crypto.ca_not_before`: each is read by a single consumer (`render.py`), but the authors must control it centrally for determinism, and in `ca_not_before`'s case must update it annually to avoid staleness (§5, §6).
- **Credential** — a password or the username it belongs to, authored here so that no plaintext is held in any component's source and each is hashed at render time (§6). A credential may have a single consumer: `identities.admin` is read only by the FMS.
- **Scenario** — a different Ravensport scenario may legitimately change it: the fuel and its properties, the tug roster and each tug's time away, the supplier's ordering, poll intervals and timeouts. A scenario parameter may have a single consumer. `process.sulphur_pct_mm` and `process.flashpoint_c` are read only by the sim, for the bunker delivery note (`sim.md §8.2`), and are authored beside `process.grade` so that a change of fuel is a change to one file.

A constant in none of the four stays its consumer's own constant: service-internal FMS and HMI constants are not lifted here, and the sim keeps its private physical constants compiled into its source (the partition below).

**Categories are annotated.** Every value in `world.yaml` carries a trailing comment that opens with its categories in brackets — `[shared]`, `[determinism]`, `[credential]`, `[scenario]`, comma-separated where more than one applies — followed by any explanatory text, as in the example above. `deploy/tests/unit/test_world_categories.py` fails on a value without one, or with a category outside the four.

**Scenarios.** `world.yaml` describes a single scenario which includes physical constants, passwords, and seeds. Only "scenario" parameters - such as fuel grade, each tug's time away - are expected to be editable; credentials and the determinism inputs are not. The quantities, rates and frequencies `narrative.md` gives describe its default scenario (the opening of `narrative.md`, after its scope lists), so a scenario may set them differently without a narrative addition, provided the processes, actors, systems and correctness conditions it describes are unchanged. The render does not hold them to the narrative's figures; it rejects only values that are physically meaningless or inconsistent with one another (§5). Any other value outside what `narrative.md` states remains subject to the provenance rule above and requires a narrative addition.

**Sim config partition.** The sim's `Config` literal (`infra/sim/src/coupled.rs`) holds far more constants than `world.yaml` projects. Only facts in the categories above are lifted; the sim's private physical constants are neither authored here nor projected. The lists below define the current partition and must be confirmed against the consuming specifications (`sim-impl.md`, `fms.md`, `tls-protocol.md`, `dispenser-modbus-tcp-spec.md`) before implementation.

World facts, projected to the sim:
- `process.tank_capacity_l`, `process.k_factor`, `process.density_kg_per_l`
- `process.grade`, `process.sulphur_pct_mm`, `process.flashpoint_c` — the product named and described on both paper artefacts
- `identities.operators[].rfid_card`, `roster.vessels[].rfid_card`
- `roster.vessels[].imo_number` — the vessel's International Maritime Organization (IMO) number: the identifier the dock operator enters on a dispense job, which the FMS matches against the vessel card's label, and which the dock operator writes on the BDN
- `roster.vessels[].name` — the vessel's name, written on the BDN alongside its IMO number (`narrative.md §2.2`)
- `roster.vessels[].mean_time_away_s` — each tug's mean time away from the dock, from which the tug arrival generator draws that tug's times away (`sim.md §6`); a scenario parameter with the sim as its only consumer
- `roster.refuel_volume_min_l` and `roster.refuel_volume_max_l` — the bounds of each tug's requested volume, the same for every tug (`sim.md §6`)
- `process.dispenser_flow_rate_l_per_min` and `process.overfill_interlock_pct` — the dispenser's flow rate and the overfill interlock level (`sim.md §1.1`, `§1.3`)
- `fuel_supplier.reorder_threshold_l`, `.delivery_volume_l`, `.lead_time_s` and `.poll_interval_s` — the supplier's ordering (`sim.md §5.3`); `fuel_supplier.tanker_pump_rate_l_per_min` — the road tanker's pump rate, which is the tank's inflow rate (`sim.md §1.1`)
- `intervals.session_timeout_s` — the FMS session lifetime, by which the reconciliation user's retrieval reaches back for the `LOGIN` that opened a session (`sim-impl.md`, reconciliation user); shared with the FMS, which reads it as `FMS_SESSION_TIMEOUT`
- `audit.period`, `audit.settling_delay_s` and `audit.boundary_margin_s`: the period each reconciliation audit covers, how far into the following period the audit runs, and how far beyond each end of the period the audit also retrieves transaction records (`sim-impl.md`, reconciliation user). The three audit values are the port's procedure, scenario parameters with the sim as their only consumer. The rest of the actor's timings are the sim's own and stay in its source
- `identities.operators[].username` and `.password` — the dock operator's HMI login
- `identities.reconciliation_user.username`, `.password` and `.workstation_password` — the reconciliation user's log-server and workstation logins
- `identities.systems_administrator.username` and `.workstation_password`: the systems administrator's workstation login
- `intervals.admin_check_interval_h` — the systems administrator's routine-check cadence
- `intervals.dock_operator_poll_s`: how often the dock operator checks a job's status on the HMI (`sim-impl.md`, "Dock operator"), a scenario parameter with the sim as its only consumer
- the public host key of `admin-01`, derived at render time (§6): the systems administrator actor verifies it at sign-in
- the internal certificate authority's root certificate — not a `world.yaml` value but derived from it deterministically (§6) — by which the reconciliation user verifies the workstation's remote-desktop certificate. `build-ca.sh`, which signs it, writes it as `ca.pem` beside `sim-constants.toml` in `infra/sim/`, committed so that the crate builds standalone, and `build.rs` compiles it into the binary; a test keeps the committed copy equal to the rendered one

The credentials are projected **as plaintext**, unlike every digital-world
target, which receives a hash.
The sim's actors present them the way a person does.
The dock operator types a password into the HMI, the reconciliation user types one into a terminal, and the systems administrator types one at the workstation's Secure Shell (SSH) sign-in.
A verifier would be useless to them. §6 records
why this is consistent with the rest of the secrets handling rather than an
exception to it.

Sim-only constants, staying in sim source and not projected:
- Physical quantities: `initial_volume_l`, `initial_temperature_c`, `k_l_per_mm`
- Actor and appliance constants: `driver_id`, `tanker_registration`, `tug_seed`, `probe_noise_enabled`, `probe_noise_seed`

If a sim-only constant later gains a second consumer — for example if the FMS begins recording `driver_id` on a delivery — or a scenario needs to vary it, it moves into `world.yaml` with an explicit projection target at that point, as the flow rates and the supplier's ordering values have.

**Compressed scenarios.** A compressed scenario varies every value in operation that the compression factor scales (`narrative.md` §6, deviations from real-world practice). Each value in operation that the compression factor scales, and that a component receives from `world.yaml`, is a scenario parameter there. An additional override file sets the compression factor (`deployment.md` §7.3, additional override file). The compression factor is no fact about the fictional asset, so `world.yaml` must not hold it. The scenario parameters that the simulator receives scaled are `fuel_supplier.tanker_pump_rate_l_per_min`, `process.dispenser_flow_rate_l_per_min`, `roster.vessels[].mean_time_away_s`, `fuel_supplier.poll_interval_s`, `fuel_supplier.lead_time_s`, `intervals.admin_check_interval_h`, `intervals.dock_operator_poll_s`, `intervals.session_timeout_s`, `audit.period`, `audit.settling_delay_s` and `audit.boundary_margin_s`. The scenario parameters that the FMS receives scaled are `intervals.atg_poll_s`, `intervals.modbus_poll_s` and `intervals.session_timeout_s` (`fms.md` §4, authentication and session management; §5.1, ATG poller; §5.2, Modbus poller). The HMI receives `intervals.challenge_timeout_s` scaled (`hmi.md` §4, authentication bridge). `world.yaml` holds every scenario parameter at its value in operation, and `render.py` derives each scaled value from the value in operation (§4, projection targets). The time ranges in which the audit retrieves a period's records follow from the dock's arrival rate (`sim.md` §6, tug arrival generator), so the simulator holds no compression factor (`sim-impl.md`, "Compressed scenarios"). The scenario parameters that the security monitoring's agents receive scaled are `intervals.file_integrity_interval_h`, `intervals.inventory_interval_h` and `intervals.listening_port_interval_s` (`siem.md` §2.1, the agent on every monitored host).

---

## 4. Projection targets<a id='projection'></a>

The `render.py` pipeline reads `world.yaml` and generates build artefacts for each host, respecting the following constraints:
- Every consumer reads a generated projection of `world.yaml`; no consumer reads the file itself.
- Every projection is baked into its consumer's image at build time, before the instance ever starts (`deployment.md §6`), and no generated intermediate or provisioning residue survives into a deployed instance.
- Each parameter is projected to the location a real deployment would hold it, and nowhere else.
- There is no runtime configuration channel: no config server, no config fetch, and no phone-home from any deployed instance (`deployment.md §8`).
- For a compressed scenario, `render.py` derives each scaled value from its value in operation and the compression factor. `render.py` reads the compression factor from the additional override file that `EXTRA_OVERRIDE` names, and uses a compression factor of 1 when none sets it (`deployment.md` §7.3, additional override file). A render at a compression factor of 1 therefore projects every value as `world.yaml` gives it. `render.py` divides by the compression factor every interval and duration in the port's operation that it projects from `world.yaml`. For the audit, `render.py` converts `audit.period` to hours, 720 for a month, 168 for a week and 24 for a day. `render.py` divides the audit period in hours, `audit.settling_delay_s` and `audit.boundary_margin_s` by the compression factor. `render.py` projects the audit period as the resulting number of hours, which the simulator accepts in a compressed scenario (`narrative.md` §6, deviations from real-world practice). `render.py` multiplies every fuel flow rate by the compression factor. A compressed scenario shortens the intervals and durations in the port's operation (`narrative.md` §6, deviations from real-world practice). The timings of the software and networks lie outside the port's operation and stay as authored.
- Every projection is produced deterministically: identical inputs to `render.py` yield byte-identical artefacts. In particular the projection is emitted with stable ordering and formatting: sorted keys, a fixed row and entry order, and fixed numeric formatting.

| Consumer | Projection | Mechanism |
|---|---|---|
| FMS | `/etc/fms/fms.env` | the relevant slice (admin hash, DB URL, LDAP DN, intervals, process constants) is merged into the rendered env file |
| HMI | `/etc/hmi/hmi.env` | the HMI's slice (FMS URL, challenge timeout) merged into its env file |
| Sim | `infra/sim/sim-constants.toml` (committed at crate root — see below), and in a compressed scenario `build/sim/sim-constants.toml` | `render.py` writes a TOML file of the sim's world-constant slice (the `process` constants, the fuel's properties, the supplier's ordering values, the operator's RFID card number, the roster with each tug's IMO number, name, RFID card and mean time away, the refuel range, and the plaintext credentials its actors present; see the §3 partition and §4.1); the sim's `build.rs` compiles it into the binary at build time |
| Directory (`corp-it`) | LDIF / seed script | `identities` and `ldap` sections projected into directory entries |
| Directory (`corp-it`) rootDN | slapd `cn=config` (via debconf) and the LDIF-seeding bind, in `corp-it.sh.j2` | `identities.ldap_admin.password` projected as the slapd rootDN password. The rootDN (`identities.ldap_admin.dn`) binds with write over the whole tree; it is the directory-manager credential the attack-chain live suite reads to restore directory state after a lock/overwrite test (`attack_chains/attack-chains-tests.md §7`) |
| `hist-01` Grafana | Seeded admin user in Grafana's internal user store | `identities.hist_admin` projected to a build-time-seeded Grafana user row (PBKDF2-HMAC-SHA256 hash, §6); see `host_specs/hist/hist.md §7` |
| `hist-dmz-01` Grafana | Seeded reconciliation-user in Grafana's internal user store | `identities.reconciliation_user` projected the same way; see `host_specs/hist/hist.md §7` |
| Reconciliation workstation account | Directory entry on `corp-it`, via `build/ldif/corp-it.ldif` | `identities.reconciliation_user`'s **workstation** password — a distinct value from the Grafana one above — projected as the `{SSHA}` `userPassword` of a `posixAccount`. `recon-01` authenticates against it and holds no verifier of its own; see `host_specs/recon/recon.md §7.1` |
| `recon-01` directory client | `/etc/sssd/sssd.conf`, via `build/sssd/recon-01.conf` | `ldap` section and `corp-it`'s address projected into the workstation's name-service and authentication client. Carries no credential: the client searches anonymously and binds as the authenticating user |
| Administration workstation account | Directory entry on `corp-it`, via `build/ldif/corp-it.ldif` | `identities.systems_administrator.workstation_password` projected as the `{SSHA}` `userPassword` of a `posixAccount`. `admin-01` authenticates against it and holds no password verifier of its own |
| `admin-01` directory client | `/etc/sssd/sssd.conf`, via `build/sssd/admin-01.conf` | `ldap` section and `corp-it`'s address projected into the workstation's name-service and authentication client. Carries no credential: the client searches anonymously and binds as the authenticating user |
| Systems-administration SSH identity | Private key at `~/.ssh/id_ed25519` in the systems administrator's home directory on `admin-01`, mode 0600; public key in `/etc/ssh/authorized_keys/<username>`, owned by root, on `dmz-bastion-01`, `fms-01`, `hist-01`, and `hmi-01`, for the administration account, a local account with the username `identities.systems_administrator.username` | One unencrypted Ed25519 keypair derived deterministically from `crypto.password_salt_seed`, the identity name, and an SSH-specific context (§6). Only `admin-01` receives the private key; every managed host receives the public key only (`digital.md §4.6`). The build creates the home directory, so the key is in place before the first sign-in |
| SSH host keys | `/etc/ssh/ssh_host_ed25519_key` and its public half on `admin-01`, `dmz-bastion-01`, `fms-01`, `hist-01` and `hmi-01` | One Ed25519 host keypair per node, derived deterministically from `crypto.password_salt_seed`, the node name and a host-key context (§6). Each node receives only its own private host key |
| `admin-01` SSH client files | `/etc/ssh/ssh_known_hosts` and a file in `/etc/ssh/ssh_config.d/` on `admin-01` | `ssh_known_hosts` holds the public host keys of `dmz-bastion-01`, `fms-01`, `hist-01` and `hmi-01`. The client configuration names the administration account for those four hosts and sets `ProxyJump dmz-bastion-01` for the three managed hosts. Carries no secret |
| FMS card table | SQL seed | `identities` and `roster` RFID numbers projected into initial `enrol` rows of `card_table`, keyed by `card_number` and labelled with the holder: the operator's username or the vessel's IMO number (`fms.md §8.2`) |
| `siem-01` management interface | Seeded user in the manager's own credential store | `identities.siem_admin` projected as a hash computed at render time (§6); see `host_specs/siem/siem.md §8.5` |
| Security monitoring agents | `build/siem/<node>.keys`, baked into each monitored node's image | One pre-shared key per monitored node, derived from `crypto.password_salt_seed` and the node name (§6). Each node receives only its own key; `siem-01` receives the complete set |
| Security monitoring agent configuration | The agent configuration in each monitored node's image, `siem-01`'s own agent included | `intervals.file_integrity_interval_h`, `intervals.inventory_interval_h` and `intervals.listening_port_interval_s`, each divided by the compression factor in a compressed scenario, written as the agent's three schedules by the node's rendered build script (`deployment.md` §5.2, render.py) |
| CA root certificate | Per-node trust bundle | Root CA certificate (`ca.pem`) projected into every node's trusted certificate store, not only the ones that make a CA-dependent TLS connection today; the root private key is derived deterministically from `crypto.password_salt_seed` (§6) but never enters any instance; every certificate's validity window is derived from `crypto.ca_not_before` (§5, §6) — see `digital.md §2.7` and `digital.md §8` decision 7 |

## 4.1 Sim projection<a id='sim-projection'></a>

A change to the sim's projected constants requires a full sim image rebuild, not only a re-render, because the sim compiles world facts into its binary at build time. The render script is responsible only for producing the intermediate TOML file; it has no knowledge of Rust. The sim's `build.rs` reads the TOML file via `CARGO_MANIFEST_DIR`, emits a `pub const` block into `OUT_DIR/world_constants.rs` (included via `include!`), and declares a `cargo:rerun-if-changed` directive so that Cargo recompiles the binary whenever the file changes. This division keeps `render.py` language-agnostic and lets Cargo's dependency tracking work correctly. The constants file is committed at the crate root, `infra/sim/sim-constants.toml`, so the crate builds without a prior `make render`; `render.py` rewrites it unconditionally on every render, and `deploy/tests/unit/test_sim_constants_agreement.py` guards that the committed copy matches the current `world.yaml`. The committed file holds the values in operation. In a compressed scenario, `render.py` also writes the constants scaled by the compression factor to `build/sim/sim-constants.toml`. `build.rs` reads the file that the environment variable `SIM_CONSTANTS` names, when it is set, in place of the committed file.

`build.rs` reads the file line by line and does not distinguish TOML sections: a lookup returns the first line in which the key is followed by `=`, wherever it appears. Every key in `sim-constants.toml` must therefore be unique across the whole file, not only within its section. The reconciliation user's credentials are emitted as `recon_username`, `recon_password` and `recon_workstation_password` for this reason, and the supplier's poll interval as `supplier_poll_interval_s`. The flow rates take the sim's own names (`sim.md §1.1`): `process.dispenser_flow_rate_l_per_min` is emitted as `outflow_rate_l_per_min` and `fuel_supplier.tanker_pump_rate_l_per_min` as `inflow_rate_l_per_min`.

The roster is emitted under a `[vessels]` section as numbered keys: `vessel_count`, then `vessel_<n>_imo_number`, `vessel_<n>_name`, `vessel_<n>_rfid_card` and `vessel_<n>_mean_time_away_s` for each n from 1 to `vessel_count`, numbered in the order of `roster.vessels`. Because a key matches only when `=` follows it, `vessel_1_name` cannot match `vessel_10_name`. `build.rs` reads `vessel_count` and then every numbered key up to it, so a tug missing a field fails the build rather than shortening the roster. A tug's number does not affect its arrivals, which derive from its IMO number (`sim.md §6`).

---

## 5. Render-time validation<a id='validation'></a>

In the same way that `render.py` validates the content of `topology.yaml` (see `deployment.md §5.2`), it also performs analogous checks against `world.yaml`. These checks are run before any projection is written:

- **Uniqueness.** Every RFID card number is unique across operators and vessels; every username is unique.
- **Referential integrity.** Every `grade` is a fuel grade defined in `narrative.md` (currently the single grade `MGO`); every operator/vessel referenced by a projection exists.
- **Range.** Each value is physically meaningful on its own. Every interval, lead time and mean time away is positive; every volume, rate and percentage — the reorder threshold, the delivery volume, the refuel range, the flow rates, `cavitation_threshold_pct` and `overfill_interlock_pct` — is positive and finite: not zero, negative, NaN or infinite; `k_factor`, `tank_capacity_l` and `density_kg_per_l` are positive; `sulphur_pct_mm` is positive and no greater than 0.50, the regulation 14.1 limit the bunker delivery note declares against (`sim.md §8.2`); `flashpoint_c` is no lower than 60 °C, the ISO 8217 minimum for distillate marine fuels.
- **Consistency of the scenario values.** The tank, supplier and refuel values fit together. With `C` the tank's capacity, `c` the cavitation level (`C × cavitation_threshold_pct / 100`) and `o` the overfill interlock level (`C × overfill_interlock_pct / 100`):
  - `0 < cavitation_threshold_pct < overfill_interlock_pct < 100`;
  - `c < reorder_threshold_l < C`: the supplier orders before the tank reaches the cavitation level;
  - `reorder_threshold_l + delivery_volume_l ≤ o`: a delivery that arrives with the tank at the reorder threshold completes below the overfill interlock level, so in normal operation the interlock does not stop a delivery part-way. This relies on one order being pending at a time (`sim.md §5.3`); the tank draws down during the lead time, so the check is conservative;
  - `refuel_volume_min_l < refuel_volume_max_l ≤ 6553.5`: each tug's requested volume is drawn from a non-empty range, and no request exceeds the largest preset the dispenser's register holds (`host_specs/fms.md §7.4`), which the FMS would refuse.

  None of these checks depends on how much fuel the tugs draw.
- **Audit cadence.** `audit.period` is `month`, `week` or `day`, and `audit.settling_delay_s` is positive and shorter than the shortest period of its kind (28, 7 or 1 days). A delay of one period or more would start each audit after the following window had ended, and the audits would fall behind. The delay's lower bound is `audit.boundary_margin_s`, which is positive. `render.py` checks that the delay is at least the margin, and `spawn` also refuses a delay below it. In a compressed scenario `render.py` scales the period, the delay and the margin together (§4, projection targets), so the rule holds for the scaled values.
- **Compression factor.** `render.py` refuses a compression factor that is not a whole number of at least 1. The FMS and the HMI read their intervals in whole seconds (`fms.md` §6, service configuration; `hmi.md` §6, service configuration), and the security monitoring's agents read their three schedules in whole seconds. `render.py` refuses a compression factor that leaves a remainder when it divides any of those intervals or schedules.
- **Typable credentials.** The reconciliation user's username and Grafana password are typed into a terminal, as `username:password` inside the single-quoted `-u` argument of the audit command, through a scancode table covering printable ASCII. Both are therefore printable ASCII and contain no single quote, and the username contains no colon, which HTTP Basic authentication reads as the end of the username; the workstation password is printable ASCII.
- **Identifiers.** Every `imo_number` is a seven-digit integer whose last digit is the check digit — the sum of the first six digits weighted 7, 6, 5, 4, 3, 2, modulo 10 (IMO Resolution A.1117(30)) — and no two vessels share one, since it is the vessel card's label in the FMS card table.
- **Determinism inputs.** `crypto.password_salt_seed` is present and is valid hexadecimal of the required length. This feeds the deterministic salt derivation (§6); a missing or malformed value would silently destabilise the deterministic-build guarantee of `deployment.md §9.1`.
- **CA certificate staleness.** `crypto.ca_not_before` is present and well-formed (`world_schema.py` provides this via a `date`-typed field); `ca_provisioning.py` additionally refuses to render if `ca_not_before` plus the internal CA's leaf validity period (1 year, `digital.md §8` decision 7) has already elapsed — a render-time failure instead of a deployment presenting an already-expired certificate.
- **Template mapping integrity.** For each `process` constant that flows to more than one consumer, `render.py` must assert after rendering that the source field (for example `world.process.k_factor`) maps to the correct destination key in each generated artefact (`FMS_K_FACTOR` in `fms.env`; the corresponding key in the rendered `sim-constants.toml` content) and that the rendered values match. The source-to-destination-key map must be a declared table that both the projection templates and this check read from, not a set of keys hard-coded independently in each place; otherwise the check and the templates drift and the assertion becomes worthless. This confirms only that the mapping templates are correct. It does not confirm semantic equivalence across consumers: a units mismatch or an arithmetic divergence in how each consumer applies the constant cannot be detected at render time. That class of defect must be covered by integration tests that exercise both consumers against the same physical scenario and compare their outputs.
- **Least-knowledge per node (the realism check).** For each deployed node, assert that its baked configuration set — every projection written into the node's image (§4) — contains only the facts that node would realistically possess. For instance, the `hmi-01` should carry the FMS URL and its own RFID reader address; it must *not* carry the admin password hash or the vessel roster. Checks run against the full set of baked artefacts, rather than only the final env file, to ensure against leaks. `siem-01` is the single exception: the manager must know every agent's key, so it legitimately holds the complete set while each monitored node must hold only its own. The check asserts both halves of that — one key per monitored node, and no monitored node carrying another's.
- **Declared schema.** `world.yaml` is a load-bearing configuration consumed across four output languages (Rust constants, SQL seed, LDIF, env files). Its structure, types, and required fields are declared once as a `pydantic` model, `World`, in `deploy/scripts/world_schema.py`. `render.py` parses `world.yaml` through `World.model_validate`, which performs the checks above and fails before any projection is written. The schema is machine-checkable, serves as the file's documentation, and gives the source-to-destination-key map (`PROJECTION_MAP`, declared in the same module) a single home to validate against.

A validation failure aborts the render, consistent with the existing pipeline's fail-before-emit behaviour.

---

## 6. Secrets<a id='secrets'></a>

The "credentials" in `world.yaml` — operator passwords, the admin password, RFID numbers — are props in a simulated world whose purpose is to be attacked and detected. There is no real-world asset behind them. They are therefore written as **plaintext in `world.yaml` on the build host**: a secret store would buy nothing and would cost reproducibility (external mutable state, non-deterministic provisioning), which is a primary objective.

These rules keep this "honest":

- **Hash at render time, not at authoring or provisioning time.**
  The argon2id admin hash is generated by `render.py` from the plaintext and written into `fms.env` as `FMS_ADMIN_PASSWORD_HASH`; that pre-rendered value is baked into the FMS image (§4) and is the sole source of `FMS_ADMIN_PASSWORD_HASH` in the deployed environment. The FMS image build must never regenerate the hash. argon2id uses a random salt on every invocation: regenerating the hash from the plaintext at build time would produce a freshly salted hash differing from the one `render.py` wrote, silently breaking the single-source guarantee.

  The same random-salt property means a naive `render.py` implementation is itself non-deterministic: each render produces a different `FMS_ADMIN_PASSWORD_HASH` for the same password, so `fms.env` changes on every render. This contradicts the reproducibility objective the rest of this section depends on — the LDAP encoding below is justified *because* it is deterministic, and the reproducibility guarantee generally depends on "same inputs produce the same outputs". `render.py` must therefore derive the argon2id salt deterministically from the `crypto.password_salt_seed` field of `world.yaml` (§3) combined with the username, so that each identity receives a distinct salt and the same plaintext yields the same `FMS_ADMIN_PASSWORD_HASH` across renders.

- **Grafana admin/reconciliation-user password encoding.** `hist-01`'s Grafana admin
  (`identities.hist_admin`) and `hist-dmz-01`'s Grafana reconciliation user
  (`identities.reconciliation_user`) are seeded directly into Grafana's internal user
  store at image-build time, hashed with Grafana's own real hashing scheme —
  PBKDF2-HMAC-SHA256, 10,000 iterations, a 50-byte derived key, hex-encoded
  (`EncodePassword` in Grafana's `pkg/util/encoding.go`), with the salt held in the
  user table's own `salt` column rather than embedded in the hash — rather than left
  for Grafana to hash at first startup. The same deterministic-salt reasoning as the
  FMS admin hash applies: Grafana's own password-setting path
  (`GF_SECURITY_ADMIN_PASSWORD`, or the admin API) generates a fresh random salt at
  the moment it runs — exactly the non-determinism §3 and this section rule out.
  `render.py` must therefore compute the PBKDF2 hash itself, with the salt derived
  deterministically from `world.yaml`'s `crypto.password_salt_seed` combined with
  the username (the same mechanism as the argon2id and SSHA salts above; needing no
  external dependency, since PBKDF2-HMAC-SHA256 is in Python's standard library),
  and the image build step writes the resulting hash and salt directly into
  Grafana's user database rather than invoking Grafana's own hashing path.

- **The sim receives plaintext; every digital target receives a hash.** The
  rules above exist because a digital host stores a *verifier* — the FMS keeps an
  argon2id hash, the directory an `{SSHA}` attribute, Grafana a PBKDF2 digest —
  and a realistic deployment never holds the password itself. The sim is not a
  host in the modelled network: its actors stand in for the people who log in,
  and a person knows their password.
  The dock operator types one into the HMI, the reconciliation user types one into a terminal, and the systems administrator types one at the workstation's SSH sign-in, so the plaintext is the realistic thing for them to hold and a hash would be useless.

  This is not a relaxation of §2. What that section forbids is `world.yaml`
  itself reaching a deployed instance — the roster file mapping every identity at
  once — and what it permits is each consumer's realistic slice. The sim's slice
  is the credentials its own actors present and nothing else: no operator
  receives another's password, and no credential for an identity the sim does not
  act as is projected to it.
  The sim's slice excludes the application administrator accounts (`identities.admin`, `.hist_admin` and `.siem_admin`), because no actor signs in as them.
  It also excludes the systems administrator's SSH key, which stays on `admin-01`, where the actor's session uses it.

  The consequence for the build is the same single-source guarantee the hashes
  have: the plaintext the sim compiles in and the verifier `render.py` writes into
  the directory derive from one authored value, so a login cannot fail because the
  two drifted apart.

- **Workstation account password encoding.** The reconciliation user's and systems
  administrator's workstation accounts are directory accounts on `corp-it`, not local
  accounts on `recon-01` or `admin-01`. Their passwords are projected as `{SSHA}`
  `userPassword` values by the same mechanism and derivation as the operator entries
  below, and neither workstation receives a verifier.

  The reconciliation user's workstation password is **distinct** from the same identity's
  Grafana credential: two
  systems, two user stores, and compromising one must not yield the other. They
  are salted by different derivations — `{SSHA}` for the directory, Grafana's own
  parameters for the log server — so one person's two verifiers cannot collide
  despite sharing a seed and username.

  Both directory entries are `posixAccount` records and carry `loginShell`, unlike the
  service-facing operator entries: these are the directory accounts that open interactive
  sessions on hosts. A `posixGroup` backs the `gidNumber` every account claims,
  so a login resolves its primary group to a name rather than to a bare number.

  **Directory-allocated identifiers begin at 10000, and the group's name is one
  no distribution ships.** `files` precedes `sss`, so a directory entry sharing
  a local entry's name or number is shadowed and never reached. Hosts allocate
  0-99 to static system accounts, 100-999 to those from package post-install
  scripts, and 1000 upwards to local users, so an identifier inside those ranges
  can be taken by an account the distribution creates. Not detectable at render
  time: it presents as a successful login with the wrong primary group.

- **LDAP operator password encoding.** Operator passwords in the LDIF projection use the `{SSHA}` encoding scheme: the salted SHA-1 digest, base64-encoded, prefixed `{SSHA}`, with the salt derived deterministically from `world.yaml` by the same mechanism as the admin hash above. This keeps the projection reproducible (same password and salt give the same encoded value across all invocations of `render.py`) while remaining realistic. Unsalted `{SHA}` appears in no modern OpenLDAP deployment, so it would read as a synthetic artefact, which cuts against the digital-world objective of being indistinguishable from a real deployment (`digital.md`); `{SSHA}` removes that observable at no reproducibility cost and is supported by all OpenLDAP deployments. Production deployments use SSHA-512 or Argon2; the divergence from those is recorded here and is acceptable because these are simulation-only credentials. `render.py` must produce `userPassword: {SSHA}<base64(sha1(password || salt) || salt)>` for each operator LDIF entry, with `salt` derived deterministically as above.

- **Directory rootDN password.** Unlike the verifiers above, the directory's
  slapd rootDN password (`identities.ldap_admin.password`) is not hashed by
  `render.py`. It is projected as plaintext into `corp-it.sh.j2`, where the image
  build passes it to slapd through debconf; slapd then hashes it into its own
  `cn=config` at install time. The build script text is deterministic across
  renders (the same plaintext each time), so no reproducibility guarantee is lost
  relative to the previous hard-coded value. The rootDN binds with write over the
  whole tree, so it is held only on `corp-it` itself and projected to no other
  node.

- **Security monitoring credential encoding.** `siem-01`'s management interface credential
  (`identities.siem_admin`) is seeded into the manager's own credential store at
  image-build time, hashed by `render.py`, rather than left for the manager to hash at
  first start — the same reasoning as the FMS admin and Grafana hashes above. The scheme is
  the manager's own. Wazuh 4.14.7 stores management-interface (application programming
  interface, port 55000) users in a role-based access control database
  (`/var/ossec/api/configuration/security/rbac.db`, table `users`, column `password`),
  hashing through the bundled Werkzeug 3.1.6 `generate_password_hash`, whose default is
  scrypt with cost parameters `n = 32768`, `r = 8`, `p = 1` and a 16-character salt drawn
  from `[a-zA-Z0-9]`. The stored form is `scrypt:32768:8:1$<salt>$<hex-digest>`. `render.py`
  reproduces it deterministically: the salt is derived from `crypto.password_salt_seed`
  combined with the username (the same mechanism as the hashes above) and mapped into that
  16-character alphabet, and the digest is computed with Python's standard-library
  `hashlib.scrypt` under the same parameters, so the same plaintext yields the same stored
  string across renders. The pre-computed hash is loaded through the manager's own
  `add_user(..., hashed_password=True)` path, which accepts an already-hashed value, or
  written directly into `rbac.db`; the manager never re-hashes it. This characterisation is
  tied to the pinned `WAZUH_VERSION` and its bundled Werkzeug: the Werkzeug default has
  changed across major versions (before 3.x it was PBKDF2-HMAC-SHA256), so a version bump
  re-establishes the algorithm, parameters and format before the pin moves.

- **Security monitoring agent keys.** These are not passwords and are not hashed: each is a
  symmetric pre-shared key held identically by one agent and the manager, and used to
  encrypt that agent's traffic (`host_specs/siem/siem.md §2.1`). `render.py` derives each
  from `crypto.password_salt_seed` combined with the node name, so the set is stable across
  renders and no enrolment step runs at instance start. One consequence follows from the
  derivation: the seed reproduces every agent key, so it remains a build-host value and is
  never projected to any node (§3). This is acceptable on the same grounds as the rest of
  this section — these are simulation-only credentials — but unlike the hashes above, an
  agent key is a live authentication secret rather than a verifier, so its projection is
  the one place where a leak into the wrong node's image would matter operationally. The
  least-knowledge check in §5 is what prevents that.

- **Internal CA key material.** The CA root and leaf private keys
  (`digital.md §2.7`, `§8` decision 7) are not passwords and are not hashed:
  each is an RSA-2048 key, deterministically generated — not from host
  randomness — via a per-key seed derived from `crypto.password_salt_seed`
  the same way every other secret in this section is, expanded into a
  counter-mode SHA-256 byte stream that drives PyCryptodome's `RSA.generate`.
  This is deliberately unusual: RSA key generation is normally seeded from
  OS randomness precisely to prevent predictable keys, but the same
  reasoning as the rest of this section applies — these are simulation-only
  credentials, and the render pipeline's reproducibility guarantee
  (`deployment.md §9.1`) should hold for the CA the same way it holds for
  every other build artefact. Unlike the agent keys above, the root private
  key is not projected to any node at all — only the
  public root certificate and each leaf's signed certificate are.

  Determinism extends to the signed certificate bytes, not only the keys:
  every certificate's `notBefore`/`notAfter` is derived from
  `crypto.ca_not_before` (§3, §5) rather than the build host's current time.
  Unlike the other fields in this section, this one is not a secret but it
  is required for the same determinism reason, and because it must be 
  easy to update without modifying source code: leaf certificates are valid
  for only 1 year, so this value needs periodic maintenance.

- **SSH key material.** The systems administrator's keypair and the host keypair of each SSH server (`admin-01`, `dmz-bastion-01`, `fms-01`, `hist-01`, `hmi-01`) are Ed25519 keys.
  Each private key is a 32-byte Ed25519 seed derived from `crypto.password_salt_seed`, the identity or node name and an SSH-specific context, the same way every other secret in this section is.
  A rebuild therefore reproduces every key, and `admin-01`'s `known_hosts` stays valid across rebuilds.
  Each node receives only its own private host key, and only `admin-01` receives the systems administrator's private key (§4).
  That key is stored unencrypted and protected by its file permissions, so whoever can read it holds the credential for the bastion and for root on the managed hosts (`digital.md §4.6`).

- **Project to a realistic form.** A real operator credential is not a plaintext roster entry; it is an LDAP bind password and a hashed service credential. The projection must place it as such — hashed in the FMS env, as a directory entry in LDAP — and never as a plaintext file anywhere inside the deployed environment. The render step converts build-time plaintext into runtime realistic form.

The `world.yaml` file carries a header comment stating these are simulation-only credentials, so the pattern is never mistaken for one safe with real secrets. A genuine secret enters this scheme only if a scenario demands it (e.g. a credential-theft detection scenario where the realism of secret handling is itself the thing under test) — added when the scenario requires it, not pre-emptively.

---

## 7. Decisions on seeded-but-mutable tables<a id='open'></a>

Several facts seeded from `world.yaml` correspond to tables that a real, running FMS can mutate at runtime. The build-time/runtime boundary for each is recorded here, since "seed only" and "seed then live" require different handling in `render.py` and different expectations of what is observable at runtime.

| Table | Decision | Rationale |
|---|---|---|
| RFID card table | **Seed only at build time.** `world.yaml` is the sole *build-time* source; the seed is written once. | The card table is a static configuration surface in the current scenario design. "Seed only" describes the build pipeline, not an enforced runtime invariant: the FMS card enrolment and revocation endpoints (`POST /api/v1/cards`, `DELETE /api/v1/cards/{card_id}` — `fms.md §7.5`) remain implemented and reachable, so these endpoints remain able to mutate the table at runtime. |
| Operator directory entries (`corp-it` LDAP) | **Seed only.** | No current scenario element modifies the operator directory at runtime. |

For both tables, `render.py` produces a one-time seed script and the datastore becomes authoritative from the moment provisioning completes; no *current* scenario element writes to the seeded rows. If a future scenario requires runtime mutation of either table — or relies on the card endpoints being exercised — the decision and the required `render.py` changes must be recorded here before implementation begins.

---

## 8. Per-profile overrides<a id='profile-overrides'></a>

The release profiles (`full`, `ci-minimal`) render `world.yaml` unchanged: its
values are the realistic asset's values. A **test profile** (`general-deployment.md`
§1.2) may override specific world facts — the release deployment plus the minimum
extra needed to keep a test runnable — through a `world:` section in its override
file (`deploy/overrides/<profile>.yaml`). `render.py` deep-merges that section onto
`world.yaml` **before** `World.model_validate`, so an override is type-checked and
validated exactly as an authored value is, and a profile restates only the facts it
changes (a nested mapping is merged key by key; any other value is replaced).

The override is a deliberate, narrow exception to single-source, confined to test
profiles and recorded here and in each profile's override file. It never applies to
a release profile, so the deployed asset a release builds is the one `world.yaml`
describes.

The only current use is the `attack` profile (`deployment.md` §7.4), which
compresses `intervals.session_timeout_s` from 1800 s to 60 s so the operator
re-authenticates about once a minute. The capture variant of the operator-lock
attack (`attack_chains/attack-chains-tests.md`) sniffs the dock operator's LDAP
bind off the wire, and that bind crosses the wire only on re-authentication; at the
realistic 30-minute session lifetime a capturable bind is far too rare to test
against. Compressing the lifetime is a timing change that keeps the test runnable
without altering the attack or the directory it targets.
