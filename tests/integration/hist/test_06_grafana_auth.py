"""
Grafana authentication tests (hist-tests.md §6), targeting hist.md §7 and
digital.md §5.1/§5.2.

Each log server runs its own Grafana instance with an independent internal user
store (hist.md §7). hist-01 holds a single seeded admin account
(identities.hist_admin); hist-dmz-01 holds the seeded reconciliation user
(identities.reconciliation_user). The stores are not shared, so a credential
accepted by one instance is rejected by the other — the first four tests assert
both directions of that isolation.

Assumption: render.py will utilise a grafana password helper.
`compute_pbkdf2_grafana_hash(password, seed_hex, username)` — the Grafana
counterpart of the existing `compute_argon2id_hash` / `compute_ssha_password` —
that derives the PBKDF2-HMAC-SHA256 hash with a salt derived deterministically
from crypto.password_salt_seed and the username (deployment-config.md §6). If the
render implementation names it differently, adjust the import below.
"""

from __future__ import annotations

import yaml

from tests.integration.hist.conftest import Config, SeededCredentials
from tests.integration.hist.stubs.grafana import GrafanaAuth, IncusExecGrafanaAuth

_ACCEPTED = 200
_REJECTED = 401


def test_hist01_admin_login_succeeds_with_seeded_credential(
    hist01_grafana: IncusExecGrafanaAuth, world_credentials: SeededCredentials
) -> None:
    cred = world_credentials.hist_admin
    status = hist01_grafana.login_status(cred.username, cred.password)
    assert status == _ACCEPTED, (
        f"hist-01 Grafana rejected the seeded admin {cred.username!r} "
        f"(status {status}, expected {_ACCEPTED})"
    )


_DEFAULT_LOGIN = "admin"
_DEFAULT_PASSWORD = "admin"


def test_hist01_rejects_default_grafana_credential(
    hist01_grafana: IncusExecGrafanaAuth,
) -> None:
    """Grafana creates admin/admin on first start. The seed of hist.md §10.2
    renames that row rather than adding a second one; had it inserted instead,
    a well-known credential would ship in the image and §7's single-account
    property would be false."""
    status = hist01_grafana.login_status(_DEFAULT_LOGIN, _DEFAULT_PASSWORD)
    assert status == _REJECTED, (
        f"hist-01 Grafana accepted the default {_DEFAULT_LOGIN}/{_DEFAULT_PASSWORD} "
        f"credential (status {status}, expected {_REJECTED}); the seed added an "
        "account instead of renaming the default one"
    )


def test_histdmz01_rejects_default_grafana_credential(
    histdmz01_grafana: GrafanaAuth,
) -> None:
    """The mirror of the previous test, on hist-dmz-01."""
    status = histdmz01_grafana.login_status(_DEFAULT_LOGIN, _DEFAULT_PASSWORD)
    assert status == _REJECTED, (
        f"hist-dmz-01 Grafana accepted the default "
        f"{_DEFAULT_LOGIN}/{_DEFAULT_PASSWORD} credential (status {status}, "
        f"expected {_REJECTED})"
    )


def test_hist01_user_store_holds_exactly_one_account(
    hist01_grafana: IncusExecGrafanaAuth, world_credentials: SeededCredentials
) -> None:
    """hist.md §7: each instance holds exactly one account.

    Asserted directly rather than inferred from which credentials are rejected,
    so that a seed which renames the default row but leaves some third account
    behind is caught. Possible only because §7 seeds the account as a server
    administrator, which /api/users requires.
    """
    cred = world_credentials.hist_admin
    users = hist01_grafana.get_json("/api/users", cred.username, cred.password)
    logins = [user["login"] for user in users]
    assert logins == [cred.username], (
        f"hist-01 Grafana holds {len(logins)} account(s) {logins}; hist.md §7 "
        f"specifies exactly one, {cred.username!r}"
    )


def test_histdmz01_user_store_holds_exactly_one_account(
    histdmz01_grafana: GrafanaAuth, world_credentials: SeededCredentials
) -> None:
    """The mirror of the previous test, on hist-dmz-01."""
    cred = world_credentials.reconciliation_user
    users = histdmz01_grafana.get_json("/api/users", cred.username, cred.password)
    logins = [user["login"] for user in users]
    assert logins == [cred.username], (
        f"hist-dmz-01 Grafana holds {len(logins)} account(s) {logins}; hist.md §7 "
        f"specifies exactly one, {cred.username!r}"
    )


