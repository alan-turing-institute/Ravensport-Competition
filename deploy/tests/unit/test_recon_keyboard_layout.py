"""recon-01 keyboard layout and credential character set. Run with: pytest tests/unit -q

Covers `recon.md` §5.2: the actor injects PS/2 Set 1 scancodes rather than
characters, so which character arrives is decided by the X keyboard layout the
session pins. Two properties are asserted here, and neither needs a deployment.

**The layout pinned in the session is the one the specification names.** The
value lives in one place — the `.xsession` heredoc in
`build-scripts/recon-01.sh.j2` — and nothing else in the repository records it,
so a change there is currently silent. Asserting it against the value named in
this module makes the choice deliberate: the layout cannot be altered without
also altering the test that states what it should be.

**The credentials contain only characters that survive a layout change.** The
United States and United Kingdom layouts agree on letters, digits and most
punctuation, and disagree on a small set (`@ " # £ ~ ¬ \\ |`). A credential drawn
from the agreed set authenticates whichever of the two is in force, so a layout
regression cannot present as a rejected login. This is a deliberate restriction
rather than an accident of the current values, and it is asserted so that it
stays true when a credential is next changed.

**Why this is not the whole of §5.2.** What a scancode produces can only be
measured in a running session, which needs the client that does not yet exist;
that assertion is `keyboard_layout_is_the_pinned_one` in `recon-tests.md` §3 and
waits. These tests catch a build that pinned the wrong layout, or a credential
that would not survive the wrong layout being pinned. They cannot catch a
running session that ignores what the build produced — see `recon-tests.md`
§0.3 on image-level tests and the behavioural assertions they do not replace.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]  # deploy/
sys.path.insert(0, str(ROOT / "scripts"))
from world_schema import World  # noqa: E402

NODE_BUILD = ROOT / "build" / "node-build" / "recon-01.sh"

# GB keyboard. The actor's scancode table is derived against this value and must
# change with it: Note`|` is Shift+0x56 under `gb` and isn't a a key `us` has
PINNED_LAYOUT = "gb"

SETXKBMAP_RE = re.compile(r"^\s*setxkbmap\s+(\S+)\s*$", re.MULTILINE)

# Characters occupying the same key, in the same shift state, on both the United
# States and United Kingdom layouts. Letters, digits and space, plus the
# punctuation the two layouts agree on.
#
# Excluded, and why:
#   @   US Shift+2;         GB Shift+'
#   "   US Shift+';         GB Shift+2
#   #   US Shift+3;         GB its own key, right of '
#   £   absent from US;     GB Shift+3
#   ~   US Shift+backtick;  GB Shift+#
#   ¬   absent from US;     GB Shift+backtick
#   \ | present on both, but on different physical keys (US above Enter,
#       GB left of Z), so the scancode that produces them differs
LAYOUT_INVARIANT = frozenset(
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789"
    "!$%^&*()"  # Shift+1 and Shift+4 through Shift+0
    "-_=+"  # the two keys right of 0
    "[{]}"  # the two keys right of P
    ";:'"  # the two keys right of L; the apostrophe unshifted only
    ",<.>/?"  # the three keys right of M
    "`"  # unshifted only; its shifted meaning differs
    " "
)

# The characters the two layouts place differently. Named separately so that
# widening LAYOUT_INVARIANT cannot silently admit one of them.
LAYOUT_DIVERGENT = frozenset('@"#£~¬\\|')


def load_world() -> World:
    return World.model_validate(yaml.safe_load((ROOT / "world.yaml").read_text()))


def render_profile(profile: str = "full") -> None:
    out = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "render.py"), "--profile", profile],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert out.returncode == 0, f"render --profile {profile} failed:\n{out.stderr}"


@pytest.fixture(scope="module", autouse=True)
def rendered():
    render_profile()


def test_session_pins_a_keyboard_layout():
    """`recon.md` §5.2 requires the layout to be pinned explicitly rather than
    left to a default. xrdp performs its own client-advertised keyboard
    negotiation and loads a `km-<layout>.ini` on every connect, so a session
    that pins nothing takes whatever the client asked for."""
    script = NODE_BUILD.read_text()
    assert SETXKBMAP_RE.search(script), (
        f"{NODE_BUILD.relative_to(ROOT)} contains no `setxkbmap` line. The "
        "session must pin the layout explicitly (recon.md §5.2): without it the "
        "layout is whatever the connecting client advertised, and the same "
        "scancode produces different characters from one session to the next."
    )


def test_pinned_layout_is_the_specified_one():
    """The value is recorded in the build script and nowhere else. This test is
    its second reader, so that changing it requires saying so here."""
    script = NODE_BUILD.read_text()
    found = {m.group(1) for m in SETXKBMAP_RE.finditer(script)}
    assert found == {PINNED_LAYOUT}, (
        f"session pins {sorted(found)}, expected {[PINNED_LAYOUT]!r} "
        "(recon.md §5.2). If the layout was changed deliberately, update "
        "PINNED_LAYOUT in this module and the scancode table the actor injects "
        "— the scancodes are derived against a layout, not against characters."
    )


@pytest.mark.parametrize(
    "field",
    ["workstation_password", "password"],
)
def test_credentials_use_only_layout_invariant_characters(field):
    """A credential containing a character the two layouts place differently
    would authenticate under one and fail under the other, and the failure
    presents as a rejected login with nothing pointing at the layout
    (`recon.md` §5.2)."""
    recon = load_world().identities.reconciliation_user
    value = getattr(recon, field)
    offending = sorted(set(value) - LAYOUT_INVARIANT)
    assert not offending, (
        f"identities.reconciliation_user.{field} contains {offending}, which the "
        "United States and United Kingdom layouts place differently. The actor "
        "injects scancodes, so such a character types as something else if the "
        "pinned layout changes, and the fault presents as a rejected credential. "
        "Choose a value from letters, digits and the punctuation both layouts "
        "agree on."
    )


def test_username_uses_only_layout_invariant_characters():
    """The username is typed at the same prompt as the password and carries the
    same exposure."""
    username = load_world().identities.reconciliation_user.username
    offending = sorted(set(username) - LAYOUT_INVARIANT)
    assert not offending, (
        f"identities.reconciliation_user.username contains {offending}; see "
        "test_credentials_use_only_layout_invariant_characters."
    )


def test_the_invariant_set_excludes_every_known_divergent_character():
    """Guards the guard. The credential tests are only as good as the set they
    check against, and widening it by hand is the way that protection would be
    lost without any test failing."""
    admitted = sorted(LAYOUT_INVARIANT & LAYOUT_DIVERGENT)
    assert not admitted, (
        f"LAYOUT_INVARIANT admits {admitted}, which the United States and "
        "United Kingdom layouts place differently. Removing a character from "
        "LAYOUT_DIVERGENT, or adding one to LAYOUT_INVARIANT, defeats the "
        "credential assertions in this module."
    )
