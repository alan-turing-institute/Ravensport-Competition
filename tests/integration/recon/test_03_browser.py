"""
Browser (`recon-tests.md` §4), targeting `recon.md` §6.2 and §6.3.

Audit is conducted with curl from terminal - this is a check that an
installed application doesn't obviously fail. Browser would be an interactive
alternative to curl.

§4's three policy assertions wait on the client. Reading back what a policy did
means rendering `about:policies` and parsing it, or driving the browser over its
remote protocol; a headless screenshot yields an image, not text.

  browser_does_not_update_itself   §4's only requirement
  password_manager_offers_nothing  retained, demoted
  password_reveal_is_unavailable   retained, demoted

`json_document_renders_as_plain_text` and `reload_is_not_served_from_cache` were
retired rather than deferred: both served a browser-based audit.

Run on the VM: python -m pytest tests/integration/recon/test_03_browser.py
"""

from __future__ import annotations

from tests.integration.recon.conftest import Node

FIREFOX = "/opt/firefox/firefox"
PROBE_HOME = "/tmp/ffprobe"


def test_browser_starts(recon: Node) -> None:
    """Covers `recon.md` §2.1's runtime dependencies through their observable
    consequence: a missing one exits immediately rather than failing the build,
    so the package list cannot be checked by reading it.

    Asserted from the image, not repeated from the build: a stale image behind a
    corrected build is reachable while the input-hash list is maintained by hand
    (§10.2). Asserted on the screenshot rather than the exit status, since
    Firefox can exit zero having rendered nothing.

    `--version` alone would catch a missing directly linked library; the render
    is what catches one opened lazily.

    Two limits. `about:blank` renders no text and headless touches no audio, so
    this covers the subset of §2.1 on the path to a render. And headless
    bypasses the X server, so it says nothing about the session's browser —
    that is `browser_can_be_launched_from_within_the_session` (§3), pending the
    client.
    """
    # Firefox's own diagnostics are captured and shown only on failure: a
    # sandbox EACCES and libEGL warnings appear on every start (`recon.md` §2).
    # `rm -rf` first as well as last, so a screenshot left by a run that died
    # before cleanup cannot satisfy the check.
    script = "\n".join(
        [
            "set -e",
            f"export HOME={PROBE_HOME}",
            'rm -rf "$HOME"',
            'mkdir -p "$HOME"',
            f"timeout 60 {FIREFOX} --headless --screenshot "
            '"$HOME/shot.png" about:blank >"$HOME/log" 2>&1 || true',
            'if ! test -s "$HOME/shot.png"; then cat "$HOME/log" >&2; exit 1; fi',
            f"{FIREFOX} --version",
        ]
    )
    result = recon.run("bash", "-c", script)
    recon.run("rm", "-rf", PROBE_HOME)
    assert result.returncode == 0, (
        "Firefox did not start (`recon.md` §2.1). The `t64` dependency names are "
        "correct for 24.04 only. Ignore the sandbox and libEGL lines below and "
        f"look for a missing shared object.\n{result.stderr.strip()}"
    )
    assert "Firefox" in result.stdout, (
        f"started but reported no version: {result.stdout.strip()!r}"
    )


def test_the_image_creates_no_browser_profile(recon: Node, world: dict) -> None:
    """The build starts the browser under a throwaway `HOME` and removes it, so
    the image carries no profile.

    This matters for §6.4 rather than for itself: whether the trust anchor
    becomes a property of the image or of the first session depends on a profile
    existing at build time to import into. It also catches a profile made by
    hand at the workstation, where a certificate exception clicked through would
    satisfy §5's trust assertions whether or not the anchor works.
    """
    username = world["identities"]["reconciliation_user"]["username"]
    profile = f"/home/{username}/.mozilla"
    assert not recon.path_exists(profile), (
        f"{profile} exists, so a session created it. A certificate exception "
        "accepted in one would silently satisfy `recon.md` §6.4's assertions."
    )