def test_hist01_rejects_reconciliation_user_credential(
    hist01_grafana: IncusExecGrafanaAuth, world_credentials: SeededCredentials
) -> None:
    """The reconciliation user exists only in hist-dmz-01's store; hist-01 must
    not accept it — the two Grafana instances share no user store (§7)."""
    cred = world_credentials.reconciliation_user
    status = hist01_grafana.login_status(cred.username, cred.password)
    assert status == _REJECTED, (
        f"hist-01 Grafana accepted the reconciliation user {cred.username!r}, "
        f"which belongs only to hist-dmz-01 (status {status}, expected {_REJECTED})"
    )


def test_histdmz01_reconciliation_login_succeeds_with_seeded_credential(
    histdmz01_grafana: GrafanaAuth, world_credentials: SeededCredentials
) -> None:
    cred = world_credentials.reconciliation_user
    status = histdmz01_grafana.login_status(cred.username, cred.password)
    assert status == _ACCEPTED, (
        f"hist-dmz-01 Grafana rejected the seeded reconciliation user "
        f"{cred.username!r} (status {status}, expected {_ACCEPTED})"
    )


def test_histdmz01_rejects_hist01_admin_credential(
    histdmz01_grafana: GrafanaAuth, world_credentials: SeededCredentials
) -> None:
    """Mirror of the hist-01 isolation test: the hist-01 admin belongs only to
    hist-01's store and must be rejected by hist-dmz-01."""
    cred = world_credentials.hist_admin
    status = histdmz01_grafana.login_status(cred.username, cred.password)
    assert status == _REJECTED, (
        f"hist-dmz-01 Grafana accepted the hist-01 admin {cred.username!r}, "
        f"which belongs only to hist-01 (status {status}, expected {_REJECTED})"
    )


def test_credentials_are_deterministic_across_rebuilds(config: Config) -> None:
    """The seeded Grafana hash must be identical each time it is rendered from the
    same world.yaml, or a rebuild would silently change the deployed credential
    and break the reproducibility guarantee (deployment.md §9.1). This holds only
    if the PBKDF2 salt is derived deterministically from crypto.password_salt_seed
    rather than generated randomly (deployment-config.md §6).

    Imported inside the test so the module stays collectable until render.py
    grows the Grafana hash helper; until then this fails red with a clear import
    error, which is the intended state.
    """
    import sys

    scripts_dir = config.world_yaml.resolve().parent / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    from render import compute_pbkdf2_grafana_hash  # type: ignore[import-not-found]

    world = yaml.safe_load(config.world_yaml.read_text())
    seed = world["crypto"]["password_salt_seed"]
    admin = world["identities"]["hist_admin"]
    recon = world["identities"]["reconciliation_user"]

    # (a) Determinism: two independent renders of the same identity agree. A
    # random-salt implementation would fail here, which is the property under
    # test — the reproducibility guarantee depends on it.
    first = compute_pbkdf2_grafana_hash(admin["password"], seed, admin["username"])
    second = compute_pbkdf2_grafana_hash(admin["password"], seed, admin["username"])
    assert first == second, (
        "compute_pbkdf2_grafana_hash is not deterministic for a fixed "
        "(password, seed, username) — the Grafana seed uses a random salt"
    )

    # (b) The seed actually feeds the derivation: a different salt seed must
    # change the hash, or the "deterministic salt" is not seed-derived at all.
    # other_seed 1/2 choice is a guard against palindromes!
    other_seed = ("1" if seed[0] != "1" else "2") + seed[1:]
    assert other_seed != seed, (
        f"failed to derive a salt seed different from {seed!r}; the assertion "
        "below would compare a hash with itself"
    )
    assert (
        compute_pbkdf2_grafana_hash(admin["password"], other_seed, admin["username"])
        != first
    ), "changing crypto.password_salt_seed did not change the Grafana hash"

    # (c) Each identity gets a distinct salt: the two seeded users must not
    # collide on a shared salt even with the same seed (deployment-config.md §6,
    # salt derived from seed *and* username).
    assert compute_pbkdf2_grafana_hash(
        recon["password"], seed, recon["username"]
    ) != compute_pbkdf2_grafana_hash(recon["password"], seed, admin["username"]), (
        "the Grafana salt does not depend on the username — two identities "
        "would share a salt"
    )
