"""
Grafana artefact unit tests (hist-tests.md §8), targeting hist.md §7 and §9.

render.py emits three artefacts per log-server node into build/grafana/:

  <node>-user-seed.sql      the seeded account, applied to the database Grafana
                            itself creates at /var/lib/grafana/grafana.db
  <node>-grafana.ini        the [server] block of hist.md §9
  <node>-datasources.yaml   the two loopback datasources of hist.md §7

Every assertion here is on rendered *content*, not on a file existing. The
FMS_ADMIN_HASH versus FMS_ADMIN_PASSWORD_HASH mismatch on this branch passed an
existence check and surfaced only as a crash loop on the VM; a seed that names a
column Grafana does not have, or omits the org membership, would fail the same
way — at §7 query time, on the VM, long after the render looked healthy.

Run with: pytest tests/unit -q
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]  # deploy/
sys.path.insert(0, str(ROOT / "scripts"))
import render  # noqa: E402

_NODES = {
    "hist-01": "hist_admin",
    "hist-dmz-01": "reconciliation_user",
}


def _render(profile: str = "ci-minimal") -> Path:
    out = subprocess.run(
        [sys.executable, "scripts/render.py", "--profile", profile],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert out.returncode == 0, out.stderr
    return ROOT / "build"


def _world() -> dict:
    return yaml.safe_load((ROOT / "world.yaml").read_text())


def _artefact(node: str, suffix: str) -> str:
    build = _render()
    path = build / "grafana" / f"{node}-{suffix}"
    assert path.exists(), (
        f"{path.relative_to(ROOT)} was not rendered; hist.md §10.2 requires the "
        f"build script to push it into the {node} image"
    )
    return path.read_text()


# ── The seeded account ────────────────────────────────────────────────────────


@pytest.mark.parametrize("node,identity", _NODES.items())
def test_user_seed_carries_the_rendered_hash_and_salt(node: str, identity: str):
    """The seed must carry exactly the hash and salt compute_pbkdf2_grafana_hash
    produced for that node's identity. A seed written from any other source — or a
    salt that does not match the hash — leaves Grafana computing a different
    digest at login and rejecting the credential."""
    world = _world()
    entry = world["identities"][identity]
    expected_hash, expected_salt = render.compute_pbkdf2_grafana_hash(
        entry["password"], world["crypto"]["password_salt_seed"], entry["username"]
    )

    sql = _artefact(node, "user-seed.sql")
    assert expected_hash in sql, (
        f"{node} seed does not carry the rendered PBKDF2 hash; Grafana would "
        f"reject {entry['username']!r} at login"
    )
    assert expected_salt in sql, (
        f"{node} seed does not carry salt {expected_salt!r}; Grafana recomputes "
        "the digest from the stored salt, so a mismatched pair fails every login"
    )
    assert entry["password"] not in sql, (
        f"{node} seed contains the plaintext password; only the hash and salt may "
        "reach the image (deployment-config.md §5)"
    )


@pytest.mark.parametrize("node,identity", _NODES.items())
def test_user_seed_renames_the_default_account(node: str, identity: str):
    """Grafana creates admin/admin on first start. The seed must UPDATE that row,
    not INSERT a second one: an insert leaves the default credential live, and
    org 1's membership row still points at the original user."""
    world = _world()
    username = world["identities"][identity]["username"]
    sql = _artefact(node, "user-seed.sql")
    lowered = sql.lower()

    assert "insert into user" not in lowered, (
        f"{node} seed inserts a new user row, leaving Grafana's default "
        "admin/admin account live and contradicting hist.md §7's single account"
    )
    assert "update" in lowered and "user" in lowered, (
        f"{node} seed contains no UPDATE of the user table"
    )
    for column in ("login", "email", "name", "password", "salt", "is_admin"):
        assert column in lowered, (
            f"{node} seed does not set {column!r}; a partially updated row leaves "
            "the default account's values in place"
        )
    assert username in sql, f"{node} seed does not set the login to {username!r}"


