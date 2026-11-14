# Reconciliation Workstation Implementation Specification

## 1  Role and scope

The reconciliation workstation runs on `recon-01` in the IT network zone. It is the desktop the reconciliation user accesses to conduct the end-of-period audit described in `narrative.md` §3. An office machine with a **remote desktop service**, a terminal, an audit client, a clipboard tool, and a browser.

**The audit is conducted from a terminal.** The reconciliation user opens a terminal in the desktop session and retrieves the period's record from `hist-dmz-01` with `curl`, through Grafana's datasource proxy (§6.1). A browser is installed, because an office workstation has one, and can be used to interactively access the Grafana dashboard but it is not the route for completing the audit (§6.2).

The workstation holds no bespoke application software and no copy of the period's records. The data retrieved from `hist-dmz-01` is piped directly onto the clipboard for the recon user to access, so no export of it is saved on the workstation.

No durable copy of bunkering records is kept in the IT zone, but a summary of each end-of-period audit is stored on the workstation (`narrative.md` §3).
The summary consists of the period audited, whether the records agreed, and the document numbers of any that did not.

The authoritative description of the reconciliation user's role is `digital.md` §5; the component class is named in `digital.md` §2.6.

This document specifies the workstation as a *host*, independent of the actor that dials into it. The actor — the sim's reconciliation user, which drives a session on this host over Remote Desktop Protocol (RDP) and reaches `hist-dmz-01` only from within it — is specified in `sim/sim-impl.md`. Where the two meet, the obligations that fall on this host are stated here and the obligations that fall on the actor are marked as such.

---

## 2  Technology

Ubuntu 24.04, consistent with the rest of the fleet, deployed as an Incus container. The graphical session runs in an unprivileged container; nothing this host does requires a virtual machine in place of a container.

| Component | Product | Provenance |
|---|---|---|
| Remote desktop service | xrdp with the `xorgxrdp` backend | Distribution package |
| Desktop environment | Xfce | Distribution packages |
| Terminal emulator | xfce4-terminal | Distribution package |
| Audit client | curl | Distribution package (§6.1) |
| Clipboard tool | xclip | Distribution package |
| Browser | Mozilla Firefox 153.1.0esr | Pinned upstream tarball (§6.2) |
| Time client | chrony | Distribution package (§9) |
| Directory client | sssd | Distribution package (§7.1.1) |

The expected audit path using these components:
1. a terminal is opened
1. `curl` retrieves
1. `xclip` returns

The browser is simply an application the workstation carries rather than a participant in the audit, but it can access the Grafana dashboard using the same credentials `curl` uses.

Distribution packages are installed with `--no-install-recommends` and named explicitly, consistent with fleet practice, which pins upstream artefacts but not distribution packages:

```
xrdp xorgxrdp
xfce4 xfce4-session xfce4-terminal dbus-x11
x11-xserver-utils x11-xkb-utils
ca-certificates xz-utils
curl xclip
sssd-ldap libnss-sss libpam-sss
```

`x11-xkb-utils` supplies `setxkbmap`, the keyboard layout, which is a required distinction as outlined in §5.2. `ca-certificates` supplies the system trust store that §6.4 installs into, and is also the store `curl` verifies the log server's certificate against.

The audit is conducted with `curl` and `xclip` is required for the audit. Both are explicitly named. 

`xfce4-terminal` is named as it's classed as a *recommendation* of the `xfce4` metapackage rather than a dependency, so the fleet's `--no-install-recommends` practice removes it unless it is named here.
Naming it also keeps the default a real Xfce desktop would have: an office workstation carries a terminal whether or not its user opens one, and removing a component the chosen desktop environment installs by default is the decision that would need reasoning in `narrative.md`, not keeping it.
### 2.1  Firefox runtime dependencies

A tarball build links against system libraries that the browser package would otherwise pull in. These are named explicitly rather than acquired through recommendations:

```
libgtk-3-0t64 libdbus-glib-1-2 libasound2t64 libxt6t64 fonts-liberation
```

The `t64` suffixes are the 24.04 time-64 transition names and are correct for **this release only**. A missing dependency here presents as a browser that exits immediately rather than as a build failure, so the set is confirmed by starting the browser during the image build (§10.1) rather than by inspecting the package list.

---

## 3  Network interfaces

`recon-01` sits on `vnet-it` (10.30.0.0/24) at 10.30.0.10, with `fw-dmz-it`'s
`it0` interface at 10.30.0.2 as its default gateway.

| Layer | Endpoint | Protocol | Purpose |
|---|---|---|---|
| xrdp — inbound | `10.30.0.10:3389` | RDP over Transport Layer Security (TLS) | The reconciliation user's session |
| `curl` → Grafana | `10.20.0.20:443` | HTTPS | The audit's retrieval through the datasource proxy on `hist-dmz-01` (§6.1) |
| chrony → `corp-it` | `10.30.0.20:123` | Network Time Protocol | Time synchronisation (§9) |
| sssd → `corp-it` | `10.30.0.20:636` | Lightweight Directory Access Protocol over TLS | The workstation login (§7.1) |
| Security monitoring agent → `siem-01` | TCP 1514 | Wazuh agent protocol | Record forwarding (§3.3) |

