"""
Desktop session — the image-level half (`recon-tests.md` §3), targeting
`recon.md` §5 and §2.

**These are not §3's behavioural assertions, and must not be mistaken for
them.** §3 asserts what a session produces — that a desktop starts, that the
pinned layout is what the scancodes type, that nothing stands in front of the
window — and each of those needs a client speaking Remote Desktop Protocol,
which does not exist yet. What is asserted here is what the image carries: the
furniture a session finds, and the entry that makes the browser reachable from
within it.

**Why the browser's entry is asserted here rather than in §4.** Reaching the
browser is a property of the session, not of the browser: the binary runs
correctly whether or not anything in the desktop points at it. Firefox is
installed from a tarball (`recon.md` §6.2), and a tarball contributes nothing to
a desktop — no menu entry, no launcher, no icon. What the distribution package
would have supplied, the build must supply instead.

That failure is invisible from inside the build, which is why it is asserted
from the image rather than left to the browser tests. A manual session found the
browser installed, on PATH, runnable, and reachable by no route the session
offered; every check that ran during the build passed, because each of them
named the binary on a command line.

Run on the VM: python -m pytest tests/integration/recon/test_02_session.py
"""

from __future__ import annotations

import re
from html import unescape

import pytest

from tests.integration.recon.conftest import Node

TERMINAL = "/usr/bin/xfce4-terminal"
FIREFOX_DESKTOP = "/usr/share/applications/firefox.desktop"

# The two the audit is conducted with (`recon.md` §2). Asserted by absolute path
# rather than through `command -v`, so that a shell function or an entry earlier
# on `PATH` cannot satisfy the assertion in place of the installed program.
CURL = "/usr/bin/curl"
XCLIP = "/usr/bin/xclip"

# The name the entry presents in the applications menu and in the application
# finder. `recon.md` §5.3 states it as a contract with the actor rather than a
# label, so it is asserted rather than left to whatever the build happened to
# write.
FIREFOX_MENU_NAME = "Firefox"

# Xfce's packaged keyboard defaults. The per-user channel under
# ~/.config/xfce4/xfconf/ is written by a session, so it is absent on a host
# nobody has logged into.
XFCE_DEFAULTS = "/etc/xdg/xfce4/xfconf/xfce-perchannel-xml"
LAUNCH_TERMINAL = "exo-open --launch TerminalEmulator"


def _desktop_entry(node: Node, path: str) -> dict[str, str]:
    """Parse the `[Desktop Entry]` group of a desktop entry file.

    Keys outside that group are ignored: an action group such as
    `[Desktop Action new-window]` carries its own `Name` and `Exec`, and reading
    those as though they were the entry's own would assert the wrong values.
    """
    entry: dict[str, str] = {}
    in_group = False
    for raw in node.read(path).splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            in_group = line == "[Desktop Entry]"
            continue
        if not in_group or not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        entry.setdefault(key.strip(), value.strip())
    return entry


@pytest.fixture(scope="module")
def firefox_entry(recon: Node) -> dict[str, str]:
    if not recon.path_exists(FIREFOX_DESKTOP):
        pytest.fail(
            f"{FIREFOX_DESKTOP} does not exist, so the browser appears in no "
            "applications menu and in no application finder. Firefox is "
            "installed from a tarball, which supplies no desktop entry; the "
            "build must write one (`recon.md` §5.3)."
        )
    return _desktop_entry(recon, FIREFOX_DESKTOP)


def test_a_terminal_emulator_is_installed(recon: Node) -> None:
    """`recon.md` §2. A stock Xfce desktop carries a terminal: `xfce4-terminal`
    is a recommendation of the `xfce4` metapackage, and the fleet's
    `--no-install-recommends` practice drops it unless it is named explicitly.

    The workstation models an office machine and an office machine has one. The
    actor never opens it; it is present because removing a default is the
    decision that would need justifying, not keeping it.
    """
    assert recon.run("test", "-x", TERMINAL).returncode == 0, (
        f"{TERMINAL} is not installed. `--no-install-recommends` drops "
        "`xfce4-terminal`, so it must be named explicitly in the package list "
        "of `recon.md` §2."
    )


def test_the_audit_client_is_installed(recon: Node) -> None:
    """`recon.md` §2. The audit is conducted with `curl` against the log
    server's query interfaces, so it is the program the workstation's purpose
    rests on.

    It is asserted because until it was named in the package list it was present
    only by inheritance from the base image: nothing in `recon-01.sh.j2`
    installed it and no test read it. A base image that stopped carrying it
    would have removed the audit's only client without failing a build.
    """
    assert recon.run("test", "-x", CURL).returncode == 0, (
        f"{CURL} is not installed, so the workstation has no client for the "
        "log server's query interfaces. It must be named explicitly in the "
        "package list of `recon.md` §2 — the base image is not a contract."
    )


def test_a_clipboard_tool_is_installed(recon: Node) -> None:
    """`recon.md` §2. The clipboard is the actor's only route for text out of
    the session (`recon.md` §11, and `tests/integration/recon-probe/src/clipboard.rs`):
    the actor receives a framebuffer it cannot read as text, so a response that
    reaches only the terminal reaches nothing.

    The audit pipes the retrieval straight onto the selection — `curl … | xclip`
    in one command, writing no file (`recon.md` §6.1; §11.4 records the two-step
    variant via a file and why it was not adopted). So `xclip` is not a
    convenience beside the client, it is the second half of the only route a
    record travels.

    No package depends on it, so `--no-install-recommends` leaves it absent
    unless it is named, and its absence presents as an audit that retrieves
    records and returns none of them.
    """
    assert recon.run("test", "-x", XCLIP).returncode == 0, (
        f"{XCLIP} is not installed, so nothing on this host can place a file's "
        "contents on the clipboard and the actor has no way to read what "
        "`curl` retrieved. It must be named explicitly in the package list of "
        "`recon.md` §2."
    )