@pytest.mark.parametrize("node,identity", _NODES.items())
def test_user_seed_grants_organisation_admin(node: str, identity: str):
    """hist.md §7: the single account is both server admin and organisation Admin.
    A user row without an org_user membership makes /api/datasources return 403
    rather than the two datasources — a failure that surfaces only on the VM."""
    sql = _artefact(node, "user-seed.sql")
    lowered = sql.lower()

    assert "is_admin" in lowered and "1" in sql, (
        f"{node} seed does not set is_admin; the account cannot administer its "
        "own instance"
    )
    assert "org_user" in lowered, (
        f"{node} seed writes no org_user row, so the account holds no "
        "organisation membership and /api/datasources returns 403"
    )
    assert "admin" in lowered, (
        f"{node} seed does not set the organisation role to Admin"
    )


@pytest.mark.parametrize("node", _NODES)
def test_user_seed_holds_only_its_own_nodes_credential(node: str):
    """Least knowledge (deployment-config.md §5): each node's artefacts carry its
    own credential and no other. A shared seed would put the reconciliation user's
    credential inside the OT zone, and hist-01's admin inside the DMZ."""
    world = _world()
    foreign = "reconciliation_user" if node == "hist-01" else "hist_admin"
    foreign_entry = world["identities"][foreign]
    foreign_hash, _ = render.compute_pbkdf2_grafana_hash(
        foreign_entry["password"],
        world["crypto"]["password_salt_seed"],
        foreign_entry["username"],
    )

    sql = _artefact(node, "user-seed.sql")
    assert foreign_entry["username"] not in sql, (
        f"{node} seed names {foreign_entry['username']!r}, which belongs to the "
        "other log server"
    )
    assert foreign_hash not in sql, (
        f"{node} seed carries the other log server's credential hash"
    )
    for secret in (
        world["identities"]["admin"]["password"],
        world["identities"]["operators"][0]["password"],
    ):
        assert secret not in sql, (
            f"{node} seed carries an unrelated credential from world.yaml"
        )


# ── grafana.ini ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("node", _NODES)
def test_grafana_ini_serves_https_on_443(node: str):
    """hist.md §9: Grafana terminates TLS itself on 443 with the build-time
    certificate. Falling back to plain HTTP would put the reconciliation user's
    password on the wire in clear across the IT/DMZ boundary."""
    ini = _artefact(node, "grafana.ini")
    for setting in (
        "protocol = https",
        "http_addr = 0.0.0.0",
        "http_port = 443",
        "cert_file = /etc/hist-tls/cert.pem",
        "cert_key = /etc/hist-tls/key.pem",
    ):
        assert setting in ini, f"{node} grafana.ini missing {setting!r} (hist.md §9)"
    assert "[server]" in ini, f"{node} grafana.ini has no [server] section"

    # transient check in debugging - should be removed
    assert "key_file" not in ini, (
        f"{node} grafana.ini sets key_file, which Grafana does not read; the TLS "
        "private key is named cert_key"
    )


@pytest.mark.parametrize("node", _NODES)
def test_grafana_ini_disables_plugin_preinstall(node: str):
    """Grafana installs four applications from grafana.com at first start unless
    told not to.

    Observed on a built image: `/var/lib/grafana/plugins/` held
    grafana-exploretraces-app, grafana-lokiexplore-app,
    grafana-metricsdrilldown-app and grafana-pyroscope-app beside the pinned
    VictoriaLogs datasource, all four carrying the build's timestamp rather than
    the cached archive's.

    Three things break. §2.1 pins every artefact by version and checksum and these
    carry neither, so identical inputs can produce different images. §10.2 unpacks
    the VictoriaLogs plugin from an archive expressly to avoid depending on the
    Grafana plugin catalogue being reachable, which this defeats. And they are
    outside `current_hash`, so a change in what the catalogue serves rebuilds
    nothing and is reported nowhere.

    `preinstall_disabled` is the setting rather than an empty `preinstall` list.
    Grafana's own defaults.ini ships `preinstall =` empty already, and the four
    applications were installed regardless — the list is defaulted in Grafana's
    code, not in its configuration file, so emptying it in ours changes nothing.
    The build asserts the outcome as well, because that reasoning is about a
    product's internals and could stop holding at a later version.
    """
    ini = _artefact(node, "grafana.ini")
    assert "[plugins]" in ini, (
        f"{node} grafana.ini has no [plugins] section, so Grafana's built-in "
        f"preinstall list applies and the image gains four unpinned applications"
    )
    assert "preinstall_disabled = true" in ini, (
        f"{node} grafana.ini does not set preinstall_disabled = true"
    )