The session arrives from 10.30.0.11, the sim host's `vnet-it` interface. Both endpoints are on `vnet-it`, so this leg crosses no firewall. The path to `hist-dmz-01` crosses `fw-dmz-it` under the rule `{ from: vnet-it, to: vnet-dmz, dst: hist-dmz-01, port: 443, proto: tcp }`, and `hist-dmz-01` carries the return route `10.30.0.0/24 via 10.20.0.3`.

**Name resolution is minimal.** `/etc/hosts` carries loopback, the host's own name, `10.20.0.20 hist-dmz-01` — the name the audit uses — and `10.30.0.20 corp-it` — the name the directory bind uses (§7.1.1) — and `siem-01` — the name the security monitoring agent forwards to (§3.3). The subject alternative name - SAN - on `hist-dmz-01`'s Grafana certificate is `DNS:hist-dmz-01`, and on `corp-it`'s certificate is `DNS:corp-it`, so for the audit and the directory bind the name the client dials is the name the certificate asserts.

### 3.1  Intra-zone posture
`recon-01` reaches `corp-it` for directory and time services without restriction as hosts within a zone communicate without restriction. This is a corporate workstation using corporate services.

The workstation login is a directory login (§7.1), so an unreachable `corp-it` means no new session on this host. The bind is over LDAPS (§7.1.1), so the password is encrypted in transit across this zone.

### 3.2  Inter-zone posture
The workstation must not reach:

- **The operational technology zone** `vnet-it → vnet-ot` is denied by default. 
- **The internet** Every bridge is `nat: false`; 
- **The sim** `deploy/sim-host/egress-guard.nft` drops inbound connections on the sim's `it-recon-user` interface, making it egress-only. The workstation can never dial the actor. This is outlined in §11.


The workstation has access to:
- **The DMZ log-server replica** to complete the audit work
- **Other hosts in the IT zone** based on topology rules this includes `corp-it` and `siem-01` §3.3

### 3.3  Security monitoring agent

`recon-01` is one of the twelve monitored hosts (`siem.md` §1), so it carries a security monitoring agent forwarding to `siem-01` in the IT zone, which it names in its configuration and resolves through `/etc/hosts` (§3). Both hosts are on `vnet-it`, so the path crosses no firewall and depends on §3.1's intra-zone posture. The agent's pre-shared key is derived at render time from `crypto.password_salt_seed` and the node name, and only this node's key reaches this node's image (`deployment-config.md` §6); enrolment is disabled, since the key is installed at build time.

---

## 4  Remote access

xrdp listens on 3389. **Any host on `vnet-it` can reach it**, since hosts within
a zone communicate without restriction (§3.1); the sim host is the only one that
does. Nothing limits the source address, and the workstation's remote-access 
service is reachable from every neighbour in `vnet-it`

**A disconnected session is not torn down.** xrdp holds the display and window
manager after the client goes away, so a later connection reattaches to whatever
a previous one left. The session ends when its window manager exits, which
nothing on this host causes.

§8's first obligation on the actor rests on this, as does `recon-tests.md`
§0.4's requirement that every test needing initial state establish it
explicitly.

**The security layer is forced to TLS.**

```
security_layer=tls    # /etc/xrdp/xrdp.ini
```

Under xrdp's default `negotiate` the server falls back to the legacy standard RDP security layer, which a client requiring enhanced security refuses. 
This setting and the client's configuration must agree: a client that disables Credential Security Support Provider negotiation requires a server offering plain TLS, which is what this produces.

**The certificate is issued by the internal certificate authority**, as for every other network-facing service in the deployment (`digital.md` §2.7), rather than the distribution's self-signed pair:

```
certificate=/etc/xrdp/tls/recon-01.pem    # /etc/xrdp/xrdp.ini
key_file=/etc/xrdp/tls/recon-01.key
```

Its subject alternative name is `recon-01`, with no address, as for every leaf (`digital.md` §8 decision 7). The client connecting to it verifies the chain against the deployment's root and the name `recon-01`, which it supplies itself, since it reaches the service through a proxy whose address names nothing (`sim/sim-impl.md`, reconciliation user). The key is readable by the `xrdp` user alone; without that the server reports `SSL_CERT_NOT_ON_SERVER` when a client arrives, a failure invisible at build time and asserted from a connection in the deployment tests.

## 5  Desktop session

xrdp's `startwm.sh` sources `~/.xsession`, so the session is selected per user. The file is installed into `/etc/skel` and reaches the home directory when `pam_mkhomedir` creates it at first login (§7.1.1):

```sh
#!/bin/sh
export XDG_SESSION_DESKTOP=xfce
export XDG_CURRENT_DESKTOP=XFCE
setxkbmap gb
exec startxfce4
```

Nothing is autostarted. §5.3 specifies the route to the browser - an audit route for a "hands-on" user at the `recon-01` workstation. §5.4 specifies the route to a terminal; the audit path used by `it-recon-user`. 

### 5.1  Screen geometry

