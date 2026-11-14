# Reconciliation Workstation Test Plan

Test plan for `recon-01`, the reconciliation workstation specified in
`recon.md`. One section per behavioural area; each names the individual tests it
comprises and the requirement each targets.

---

## 0  Test harness

### 0.1  What this document covers, and what it does not

This plan covers `recon-01` **as a host**: that the image carries what
`recon.md` specifies, that a client can reach a session, that a terminal opens and executes what is typed into it, and that the audit's query path reaches the log-server replica. It does not cover the reconciliation user actor. The actors audit sequence, its reconciliation logic, its retry policy, and its classification of failures are properties of the sim and belong in
`sim-tests.md`, alongside every other actor.

The division follows the document hierarchy rather than convenience:
`hist.md` pairs with `hist-tests.md` for the host and leaves the FMS's push
schedule to `fms.md`; this plan pairs with `recon.md` the same way.

**The division is also what keeps a diagnosis possible.** The reconciliation
logic is the point of the role, and it needs neither a workstation nor a
browser nor a log server to test. If its tests were written into the same suite
as the session mechanics, a failure to reach a desktop would present as a
failure to reconcile — which is the specific confusion `recon.md` §8 and the
actor's error taxonomy exist to prevent. Nothing in this plan is a precondition
for testing the audit itself.

### 0.2  Three locations

| Location | Needs a deployment | Covers |
|---|---|---|
| `deploy/tests/unit/` | No | The rendered artefacts and the build script: schema, credential projection, input-hash coverage (§1) |
| `tests/integration/recon/` | Yes — a built `recon-01` | The running host: remote access, session and terminal, browser, query path and trust, network configuration, time, credentials (§2–§8) |
| `infra/sim/tests/` | No | The actor — **out of scope here**, see `sim-tests.md` |

### 0.3  What is built, and what waits on the client

| Section | State |
|---|---|
| §1 build-time configuration | Built |
| §2 remote access | Built at image level; the negotiation assertions wait |
| §3 desktop session | Built at image level, including the terminal route: `terminal_keybinding_is_present` here and `terminal_helper_is_pinned` as `skel_carries_the_session_files` in §8. The session assertions wait; `terminal_opens_from_the_keyboard_binding` and the layout sequence are measured by hand but unautomated |
| §4 browser | `browser_starts` and `the_image_creates_no_browser_profile` built; the three policy assertions wait on the client, and the section is no longer load-bearing — see the note in §4 |
| §5 certificate trust and query path | Built, through `incus exec` and without the client: the retrieval command is run less its clipboard pipe, against records seeded through `hist-01`. The clipboard leg is the reconciliation user's run |
| §6 name resolution and containment | Built, including `only_the_remote_desktop_service_listens`; firewall policy asserted in `test_policy.sh` instead |
| §7 timekeeping | Built |
| §8 credentials on the host | Built, except `workstation_account_authenticates`, which needs the client and `corp-it` |

Everything built reads through `incus exec`, inside `recon-01`, inside a
same-zone peer, or, to seed §5's records, inside `hist-01`, so this suite
requires no runner zone membership and adds none —
there is no `configure-` step, unlike the log-server suite, whose push simulator
must be a real client on `vnet-ot`.

**That is not a claim that the runner is isolated**, and the distinction matters
for reading the results. The runner may already sit on `vnet-ot` with a
firewall-policed route to `vnet-it` from `configure-fms` or `configure-corp-it`,
and `incus exec` runs as root inside any instance on any zone regardless.

What probing from inside the instance buys is a correct measurement rather than
containment. The reachability assertions concern what `recon-01` can and cannot
reach, which is a property of its position and not the runner's. For the negative
cases, probing from the runner would answer wrongly rather than merely
differently: `cannot_reach_the_operational_technology_zone` would find the
operational-technology zone reachable on any host where `configure-fms` has been
applied, and would fail while `recon-01` was correctly isolated.

**The image-level tests are not substitutes for the ones that wait.** Where a
built test is the weaker counterpart of a pending behavioural assertion — §2's
`security_layer_is_configured_as_tls` against `security_layer_is_tls_not_the_legacy_layer`
— it catches a build that produced the wrong thing, and cannot catch a running
service that ignores what the build produced. The pending assertion supersedes
it rather than duplicating it.

Run with `make -f tests/integration/Makefile test-recon`.

### 0.4  The workstation client

Sections §2–§4 need something that connects over Remote Desktop Protocol, reaches a session, types into it, and reads the clipboard back; §5 does not, since its retrieval is run through `incus exec` less its clipboard pipe. That is the same mechanism the actor uses, but it is not the actor: these tests assert the *host's* contract — that a conforming client can connect, that the security layer is what `recon.md` §4 requires, that a session starts, that a terminal opens from the keyboard and executes what is typed into it, and that a retrieval reaches the clipboard — and make no assertion about what a client does with the session afterwards.

**The clipboard is the client's only reading of the session**, which limits what these tests can assert. Every session-level assertion here takes the form of typing something and reading what comes back. `tests/integration/recon-probe/` is the existing instrument for this and the starting point for the client.

Following the precedent of the log server's push simulator, this is a thin
client run from the test runner rather than a long-lived stub holding state.
Each test establishes its own session and does not depend on one left by another.

**Ordering constraint.** `recon.md` §4 records that a disconnected session is
not torn down, so tests in §3 and §4 may reattach to a session an earlier test
left rather than starting a fresh one. Every test that depends on initial state
must establish it explicitly rather than assume it — which is the same
obligation `recon.md` §8 places on the actor, asserted here so that the suite
does not quietly depend on execution order.

---

## 1  Build-time configuration

Test module: `deploy/tests/unit/` — no deployment required.

Targets `recon.md` §7.1 (credential projection), §6.2 (artefact pinning), §5.2
(keyboard layout) and §10.2 (input hash).

### `test_world_schema.py` — the workstation credential