# Each setting turns off one connection Grafana makes to a service outside the
# port (narrative §4, hist.md §9.1).
_NO_EXTERNAL = {
    "plugins": ("public_key_retrieval_disabled = true", "plugin_admin_enabled = false"),
    "analytics": (
        "reporting_enabled = false",
        "check_for_updates = false",
        "check_for_plugin_updates = false",
    ),
    "snapshots": ("external_enabled = false",),
    "news": ("news_feed_enabled = false",),
    "security": ("disable_gravatar = true",),
}


@pytest.mark.parametrize("node", _NODES)
def test_grafana_ini_makes_no_external_connection(node: str):
    """Observed on a deployed hist-01 and hist-dmz-01: Grafana's update checker
    looked up grafana.com at start, and with no route out the lookup failed and
    reached the security monitoring archive as DNS traffic at the zone
    boundaries. Each setting must sit in its own section, since Grafana ignores
    a setting under the wrong section without warning."""
    sections: dict[str, list[str]] = {}
    current = ""
    for line in _artefact(node, "grafana.ini").splitlines():
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1]
        elif line and not line.startswith("#"):
            sections.setdefault(current, []).append(line)
    for section, settings in _NO_EXTERNAL.items():
        for setting in settings:
            assert setting in sections.get(section, []), (
                f"{node} grafana.ini does not set {setting} under [{section}]"
            )


@pytest.mark.parametrize("node", _NODES)
def test_grafana_ini_sets_no_admin_password(node: str):
    """Grafana's own password paths generate a fresh random salt when they run,
    which would overwrite the deterministic seed and break reproducibility
    (deployment-config.md §6). This is the Grafana form of the cloud-init step
    that re-derived the FMS argon2 hash."""
    ini = _artefact(node, "grafana.ini")
    lowered = ini.lower()
    for forbidden in ("admin_password", "admin_user"):
        assert forbidden not in lowered, (
            f"{node} grafana.ini sets {forbidden}; Grafana would re-hash the "
            "credential with a random salt at startup, discarding the seeded row"
        )


# ── Datasource provisioning ───────────────────────────────────────────────────


_LOOPBACK_DATASOURCE_PORTS = {
    # hist-01: each store has one listener, so Grafana addresses it directly.
    "hist-01": ("8428", "9428"),
    # hist-dmz-01: each store answers two listeners on one port (hist.md §9.2)
    # — the zone address, TLS, for hist-01's replication traffic, and loopback,
    # plain HTTP, on a second, dedicated port, since 0.0.0.0 and 127.0.0.1
    # cannot both be bound on the same one. Grafana addresses the dedicated pair.
    "hist-dmz-01": ("18428", "19428"),
}


@pytest.mark.parametrize("node", _NODES)
def test_datasources_point_at_the_local_stores_on_loopback(node: str):
    """hist.md §7: both instances provision a Prometheus-compatible datasource on
    the local VictoriaMetrics and a VictoriaLogs datasource on the local
    VictoriaLogs, each over loopback. A datasource pointing at the other host
    would make a Grafana query cross a firewall boundary."""
    metrics_port, logs_port = _LOOPBACK_DATASOURCE_PORTS[node]
    parsed = yaml.safe_load(_artefact(node, "datasources.yaml"))
    urls = {ds["url"] for ds in parsed["datasources"]}
    assert urls == {
        f"http://127.0.0.1:{metrics_port}",
        f"http://127.0.0.1:{logs_port}",
    }, (
        f"{node} datasource URLs are {sorted(urls)}; hist.md §7 requires the two "
        "local stores on loopback"
    )

    types = {ds["type"] for ds in parsed["datasources"]}
    assert "prometheus" in types, (
        f"{node} has no Prometheus-compatible datasource for VictoriaMetrics"
    )
    assert any("victoria" in t and "logs" in t for t in types), (
        f"{node} datasource types are {sorted(types)}; the VictoriaLogs plugin "
        "datasource is missing"
    )


