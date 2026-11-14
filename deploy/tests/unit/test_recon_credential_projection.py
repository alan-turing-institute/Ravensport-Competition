"""recon-01 workstation credential projection. Run with: pytest tests/unit -q

Covers `recon.md` §7.1: the reconciliation user's workstation login is validated
against the directory on `corp-it`, so the password is projected into
`build/ldif/corp-it.ldif` as an `{SSHA}` `userPassword` and **no verifier is
rendered for `recon-01` at all**.

The negative half is the substantive one: a local account left beside the
directory one would authenticate and be invisible in a running deployment,
because both logins work.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]  # deploy/
sys.path.insert(0, str(ROOT / "scripts"))
import render  # noqa: E402
from world_schema import World  # noqa: E402

BUILD = ROOT / "build"
LDIF = BUILD / "ldif" / "corp-it.ldif"
SSSD_CONF = BUILD / "sssd" / "recon-01.conf"


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


def _ldif_entry(uid: str) -> dict[str, str]:
    """The attributes of one LDIF entry, keyed by attribute name.

    Repeated attributes are joined rather than collapsed: `objectClass` appears
    once per class, and keeping only the first would make a membership check
    depend on emission order.
    """
    for block in LDIF.read_text().split("\n\n"):
        lines = [ln for ln in block.splitlines() if ln.strip()]
        if not lines or not lines[0].startswith(f"dn: uid={uid},"):
            continue
        entry: dict[str, list[str]] = {}
        for line in lines:
            key, _, value = line.partition(": ")
            entry.setdefault(key.strip(), []).append(value.strip())
        return {key: "\n".join(values) for key, values in entry.items()}
    pytest.fail(f"no LDIF entry for uid={uid}; the directory has no such account")


# ── What the directory carries ────────────────────────────────────────────────


def test_the_reconciliation_user_has_a_directory_account():
    """The account exists in `corp-it`'s seed, which is what makes a login
    against the directory possible at all."""
    world = load_world()
    entry = _ldif_entry(world.identities.reconciliation_user.username)
    assert "posixAccount" in entry.get("objectClass", ""), (
        "the entry is not a posixAccount, so it carries no uidNumber, gidNumber "
        "or homeDirectory and cannot back a Linux login"
    )


def test_the_directory_entry_carries_a_login_shell():
    """The one directory account that opens an interactive session.

    Without `loginShell` the value comes from sssd's `default_shell`, making the
    shell a property of the workstation rather than of the account. The
    operators have none: the FMS binds to authenticate them and they log in to
    no host.
    """
    world = load_world()
    recon = world.identities.reconciliation_user.username
    assert _ldif_entry(recon).get("loginShell") == "/bin/bash"
    for op in world.identities.operators:
        assert "loginShell" not in _ldif_entry(op.username), (
            f"{op.username!r} has a login shell in the directory. Operators "
            "authenticate to the FMS and log in to no host; a shell grants a "
            "capability the narrative does not describe."
        )


def test_the_primary_group_resolves_to_an_object():
    """Every account claims `gidNumber`, and a POSIX login resolves it. While no
    directory account logged in to a host the number could name nothing without
    consequence; it cannot now."""
    text = LDIF.read_text()
    assert f"gidNumber: {render.DIRECTORY_GID_NUMBER}" in text
    assert "objectClass: posixGroup" in text, (
        "no group object backs the gidNumber every account claims, so the "
        "session's primary group resolves to a bare number"
    )


# Group names and identifiers the distribution ships. `files` precedes `sss` in
# the name-service order, so a directory entry colliding with one of these is
# shadowed by the local entry and never reached — the directory account then
# runs with a local group it was never meant to be in.
#
# Both halves were observed on a deployed host, not derived: a directory group
# named `staff` was shadowed by the distribution's own `staff` at GID 50, and
# one numbered 1000 resolved as `lxd`, the container runtime's group.
DISTRIBUTION_GROUP_NAMES = frozenset(
    """root daemon bin sys adm tty disk lp mail news uucp man proxy kmem dialout
    fax voice cdrom floppy tape sudo audio dip www-data backup operator list irc
    src shadow utmp video sasl plugdev staff games users nogroup netdev lxd
    ubuntu systemd-journal ssh crontab messagebus""".split()
)

# Locally allocated ranges: 0-99 static system accounts, 100-999 ones created by
# package post-install scripts, 1000+ local users.
FIRST_LOCAL_USER_ID = 1000


def test_directory_identifiers_cannot_collide_with_local_ones():
    """A collision produces a working login with the wrong primary
    group, visible only as `id` on a deployed host. Asserting the range and the
    name here is what stops it being reintroduced.
    """
    assert render.DIRECTORY_GROUP_CN not in DISTRIBUTION_GROUP_NAMES, (
        f"{render.DIRECTORY_GROUP_CN!r} is a group the distribution ships, so "
        "the local entry shadows the directory one and the directory group is "
        "unreachable by name"
    )
    ids = {
        "group": render.DIRECTORY_GID_NUMBER,
        "reconciliation user": render.RECON_UID_NUMBER,
        "first operator": render.OPERATOR_UID_BASE,
        "FMS administrator": render.directory_admin_uid(load_world()),
    }
    for label, value in ids.items():
        assert value >= render.DIRECTORY_ID_BASE > FIRST_LOCAL_USER_ID, (
            f"the {label} identifier {value} is inside the range a host "
            "allocates locally, so a local account can be given the same number"
        )


def test_every_allocated_login_identifier_is_distinct():
    """Directory and local users must not resolve to another account's ID."""
    world = load_world()
    assigned = [
        render.RECON_UID_NUMBER,
        *[render.OPERATOR_UID_BASE + i for i in range(len(world.identities.operators))],
        render.systems_administrator_uid(world),
        render.directory_admin_uid(world),
    ]
    assert len(assigned) == len(set(assigned)), f"duplicate uidNumber in {assigned}"
    assert render.DIRECTORY_GID_NUMBER not in assigned, (
        "the group identifier is also in use as a user identifier"
    )


