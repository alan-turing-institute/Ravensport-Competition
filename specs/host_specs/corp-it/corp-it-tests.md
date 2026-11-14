# Corporate IT Host Test Plan

Test plan for `corp-it` (IT zone), specified in `corp-it.md`. One section per
behavioural area; each names the individual tests it comprises and the
requirement each targets.

---

## 0  Test harness

Tests run against a real `corp-it` Incus image built from
`deploy/build-scripts/corp-it.sh.j2`, matching the `tests/integration/fms/`
and `tests/integration/hist/` convention — highest fidelity, exercising the
actual image-build/systemd/network path rather than a proxy for it.

**Directory client** (`tests/integration/ldap/conftest.py`): a thin `ldap3`
wrapper offering an admin connection and a `bind_as(dn, password)` helper for
authentication checks. It connects to `corp-it` over LDAPS on port 636,
addressed by hostname rather than IP (`corp-it.md §5`), and validates the
presented certificate against `build/ca/ca.pem`, the CA's public root
(`digital.md §2.7`) — a build artefact, not a secret, readable directly from
the test runner host rather than requiring that host to be enrolled in any
node's system trust store. The one exception is
`no_plaintext_listener_on_port_389` (§3), which connects without TLS by
design.

**Time client**: no dedicated wrapper. Time-service tests either inspect the
rendered `chrony.conf` directly (build-time properties, §4) or issue a
one-shot `chronyd -Q` query or read `chronyc tracking` from a peer node over
`incus exec` (live properties, §4) — the same approach
`deploy/tests/integration/test_policy.sh` already uses fleet-wide.

---

## 1  Directory content

Targets `corp-it.md §5`'s base configuration: the base DN, the `ou=people`
container, and the seeded operator entry.

### `base_dn_exists`

The base DN entry (`dc=ravensport,dc=local`) is present.

### `people_ou_exists`

Exactly one `ou=people` entry exists directly under the base DN.

### `operator_entry_exists`

Exactly one `uid=<operator>` entry exists under `ou=people`.

### `operator_entry_carries_posix_attributes`

The operator entry carries `uidNumber`, `gidNumber`, and `homeDirectory` —
the attributes the FMS's directory bind depends on (`fms.md §4`) and, for the
same directory, the reconciliation user's login on `recon-01` depends on
(`recon.md §7.1`).

---

## 2  Directory authentication

Targets `corp-it.md §5`'s access-control statement: the directory searches
anonymously and authenticates by binding as the account presenting the
password.

### `admin_bind_succeeds`

The directory admin can bind — confirms `slapd` is reachable and responding
to authenticated binds at all, independently of any specific seeded identity.

### `operator_bind_succeeds`

The seeded operator account can bind with its configured password — the
exact bind the FMS performs at operator login (`fms.md §4`).

### `operator_bind_fails_with_wrong_password`

The same account, presented with an incorrect password, is refused. Confirms
the bind actually verifies the password rather than merely confirming the
account's existence.

---

## 3  Directory transport security

Targets `corp-it.md §3` and §5: the directory is reachable only over TLS.

### `slapd_presents_ca_signed_certificate`

Fetch the TLS certificate `slapd` presents on `:636`. Assert its issuer is
the RavensPort internal CA and its subject alternative name is `corp-it` —
confirms `corp-it.md §5`/§6's certificate installation, independently of
whether a bind happens to succeed (a client that skips verification would
not otherwise notice a wrong or expired certificate).

### `no_plaintext_listener_on_port_389`

Attempt a TCP connection to `corp-it:389` over the same two-hop production
firewall path every other test in this section uses (§0). The connection
times out: no firewall rule admits port 389 to `corp-it` on either hop
(`deployment.md §4.2`), so the packet is dropped in transit and never reaches
a host that could answer it either way — a stronger property than "nothing
is listening", which a directly-connected client could only confirm with a
refusal. `slapd`'s service list carries only `ldaps:///` and the local
`ldapi:///` socket regardless (`corp-it.md §3`), but this test observes the
network path, not the host. A refusal here would mean the packet got through
and an absent listener answered it; a reset would mean a listener is present
but misbehaving. Only a timeout confirms the firewall itself admits nothing
on this port.

### `unauthenticated_bind_over_tls_still_requires_a_password`

A bind attempt against the LDAPS listener with no password (or an empty one)
is refused. Confirms that moving the transport to TLS did not loosen the bind
requirement it wraps — TLS protects the credential in transit; it does not
substitute for one.

---

## 4  Time service

Targets `corp-it.md §4`. `corp-it` is the fleet's stratum-10 anchor, and NTP
is explicitly not carried over TLS.

### `chrony_declares_stratum_10_with_no_upstream`

The rendered `chrony.conf` on `corp-it` carries `local stratum 10` and no
`server`/`pool` directive — the anchor offers its own free-running clock
rather than deferring to a source outside the deployment.

### `chrony_allow_list_matches_firewall_policy`

`chrony.conf`'s `allow` lines name exactly the networks the firewall policy
admits to port 123, plus `corp-it`'s own zone. Asserted against
`topology.yaml` rather than against the firewall's own rule table, so the two
independently-derived views must agree.

### `chrony_does_not_serve_the_external_zone`

No `allow` line names `vnet-ext`'s address range — the supplier zone has no
business reading the site clock, and no firewall rule admits it to port 123
either.

### `ntp_reply_is_received_end_to_end`

A one-shot NTP query from a same-zone host (and, separately, from an OT host
over the two-hop path) against `corp-it:123/udp` receives a valid reply.
Confirms the service actually answers, not only that its configuration looks
correct.

### `clients_track_corp_it_as_reference`

`chronyc tracking` on a client node names `corp-it`'s address as its
reference. Confirms the client actually selected this host as its time
source, not merely that it is configured to try.

---

## 5  Deployment and build

Targets `corp-it.md §6`.

### `exactly_one_certificate_key_pair_is_installed`

`/etc/ldap/tls/cert.pem` and `key.pem` are both present, and no other TLS
material exists elsewhere on the host — `slapd` is the only TLS-terminating
service here, so there is exactly one keypair to find.

### `certificate_and_key_permissions`

The key is readable only by the `openldap` system user; the certificate is
world-readable, as a public artefact. A key readable by any other account
would let a lower-privileged process on the host impersonate the directory.

### `slapd_fails_to_start_without_its_certificate`

With the certificate or key absent or unreadable, `slapd` does not start and
does not fall back to serving `ldap:///` unencrypted (`corp-it.md §5`). The
selective-image-rebuild suite already covers this failure mode generically
across every CA-dependent host; this test is the `corp-it`-specific instance
of it.