@pytest.mark.parametrize("node", ["hist-01", "hist-dmz-01"])
def test_datasource_uids_are_pinned_not_generated(node: str):
    """A provisioned dashboard binds its panels to a datasource by uid.

    Grafana generates one when the file omits it and regenerates it on every
    rebuild, so a dashboard would reference a value that no longer exists — and
    a panel whose datasource cannot be resolved renders empty rather than
    failing, making the break silent.
    """
    parsed = yaml.safe_load(_artefact(node, "datasources.yaml"))
    for ds in parsed["datasources"]:
        uid = ds.get("uid")
        assert uid, f"{node}: datasource {ds['name']!r} has no pinned uid"
        assert uid == render.DATASOURCE_UIDS[node][ds["name"]], (
            f"{node}: {ds['name']} uid is {uid!r}, not the declared value — a "
            "dashboard referencing the constant would bind to nothing"
        )


def test_the_two_hosts_use_different_datasource_uids():
    """The instances carry different dashboards for different users (§7.1).

    Shared uids would let `hist-01`'s operations dashboard bind successfully if
    it were provisioned to `hist-dmz-01`, rendering its panels against the
    replica's data without complaint. Distinct uids make that mistake fail
    visibly instead, which is the only protection against it — nothing in the
    provisioning checks which host a dashboard file was meant for.
    """
    ot = set(render.DATASOURCE_UIDS["hist-01"].values())
    dmz = set(render.DATASOURCE_UIDS["hist-dmz-01"].values())
    assert not (ot & dmz), f"uids shared between the two hosts: {ot & dmz}"

    rendered = {
        node: {
            ds["uid"]
            for ds in yaml.safe_load(_artefact(node, "datasources.yaml"))["datasources"]
        }
        for node in ("hist-01", "hist-dmz-01")
    }
    assert not (rendered["hist-01"] & rendered["hist-dmz-01"]), (
        f"rendered artefacts share uids: {rendered}"
    )


@pytest.mark.parametrize("node", _NODES)
def test_datasources_are_not_editable_through_the_interface(node: str):
    """Provisioned datasources are configuration, not user state. Leaving them
    editable lets a Grafana admin repoint a datasource at another host from the
    browser, which would defeat the loopback constraint above without any change
    to a rendered artefact."""
    parsed = yaml.safe_load(_artefact(node, "datasources.yaml"))
    for ds in parsed["datasources"]:
        assert ds.get("editable") is False, (
            f"{node} datasource {ds.get('name')!r} is editable; it can be "
            "repointed from the browser, bypassing the rendered configuration"
        )


# ── Coupling to the build ─────────────────────────────────────────────────────


@pytest.mark.parametrize("node", _NODES)
def test_grafana_artefacts_are_hashed(node: str):
    """Each rendered artefact must appear in the build script's current_hash list,
    so that editing a datasource URL or a seeded credential rebuilds the image.
    An input that is consumed but not hashed is the selective-rebuild defect
    behind the FMS_ADMIN_PASSWORD_HASH regression.

    Pushing the artefacts into the image and applying the seed with sqlite3 are
    asserted by the build-script tests once Grafana is installed there; they
    cannot be exercised before the fetch helper exists.
    """
    build = _render()
    script = (build / "node-build" / f"{node}.sh").read_text()
    manifest = json.loads((build / "manifest.json").read_text())
    assert node in manifest["build_script_nodes"], f"{node} has no build script"

    hash_block = script.split("current_hash=$(sha256sum", 1)[1].split("| sha256sum", 1)[
        0
    ]
    for suffix in ("user-seed.sql", "grafana.ini", "datasources.yaml"):
        artefact = f"build/grafana/{node}-{suffix}"
        assert artefact in hash_block, (
            f"{artefact} is not in {node}.sh's current_hash list; editing it "
            "would leave the published image unchanged"
        )


# ── Into the image ────────────────────────────────────────────────────────────
#
# Rendering an artefact correctly and installing it correctly are separate
# failures. These assertions are on the rendered build script: they were held
# back from the render tests above because none of them can hold until Grafana
# is installed in the image, which needs the fetch helper of hist.md §2.2.