def test_workstation_password_is_projected_as_ssha():
    """The same encoding as every other directory entry (`deployment-config.md`
    §6), derived deterministically so a second render reproduces it."""
    world = load_world()
    recon = world.identities.reconciliation_user
    expected = render.compute_ssha_password(
        recon.workstation_password,
        world.crypto.password_salt_seed,
        recon.username,
    )
    assert _ldif_entry(recon.username)["userPassword"] == expected
    assert expected.startswith("{SSHA}")


def test_the_two_credentials_do_not_share_a_verifier():
    """The directory entry carries the workstation password, not the
    Grafana one (`recon.md` §7.1).

    Nothing else would catch the two being swapped: both are valid passwords,
    both render to well-formed hashes, and the result would authenticate nobody
    while looking correct.
    """
    world = load_world()
    recon = world.identities.reconciliation_user
    grafana_encoded = render.compute_ssha_password(
        recon.password, world.crypto.password_salt_seed, recon.username
    )
    assert _ldif_entry(recon.username)["userPassword"] != grafana_encoded


def test_the_projection_is_deterministic_across_renders():
    """The salt derives from `crypto.password_salt_seed` and the username, so a
    second render must reproduce the directory seed byte for byte — the
    reproducibility `deployment.md` §9.1 depends on."""
    first = LDIF.read_bytes()
    render_profile()
    assert LDIF.read_bytes() == first


# ── What the workstation does not carry ───────────────────────────────────────


def test_no_verifier_is_rendered_for_the_workstation():
    """Nothing in `recon-01`'s artefacts may carry a verifier, and the
    shadow projection the local account used must not come back."""
    assert not (BUILD / "shadow").exists(), (
        "build/shadow/ was rendered. The workstation account is a directory "
        "one; a shadow projection means a local account is being seeded "
        "alongside it (recon.md §7.1)."
    )