Extends the existing module rather than adding a new one; the field is part of
`world.yaml`'s schema and belongs with the rest of it.

- **`workstation_password` is required on `reconciliation_user`.** Removing it
  must fail validation. Required rather than optional because `recon-01`'s
  shadow entry has nothing to project without it, and a workstation whose
  account cannot be seeded cannot be logged into — which is the host's only
  purpose.
- **The workstation password and the Grafana password must differ.**
  `recon.md` §7.1 requires that compromising one does not yield the other.
  Setting the two equal must fail validation rather than be left to review: the
  reuse is invisible in a deployed environment, since both logins continue to
  work.

Both are added to the module's `INVALID_CASES` table, matching the existing
convention of one mutation per rejected condition.

A positive assertion accompanies them: the two passwords in the committed
`world.yaml` are in fact distinct. The rejection cases prove the schema refuses
reuse; this proves the authored file does not rely on that refusal.

### `test_recon_credential_projection.py`

The workstation login is a directory account (`recon.md` §7.1), so this module
asserts a projection into `build/ldif/corp-it.ldif` and an **absence** on
`recon-01`. The negative half is the substantive one: a local account left
beside the directory one would authenticate, invisibly, because both logins
work.

- **The reconciliation user has a directory entry**, and it is a
  `posixAccount` — without `uidNumber`, `gidNumber` and `homeDirectory` it
  cannot back a Linux login at all.
- **The entry carries `loginShell`, and no operator entry does.**
  Only this entry and the systems administrator's open an interactive session on a host.
  The operators are authenticated by the FMS's own bind and log in to nothing, so a shell on their entries would grant a capability the narrative does not describe.
- **The `gidNumber` every account claims is backed by a `posixGroup`.** A
  session started against a bare identifier still succeeds, so this cannot be
  established from a successful login.
- **The password is projected as `{SSHA}`**, by the same derivation as every
  other directory entry, and reproduces byte for byte on a second render.
- **The directory entry carries the workstation password and not the Grafana
  one.** Nothing else would catch the two being exchanged: both are valid
  passwords, both render to well-formed hashes, and the result would
  authenticate nobody while looking correct.
- **No verifier is rendered for the workstation.** `build/shadow/` must not
  exist; its reappearance means a local account is being seeded alongside the
  directory one.
- **The directory client names no credential and no account.** No bind
  password, and not the reconciliation user's own name: the client resolves
  whichever account authenticates, and naming one would make the workstation
  hold knowledge of who its user is.
- **Directory identifiers cannot collide with local ones.** The group's name is
  not one the distribution ships, and every identifier sits at or above the base
  the renderer allocates from. `files` precedes `sss`, so a colliding entry is
  shadowed by the local one and never reached — which produces a working login
  with the wrong primary group, visible only as `id` on a deployed host. Both
  halves were reintroduced by measurement rather than anticipated: a group named
  `staff` is shadowed at group identifier 50, and one numbered 1000 resolves as
  the container runtime's `lxd`.
- **Every directory identifier is distinct.** Two entries sharing a `uidNumber`
  resolve to whichever the directory returns first, and both logins then act as
  one account.
- **No plaintext of either password reaches `deploy/build/`.**

### `test_recon_keyboard_layout.py`

Covers §5.2 as far as it can be covered without a session. What a scancode
produces can only be measured in a running one, which waits on the client; these
assertions catch a build that pinned the wrong layout, and a credential that
would not survive the wrong layout being pinned.

- **The session pins a layout at all.** xrdp performs its own
  client-advertised keyboard negotiation and loads a `km-<layout>.ini` on every
  connect, so a session that pins nothing takes whatever the client asked for
  and the same scancode produces different characters between sessions.
- **The pinned layout is `gb`.** The value lives in the `.xsession` heredoc in
  `build-scripts/recon-01.sh.j2` and nowhere else; this test is its second
  reader, so changing it requires changing the scancode table the actor injects
  with it.
- **The reconciliation user's username and both passwords use only characters
  the two layouts place identically.** They disagree on `@ " # £ ~ ¬ \ |`, so a
  credential drawn from the agreed set cannot make a layout regression present
  as a rejected login. A deliberate restriction, asserted so that it survives
  the next credential change.
- **The invariant set excludes every known divergent character.** A test on the
  predicate the three assertions above share, so that widening it by hand
  cannot quietly defeat them.

**The audit command cannot be held to the invariant set.** `recon.md` §6.1 pipes
the retrieval into the clipboard tool, and the shell pipe is `|`. The address is
safe — `: / ? & = - .` are agreed, as are the single quotes protecting `&` from
the shell — but the command as a whole is not. Constraining the audit command to
the invariant set is rejected: it would choose the retrieval for the characters
it contains rather than for what it retrieves.

The requirement previously rested on LogsQL using `|` to separate pipeline
stages. That no longer holds — the audit retrieves whole records and sorts them
locally, so its expression has no pipeline — but the requirement is unchanged,
because the shell pipe into the clipboard tool needs the character
unconditionally.

Under `gb` the pipe is `Shift` + `0x56`, the extra key beside the left `Shift`,
which the United States layout does not have; under `us` it is `Shift` + `0x2B`,
a key `gb` gives to `#` and `~`. Three requirements follow:

- A layout regression presents as a malformed query returning an error from
  `hist-dmz-01`, not as a rejected login, so the credential restriction above
  cannot catch it.
- The actor's scancode table must carry `0x56` (`sim/sim-impl.md`), or the audit
  cannot compose a query at all.
- `keyboard_layout_is_the_pinned_one` in §3 types the pipe itself rather than a
  proxy for it, and is the only assertion covering this.

### `test_input_hash_symmetry.py`

Already written and passing over all nodes with build scripts. It requires no
change to cover `recon-01`: the property it asserts — that the set of rendered
artefacts a build script installs and the set its input hash covers are the same
set — is stated over whatever each script references. `recon-01` gaining a
shadow hash and a policy file is exactly the case it was written before to
guard.