def _script(node: str) -> str:
    return (_render() / "node-build" / f"{node}.sh").read_text()


def _line_containing(node: str, *needles: str) -> tuple[int, str]:
    """The first line carrying every needle, with its index, or a failure naming
    what was looked for."""
    for i, line in enumerate(_script(node).splitlines()):
        if all(needle in line for needle in needles):
            return i, line
    pytest.fail(
        f"{node}.sh has no line containing all of {list(needles)}. hist.md §10.2 "
        "requires the build script to install the rendered Grafana artefacts."
    )


@pytest.mark.parametrize("node", _NODES)
def test_grafana_configuration_reaches_the_paths_grafana_reads(node: str):
    """A rendered artefact that never reaches its final path is indistinguishable
    from one that was never rendered: Grafana falls back to its packaged defaults,
    serves plain HTTP on 3000, and provisions no datasource — all without any
    error the build would notice."""
    _line_containing(
        node, f"build/grafana/{node}-grafana.ini", "/etc/grafana/grafana.ini"
    )
    _line_containing(
        node,
        f"build/grafana/{node}-datasources.yaml",
        "/etc/grafana/provisioning/datasources/",
    )


@pytest.mark.parametrize("node", _NODES)
def test_the_plugin_is_unpacked_where_grafana_looks_for_plugins(node: str):
    """The VictoriaLogs datasource is provisioned with `type:
    victoriametrics-logs-datasource`, which resolves only if the plugin is present
    in Grafana's plugin directory. Without it the datasource appears in the
    interface but every query against it fails."""
    _line_containing(node, "/var/lib/grafana/plugins")


@pytest.mark.parametrize("node", _NODES)
def test_the_build_confirms_no_unpinned_plugin_reached_the_image(node: str):
    """`preinstall_disabled` is asserted on the rendered configuration above; this
    asserts the result.

    Grafana installs its preinstall set asynchronously at start and logs a failure
    rather than refusing to serve, so neither the rendered configuration nor a
    healthy service tells you which plugins an image actually carries. §2.1's
    pinning is worth only what the published image holds, and the setting that
    governs this is a product internal that can change at a later version — so the
    build reads the directory before publishing rather than trusting either.
    """
    script = _script(node)
    assert "PINNED_PLUGIN" in script, (
        f"{node}.sh does not check its plugin directory after Grafana's first "
        f"start, so an unpinned plugin would reach the published image unreported"
    )


def _seed_destination(node: str) -> tuple[int, str]:
    """Where the build script pushes the seed inside the image, read from the push
    itself so the test does not dictate the path.

    The container path is what distinguishes the push from the `current_hash`
    entry, which names the same rendered artefact one line in three earlier.
    """
    push_at, push = _line_containing(
        node, f"build/grafana/{node}-user-seed.sql", "$BUILD_CTR"
    )
    destination = next(
        (
            token.strip("\"'").split("$BUILD_CTR", 1)[1]
            for token in push.split()
            if "$BUILD_CTR" in token
        ),
        None,
    )
    assert destination, (
        f"{node}.sh pushes the seed but no container-side path could be read from "
        f"{push!r}"
    )
    return push_at, destination


@pytest.mark.parametrize("node", _NODES)
def test_the_seed_is_applied_between_grafana_creating_its_schema_and_being_enabled(
    node: str,
):
    """Ordering is the whole of this step. Grafana creates `grafana.db` and its
    schema on first start, so a seed applied before that has no `user` table to
    update; one applied after the service is enabled races the running instance.
    The build must start Grafana once, stop it, apply the seed, and only then
    enable it for the deployed image.
    """
    _, destination = _seed_destination(node)
    start, _ = _line_containing(node, "systemctl start", "grafana-server")
    apply_at, _ = _line_containing(node, "sqlite3", destination)
    enable_at, _ = _line_containing(node, "systemctl enable", "grafana-server")

    assert start < apply_at, (
        f"{node}.sh applies the seed at line {apply_at + 1}, before Grafana's "
        f"first start at line {start + 1}; the user table does not exist yet"
    )
    assert apply_at < enable_at, (
        f"{node}.sh enables grafana-server at line {enable_at + 1}, before the "
        f"seed is applied at line {apply_at + 1}"
    )