The session's geometry is whatever the client requests when it connects; the
host imposes none.

**What the host guarantees is that nothing is configured to appear in front of
the session.** No notification, update prompt or session dialogue is set up to
present itself. This is independent of geometry: anything holding focus absorbs
typed input, so a dialogue defeats the audit whether or not a coordinate is
used. Asserted by `no_dialogue_is_present_on_a_fresh_session` (`recon-tests.md`
§3).

**The guarantee does not extend to layout.** An earlier revision stated that a
given geometry produces the same layout each time. It does not: font density is
not pinned and also determines layout, and the browser's window position is not
fixed.

Nothing was lost by narrowing it, because the audit stopped depending on it.
Coordinate-driven interaction proved brittle to mis-typing and slow loads, so
the terminal is reached by a keyboard binding (§5.4) and the retrieval typed
into it — a path with no coordinate in it. What the narrowing costs is the
applications-menu route to the browser (§5.3), which is a pointer route and so
not reproducible across sessions; the browser is not on the audit path.

**The guarantee covers what the host introduces, not every state a session can
be driven into.** The measured case: activating an application with no
configured handler produces an Xfce preferred-application chooser, which takes
focus, absorbs a full typed command, and is still present at the next connection
because §4 tears no session down. Every step reported success and the read
returned nothing.

Nothing here can prevent that — it is reached by acting on the session, not
presented by the host. §8.1 is the actor's obligation to detect it, and §5.4
removes the specific cause on the audit path.

### 5.2  Keyboard layout

The actor injects keystrokes as PS/2 Set 1 scancodes, not as characters. 

Which character a scancode produces is decided by the keyboard layout on the host. The layout is therefore pinned explicitly in the session (`setxkbmap gb`) and stated here as a contract rather than left as a default that happens to hold.

This distinction becomes significant when an input with special characters is required. A layout change on the host would silently alter the typed input, and the resulting failure would present as a rejected credential rather than as a configuration fault.

To ensure consistency between `it-recon-user`, and `recon-01` the layout pinned here, the scancode table the actor injects, and the keyboard layout identifier the actor's RDP client advertises at connect must all agree.
The final obligation falls on the actor and should be specified in `sim/sim-impl.md`.

Special characters such as pipe `|` require additional entries, see `recon-tests.md`
§1.

#### There are two keyboard layers, and the ordering between them is the contract

xrdp performs its own keyboard negotiation, loading the
`/etc/xrdp/km-<identifier>.ini` matching the layout the client advertises on
**every** connection. That layer is chosen by the client and changes whenever a
different one connects.

The session's `setxkbmap` is a second, independent layer, and **it is sufficient
only because it runs afterwards**: `~/.xsession` executes at session start,
after xrdp's keymap load, and nothing changes the layout subsequently. The
ordering, not the value alone, is what makes the pin hold — a requirement rather
than an observation.

Measured directly with a client advertising English (United Kingdom) and xrdp
having loaded `km-00000809.ini`, the X layer decides what a scancode produces.
The same measurement against an earlier pin of `us` gave that layout's
characters, so the result depends on the ordering rather than on the two layers
agreeing.

Three changes would break the contract silently, each presenting as a rejected
credential or a malformed query rather than a configuration fault: an autostart
entry setting a layout before `~/.xsession`, an xrdp version re-synchronising
after session start, or a settings daemon restoring a saved layout.

**A successful login is not evidence the contract holds.** The credentials are
drawn from characters both layouts place identically (`recon-tests.md` §1), so
only typing a character that distinguishes them establishes it.

### 5.3  In-session access to the browser

The build installs a desktop entry at `/usr/share/applications/firefox.desktop`, so the browser appears in the applications menu and in the application finder as it would on any workstation.
That entry is the session's route to the browser.


### 5.4  In-session access to a terminal

The audit is conducted from a terminal (§6.1). The route is the keyboard binding `Ctrl+Alt+T`, which Xfce configures by default to run `exo-open --launch TerminalEmulator`. The preferred application is pinned in `/etc/skel/.config/xfce4/helpers.rc`, which reaches the account's own configuration when its home directory is created (§7.1.1); an unset preference is answered with the preferred-application chooser, a modal dialogue that takes focus and absorbs every keystroke typed after it. 

It does not depend on GUI pointer coordinates, which would couple the actor to the screen geometry requested at connect, to the panel layout, and to the window position. **The host guarantees none of the three** (§5.1), so a pointer route would rest on nothing. 

It is also a key grab on the root window rather than a keystroke delivered to the focused window, so it is not consumed by an application dialogue that happens to hold focus. 
§8 requires the actor to confirm the result rather than assume it — but it removes one way in which a session left in an unexpected state defeats the first step of the audit.

## 6  Clients and trust

Two clients are installed and only one is on the audit path. 
This section covers the audit client first, then the browser, then the trust both depend on.

### 6.1  The audit client

The audit is conducted with `curl` from a terminal in the desktop session, against Grafana on `hist-dmz-01` at 10.20.0.20:443.

The retrieval goes through Grafana's datasource proxy, which fronts the two datasources provisioned on that host (`hist.md` §7) under readable identifiers:

```
/api/datasources/proxy/uid/monthly-audit-logs/…      # VictoriaLogs, the record
/api/datasources/proxy/uid/monthly-audit-metrics/…   # VictoriaMetrics, tank volume
```

This is chosen over Grafana's unified query endpoint, which the provisioned dashboard's own panels use. 
The unified endpoint takes a request body carrying datasource identifiers, query expressions and a time range as a document; the proxy takes an address with a query string, which is an easier and more likely query for a human user.

Both routes would stay inside Grafana's authorisation.

The details of what the audit queries are specified in `sim/sim-impl.md`. What this host provides is the client, the terminal it runs in, and the trust that lets it verify the far end.

The retrieval is piped to the clipboard and no export is written to disk (§1). §11 specifies the return path and the obligations it places on the actor, and §11.4 records the two-step variant that would write a file.

**The audit's own summary is a separate matter and is written here.** `narrative.md` §3 has the outcome of each audit recorded on the workstation, and the actor appends it through the terminal it already drives — a shell redirection, not a transfer channel, so §11.1's rejection of one is unaffected. It carries the period, whether the records agreed, and the document numbers of any that did not; never record contents. The file and its location are the actor's to choose within the account's home directory (`sim/sim-impl.md`), which `pam_mkhomedir` creates at first login (§7.1.1) and which therefore starts empty on a freshly built image.

The client's diagnostics must travel with the payload. `curl` writes its retrieval to standard output and its errors to standard error, so a pipe carries the first and discards the second. 
The actor cannot read the screen, so a discarded diagnostic is lost: a failed retrieval then places an empty selection on the clipboard, and an empty selection is indistinguishable from a month with no records. 

The audit command must therefore fold standard error into the pipe and suppress the progress meter without suppressing errors — `curl -sS … 2>&1 | xclip …`. A failure then arrives at the actor as a readable diagnostic it can classify and react and respond to, rather than as an absence it cannot distinguish from an empty month.

Two further parts of the command serve the same end. `--fail-with-body` makes an HTTP error a failure of `curl`'s own: without it, a refused login returns Grafana's error body, itself a JSON object, with a successful exit, and reads as a record. And the command ends every response with a line carrying `curl`'s exit status, so that a response which did not finish is recognisable as one, and a month with no records still places text on the clipboard. The exact form is `sim/sim-impl.md`'s.

Details of appropriate responses will be in `sim/sim-impl.md`'s, and §8 states the general form of it.

Certificate verification is never disabled. `curl`'s `--insecure` would reach Grafana under the current implementation, before the trust anchor of §6.4 exists, and it is rejected on the same ground as clicking through a browser interstitial.

### 6.2  The browser — provenance and pinning

§6.1 conducts the audit with `curl`, and this section and §6.3 specify what the workstation carries rather than what the audit depends on. 
The browser is retained because an office workstation has one: removing it would make `recon-01` less like the machine `narrative.md` §3 describes, and it is a legitimate part of the modelled world an intruder would also find. Everything below is therefore held to the standard of a workstation's browser being reproducible, rather than to the standard of a component the audit rests on.

Firefox arrives as a pinned, checksummed Extended Support Release tarball, not as a distribution package and not from a third-party package repository. Ubuntu's `firefox` package is a transitional wrapper around snapd, which does not work in an unprivileged container. Verification is performed as with other artefacts in the build with the caveat that there are properties, outlined below, that diverge from other pinned properties. 
| Property | Consequence |
|---|---|
| Architecture names are `x86_64`/`aarch64` | The fleet's `ARTEFACT_ARCH` is `amd64`/`arm64`; the fetch function needs a mapping rather than the value directly. |
| `SHA256SUMS` keys on a relative path, not a bare filename | The existing `$2 == f` match in `fetch_artefact` does not select the row. |
| Builds are per-locale | The locale is part of the download path. `en-GB` is selected, consistent with the house language. |




### 6.3  Enterprise policy

**One setting is a requirement; the rest are posture.** §6.2 holds this browser
to the standard of a workstation's browser rather than of a component the audit
depends on, and only the first below follows from that standard.

**Required — reproducibility.**

| Setting | Reason |
|---|---|
| `DisableAppUpdate` | A tarball install can update itself in place, replacing the pinned build with an unpinned one inside a running deployment, after which the published image no longer describes what is running. |

The deployment has no outbound path — every bridge is `nat: false` and no rule
admits the internet (§3.2) — so this is a second guard on a property the network
already enforces, kept because that posture is not the browser's to rely on.

**Determinism.** A browser that renders a different first screen on its first run
than on its tenth is not a fixed starting state, so onboarding, telemetry and
version-change pages are suppressed: `DisableTelemetry`,
`DisableFirefoxStudies`, `DisableFirefoxAccounts`, `DisablePocket`,
`DontCheckDefaultBrowser`, `NoDefaultBookmarks`, `SkipTermsOfUse`,
`OverrideFirstRunPage`, `OverridePostUpdatePage`, and the locked preferences
`browser.aboutwelcome.enabled`, `browser.startup.homepage_override.mstone` and
`datareporting.policy.dataSubmissionPolicyBypassNotification`.

