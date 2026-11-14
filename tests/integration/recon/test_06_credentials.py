"""
Credentials on the host (`recon-tests.md` §8), targeting `recon.md` §7.

The unit suite asserts what `render.py` produces; this asserts what the image
carries. A correct render is not evidence the artefact reached the image, and
the input-hash list is maintained by hand.

**The workstation account is a directory account**, so most of this module
asserts absence: no local account, no usable local password, no plaintext.

The assertions needing `corp-it` to answer skip rather than fail when it is
unreachable — a workstation correctly configured against a dead directory is a
different fault from a misconfigured one.

Run on the VM: python -m pytest tests/integration/recon/test_06_credentials.py
"""

from __future__ import annotations

import sys

import pytest

from tests.integration.recon.conftest import REPO_ROOT, Node

# The directory group's name is a render-time constant, not a `world.yaml`
# field, so it is read from the renderer rather than copied here.
sys.path.insert(0, str(REPO_ROOT / "deploy" / "scripts"))
import render  # noqa: E402

# /proc and /sys are excluded: kernel interfaces rather than image contents.
SEARCH_ROOTS = ("/etc", "/home", "/opt", "/root", "/usr/local", "/var")

SSSD_CONF = "/etc/sssd/sssd.conf"


# ── The workstation holds no account of its own ───────────────────────────────


def test_no_local_account_for_the_reconciliation_user(recon: Node, world: dict) -> None:
    """The account lives in the directory, so no local one may sit beside it.

    The failure is invisible in a running deployment: a local account left
    behind would authenticate, the session would start, and nothing would say
    which credential was accepted.
    """
    username = world["identities"]["reconciliation_user"]["username"]
    local = recon.run("grep", "-E", f"^{username}:", "/etc/passwd")
    assert local.returncode != 0, (
        f"{username!r} has a local /etc/passwd entry: {local.stdout.strip()!r}. "
        "The workstation authenticates against corp-it and must hold no account "
        "of its own (`recon.md` §7.1)."
    )
    shadow = recon.run("grep", "-E", f"^{username}:", "/etc/shadow")
    assert shadow.returncode != 0, (
        f"{username!r} has a local /etc/shadow entry — a verifier on the one "
        "host specified to carry none."
    )


def test_no_account_can_log_in_locally(recon: Node) -> None:
    """No local account carries a usable password at all.

    Stronger than its predecessor, which had to exempt the workstation account.
    With the login in the directory there is no exemption to make, so a usable
    local password is either a package's post-install script or the local
    account this design removed.
    """
    offenders = []
    for line in recon.stdout("getent", "shadow").splitlines():
        name, _, entry = line.partition(":")
        digest = entry.split(":")[0]
        # '*' and '!' (and '!!') are the locked/no-password forms.
        if digest not in ("", "*") and not digest.startswith("!"):
            offenders.append(name)
    assert not offenders, f"local accounts carry a usable password: {offenders}"


# ── The directory client ──────────────────────────────────────────────────────


def test_directory_client_is_running(recon: Node) -> None:
    """A host carrying the configuration with no running daemon authenticates
    nobody, and the failure appears at the greeter rather than at boot."""
    state = recon.run("systemctl", "is-active", "sssd").stdout.strip()
    assert state == "active", f"sssd is {state or 'not running'} on recon-01"


def test_directory_client_holds_no_credential(recon: Node, world: dict) -> None:
    """sssd searches anonymously and binds as the authenticating user, so its
    configuration carries an address and a search base and no secret."""
    conf = recon.read(SSSD_CONF)
    recon_identity = world["identities"]["reconciliation_user"]
    for field in ("workstation_password", "password"):
        assert recon_identity[field] not in conf
    assert "ldap_default_authtok" not in conf, (
        "the directory client carries a bind credential; the stock slapd access "
        "rules permit anonymous search, so it needs none"
    )