### `test_recon_artefact_fetch.py`

Targets `recon.md` §6.2's three departures from every artefact currently pinned,
each of which fails silently if unhandled.

- **The architecture mapping is applied.** `ARTEFACT_ARCH` is `amd64`/`arm64`
  and Mozilla publishes `x86_64`/`aarch64`; the unmapped value yields a URL that
  does not exist.
- **The digest is selected from a path-keyed `SHA256SUMS`.** The existing
  `$2 == f` bare-filename match selects no row against
  `linux-x86_64/en-GB/firefox-153.1.0esr.tar.xz`, which presents as a missing
  digest rather than a wrong one.
- **The pinned version is the one named in `recon.md` §6.2.** Guards the version
  drifting from its specification.

---

## 2  Remote access

Test module: `tests/integration/recon/test_01_remote_access.py`

Targets `recon.md` §4.

### `xrdp_is_reachable_on_the_zone_address`

Connect to `10.30.0.10:3389`. Asserts the service is listening where §3's
interface table says it is.

### `security_layer_is_tls_not_the_legacy_layer`

Negotiate a connection and assert the server offers the Transport Layer Security
layer. Under xrdp's default `negotiate` the server falls back to the legacy
standard Remote Desktop Protocol layer, which a client requiring enhanced
security refuses — so this is asserted on what the server negotiates, not by
reading `xrdp.ini`. The configuration file being right is not evidence that the
running service is.

### `xrdp_can_read_its_private_key`

Assert a connection completes the security handshake rather than failing with
`SSL_CERT_NOT_ON_SERVER`. Covers the key's permissions of §4 through their
observable consequence; a key the `xrdp` user cannot read produces a service
that starts normally and fails only when a client arrives.

**Its built counterpart is `xrdp_key_is_readable_by_xrdp_alone`**, which reads
the key's ownership and mode from the image. The names are kept distinct because
the two are not interchangeable: the permissions are what the build produces,
completing a handshake is what the running service does with them. The same relationship holds
between `security_layer_is_configured_as_tls` and
`security_layer_is_tls_not_the_legacy_layer` (§0.3).

### `xrdp_presents_the_ca_issued_certificate`

Assert the certificate presented in the handshake chains to the deployment's
root and names `recon-01`, with no address. A client verifying it as the
reconciliation user does — against the root alone, and the name `recon-01` given
explicitly — completes the security handshake from a peer on `vnet-it`. The
distribution's self-signed pair being presented instead is a failure: the
reconciliation user would refuse it.

The handshake is made from a zone peer rather than through the sim host's proxy
port: the sim host carries no client that could make it, and the proxy passes
the stream through in TCP mode, so the certificate presented is the same on
either path. The proxied path is exercised by the reconciliation user itself.

### `xrdp_services_are_enabled`

Both units. `xrdp` accepts the connection and `xrdp-sesman` starts the session
behind it, so a host carrying only the first accepts a connection and then fails
to produce a desktop — which presents to an actor as a session that never
appears rather than as a service that is absent.

### `rejected_credentials_produce_a_defined_outcome`

Connect with a wrong password and assert a bounded, reported failure rather than
a hang. The host's obligation is that the failure is prompt and distinguishable;
what the actor does with it belongs to `sim-tests.md`.

---

## 3  Desktop session

Test module: `tests/integration/recon/test_02_session.py`

Targets `recon.md` §5 and, for the terminal, §2.

The module divides the same way §2 does. The assertions below that read the
image are built; those that need a session wait on the client, and the two are
not interchangeable — what the image carries is not evidence of what a session
does with it.

**Built: what the image carries.**

### `a_terminal_emulator_is_installed`

`recon.md` §2 keeps the terminal a stock Xfce desktop would have. It is a
recommendation of the `xfce4` metapackage rather than a dependency, so the
fleet's `--no-install-recommends` practice drops it unless it is named, and the
regression is silent: nothing in the session or the actor depends on it, so its
absence surfaces only as a workstation that does not resemble a real one.

### `the_audit_client_is_installed`

`curl` is on the host, at an absolute path rather than through `command -v`, so
that a shell function or an earlier entry on `PATH` cannot satisfy the
assertion in place of the program. The failure this guards against is not a
build regression but an inherited one. The base image happens to carry `curl`,
so the audit's client was present before anything installed it deliberately;
`recon.md` §2 names it in the package list for that reason, and this test is
what stops the audit resting on an inheritance no contract covers.

### `a_clipboard_tool_is_installed`

`xclip` is on the host, asserted the same way. Unlike `curl` it is absent unless
named, and unlike the terminal its absence is not cosmetic: the actor reads a
framebuffer rather than text, so an export that reaches only the workstation
reaches nothing. The regression presents as an audit that retrieves records and
returns none of them, which is `recon.md` §8's failure-indistinguishable-from-
success case reached by a build route rather than a session one.

### `browser_has_a_desktop_entry`

The entry of `recon.md` §5.3 exists and carries a `[Desktop Entry]` group. This
is the assertion whose absence allowed the gap: a tarball Firefox is installed,
on `PATH`, and runnable, while the session offers no route to it, and every
check performed during the image build passes in that state because each names
the binary on a command line.

### `browser_desktop_entry_names_the_installed_binary`

`Exec`, with field codes stripped, resolves to an executable file. An entry
naming a path that does not exist is listed in the menu, is selectable, and does
nothing when activated; the desktop reports no error.

### `browser_desktop_entry_is_displayed_in_the_menu`

`Type=Application`, with neither `NoDisplay` nor `Hidden` set. Either of those
removes the entry from every menu and finder while leaving a valid file in
place, reaching the same outcome as no entry at all by a route that inspection
of the file would pass.

### `browser_desktop_entry_name_is_the_pinned_one`