**Posture.** Settings a corporate workstation's browser would carry, retained
although the audit no longer types a credential into a browser:
`PasswordManagerEnabled`, `OfferToSaveLogins`, `DisablePasswordReveal`,
`DisableFormHistory`, `DisableProfileImport`.

**Read fidelity** `devtools.jsonview.enabled`
and the two `browser.cache.*` preferences are locked off. They existed so a
select-all copy of a JSON response returned the document rather than the viewer's
rendering, and so a reload of an unreachable log server produced a visible error
rather than the previous attempt's content. §6.1 moved the audit to `curl`, which
took both roles; they are kept because they remain correct and cost nothing.

#### The policy file is written to two locations

A packaged build reads `/etc/firefox/policies/policies.json`; a tarball install
may read `<install-dir>/distribution/policies.json` instead. Which this build
reads is not established, and **a file in the location the browser does not read
fails silently** — it starts normally and none of the settings apply.

Both are written, which removes the question rather than answering it. The file's
presence therefore proves nothing, which is why `recon-tests.md` §4 asserts the
policy's *effect* from a running browser.



### 6.4  Certificate trust

The audit client verifies `hist-dmz-01`'s Grafana certificate against the **system trust store**, which `ca-certificates` provides (§2). The internal certificate authority's root is installed there:

```
/usr/local/share/ca-certificates/<name>.crt   # then: update-ca-certificates
```

`update-ca-certificates` reads only files ending `.crt`; a `.pem` extension is
ignored without complaint, which is the one way this step fails while appearing
to have been taken.

**Note on browser certificates** Certificates for Firefox have not been implemented as the browser audit route is not used for the sim user.

The root this workstation installs is the internal certificate authority's, which signs the leaf `hist-dmz-01`'s Grafana presents (`digital.md` §2.7). `recon-01` holds the root alone and is not itself a certificate holder; whether it should become one for its remote desktop interface is a separate and open question.

**Three CA failures that are indistinguishable from one another**: 
1. A root never installed
1. A root installed into the wrong store
1. A root with a lapsed validity window

The verification failure should however register when using `curl`. With this a verification failure would return a standard error, and no corresponding data to the clipboard. With routing as outlined in §6.1, this error can be read as a typed failure and processed appropriately in the sim-user. 


---

## 7  Identity and credentials

Two identities with two different passwords are used at the `recon-01` workstation: the account the user signs in with, and the Grafana account the audit authenticates with. Neither verifier is held on this host — the first lives in the directory on `corp-it` (§7.1), the second in Grafana's own user store on `hist-dmz-01` (§7.2).

### 7.1  The workstation account

A **directory account**, held in the Lightweight Directory Access Protocol
directory on `corp-it` and consumed by xrdp through the Pluggable
Authentication Modules stack. The workstation holds no verifier: there is no
local account for the reconciliation user and no entry for them in this host's
shadow file.

The password is **distinct** from the log-server one, held as a second field on
the `reconciliation_user` identity in `world.yaml`. These are two systems with
two user stores, and compromising one should not give up the other.

**Rendering.** `render.py` projects the workstation password into
`build/ldif/corp-it.ldif` as the `{SSHA}` `userPassword` of
`uid=<name>,ou=people`, by the same derivation and encoding as every other
directory entry (`deployment-config.md` §6). The same identity's Grafana
credential is salted by a different derivation, so one person's two verifiers
cannot collide. No plaintext reaches any image and none reaches `deploy/build/`.

The entry is a `posixAccount` carrying `uidNumber`, `gidNumber`, `homeDirectory`
and `loginShell`.
`loginShell` appears on this entry and on the systems administrator's (`deployment-config.md` §6).
These are the only directory accounts that open an interactive session on a host.
The operators are authenticated by the FMS's own bind against the same directory and log in to nothing.

**The primary group is a real object.** Every directory account claims a
`gidNumber` and a login resolves it, so a `posixGroup` backs it. A session
started against a bare identifier still succeeds, so this cannot be established
from a successful login — it appears later as files owned by a group that
displays as a number.

**Directory identifiers must not collide with local ones.** `files` precedes
`sss`, so where a local entry shares a directory entry's name or number the
local one answers. Both halves were observed rather than anticipated: a
directory group named `staff` is shadowed by the distribution's own at group
identifier 50, and one numbered 1000 resolves as `lxd` — which put the
reconciliation user's session in the container runtime's group.

Directory-allocated identifiers therefore start at 10000, clear of every range a
host allocates itself: 0-99 static system accounts, 100-999 those created by
package post-install scripts, 1000 upwards local users. The group's name is
likewise one no distribution ships.

Neither the render nor the build can detect a collision: it presents as a
successful login with the wrong primary group, visible only as `id` on a
deployed host, which is where `recon-tests.md` §8 asserts it.

### 7.1.1  The directory client