def test_directory_client_binds_over_tls(recon: Node) -> None:
    """The image carries the LDAPS bind, addressed by the hostname corp-it's
    certificate asserts (`recon.md` §7.1.1).

    A client that falls back to an unencrypted or unverified bind passes
    every other assertion here while presenting the workstation password
    to whichever host answers on `corp-it`'s address.
    """
    conf = recon.read(SSSD_CONF)
    assert "ldap_uri = ldaps://corp-it/" in conf, (
        "the directory client does not bind over TLS by hostname; corp-it's "
        "certificate carries a hostname-only SAN, so an IP-literal or "
        "plaintext URI fails verification or finds nothing listening"
    )
    assert "ldap_auth_disable_tls_never_use_in_production" not in conf, (
        "the directory client permits an unencrypted authentication bind"
    )
    assert "ldap_tls_reqcert = never" not in conf, (
        "the directory client does not verify corp-it's certificate"
    )


def test_directory_client_configuration_is_not_readable(recon: Node) -> None:
    """sssd refuses to start on a configuration that is group- or world-readable
    and says so only in the journal, so a permissive mode presents as a
    workstation that authenticates nobody."""
    mode = recon.stdout("stat", "-c", "%a %U:%G", SSSD_CONF).strip()
    assert mode == "600 root:root", f"{SSSD_CONF} is {mode}, expected 600 root:root"


def test_name_service_consults_the_directory(recon: Node) -> None:
    """`passwd`, `group` and `shadow` all name `sss` in nsswitch.

    Authentication and resolution are separate stacks. A host that
    authenticates a user it cannot resolve reaches a session with no home
    directory and no primary group, which presents as a broken desktop rather
    than as a name-service fault.
    """
    nsswitch = recon.read("/etc/nsswitch.conf")
    for database in ("passwd", "group", "shadow"):
        line = next(
            (ln for ln in nsswitch.splitlines() if ln.startswith(f"{database}:")), ""
        )
        assert "sss" in line, f"nsswitch {database!r} does not consult sss: {line!r}"


def test_home_directories_are_created_at_login(recon: Node) -> None:
    """A directory account has no home directory until one is made for it.

    Without `pam_mkhomedir` the session starts in a directory that does not
    exist, so `~/.xsession` is absent and no desktop is reached — which looks
    like a broken remote desktop service rather than a missing module.
    """
    session = recon.read("/etc/pam.d/common-session")
    assert "pam_mkhomedir" in session, (
        "pam_mkhomedir is not in the session stack, so the home directory is "
        "never created (`recon.md` §5, §7.1)"
    )


def test_skel_carries_the_session_files(recon: Node) -> None:
    """`pam_mkhomedir` populates a new home from `/etc/skel`, so the session
    files live there rather than in a home directory that does not exist at
    build time.

    Both are load-bearing: `.xsession` selects the desktop and pins the layout
    (`recon.md` §5, §5.2); `helpers.rc` makes `Ctrl+Alt+T` start a terminal
    rather than the preferred-application chooser (§5.4).
    """
    assert recon.path_exists("/etc/skel/.xsession"), (
        "/etc/skel/.xsession is absent, so a new home gets no session file and "
        "the login reaches no desktop"
    )
    helpers = "/etc/skel/.config/xfce4/helpers.rc"
    assert recon.path_exists(helpers), (
        f"{helpers} is absent, so `exo-open --launch TerminalEmulator` answers "
        "with the chooser, which takes focus and absorbs the typed audit "
        "command (`recon.md` §5.4)."
    )
    assert "TerminalEmulator=xfce4-terminal" in recon.read(helpers)


# ── Assertions that need the directory to answer ──────────────────────────────


def _skip_without_directory(recon: Node, corp_it_host: str) -> None:
    """Skip when `corp-it` is absent, not when the bind is broken.

    The port is the one the client actually dials (`recon.md` §7.1.1). Probing
    389 while the client binds to 636 made every failure below a skip: nothing
    has answered on 389 since the directory became TLS-only (`corp-it.md` §3),
    so the guard reported a deployment that was not up whatever was wrong.
    """
    if not recon.can_reach(corp_it_host, 636):
        pytest.skip(f"corp-it is not reachable at {corp_it_host}:636")


