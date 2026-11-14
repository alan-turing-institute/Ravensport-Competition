# Reconciliation workstation probe

A scripted Remote Desktop Protocol client that opens a session on `recon-01`,
drives it with keystrokes and clicks, and reads the clipboard back. It exists to
answer one question: **does text copied inside a session on the workstation
reach the client, and intact?**

**It does.** A 64 KiB export placed on the clipboard by `xclip` inside the
session reached the client byte-exact — matching SHA-256, unconverted line
endings, no truncation and no fragmentation loss. §"What was measured" records
the runs.

It is not the reconciliation user actor, and not yet the workstation client
`recon-tests.md` §0.4 describes. It decides nothing — every coordinate, key and
timing is in the script given on the command line, so a sequence can be changed
without recompiling.

## Where it runs, and why not the test runner

The runner cannot reach `recon-01:3389`. The zone bridges are `nat: false`, so
the host has no address on any zone subnet; the firewall nodes forward with
`policy drop` and an explicit allow list, and no `firewall_rules` entry in
`topology.yaml` admits 3389 into `vnet-it`. The route `make configure-corp-it`
adds carries packets as far as `fw-dmz-it`, which drops them.

The probe therefore runs **inside `sim-host`**, against `127.0.0.1:20003` — the
HAProxy egress port already rendered for the reconciliation user, which dials
`recon-01:3389` sourced from `10.30.0.11` (`sim-network.md` §5,
`render.py:612`). That is the path the actor will take, so what the probe
measures is what the actor will meet.

`sim-host` has a read-only root, but `/tmp` is a tmpfs mounted `rw,nosuid,nodev`
— not `noexec` — so a pushed binary runs. Its egress guard drops only *inbound*
connections on `it-recon-user`.

**The tmpfs is why the binary disappears.** `/tmp` does not survive a container
restart, and an `incus exec` naming a binary that is no longer there reports
`Error: Command not found`. Re-push rather than rebuild — the build artefact on
the host's own disk is unaffected.

## Building

The unit tests cover the scancode table, the script parser and the clipboard
decoding, and need no deployment and no session:

```sh
cargo test --manifest-path tests/integration/recon-probe/Cargo.toml
```

`sim-host` is Alpine and runs a static-musl `sim` binary, so the probe is built
for the same target:

```sh
rustup target add "$(uname -m)-unknown-linux-musl"
cargo build --release \
    --manifest-path tests/integration/recon-probe/Cargo.toml \
    --target "$(uname -m)-unknown-linux-musl"
```

`ring` compiles C, so this needs a C compiler targeting musl:
`sudo apt-get install -y musl-tools musl-dev` if the build stops there.

## The fixture

**The image carries nothing for this measurement.** `recon-01` is the
workstation `recon.md` specifies and no more: it holds no document to copy and
no editor beyond what the desktop brings, because measurement scaffolding in a
published image is state the specification does not describe and an intruder
would find.

The fixture is therefore made at run time, in `/tmp`, where it does not survive
a restart:

```sh
incus exec recon-01 -- su - recon-user -c \
  'printf "%s\n" "The quick brown fox jumps over the lazy dog." > /tmp/probe-fixture.txt
   sha256sum /tmp/probe-fixture.txt'
```

Keep that digest — it is what a read is compared against. For the size runs,
graded fixtures:

```sh
incus exec recon-01 -- su - recon-user -c \
  'for n in 64 512 4096; do
     yes "$(head -c 96 /dev/urandom | base64 | tr -d "\n")" \
       | head -c $((n*1024)) > /tmp/probe-fixture-${n}k.txt
     sha256sum /tmp/probe-fixture-${n}k.txt
   done'
```

## Running

```sh
incus file push --mode 0755 \
    "tests/integration/recon-probe/target/$(uname -m)-unknown-linux-musl/release/recon-probe" \
    sim-host/tmp/recon-probe
```

**First rung — does a session start, and where is the desktop?** Photograph it
before assuming any coordinate:

```sh
incus exec sim-host --env RECON_PASS="$RECON_PASS" -- \
    /tmp/recon-probe "ready 20000" "wait 5000" "shot /tmp/desktop.png"
incus file pull sim-host/tmp/desktop.png .
```

That alone settles several things: that the negotiated security layer works,
that the account authenticates, that a desktop session starts, and that the
clipboard channel completes its initialisation handshake.

**Second rung — the copy.** The terminal icon in the bottom dock is one stable
target and is preferred over navigating the applications menu:

```sh
incus exec sim-host --env RECON_PASS="$RECON_PASS" -- /tmp/recon-probe \
    "ready 20000" "wait 3000" \
    "click 567,999" "wait 3000" \
    "clear" \
    "type xclip -selection clipboard /tmp/probe-fixture.txt" \
    "key enter" "wait 1000" "read 8000"
```

The probe prints the advertised formats, the byte and character counts, the
line-ending mix, the SHA-256 as received and after converting CRLF to LF, and
then the text itself. Compare with the file it came from:

```sh
incus exec recon-01 -- sha256sum /tmp/probe-fixture.txt
```

**Three traps, each of which cost a run.**

- **`clear` goes before the action that copies, never after.** `xclip` takes the
  selection the instant it runs, so the remote announces the copy within
  milliseconds; a `clear` placed after the trigger wipes the latch holding that
  announcement and the subsequent `read` waits for a second one that never
  comes. It reports `Announced formats: none`, which is indistinguishable from
  the copy never happening.