`sssd` provides both name service and authentication. It searches
**anonymously** and authenticates by binding as the account presenting the
password, so the workstation carries no service credential either. The stock
slapd access rules on `corp-it` permit exactly this and no more — `by anonymous
auth` against `userPassword`, and read elsewhere — so the workstation can
resolve an account and verify a password by binding as it, and cannot read a
verifier.

The directory is addressed by hostname, as the FMS's bind is: `corp-it`'s
leaf certificate carries a hostname-only SAN, so this host's `/etc/hosts`
carries an entry for `corp-it` (§3) and `sssd` dials that name rather than
its IP address.

**The bind is over TLS (LDAPS), on port 636**, consistent with the FMS's
bind. `sssd` validates `corp-it`'s certificate against the RavensPort
internal certificate authority's root, installed in this host's system trust
store the same way as every other node (`digital.md §2.7`). No firewall
change is needed for this host: `recon-01` and `corp-it` share a zone and the
bind crosses no boundary in either direction (§3.1).

**Two consequences for the image.** There is no home directory at build time, so
`pam_mkhomedir` creates one at first login from `/etc/skel`, and the session
files of §5 and §5.4 are installed there. And authentication now depends on
`corp-it` being reachable — `cache_credentials` allows a previously successful
login to be repeated while it is not, as a real domain workstation does.

**Who can sign in.** `sssd` admits every directory account with a login shell.
Two accounts carry one, the reconciliation user's and the systems administrator's (§7.1, `digital.md` §2.9).
The systems administrator can therefore sign in on this workstation, as IT staff usually can on an office workstation in a directory.

### 7.2  The log-server account and residues

The reconciliation user authenticates to Grafana on `hist-dmz-01` against that instance's own built-in user store (`digital.md` §5.1). That credential is authored in `world.yaml`, rendered into `hist-dmz-01`'s seeded user row as a PBKDF2 hash, and rendered into the sim binary through `sim-constants.toml` — the same process the dock operator's credential already receives, and the same documented exception, because the actor must present it.

The image doesn't carry the credentials, bookmarks, or autostart arguments. These are only available to the user. An audit as specified would leave the following traces: 

| Where the credential is left | Lifetime |
|---|---|
| `~/.bash_history` — the audit command, credential included | Durable, until the account's history is truncated |
| `/proc/<pid>/cmdline` — the `bash -c` retrieval wrapper | For the length of the request |
| The terminal's scrollback, and any capture of the session | Until the terminal is closed (§8) |

**The credential is deliberately left in `/proc`.** `curl` overwrites the value of its `-u` option in its own argument list as it starts, so its own `/proc/<pid>/cmdline` shows the value blanked. The audit therefore runs the retrieval through `bash -c '…'`. The wrapper's argument holds the complete command, including the credential, and remains readable to another workstation user for the length of the request. This models an ordinary user wrapping a compound pipeline rather than taking precautions to hide its arguments.

A person conducting this audit at a command line would leave the credential in the same places, and narrative.md §3 does not describe an auditor who takes precautions against it. It is therefore kept rather than suppressed: it belongs to the deployment this host models, and security monitoring that establishes normal behaviour on this host (siem.md §1) should do so against a deployment in which it occurs. It can be narrowed later — HISTCONTROL=ignorespace, or unsetting HISTFILE — but that would change what the host models rather than correct a fault in it.

The history residue is made deterministic: `bash` writes its history when the shell exits, so an audit that leaves its terminal open may leave no history at all, and one that closes it leaves the full record — this would have the same host producing the residue on some months and not others. 
The image therefore is built to append each command as it is entered:

```sh
PROMPT_COMMAND='history -a'    # /etc/bash.bashrc
```

**Placement.** `/etc/bash.bashrc`: the terminal of §5.4 spawns an interactive non-login shell, which never reads a `/etc/profile.d/` drop-in, so a profile placement would be silently absent from the shell the audit types into.

**Timing.** `history -a` runs before each prompt, so the command reaches the history file when the request completes rather than when it is entered. `bash`'s write at shell exit still happens and is no longer the only one.



---

## 8  Session lifecycle

One audit is one session: the actor connects, opens a terminal, confirms it is live, conducts the retrieval, closes the terminal, and disconnects.


**The X session persists across disconnects** (§4). Four required behaviours from the actor are expected/required, and will be logged in `sim/sim-impl.md`. They are recorded here because each originates as a property of the workstation rather than of the actor's own design.

1. Audit must be independent of session state
  - Every audit opens its own terminal and does not reuse one a previous audit may have left. What the previous audit left is not bounded by the host, which does not tear anything down. Additionally there is no way to confirm the closure of a terminal after a session.
1. Confirmation of a live terminal
  - Mechanism outlined in §8.1
1. Actor must be able to classify what arrives at the clipboard
  - Error or data or failed retrieval should all be able to be parsed by the actor. Outlined in §6.1
1. CA failure should not result in a retry
  - Outlined in §6.4


### 8.1  Confirming the session

This section outlines expected behaviour from the actor and might be better placed in `sim/sim-impl.md`.

The actor receives a framebuffer it has no text reading of, and input injection carries no acknowledgement of what consumed it. 
Of everything the actor does inside the session, **the clipboard is the only thing that returns**. Anything not confirmed through it is assumed rather than known.