def test_the_directory_client_names_no_credential():
    """sssd searches anonymously and binds as the authenticating user, so its
    configuration holds an address and a search base and no secret.

    Asserted against `world.yaml`'s values rather than by pattern, so a bind
    password introduced later is caught by name.
    """
    world = load_world()
    conf = SSSD_CONF.read_text()
    recon = world.identities.reconciliation_user
    for secret in (recon.workstation_password, recon.password):
        assert secret not in conf
    assert "ldap_default_authtok" not in conf, (
        "the directory client carries a bind credential. The stock slapd access "
        "rules permit anonymous search and `by anonymous auth`, so the "
        "workstation needs none."
    )
    assert recon.username not in conf, (
        "the directory client names the reconciliation user. It resolves "
        "whichever account authenticates; naming one gives the workstation "
        "knowledge of who its user is."
    )


def test_directory_client_binds_over_tls():
    """`recon.md` §7.1.1: the workstation's bind is over TLS, on port 636,
    addressed by the hostname `corp-it`'s leaf certificate asserts.

    The URI's host is checked against `build/hosts/recon-01` rather than
    asserted alone: the certificate carries a hostname-only subject
    alternative name, so a name this host cannot resolve fails the bind
    the same as a wrong name would, and the two artefacts are rendered
    independently of one another.

    A client that falls back to an unencrypted or unverified bind passes
    every other assertion here while presenting the workstation password
    to whichever host answers on `corp-it`'s address.
    """
    conf = SSSD_CONF.read_text()

    assert "ldap_uri = ldaps://corp-it/" in conf, (
        "the directory client does not bind over TLS. `recon.md` §7.1.1 makes "
        "the bind LDAPS on 636, addressed by hostname so that corp-it's "
        "hostname-only certificate SAN validates."
    )
    assert "ldap_uri = ldap://" not in conf, (
        "the directory client still carries a plaintext ldap:// URI. corp-it "
        "serves ldaps:/// and ldapi:/// only (`corp-it.md` §3), so nothing "
        "answers on 389."
    )

    hosts = (BUILD / "hosts" / "recon-01").read_text()
    assert "corp-it" in hosts, (
        "recon-01's /etc/hosts does not resolve corp-it, so the bind cannot "
        "reach the name its certificate asserts (`recon.md` §3)."
    )

    assert "ldap_auth_disable_tls_never_use_in_production" not in conf, (
        "the directory client permits an unencrypted authentication bind. "
        "That setting was the plaintext posture's explicit refusal; with the "
        "certificate authority in place it has no remaining purpose."
    )
    assert "ldap_tls_reqcert = never" not in conf, (
        "the directory client does not verify corp-it's certificate, so any "
        "host answering on that address collects the workstation password."
    )


# The record under build/ that holds world.yaml itself and is never copied
# into an image (deployment.md §5.2): the inputs of the render.
RECORDS = ("inputs",)


def test_no_plaintext_password_reaches_the_build_directory():
    """Neither password may appear in any rendered artefact. The
    projection moved from `/etc/shadow` to the directory; the property is
    unaffected by which target it has.
    """
    world = load_world()
    recon = world.identities.reconciliation_user
    offenders = []
    for path in BUILD.rglob("*"):
        if not path.is_file() or path.relative_to(BUILD).parts[0] in RECORDS:
            continue
        try:
            content = path.read_text()
        except UnicodeDecodeError:
            continue
        for label, secret in (
            ("workstation", recon.workstation_password),
            ("grafana", recon.password),
        ):
            if secret in content:
                offenders.append(f"{path.relative_to(BUILD)} ({label} password)")
    assert not offenders, "plaintext reached the build directory:\n  " + "\n  ".join(
        offenders
    )
