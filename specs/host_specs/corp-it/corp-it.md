# Corporate IT Host Implementation Specification

## 1  Role and scope

`corp-it` runs in the IT network zone. It is the site's corporate
infrastructure host: an identity provider and a time source, shared across
the fleet rather than provisioned per facility. Two independent services run
on it:

- an LDAP directory, authoritative for the dock operator's and the local admin's credentials, and for the reconciliation user's and the systems administrator's workstation logins;
- an NTP daemon, the fleet's single time source.

The authoritative description of `corp-it`'s role is `digital.md §2.7`; the
directory's place in the identity model is `digital.md §5.1`; the three roles
that authenticate against it are `digital.md §5.2`.
Three documents specify its consumers.
`host_specs/fms.md §3`–§4 specifies the operator bind.
`host_specs/recon/recon.md §7.1`–§7.1.1 specifies the reconciliation workstation's login and its directory client.
`digital.md §2.9` specifies the administration workstation's login.

This document specifies `corp-it` as a *host*: the services it runs, the
interfaces it presents, and the credentials and key material it holds. It
does not specify FMS's or `recon-01`'s own client-side behaviour, which is
specified in those hosts' own documents.

---

## 2  Technology

Ubuntu 24.04, consistent with the rest of the fleet, deployed as an Incus
container.

| Component | Product | Provenance |
|---|---|---|
| Directory service | OpenLDAP (`slapd`) | Distribution package |
| Time service | chrony | Distribution package |

`digital.md §2.7` records **FreeIPA** and **Samba AD-DC** as open-source
alternatives to OpenLDAP; both are planned upgrade targets, not implemented
here.

---

## 3  Network interfaces

`corp-it` sits on `vnet-it` (10.30.0.0/24) at 10.30.0.20, with `fw-dmz-it`'s
`it0` interface at 10.30.0.2 as its default gateway.

| Layer | Endpoint | Protocol | Purpose |
|---|---|---|---|
| `slapd` — inbound | `10.30.0.20:636` | LDAP over TLS (LDAPS) | Directory binds (§5) |
| `chrony` — inbound | `10.30.0.20:123` | Network Time Protocol | Time synchronisation (§4) |

Same-zone hosts (`recon-01`, `siem-01`) reach both services without
restriction, as every intra-zone connection does (`digital.md §1.2`). OT and
DMZ hosts reach both services over the two-hop path vnet-ot/vnet-dmz →
`fw-ot-dmz` → vnet-dmz → `fw-dmz-it` → vnet-it, under firewall rules naming
`corp-it` explicitly on each hop (`deployment.md §4.2`).

No other port answers on either zone-facing address. `slapd`'s `cn=config`
service list carries only `ldaps:///` and the local `ldapi:///` UNIX-domain
socket — no `ldap:///` — so nothing on the network reaches the directory
except over TLS; the UNIX-domain socket is reachable only from processes on
this host (§6).

---

## 4  Timekeeping (NTP)

`corp-it` is the fleet's NTP anchor: it declares itself stratum 10 with no
upstream of its own, and every managed node runs a chrony client against it
(`digital.md §1.3`). Every OT and DMZ host reaches it over the same two-hop
path the directory uses; same-zone hosts reach it directly.

**NTP is not run over TLS, and no alternative secure time protocol is
modelled.** Network Time Security (NTS) is the standard mechanism for
authenticated, encrypted time synchronisation, but it is uncommon enough in
real operational technology deployments that modelling it here would make
the testbed less representative of a real OT fleet, not more secure as one.
Plain NTP itself
resists running over a stream-oriented, connection-establishing transport
like TLS: the protocol depends on symmetric, low-latency round trips to
compute clock offset, and the TLS handshake's added round trips and
connection state work against exactly the property NTP needs. `corp-it`
therefore serves plain NTP on port 123/UDP, unauthenticated and unencrypted,
to every host the firewall admits — the same posture NTP has in the great
majority of real OT networks.

---

## 5  Directory service (LDAP over TLS)

`slapd` presents a CA-signed leaf certificate with
a DNS Subject Alternative Name of `corp-it` and no IP SAN, signed by the
RavensPort internal certificate authority (`digital.md §2.7`, `digital.md §8`
decision 7). A client therefore validates the directory by resolving
`corp-it` as a hostname and matching the certificate's SAN against it, never
against the IP address the connection is made to
(`host_specs/fms.md §3`, `host_specs/recon/recon.md §3`).

`slapd` treats a missing or unreadable certificate or key at its configured
path as a startup failure: it does not fall back to serving `ldap:///`
unencrypted, and it does not generate or accept a self-signed substitute.

**Access control is unchanged by the transport.** The directory searches
anonymously and authenticates by binding as the account presenting the
password: `by anonymous auth` against `userPassword`, and read elsewhere.
Every directory client (the FMS's operator bind and the logins on the reconciliation and administration workstations) resolves an account and verifies a password this way, and none of them can read a stored verifier.
TLS changes what a network
observer between the client and `corp-it` can see; it does not change what a
bound client itself is permitted to read.

**Base configuration.** The directory serves `dc=ravensport,dc=local` as its
base distinguished name (DN), with an `ou=people` container holding one
`posixAccount` entry per identity that authenticates against it.
These are the dock operator (bound by the FMS), the local admin (verified by the FMS independently of the directory, `host_specs/fms.md §4`), the reconciliation user (bound directly by `sssd` on `recon-01`) and the systems administrator (bound directly by `sssd` on `admin-01`).
Operator, reconciliation-user and systems-administrator passwords are projected into the directory as `{SSHA}`-encoded `userPassword` values (`deployment-config.md §6`); the render pipeline never writes a plaintext credential into the seed file or the deployed image.

---

## 6  Deployment

The image build script (`deploy/build-scripts/corp-it.sh.j2`) assembles the
`ravensport/corp-it:latest` Incus image. It:

- Installs `slapd` and `chrony` system packages, and pre-seeds `slapd`'s
  Debian package configuration non-interactively.
- Seeds the directory from `build/ldif/corp-it.ldif`, rendered by
  `render.py` from `world.yaml` (`deployment-config.md §4`).
- Creates `/etc/ldap/tls/` and installs the CA-signed certificate and key
  (`build/tls/corp-it/cert.pem`, `key.pem`, generated by
  `deploy/scripts/build-ca.sh`), owned by the `openldap` system user, and
  sets `slapd`'s `cn=config` `olcTLSCertificateFile`,
  `olcTLSCertificateKeyFile`, and `olcTLSCACertificateFile` attributes to
  reference them.
- Sets `slapd`'s service list to `ldaps:/// ldapi:///`, so no plaintext
  `ldap:///` listener is ever started.
- Also installs the CA root certificate into the host's system trust store
  (`update-ca-certificates`), the same as every other node (`digital.md
  §2.7`).
- Installs `build/chrony/corp-it.conf` as `/etc/chrony/chrony.conf`.
- Converts `build/network-config/corp-it.yaml` to systemd-networkd `.network`
  units and enables `systemd-networkd`.
- Installs `/etc/hosts` from `build/hosts/corp-it`.
- Enables `slapd` and `chrony`.
- Purges `cloud-init` from the image.