The displayed name is `Firefox`. An earlier revision made this a contract
because the actor selected the browser by what the session displayed. The audit
no longer uses the browser, so the assertion is retained as a check that the
entry is coherent rather than as a contract the audit depends on.

### `terminal_helper_is_pinned` — moved to §8

The preference now lives in `/etc/skel/.config/xfce4/helpers.rc`, because the
workstation account is a directory account and there is no home directory in the
image to assert it in (`recon.md` §7.1.1). The assertion is
`skel_carries_the_session_files` in §8, alongside `.xsession`, which moved for
the same reason.

The requirement is unchanged and is the one the audit path depends on.
`exo-open --launch TerminalEmulator`, which the keyboard binding runs, answers
an unset preference with Xfce's preferred-application chooser — a modal dialogue
that takes focus and absorbs every subsequent keystroke. The audit command is
then typed into a dialogue, the actor's steps all report success, and the read
returns nothing.

What is no longer assertable from the image is that the file reached the
account. That is a property of the first login rather than of the build, and it
belongs with `terminal_opens_from_the_keyboard_binding` below, which asserts the
outcome the file exists to produce.

### `terminal_keybinding_is_present`

The Xfce keyboard shortcut for `Ctrl+Alt+T` exists and runs the helper launcher.

Xfce supplies this binding by default, so the assertion guards an inherited
property rather than one the build creates — which is exactly why it is worth
asserting. `recon.md` §5.4 makes it the session's route to a terminal, and a
default that changed between distribution releases would remove the audit's
first step with nothing in the build failing.

**Waiting on the client: what a session does.**

### `session_starts_a_desktop`

Connect with the seeded credential and assert a desktop session is reached.

### `keyboard_layout_is_the_pinned_one`

Type a known sequence of scancodes and assert the characters produced are those
the pinned layout defines. This is the contract of §5.2, and it is asserted
through what the session produces rather than by reading the session file. A
layout change would otherwise present as a rejected credential somewhere later,
with nothing pointing at the cause.

**The sequence is `Shift` + `0x56`, `0x2B`, `Shift` + `0x04`**, which under `gb`
produce `|`, `#`, `£` and under `us` produce nothing, `\`, `#`. Specified as
scancodes rather than characters so that it tests the path the audit takes, and
including the pipe because that is the character §1 identifies as load-bearing.
A sequence drawn only from the classes the credentials use would pass under
either layout.

A successful login is not evidence that the contract holds:
`test_recon_keyboard_layout.py` requires the credentials to avoid exactly the
characters that distinguish the layouts. The two tests are complementary — one
keeps the credential insensitive to the layout, the other proves the layout is
nonetheless the specified one.

**Measured by hand; automation is what waits.** All three scancodes were sent
from a manual Remote Desktop Protocol session and produced `|`, `#`, `£`, so
§5.2's contract holds against the deployed host — including the ordering between
xrdp's keymap and the session's `setxkbmap`.

What remains is the assertion, not the answer: a manual measurement establishes
the layout at one moment and catches no regression, which is this test's whole
purpose. The client's scancode table carries `0x56` and is unit-tested against
these three.