Before the first command that carries the log-server credential, the actor therefore places a unique value per audit on the clipboard from the terminal and reads it back:

```
clear the latch  →  type a command echoing a per-audit value to xclip  →  read
```

A match establishes that the window manager delivered the keystrokes to a shell; the shell is executing
commands rather than a dialogue absorbing them; the clipboard channel is live in *this* session and not merely initialised; and the latch discipline of §11.2 works. 
No match, or nothing, is a session-state failure — reported as one, and never as an audit that found no records.

As the log-server password is typed into the command line, if the focus isn't where the actor expects it - the password could be exposed elsewhere on the host. 



---

## 9  Timekeeping

`recon-01` is a chrony client of `corp-it`, the site time source, on the same zone; the query crosses no firewall. 

**The workstation's clock does not define the audit window.** §6.1's client takes the time period as explicit bounds in the query string, computed by the actor from the sim's own clock, rather than using `now-30d` or similar where the workstation's clock would impact which records are returned. 

---

## 10  Deployment

`recon-01` is built as an Incus image from a generated build script, following
the pattern of every other node.

### 10.1  Image build script

Beyond the fleet-standard steps — network configuration, the canonical
`/etc/hosts` and its restore unit, the cloud-init purge, publish with an input
hash — the script:

1. Installs the package sets of §2 and §2.1 with `--no-install-recommends`.
2. Fetches, verifies and unpacks the pinned Firefox tarball (§6.2).
3. Installs the enterprise policy file to both locations of §6.3 and **starts
   the browser once**, confirming it runs. A missing runtime dependency presents
   as a normally built image, so it is established here rather than assumed. The
   policy taking *effect* is not established here — see §6.3.
4. Writes the applications menu entry of §5.3 and confirms the icon it names is
   present in the pinned tarball.
5. Installs the rendered `sssd` configuration, adds `sss` to the `passwd`,
   `group` and `shadow` name-service databases, and enables `pam_mkhomedir`
   (§7.1.1). **It creates no account**: the reconciliation user's account is a
   directory one, and a local account beside it would authenticate while being
   invisible in a running deployment.
6. Pins the preferred terminal emulator in `/etc/skel/.config/xfce4/helpers.rc`,
   so that the `Ctrl+Alt+T` binding starts a terminal rather than the
   preferred-application chooser (§5.4).
7. Writes the shell profile that appends history as each command is entered, so
   that the residue of §7.2 is present by decision rather than intermittently.
8. Writes `/etc/skel/.xsession` (§5), forces the xrdp security layer, and
   installs the CA-issued certificate and key, the key readable by the `xrdp`
   user alone, naming both in `xrdp.ini` (§4).
9. Enables `xrdp`, `xrdp-sesman` and `sssd`.

Steps 6 and 8 write into `/etc/skel` because there is no home directory at build
time (§7.1.1); `pam_mkhomedir` populates one from there at first login. The build
must not create the home directory in advance: `pam_mkhomedir` will not populate
a directory that already exists, leaving the account with no session file and no
route to a desktop.


### 10.2  Input hash

Every rendered artefact the script installs must appear in the list the script hashes to decide whether to rebuild — for this node, the network configuration, the hosts file, the chrony configuration, the `sssd` configuration of §7.1.1, the pinned browser tarball, and the root certificate of §6.4.

---

## 11  The return path

§2 makes the clipboard the only route by which a retrieved audit record reaches the actor. This section states what that route may carry and what it may not, and why no second route is added beside it.

The obligations below fall on the **actor**, not on this host, and are recorded here for the same reason §8's four obligations are: the requirement originates from this workstation's position rather than from the actor's own design. 

Their normative home is `sim/sim-impl.md`; the requirement they serve is `sim.md` §5.4.

### 11.1  No transfer channel is added for the audit

A file-transfer channel between this workstation and the actor was considered and rejected. 
Three forms were available: 
1. Secure copy over a remote-access service on the recon host
1. Device redirection within the remote desktop session
1. The clipboard's own file-transfer capability. 

Each would let the audit move an arbitrarily large export without depending on the clipboard.

They are rejected on one ground, which is `sim.md` §5.4's second requirement. Each would give a compromised workstation a write onto the sim's filesystem, and the secure-copy option also places a transfer client and a credential inside the sim image, against the hardening rationale of `sim-network.md` §8. 

The audit is bookkeeping; nothing it needs justifies a route by which the digital world could act on the mechanism the physical model runs on.

**This is independent of whether the workstation later runs a remote-access service for its own reasons.** A corporate workstation plausibly does, and such a service would be a legitimate part of the modelled world. What §5.4 forbids is the *actor* using one in this context.

### 11.2  Obligations on the actor

Four obligations, ordered by what actually fails rather than by what is easiest to assert.

1. Every read is bounded in time, and a read that does not complete within its deadline resolves to a typed error
    - Past the limit of the second obligation the workstation neither delivers nor refuses: the copy is announced, the format data is requested, and no response of any kind follows. Nothing is truncated, no failure is signalled, and no diagnostic is written.
    - A read without a deadline therefore blocks for as long as the actor runs, which is the stall `sim.md` §5.4 forbids — and a stalled actor stops driving the physical model.
    - The deadline is what ends an oversized read. The size bound below is a guard against attempting a retrieval known to be undeliverable; it is not the mechanism that ends one.