- **A modal dialogue swallows everything and persists.** A misplaced click can
  activate an application with no configured handler, and Xfce answers with a
  chooser that takes focus and accepts every subsequent keystroke. Because
  `recon.md` §4 does not tear a disconnected session down, it is still there at
  the next connection. Every probe step reports success and the read returns
  nothing. Dismiss it with a click on Cancel before anything else, and read the
  failure screenshot the probe saves rather than guessing.
- **Coordinates are read off a screenshot, never assumed.** They are coupled to
  the geometry requested at connect, to the panel layout, and to the window
  position. `567,999` is the dock terminal at 1280×1024; it is not a constant.

## Actions

| Action | Meaning |
|---|---|
| `ready [ms]` | Wait for the clipboard channel to initialise (default 20000) |
| `wait <ms>` | Pump frames, drawing but doing nothing |
| `click <x>,<y>` | Left-click at a desktop coordinate |
| `key <chord>` | One chord: `enter`, `ctrl+a`, `ctrl+shift+c` |
| `type <text>` | Type a string, one chord per character |
| `shot <path>` | Save the framebuffer as a PNG |
| `clear` | Forget copies seen so far |
| `read [ms]` | Wait for a copy, request it as text, report what arrived |

Environment: `RECON_ADDR` (default `127.0.0.1:20003`), `RECON_USER` (default
`recon-user`), `RECON_PASS` (required), `RECON_SIZE` (default `1280x1024`),
`IRONRDP_LOG` (default `info`).

## What was measured

Two runs, both through `xclip` from a terminal in the session.

| Fixture | Wire bytes | Decoded | Lines | Line endings | Digest |
|---|---|---|---|---|---|
| `probe-fixture.txt`, 1018 B | 2038 | 1018 chars | 18 | 0 CRLF, 18 LF | matched |
| `probe-fixture-64k.txt`, 64 KiB | 131074 | 65536 chars | 509 | 0 CRLF, 508 LF | matched |

**The decode path is confirmed.** Wire bytes are `n × 2 + 2` in both cases:
`CF_UNICODETEXT` as little-endian sixteen-bit units plus the terminating null
unit, which the decoder stops at rather than trimming.

**Line endings are not converted.** The protocol conventionally uses CRLF and
the fixtures use LF; the fixtures' own endings survived intact. This is observed
xrdp behaviour rather than a protocol guarantee, and it matters only for digest
comparison — a JSON payload is indifferent to it.

**The advertised format set is text-only.** A text copy announces `13`
(`CF_UNICODETEXT`), `16` (`CF_LOCALE`), `1` (`CF_TEXT`) and `7`
(`CF_OEMTEXT`). **No file-descriptor format is offered**, which is direct
evidence for `recon.md` §11.2's requirement that the actor refuse them: on this
path the refusal costs nothing and would engage only on an actual file copy.

**64 KiB is comfortably above the audit's payload.** `narrative.md` §1 gives
2–3 tugs drawn down daily and tanker top-ups every few days, so a month's audit
compares roughly 100 records — tens of kilobytes. The size ceiling that this
probe was built to find is therefore not a constraint on the design.

## Open questions

- **Where the ceiling actually is.** Measured intact to 64 KiB; above that
  unmeasured. Characterisation rather than risk, since the payload sits an order
  below. Raise the `read` timeout as the fixture grows — a large transfer
  fragments, and the failure to watch for is a partial arrival reported as
  success, so compare the digest rather than the byte count.
- **The keyboard layout is retargeted but unmeasured in a session.** The
  scancode table and the identifier the client advertises are now both `gb`
  (`keys.rs`, `session.rs`), matching what `.xsession` pins. What remains
  unevidenced is the sequence `recon-tests.md` §3's
  `keyboard_layout_is_the_pinned_one` requires: `Shift`+`0x56`, `0x2B`,
  `Shift`+`0x04`, which should produce `|`, `#`, `£`. The client half of that
  contract is asserted in `keys.rs`'s unit tests; only a session can establish
  the other half. The pipe is the one that matters, since `recon.md` §6.1's
  query expressions need it and the United States layout has no key in that
  position at all. This is the cheapest outstanding measurement and this probe
  is the instrument for it.

  Typed commands previously proved `-`, `/` and `.` resolve, but those are
  characters the two layouts agree on, so they evidenced nothing about the pin.

- **None of this is automated.** The probe is a command-line instrument, so
  nothing here catches a regression. Every session-level assertion in
  `recon-tests.md` §2–§4 remains "waits on the client".

## If nothing arrives

Separate the clipboard channel from the desktop by copying on the host instead
of through the session. With the probe connected and waiting on `read`, in
another shell:

```sh
incus exec recon-01 -- su - recon-user -c \
    'DISPLAY=:10 xclip -selection clipboard /tmp/probe-fixture.txt'
```

If that arrives and the in-session sequence does not, the channel is sound and
the fault is in the session — focus, a modal dialogue, the coordinates, or the
layout. If neither arrives, the fault is the channel or `chansrv`. Confirm the
session's display number with `incus exec recon-01 -- ls /tmp/.X11-unix`.

An X selection dies with its owner and no clipboard manager is installed, so the
read must happen while the process holding the selection is still alive. `xclip`
serves a fixed number of selection requests before exiting — `man xclip` on
`recon-01` for the `-loops` default — and an exit before the client pastes would
present identically to a channel fault.