The middle scancode is the one most easily got wrong. `0x2B` unshifted is `#`
under `gb` and `\` under `us`, both plausible-looking, so an assertion that
merely checks a character arrived passes under either layout.

### `no_dialogue_is_present_on_a_fresh_session`

Assert the session presents the desktop and nothing in front of it. This is
`recon.md` §5.1's one guarantee about the session's initial state, and the
reason it is load-bearing has nothing to do with coordinates: anything holding
focus absorbs typed input, so a notification or update prompt defeats an audit
conducted entirely by typing.

§5.1 no longer guarantees layout, and the audit path holds no coordinate to
invalidate — the terminal is reached by a keyboard binding and the retrieval is
typed into it.

### `terminal_opens_from_the_keyboard_binding`

Send `Ctrl+Alt+T` into a session and assert a terminal appears and accepts
input. **This is the audit's first step and the most valuable session-level
assertion in this document.**

The two image-level assertions above establish that the binding exists and that
the helper it resolves is set. Neither establishes that pressing the keys starts
a terminal, and the gap between them is where the observed failure lived: a
binding that resolves to a helper launcher with no preference set is present,
valid, and answers with a dialogue.

**Assert what the terminal does, not that a window appeared.** The evidence
available to a remote client is the same evidence the actor has — the clipboard
— so the assertion is that a command typed into the new terminal places its
output on the selection. A window that appeared and does not have focus fails
this and would pass a check for a window.

### `session_state_is_confirmed_before_input`

The confirmation of `recon.md` §8.1, exercised as the host-level half: with a
session driven into a modal state, assert that the confirmation fails rather
than reporting success.

This is the one test in this document that must be written by provoking the
failure rather than by observing the normal path, because the normal path passes
whether or not the confirmation works. `recon.md` §5.1 records how to provoke
it: activate an application for which no handler is configured.

**The actor's use of the confirmation is out of scope here** (§9). What this
asserts is that the mechanism distinguishes a live shell from a dialogue holding
focus — which is a property of the workstation and the clipboard channel, not of
the actor's audit sequence.

### `browser_can_be_launched_from_within_the_session`

Reach the entry of §5.3 through the desktop — not by running the binary — and
assert a browser window appears. This supersedes the four image-level entry
assertions above rather than duplicating them: those establish that the file is
correct, and only this establishes that the route works.

An entry can be present, valid, displayed and correctly named and still not
start the browser when activated; only a session establishes otherwise. Every
image-build check passes on a host where the browser is unreachable from the
desktop, because each names the binary on a command line.

Confirmed by hand and unautomated until the client exists.

---

## 4  Browser

Test module: `tests/integration/recon/test_03_browser.py`

Targets `recon.md` §6.3, and asserts the policy **behaviourally** throughout.
The reason is given there: a policy file in a location the browser does not read
fails silently — the browser starts normally and none of the settings apply — so
a test that the file exists would pass in exactly the case that matters. Whether
a tarball install reads `/etc/firefox/policies/` or `<install-dir>/distribution/`
is unresolved, which is what makes this more than a stylistic preference.

**This section is not load-bearing.** `recon.md` §6.1 conducts the audit with
`curl`, so the browser is an installed application rather than the read path,
and a failure here is not an audit failure. It is retained because the browser
is still part of the modelled world.

**Two of these are built; the three policy assertions are not, for the same
reason that makes them behavioural.** Reading back what a policy did means
rendering `about:policies` and parsing it, or driving the browser over its
remote protocol. A headless start yields an image rather than text, so it can
establish that the browser runs and not what it decided.

| Test | State after the change of client |
|---|---|
| `browser_starts` | **Retained.** Covers §2.1's runtime dependencies, which are a property of the installed application and independent of the audit. |
| `browser_does_not_update_itself` | **Retained, and the only requirement in this section.** §6.3 makes reproducibility the one policy requirement: a tarball that updates itself in place leaves the published image no longer describing what is running. |
| `password_manager_offers_nothing` | **Retained, demoted.** The audit never types a credential into the browser, so this no longer protects the log-server credential. It remains sensible posture for a workstation browser. |
| `password_reveal_is_unavailable` | **Retained, demoted.** Same reasoning. |
| `the_image_creates_no_browser_profile` | **New, and built.** The build starts the browser under a throwaway `HOME` and removes it, so no profile exists in the image. Asserted because of what depends on it later, not for itself: whether §6.4's trust anchor becomes a property of the image or of the first session turns on whether a profile exists at build time to import into, and a trust step performed during a session presents a failure as a failed audit rather than as a broken workstation. It also catches a profile created by hand at the workstation, carrying a certificate exception clicked through in that session — which would satisfy the trust assertions of §5 whether or not the anchor works. |
| `json_document_renders_as_plain_text` | **Retired.** It existed so that a select-all copy of a JSON response returned the document rather than the viewer's rendering of it. Nothing copies from the browser now. |
| `reload_is_not_served_from_cache` | **Retired.** It existed so that an unreachable historian produced a visible error page rather than the previous attempt's content. That failure-detection role is now `recon.md` §6.1's routing of the client's diagnostics into the payload, asserted in §5. |

The retired pair is listed rather than deleted silently: the coverage was not
lost, the behaviour it guarded is no longer on any path.

### `browser_starts`

Covers §2.1's runtime dependencies through their observable consequence: a
missing library produces a browser that exits immediately rather than a build
that fails.

### `password_manager_offers_nothing`

Submit a login form on a local page and assert no save-login prompt appears and
no credential is written to the profile. Demoted: the audit never types a
credential into the browser, so what this now covers is that a person using the
browser does not leave one in the profile.

### `password_reveal_is_unavailable`

Assert the reveal control does not expose a typed password. Demoted for the same
reason. `recon.md` §7.2 no longer rests on either of these two.

### `browser_does_not_update_itself`

Assert no update check is attempted and the installed version is the pinned one.
A tarball install can replace the pinned build with an unpinned one inside a
running deployment, after which the published image no longer describes what is
running.

---

## 5  Certificate trust and the audit's query path

Test module: `tests/integration/recon/test_07_query_path.py`

Targets `recon.md` §6.1 and §6.4.

The internal certificate authority signs the leaf `hist-dmz-01`'s Grafana
presents, and this host installs its root into the system trust store
(`digital.md` §2.7, `recon.md` §6.4), so the audit client can verify the log
server. Three tests carry this section; "The failure direction is already
measured" below records what the third already has.

**These need a seeded replica as well as a built `recon-01`.** The assertions
are about what the audit client retrieves across the zone boundary, so the
period queried must hold known records. They are posted from inside `hist-01`
to its JSON-line ingestion listener on port 9429, verifying `hist-01`'s
certificate as the FMS does, so that they reach the replica by replication as
the FMS's records do. The runner joins no zone, as for the rest of this suite
(§0). Inserting into the replica directly would leave records the primary does
not hold, which is a divergence between the two (`digital.md` §6.3).

**The seeded records lie further back than any audit reaches.** They persist on
both hosts, so a running sim's next audit would otherwise retrieve them: a
record it cannot assign to a transaction is reported, and a transaction with no
paper is fatal (`sim/sim-impl.md`, "Outcome"). An audit retrieves no further back
than the start of the window it audits, less the boundary margin and the session
lifetime. The settling delay is shorter than the shortest period, so the window
audited next is at most the previous calendar month, and that reach is under 32
days. The records are therefore dated 35 days before the run, well within the
three-year retention (`hist.md` §8).

**The command run is the actor's, less the clipboard.** `incus exec` has no
desktop session, so `xclip` has no selection to write to. The tests run the
retrieval command of `sim/sim-impl.md` ("Retrieval") without its final
`| xclip -selection clipboard`, as root on `recon-01`, and read the text the
pipe would carry. The command is restated here from that form, which the
actor's exact-line test pins on the sim's side; a change to one is a change to
both. The clipboard leg is the reconciliation user's run.

### `audit_client_reaches_grafana_without_a_verification_failure`

From `recon-01`, request `https://hist-dmz-01/` with the audit client and assert
it completes rather than failing verification. Certificate verification is never
disabled to make this pass — `recon.md` §6.1 rejects `--insecure` and a test
that used it would assert nothing.

Three preconditions fail identically — a root never installed, a root installed
into the wrong store, and a root whose validity window has lapsed — which is why
the assertion is stated on what the client does rather than on a file being
present. **It must be asserted from `recon-01`.** The debugging procedure that
tunnels to a log server dials `localhost`, which the certificate does not name,
so it will keep reporting a certificate error after the trust anchor is working
correctly.