def test_the_reconciliation_user_resolves_through_the_directory(
    recon: Node, world: dict, corp_it_host: str
) -> None:
    """The end-to-end proof that the directory entry, the client and the name
    service agree, which the preceding assertions establish only separately."""
    _skip_without_directory(recon, corp_it_host)
    username = world["identities"]["reconciliation_user"]["username"]
    entry = recon.run("getent", "passwd", username).stdout.strip()
    assert entry, (
        f"{username!r} does not resolve. The directory holds the account and "
        "nsswitch names sss, so this is the client's search base or the "
        "directory's access rules."
    )
    fields = entry.split(":")
    assert fields[6] == "/bin/bash", (
        f"{username!r} resolves with shell {fields[6]!r}. The shell comes from "
        "the directory's loginShell; another value means sssd supplied its own."
    )


def test_the_primary_group_resolves(
    recon: Node, world: dict, corp_it_host: str
) -> None:
    """The primary group is the directory's group, **by name**.

    Asserting that a name comes back is not enough. A directory group numbered
    1000 resolved as `lxd` — `files` precedes `sss`, so the local entry won —
    and the session ran with the container runtime's group. The login succeeded
    and `id` returned a name throughout.

    Neither the render nor the build can show that; only `id` on a deployed
    host can.
    """
    _skip_without_directory(recon, corp_it_host)
    username = world["identities"]["reconciliation_user"]["username"]
    groups = recon.run("id", "-gn", username)
    assert groups.returncode == 0 and groups.stdout.strip(), (
        f"the primary group of {username!r} resolves to no name: "
        f"{groups.stderr.strip()}"
    )
    assert groups.stdout.strip() == render.DIRECTORY_GROUP_CN, (
        f"{username!r} has primary group {groups.stdout.strip()!r}, expected "
        f"{render.DIRECTORY_GROUP_CN!r}. A local entry with the same name or "
        "gidNumber is shadowing the directory one."
    )


# ── Nothing in the image carries either credential ────────────────────────────


def test_workstation_password_is_absent_from_the_image(
    recon: Node, world: dict
) -> None:
    """`world.yaml` is a build-host artefact; its plaintext must reach no
    deployed instance (`deployment-config.md` §2)."""
    password = world["identities"]["reconciliation_user"]["workstation_password"]
    hits = recon.run("grep", "-rl", password, *SEARCH_ROOTS).stdout.strip()
    assert not hits, f"workstation password found in plaintext at:\n{hits}"


def test_history_is_appended_as_each_command_is_entered(recon: Node) -> None:
    """`/etc/bash.bashrc` sets the history file to be appended per
    command rather than at shell exit (`recon.md` §7.2).

    **Read from the shell the session spawns — interactive and non-login —
    rather than from the file that sets it.** The audit's terminal spawns a
    non-login shell, which never reads a profile drop-in, so a test run in a
    login shell would pass against the placement the specification rejects as
    well as the one it requires.

    Run as root rather than as the reconciliation user: reading it as that
    account opens a session, which writes a `pam_unix` record to the host. The
    variable is set system-wide, so any interactive non-login shell shows it.

    This asserts determinism, not security. The residue is intended; what the
    setting prevents is its arriving on some months and not others.
    """
    value = recon.run("bash", "-ic", 'printf %s "$PROMPT_COMMAND"').stdout.strip()
    assert value == "history -a", (
        f"PROMPT_COMMAND is {value!r} in an interactive non-login shell, not "
        "'history -a'. Either /etc/bash.bashrc does not set it, or something "
        "read afterwards reassigns it — in which case the audit's history "
        "residue arrives on some months and not others (`recon.md` §7.2)."
    )


def test_log_server_credential_is_absent_from_the_image(
    recon: Node, world: dict
) -> None:
    """The Grafana credential reaches this host only as keystrokes typed into
    the audit command (`recon.md` §7.2).

    **The image, not the host.** An audit leaves the credential in the shell
    history by design, so asserting absence from a host that has run one would
    claim a property nothing holds.
    """
    password = world["identities"]["reconciliation_user"]["password"]
    hits = recon.run("grep", "-rl", password, *SEARCH_ROOTS).stdout.strip()
    assert not hits, f"log-server credential found in plaintext at:\n{hits}"