@pytest.mark.parametrize("node", _NODES)
def test_the_schema_probe_cannot_create_the_database(node: str):
    """Only Grafana may create `grafana.db`.

    The build polls for the schema immediately after starting Grafana, so a
    read-write probe would win the race and leave root-owned empty database
    behind; Grafana, running as its own user, can then read it but not write
    it, and fails every migration with "attempt to write a readonly database"
    until systemd stops restarting it.

    Opening read-only removes the failure rather than narrowing the race: the
    call cannot create a file at all, so it does not matter whether it runs
    before Grafana or after.
    """
    lines = _script(node).splitlines()
    _, seed_destination = _seed_destination(node)
    probes = [
        line.strip()
        for line in lines
        if "sqlite3" in line
        and "/var/lib/grafana/grafana.db" in line
        and seed_destination not in line
    ]
    assert probes, (
        f"{node}.sh never checks that Grafana has created its schema, so the seed "
        "may be applied to a database with no user table"
    )
    for probe in probes:
        assert "-readonly" in probe, (
            f"{node}.sh probes the Grafana database read-write: {probe!r}. This "
            "creates a root-owned grafana.db that Grafana cannot write to."
        )


@pytest.mark.parametrize("node", _NODES)
def test_the_build_confirms_grafana_survived_its_first_start(node: str):
    """Creating the schema is not the same as starting successfully.

    Grafana runs its database migrations before it starts the HTTP server, so a
    Grafana that migrates and then dies binding its port satisfies the schema
    wait completely. That is how a `grafana.ini` naming a setting Grafana does
    not read reached a deployed instance with a green build behind it: the image
    published, and the failure appeared at boot. The build must therefore check
    that the service is still running after the wait and before it is stopped.
    """
    lines = _script(node).splitlines()
    start, _ = _line_containing(node, "systemctl start", "grafana-server")
    stop = next(
        (
            i
            for i, line in enumerate(lines)
            if "systemctl stop" in line and "grafana-server" in line
        ),
        None,
    )
    assert stop is not None, f"{node}.sh never stops Grafana after seeding its schema"

    confirmed = [
        i
        for i, line in enumerate(lines)
        if "is-active" in line and "grafana-server" in line
    ]
    assert any(start < i < stop for i in confirmed), (
        f"{node}.sh never confirms grafana-server is still running between its "
        f"first start (line {start + 1}) and the stop (line {stop + 1}), so a "
        "Grafana that migrates and then exits produces a published image whose "
        "dashboards never start"
    )


@pytest.mark.parametrize("node", _NODES)
def test_the_seeded_database_is_left_to_grafana(node: str):
    """The seed is applied as root, so the file's ownership and mode are settled
    explicitly afterwards rather than left to whatever the build happened to
    produce. Grafana warns at every start that the database it created under the
    build's umask is world-readable, and the file holds a credential hash and its
    salt."""
    lines = _script(node).splitlines()
    _, seed_destination = _seed_destination(node)
    apply_at = next(
        i
        for i, line in enumerate(lines)
        if "sqlite3" in line and seed_destination in line
    )
    after = lines[apply_at:]

    assert any(
        "chown" in line and "grafana" in line and "grafana.db" in line for line in after
    ), (
        f"{node}.sh does not give the seeded database back to the grafana user; "
        "a root-owned database fails every migration on the next start"
    )
    assert any(
        "chmod" in line and "640" in line and "grafana.db" in line for line in after
    ), (
        f"{node}.sh leaves the seeded database world-readable; it carries the "
        "instance administrator's password hash and salt"
    )


@pytest.mark.parametrize("node", _NODES)
def test_the_seed_does_not_survive_into_the_published_image(node: str):
    """The seed carries a working credential hash and salt for the account that
    administers the instance. Once applied it is provisioning residue
    (deployment-config.md §4), and an image that still contains it hands that pair
    to anyone who can read a file on the host.
    """
    push_at, destination = _seed_destination(node)
    removed_at, _ = _line_containing(node, "rm", destination)
    assert removed_at > push_at, (
        f"{node}.sh removes {destination} at line {removed_at + 1}, before it is "
        f"pushed at line {push_at + 1}"
    )