### `datasource_proxy_returns_records_for_the_seeded_period`

Seed three records, each carrying an identifier unique to the test run, at
instants relative to a whole second `t` shortly before the test:

- a bunkering event of `hist.md` §4.2 at `t`;
- a record in the stored shape of a `LOGIN` (§4.3) — `msg_id` and
  `fms@99999.session_id`, with no `event_type` — at `t` plus half a second;
- a bunkering event at `t` plus one second.

Retrieve the span from `t` to `t` plus one second with the reconciliation user's
credential, repeating until the seeded records have replicated or a bound
passes. Assert that the output ends with `retrieval-end 0`, that every line
before it is a JSON object, and that among them are the first two seeded
records and not the third.

Covers `recon.md` §6.1's route end to end — the credential authenticates, the
proxy identifier resolves, and the datasource answers — and the three
properties of the retrieval that only the real store can establish: the span's
start is included and its end excluded, so consecutive spans neither overlap nor
leave a gap; with no filter, a record without an `event_type` is returned with
the bunkering events; and a successful retrieval ends with its completion line.

Asserted through the proxy rather than against VictoriaLogs directly, because
the proxy path is what the audit uses and the direct ports are denied from this
zone — a test that reached the store another way would pass on a host where the
audit could not run.

### `a_failed_retrieval_is_distinguishable_from_an_empty_period`

The assertion that carries the failure-detection role the retired
`reload_is_not_served_from_cache` used to hold. Two retrievals, one in each
direction:

- **A refused credential is a diagnostic, never a record.** With a wrong
  password, assert the output carries Grafana's error body and `curl`'s line
  `curl: (22) The requested URL returned error: 401`, and ends with
  `retrieval-end 22`. The diagnostic, written to standard error at once,
  arrives before the body, which `curl` writes to standard output when it
  finishes; the order is the client's, and either is accepted.
  Without `--fail-with-body` the error body, itself a JSON object, would arrive
  under a successful exit and read as a record (`sim/sim-impl.md`,
  "Retrieval").
- **An empty span is the completion line alone.** For a one-second span an hour
  after the test, assert the output is `retrieval-end 0` and nothing else.

`recon.md` §6.1 requires the audit command to fold the client's standard error
into the pipe and to end every response with its completion line for exactly
this reason: a failure that left nothing would be indistinguishable from a
month with no records. This test is what establishes that the two are
separable, and it is the host-level half of `recon.md` §8's third obligation.
The preceding test establishes the third case, that a successful retrieval
returns records and not a diagnostic.

A far end that does not answer is not produced here: it needs Grafana stopped
or a firewall's timeout waited out. Its classification, statuses 6, 7 and 28,
is the actor's and is tested in-module (`sim-tests.md`, "Retrieval").

#### The failure direction is already measured

From a terminal in a manual Remote Desktop Protocol session, the audit command
of `recon.md` §6.1 was run against the log-server replica and its output read
back from the clipboard:

```
curl: (60) SSL certificate problem: self-signed certificate
```

Three things follow, none needing the certificate authority.

**`recon.md` §6.1's routing works as specified.** `-sS … 2>&1 |` folded the
diagnostic into the pipe, so the failure arrived as readable text rather than an
empty selection — which is indistinguishable from a month with no records.

**The path to the replica is exercised by the client that will use it.** The
request completed a connection and read a certificate, so the route across
`fw-dmz-it` to `hist-dmz-01:443` works — previously asserted from routing
configuration and the fleet-wide policy suite, never from the audit client.

**It is a baseline for the authority's arrival.** A self-signed certificate is a
*distinguishable* case; the three §6.4 records as indistinguishable from one
another present differently, as a failure to build a trust chain. So this is
what the absence of the authority looks like, and not what its misconfigurations
will.

The two tests above replace the measurement with automated assertions, and add
the successful direction it could not show before the authority existed.

---

## 6  Name resolution and containment

Test module: `tests/integration/recon/test_04_network.py`

Targets `recon.md` §3.

**Firewall policy is asserted elsewhere, deliberately.**
`deploy/tests/integration/test_policy.sh` enumerates the policy and its
complement fleet-wide, and already covers this host more thoroughly than a
per-host suite would: the permitted flow to `hist-dmz-01:443`, the wrong-port
cases (22, 8428, 9428), the wrong-destination case, ICMP across both boundaries
it touches, and the blocked reverse direction. `test_internet_egress.sh` covers
outbound internet.

Two reasons beyond avoiding duplication. A rule change then has one set of tests
to update rather than one per host that references it. And `test_policy.sh`'s
`blocked()` helper distinguishes a firewall DROP from a destination that is
merely down — it requires the connection to time out rather than be refused, and
section 4 is preceded by a check that every node involved is running, on the
stated grounds that otherwise the results are unreliable. A per-host suite
reimplementing negative reachability without that distinction would report a
stopped target as a correctly enforced denial.

**Intra-zone reachability is out of scope for this document.** That hosts on one
zone bridge reach each other without restriction is a property of every zone, not
of this workstation, so asserting it from `recon-01` would test a fleet-wide
property through an arbitrary example. `digital.md` §1.2 states it and nothing
asserts it yet; it belongs in a general test.

Recorded here because the gap is otherwise invisible: `recon.md` §3.1 relies on
the property, since the workstation's use of corporate directory and time
services depends on it, and unrestricted intra-zone reachability is an attack
path as much as a convenience.

Whatever asserts it should note that a successful connection is the wrong
signal. A refusal proves the packet reached the host and was answered, which is
positive evidence of no filtering even with nothing listening; only a timeout
means filtered. Probing a port with nothing behind it and asserting a prompt
refusal tests the property more directly than reaching a live service, which
conflates the path being open with the service being up.

What remains here is a property of the image.

### `resolves_only_the_hosts_it_connects_to`