def test_browser_has_a_desktop_entry(firefox_entry: dict[str, str]) -> None:
    """The entry is what puts the browser in the applications menu and the
    application finder. Without it the session offers no route to the browser at
    all, since `recon.md` §5 autostarts nothing."""
    assert firefox_entry, (
        f"{FIREFOX_DESKTOP} exists but carries no [Desktop Entry] group, so "
        "nothing reads it."
    )


def test_browser_desktop_entry_names_the_installed_binary(
    recon: Node, firefox_entry: dict[str, str]
) -> None:
    """The failure this guards is an entry that appears in the menu and does
    nothing when chosen. `Exec` naming a path that does not exist is not an
    error the desktop reports: the entry is listed, it is selectable, and
    activating it produces nothing at all.

    Field codes are stripped before the command is resolved — `%u`, `%U` and
    their relatives are placeholders the launcher substitutes, not arguments.
    """
    exec_value = firefox_entry.get("Exec", "")
    assert exec_value, f"{FIREFOX_DESKTOP} has no Exec key, so it launches nothing."

    tokens = [t for t in exec_value.split() if not t.startswith("%")]
    assert tokens, f"Exec={exec_value!r} names no command once field codes are removed."
    command = tokens[0]

    if command.startswith("/"):
        resolved = command
    else:
        probe = recon.run("sh", "-c", f"command -v {command}")
        assert probe.returncode == 0, (
            f"Exec names {command!r}, which does not resolve on the session's "
            f"PATH. Full value: Exec={exec_value!r}"
        )
        resolved = probe.stdout.strip()

    assert recon.run("test", "-x", resolved).returncode == 0, (
        f"Exec resolves to {resolved!r}, which is not an executable file. The "
        "entry would be listed in the menu and do nothing when activated."
    )


def test_browser_desktop_entry_is_displayed_in_the_menu(
    firefox_entry: dict[str, str],
) -> None:
    """An entry that is present, valid, and never shown is the same outcome as
    no entry at all, reached by a different route. `NoDisplay` and `Hidden` both
    produce it, and neither is reported anywhere: the file is correct, the
    binary runs, and the menu is empty.
    """
    assert firefox_entry.get("Type") == "Application", (
        f"Type={firefox_entry.get('Type')!r}; only an Application entry is "
        "shown in the applications menu."
    )
    for key in ("NoDisplay", "Hidden"):
        value = firefox_entry.get(key, "false").lower()
        assert value != "true", (
            f"{key}=true removes the entry from every menu and finder while "
            "leaving the file in place and valid."
        )


def test_browser_desktop_entry_name_is_the_pinned_one(
    firefox_entry: dict[str, str],
) -> None:
    """A coherence check on the entry, not a contract the audit depends on.

    An earlier revision made the displayed name load-bearing, because the actor
    selected the browser by what the session showed. The audit is now conducted
    with `curl` from a terminal (`recon.md` §6.1), so nothing chooses the
    browser by name and a changed name would break nothing.

    Retained because an entry whose name has drifted from the application it
    launches is incoherent, and the browser remains part of the modelled world.
    """
    assert firefox_entry.get("Name") == FIREFOX_MENU_NAME, (
        f"menu name is {firefox_entry.get('Name')!r}, expected {FIREFOX_MENU_NAME!r}."
    )


def test_terminal_keybinding_is_present(recon: Node) -> None:
    """The Xfce shortcut for `Ctrl+Alt+T` exists and runs the helper launcher.

    Xfce supplies this by default, so the assertion guards an inherited property
    rather than one the build creates — which is why it is worth asserting.
    `recon.md` §5.4 makes it the session's route to a terminal, and a default
    that changed between distribution releases would remove the audit's first
    step with nothing in the build failing.

    Read from the packaged defaults rather than from a user profile: the
    reconciliation user's `xfce4-keyboard-shortcuts` channel does not exist
    until a session writes one, so a profile-based check would assert nothing
    on a host nobody had logged into.

    The chord is matched on its modifiers rather than on an exact string. Xfce
    records it as an XML property name and escapes the angle brackets, so the
    stored form is `&lt;Primary&gt;&lt;Alt&gt;t`; matching the literal
    `<Primary><Alt>t` would fail against a correct host.
    """
    found = recon.run("grep", "-rl", LAUNCH_TERMINAL, XFCE_DEFAULTS)
    assert found.returncode == 0 and found.stdout.strip(), (
        f"no default under {XFCE_DEFAULTS} binds a key to `{LAUNCH_TERMINAL}`, "
        "so `Ctrl+Alt+T` starts nothing and the audit has no route to a "
        "terminal (`recon.md` §5.4)."
    )
    lines = [
        line
        for path in found.stdout.split()
        for line in recon.read(path).splitlines()
        if LAUNCH_TERMINAL in line
    ]
    # The chord is the property's `name`, not the whole element — the `value`
    # holds the command. Unescaped so `&lt;Primary&gt;&lt;Alt&gt;t` and
    # `<Primary><Alt>t` compare alike, and lower-cased because Xfce writes
    # `Primary` rather than `Control`.
    chords = [
        unescape(m.group(1)).lower()
        for line in lines
        if (m := re.search(r'name="([^"]*)"', line))
    ]
    assert any(
        c.startswith(("<primary><alt>", "<alt><primary>")) and c.endswith("t")
        for c in chords
    ), (
        "the terminal launcher is bound, but not to Ctrl+Alt+T, which "
        f"`recon.md` §5.4 names specifically. Chords found: {chords}\n"
        + "\n".join(lines)
    )