1. The read is bounded at 1,048,575 bytes — one byte short of 1 MB
    - Five layers could in principle bound a clipboard transfer. The limiting factor is the workstation's clipboard bridge between the X selection and the RDP clipboard channel.
    - Its ceiling is 1,048,575 bytes, counted as UTF-8 bytes of the selection as the workstation holds it — not of the wire form, which carries two bytes per character plus a terminating null unit. Asking for a single-byte text format in place of the Unicode one does not raise it, because the limit applies before that conversion. The bound is strict: 1,048,576 bytes is the first size that does not arrive.
    - The ceiling is a property of the workstation's remote-desktop service and must be re-measured if that service changes.
1. Only accept text formats
    - The actor refuses any file-descriptor formats to protect the sim-host from a compromised workstation.
1. Each read clears the latch before the copy it waits for
    - The actor discards whatever is already on the selection and requires a fresh announcement before accepting content. Without this a read returns the previous audit's records, or a value an earlier step left behind, and reports success — indistinguishable from a retrieval that worked. §8.1's confirmation depends on this ordering.

**Where the size bound is enforced.** Not at the point the decoded payload is handed to the actor. By then the transfer has been reassembled from its chunks and the cost the bound exists to avoid has already been paid; a check there constrains what the actor stores, not what it can be made to hold. The bound belongs at reassembly, where the declared total length of the channel data is available before the data is accumulated, and where accumulated length can be checked against it as chunks arrive. A reassembly that never terminates is the same hazard as an oversized one and is answered by the first obligation, not the second.

All four concern what this workstation can put in front of the actor. The actor's handling of the sim's own filesystem — where it stages anything, and if it writes what a session displays — is governed by `sim.md` §5.4 and belongs with the actor, since nothing about this host bears on it.

In the current implementation, one tug arrival produces `transaction_closed`, `job_opened`, `transaction_open` and `bdn_linked` records; retrieved together the four total 2,439 bytes. The clipboard limit can therefore accommodate records for approximately 429 arrivals.

The current documentation introduces additional overheads which increase the size of the monthly payload. 

1. **Bunkering events** All of them gained `session_id`, and both `transaction_closed` variants gained `temperature_c`, `density_kg_per_l` and `temp_corrected_volume_l` (`hist.md §4.2`). The 2,439-byte estimate for an arrival is therefore low.
2. **Authentication records will also be collected** The audit also collects the authentication and card-table events held in the same store (`digital.md §4.5`, `hist.md §7`). These were not included in the initial measurement, and will accumulate on their own cadence rather than per arrival.

A month therefore reaches the ceiling sooner than 429 arrivals, by an amount not yet established. The figures are retained as the record of the current implementation capacity and will be updated when the fuller record has been implemented. This changes how many retrievals a month takes, not whether the bound holds: the adaptive split below is what absorbs it.

`narrative.md` outlines the frequency as "serving 2–3 harbour tugs… drawn down by tugs every day", and §2.2 gives the dispense frequency as "several times a day". At the narrative's rate a month remained within a single retrieval on the original measurement, and the margin was wide enough that it is expected to survive the two changes above — but that is an expectation, not a measurement. 

An increase in rate, as per `sim-tests.md` with 4 arrivals/hour, ~96 dispenses a day and thirty times the narrative's rate, would exceed the upper limit of what a single retrieval can hold.

Where a month exceeds one retrieval, the collection must be paged: The audit conducted over several retrievals, each bounded and each separately confirmed, and assembled by the actor. Grafana's datasource proxy takes a time range on the query string, so slicing a month into retrievals that each fit is a property of the query §6.1 already issues rather than a new capability.

### 11.3  What these do not restrict

These obligations don't limit the *content* the actor accepts. 
A compromised `hist-dmz-01` returning falsified records, an unreachable one returning a client diagnostic, and an authorisation refusal arriving where a period's record was expected are all data the actor must be able to read, classify and act on — §8 requires exactly that. 

An audit misled by false records is a modelled outcome and a signal of compromise. The restriction is on what reading can do to the sim, not on what it may say.

### 11.4  The two-step variant: Not adopted

The audit pipes its retrieval to the clipboard in one command (§6.1), so nothing is written to the workstation's filesystem. An alternative is two commands: the client writes the retrieval to a file, and the clipboard tool then places that file's contents on the selection.

| | One command, piped | Two commands, via a file |
|---|---|---|
| Workstation holds records | No | Yes, until removed |
| Typed input | One long command | Two shorter ones |
| Proven | The measured path used a file | — |


A person cross-checking thirty days of transactions against paper invoices could plausibly keep the digital side on the machine while working through it, perhaps on a spreadsheet, and retrieving the same data fresh for each comparison is the less natural way to work. 
This workflow would create something potentially worth stealing in the IT zone. In the first instance this was decided to be an unnecessary step. 