`/etc/hosts` names loopback, this host, `hist-dmz-01` — the name the audit
uses — `corp-it` — the name the directory bind uses — and `siem-01` — the name
the security monitoring agent forwards to (`recon.md` §3) — and nothing else. Containment rather than function: a hosts file naming the wider
topology hands an attacker on this workstation a map of the environment at no
cost.

### `only_the_remote_desktop_service_listens`

The set of listening TCP sockets on a non-loopback address is exactly
`{3389}`. Targets `recon.md` §11.1: the remote desktop session is the only
channel between this workstation and the actor, and that holds only while
nothing else on this host accepts a connection.

Asserted as an enumeration rather than as the absence of named services. A test
that probed for a secure shell daemon would pass on a host running a file
transfer service of any other kind, and the property §11.1 needs is that there
is no second channel — not that one particular second channel is missing.

**A deliberate addition would fail this test, which is the intent.** §11.1
records that a remote-access service on this workstation is a separate decision
and a legitimate one; if it is taken, this assertion changes with it, and the
change is then visible in review rather than arriving as a service nobody
decided to add. The security monitoring agent of `recon.md` §3.3 connects
outbound to `siem-01` and listens on nothing, so it does not disturb this.

Loopback-bound sockets are excluded: they are unreachable from `vnet-it` and
from the sim, so they are not a channel in the sense §11.1 means.

### Asserted in `test_policy.sh` §15 — the sim-host egress guard

`recon-01` cannot dial the sim's `it-recon-user` interface: it originates
connections only, and `egress-guard.nft` drops anything arriving on it
(`recon.md` §3.2). No firewall node can enforce this, since both hosts sit on
`vnet-it` with nothing between them.

Placed there rather than here because that suite can tell a DROP from a host that
is down. It was a genuine gap before this issue: the guard was covered only by
`test_sim_image.sh` confirming the file is present in the image, which is not
evidence that it loads or that it drops anything.

---

## 7  Timekeeping

Test module: `tests/integration/recon/test_05_time.py`

Targets `recon.md` §9.

An earlier revision gave a stronger reason for asserting this on `recon-01` in
particular: the browser was the audit's client, and Grafana resolves a relative
time range against the clock of the machine displaying the dashboard, so a
drifted workstation audited a different month from the one it believed it was
auditing. `recon.md` §9 records that this no longer holds — the audit's period
arrives as explicit bounds computed by the actor — so the audit window is no
longer at risk from this host's clock.

Two reasons remain and neither is weak. A clock behind a certificate's
`notBefore` produces a verification failure indistinguishable from §5's three
causes. And this host's own records — the shell history of §8 among them — are
evidence in an investigation, which a free-running clock makes impossible to
place in order beside the rest of the fleet's.

Four assertions rather than one, because these fail in ways that are not
equivalent and a single test would report them identically.

### `chrony_is_running`

The service is active. A host carrying the configuration with no running daemon
free-runs.

### `time_source_is_the_site_time_source`

`corp-it` is among the configured sources, reached intra-zone with no firewall
between (`digital.md` §1.3).

### `clock_is_synchronised`

The site source is **selected**, not merely configured. chrony marks the selected
source `^*`; one that is configured but never reached shows `^?` and still
appears in the source list, so the previous assertion passing is not evidence
that the clock is disciplined. A configured-but-unreachable source is exactly the
failure a list-based check misses.

### `offset_is_within_tolerance`

Within the one second `narrative.md` §5 requires, asserted with margin rather
than at the limit so a measurement at the boundary does not flap.

---

## 8  Credentials on the host

Test module: `tests/integration/recon/test_06_credentials.py`

Targets `recon.md` §7. §1 asserts what is rendered; this asserts what the image
carries and what the running host resolves.

**Most of this section asserts absence**, because the workstation account is a
directory account and this host holds no verifier.

### `no_local_account_for_the_reconciliation_user`

Neither `/etc/passwd` nor `/etc/shadow` carries an entry for the reconciliation
user. The failure it guards is invisible in a deployed environment: a local
account left behind would authenticate, the session would start, and nothing
would show which credential was accepted.

### `no_account_can_log_in_locally`

No local shadow entry carries a usable password. Stronger than the predecessor
it replaces, which had to exempt the workstation account; with the login in the
directory there is no exemption to make, so a usable local password is either a
package's post-install script or the local account this design removed.

### `directory_client_is_running`

`sssd` is active. A host carrying the configuration with no running daemon
authenticates nobody, and the failure appears at the greeter, not at boot.

### `directory_client_holds_no_credential`

`/etc/sssd/sssd.conf` carries neither of the identity's passwords and no
`ldap_default_authtok`. The client searches anonymously and binds as the
authenticating user, so it needs none (`recon.md` §7.1.1).

### `directory_client_binds_over_tls`

`sssd.conf`'s `ldap_uri` is `ldaps://corp-it/`, and no setting disables
certificate verification (`ldap_tls_reqcert` is not `never`). A client that
falls back to an unencrypted or unverified bind when TLS is unavailable would
pass every other test in this section while presenting the workstation
password to whichever host answers on `corp-it`'s address.

### `directory_client_configuration_is_not_readable`

The file is `0600 root:root`. `sssd` refuses to start on a group- or
world-readable configuration and reports it only to the journal, so a permissive
mode presents as a workstation that authenticates nobody.

### `name_service_consults_the_directory`

`passwd`, `group` and `shadow` all name `sss` in `/etc/nsswitch.conf`.
Authentication and resolution are separate stacks: a host that authenticates a
user it cannot resolve reaches a session with no home directory and no primary
group, which presents as a broken desktop rather than a name-service fault.

### `home_directories_are_created_at_login`

`pam_mkhomedir` is in the session stack. A directory account has no home
directory until one is made for it; without this the session starts in one that
does not exist, so `~/.xsession` is absent and no desktop is reached
(`recon.md` §7.1.1).

### `skel_carries_the_session_files`

`/etc/skel/.xsession` and `/etc/skel/.config/xfce4/helpers.rc` are both present,
the latter naming `xfce4-terminal`. This is where `terminal_helper_is_pinned`
now lives: asserted in the location `pam_mkhomedir` populates from, since the
image holds no home directory.

### `the_reconciliation_user_resolves_through_the_directory` — needs `corp-it`

`getent passwd <name>` resolves on `recon-01`, with the shell the directory
supplies — the end-to-end proof that the entry, the client and the name service
agree, which the assertions above establish only separately.

**Skips rather than fails when `corp-it` is unreachable.** A workstation
correctly configured against an unavailable directory is a different fault from
a misconfigured one.

### `the_primary_group_resolves` — needs `corp-it`

`id -gn` returns the directory group's name. A `gidNumber` naming nothing still
logs in, so this cannot be inferred from a successful session; it shows up later
as files owned by a group that displays as a number.

### `workstation_account_authenticates`

The seeded account can establish a session — the proof that the projection, the
client and the Pluggable Authentication Modules stack agree, and that the
LDAPS bind of `recon.md` §7.1.1 works end to end. **Needs the client**, and
now `corp-it` as well.

### `log_server_credential_is_absent_from_the_image`

The Grafana password appears nowhere in the image: not in a browser profile, not
in a bookmark, not in an environment file, not in any autostart entry.

**The image, not the host.** A running audit leaves the credential in the
account's shell history by design (`recon.md` §7.2), so a test asserting absence
from the *host* would claim a property nothing holds. The build-time property is
unaffected and worth keeping.

### `history_is_appended_as_each_command_is_entered`

`/etc/bash.bashrc` sets the history file to be appended per command rather than
at shell exit (`recon.md` §7.2).

**Spawn the shell the way the session spawns one — interactive, non-login — and
read the variable from inside it**, rather than grepping the file that sets it.
The audit's terminal spawns a non-login shell, which never reads a profile
drop-in, so a test run in a login shell would pass against the placement the
specification rejects as well as the one it requires.

Asserted from outside the image rather than during the build: reading the value
back inside the build means opening a session as the account, which writes a
`pam_unix` record to `/var/log/auth.log` and the journal — shipping an
authentication event dated before the host was deployed.

This asserts determinism, not security: the residue is intended, and what the
setting prevents is its arriving on some months and not others.

### `workstation_password_is_absent_from_the_image`

The plaintext must not survive anywhere in the image. Retained unchanged from
the local-account revision: the projection moved to the directory, and the
property that no plaintext survives is unaffected by which target it has.

---

## 9  Explicitly out of scope

- **The reconciliation user actor.** Its audit sequence, reconciliation logic,
  retry policy, clean-audit-versus-discrepancy distinction, and classification
  of failures all belong to `sim-tests.md`. §0.1 gives the reason: they need no
  workstation, and testing them here would make an unreachable desktop present
  as a failure to reconcile.
- **All four of `recon.md` §11.2's obligations.** That every read is bounded in
  time and resolves to a typed error at its deadline, that the read is bounded at
  1,048,575 bytes — one byte short of a mebibyte, the first size that does not
  arrive — that file-descriptor formats are refused rather than unrequested, and
  that each read clears the latch before the copy it waits for and requires a
  fresh announcement. All four are assertable with no workstation, no session and
  no log server. They belong in `sim-tests.md` and are named here because §11
  records them in `recon.md`, so a reader who finds the requirement in the host
  specification would otherwise look for the assertion in this suite.

  Three of them are properties of the actor's clipboard backend, which is an
  inert recorder of protocol callbacks and therefore directly unit-testable. The
  size bound is not: §11.2 places it at reassembly, where the declared total
  length is available before the data is accumulated, so the assertion is made
  there rather than at the callback.

  Two are most easily written so that they cannot fail. A latch test that clears,
  triggers and reads passes whether or not the ordering is enforced; what it must
  assert is the negative — that content already on the clipboard before the read
  began is refused. A deadline test needs a backend that never responds, which no
  healthy workstation produces on demand.

  What remains here is the host-level half: `only_the_remote_desktop_service_listens`
  in §6 asserts that no second channel exists for those obligations to be
  circumvented through.
- **Acting on a session left in a modal state.** The condition is now partly
  covered here and the division needs stating, because it changed with
  `recon.md` §8.1.

  `recon.md` §5.1 and §8 record a measured case: a click that activated an
  application with no configured handler produced an Xfce chooser, which took
  focus, absorbed a full typed command, and was still present at the next
  connection because §4 tears no session down. §3's
  `no_dialogue_is_present_on_a_fresh_session` does not cover it — that dialogue
  was provoked rather than spontaneous, and the session was not fresh.

  §3's `session_state_is_confirmed_before_input` now covers the *mechanism*:
  that a confirmation through the clipboard distinguishes a live shell from a
  dialogue holding focus. That is a property of this workstation and its
  clipboard channel, so it is assertable here.

  What remains the actor's is *using* it — confirming before the first command
  that carries a credential, repeating after a failed read, and classifying the
  result as a session-state failure rather than an empty audit. Those are
  sequence decisions and belong in `sim-tests.md`.
- **What a completed audit leaves in the shell history.** §8 asserts that the
  host appends history per command. That an audit's command is in fact found
  there needs an audit to have run, which needs the actor.
- **The security monitoring agent.** `recon-01` is a monitored host
  (`siem.md` §1), and its agent is tested with every other monitored host's in
  `siem-tests.md`. This document asserts only what the agent changes about this
  host: the name it resolves (§6) and that it adds no listener.
- **What the datasources hold, and what the audit queries them for.** The
  contents of the log-server replica are `hist-tests.md`'s; the period, the
  query expressions and which datasource the audit reads are the actor's
  (`recon.md` §6.1). This host's concern is that the client, the terminal it
  runs in, and the trust it verifies against are present and work.
