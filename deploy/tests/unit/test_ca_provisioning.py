"""
Internal certificate authority (CA) provisioning tests (digital.md §2.7, §8
decision 7; deployment-config.md §4, §6).

Three independent things are checked:

  * Key material is *deterministic*: the root and leaf RSA private keys are
    derived from `world.yaml`'s `crypto.password_salt_seed`, the same way every
    other secret in the pipeline is (deployment-config.md §6) — not generated
    from OS randomness. This is a security property, not a convenience: an
    attacker with `world.yaml` can already regenerate every other credential in
    the deployment, so the CA root key must be no different, and the render
    pipeline's reproducibility guarantee (deployment.md §9.1) depends on it too.
    A changed seed must change the key; an unchanged seed must reproduce the
    exact same key AND the exact same certificate bytes (serial, validity
    window included) on a completely independent run, not merely skip
    regeneration because a cache said so.
  * `scripts/build-ca.sh` produces a valid, hostname-scoped trust chain from
    that key material, under HAProxy-equivalent strictness, and is idempotent.
  * The rendered artefacts that depend on the CA — the sim-host HAProxy
    config's dock-operator backend, hmi-01's env file, fms-01's env file, the
    hosts files of every node that must resolve a leaf by name (hmi-01 ->
    fms-01 and hist-01; fms-01 -> hist-01 and corp-it; recon-01 ->
    hist-dmz-01 and corp-it), and every node's trust store — carry the
    CA-aware configuration rather than the old `verify none` /
    self-signed-certificate workarounds. hist-01/hist-dmz-01's own
    shared-keypair TLS material install is covered separately by
    `test_hist_tls_material.py`; corp-it's directory-service TLS material by
    `test_corp_it_tls_material.py`.

Run with: pytest tests/unit -q
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]  # deploy/
sys.path.insert(0, str(ROOT / "scripts"))
import ca_provisioning  # noqa: E402
from world_schema import World  # noqa: E402

# Every node topology.yaml gives a `build_script:` (deploy/topology.yaml
# `nodes:` keys) — i.e. every node with a `build/node-build/<node>.sh`.
# `sim-host` is deliberately excluded: it has no `build_script:` entry and is
# built by `build-sim-image.sh` instead, covered by its own test below.
#
# Kept as a literal list here — rather than parsed from topology.yaml — so
# this test fails loudly if a node is renamed instead of silently tracking
# the rename.
ALL_NODES = [
    "fms-01",
    "hmi-01",
    "hist-01",
    "hist-dmz-01",
    "atg-relay-01",
    "recon-01",
    "corp-it",
    "siem-01",
    "fw-ot-dmz",
    "fw-dmz-it",
    "fw-ext-dmz",
]

LEAF_NODES = [
    "fms-01",
    "hmi-01",
    "hist-01",
    "hist-dmz-01",
    "corp-it",
    "siem-01",
    "recon-01",
]  # digital.md §2.7: these seven get a CA-signed leaf certificate


def world() -> dict:
    return yaml.safe_load((ROOT / "world.yaml").read_text())


def seed_hex() -> str:
    return world()["crypto"]["password_salt_seed"]


def run_render(profile: str = "ci-minimal", world_path: Path | None = None) -> Path:
    args = [sys.executable, "scripts/render.py", "--profile", profile]
    if world_path is not None:
        args += ["--world", str(world_path)]
    out = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, check=False)
    assert out.returncode == 0, out.stderr
    return ROOT / "build"


def run_build_ca() -> Path:
    out = subprocess.run(
        ["bash", "scripts/build-ca.sh"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode == 0, out.stderr
    return ROOT / "build"


def full_pipeline(profile: str = "ci-minimal") -> tuple[Path, str]:
    """Render then build the CA, returning (build_dir, build-ca.sh stdout)."""
    run_render(profile)
    out = subprocess.run(
        ["bash", "scripts/build-ca.sh"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode == 0, out.stderr
    return ROOT / "build", out.stdout


def openssl(*args: str) -> str:
    out = subprocess.run(
        ["openssl", *args], capture_output=True, text=True, check=False
    )
    assert out.returncode == 0, out.stderr
    return out.stdout


# ---------------------------------------------------------------------------
# Determinism of the raw key material (ca_provisioning.py), independent of
# render.py/build-ca.sh so these run fast and pin down the derivation itself.
# ---------------------------------------------------------------------------


def test_rsa_key_derivation_is_deterministic():
    seed = seed_hex()
    key_a = ca_provisioning.generate_rsa_key_pem(seed, "ca-root")
    key_b = ca_provisioning.generate_rsa_key_pem(seed, "ca-root")
    assert key_a == key_b


def test_rsa_key_derivation_differs_by_label():
    """The root and each leaf must be distinct keys, not the same key reused —
    otherwise a compromise of one leaf's key would also compromise the root or a
    sibling leaf (e.g. hist-01's key leaking would not also compromise
    hist-dmz-01's)."""
    seed = seed_hex()
    keys = {
        node: ca_provisioning.generate_rsa_key_pem(seed, f"leaf:{node}")
        for node in LEAF_NODES
    }
    keys["ca-root"] = ca_provisioning.generate_rsa_key_pem(seed, "ca-root")
    assert len(set(keys.values())) == len(keys)


def test_rsa_key_derivation_differs_by_seed():
    """Sanity check that the derivation actually depends on the seed, not just
    the label — otherwise every RavensPort deployment would share a CA key
    regardless of its own world.yaml."""
    key_a = ca_provisioning.generate_rsa_key_pem(seed_hex(), "ca-root")
    other_seed = "00" * 16
    assert other_seed != seed_hex()
    key_b = ca_provisioning.generate_rsa_key_pem(other_seed, "ca-root")
    assert key_a != key_b


def test_rsa_key_is_actually_rsa_2048(tmp_path: Path):
    pem = ca_provisioning.generate_rsa_key_pem(seed_hex(), "ca-root")
    key_path = tmp_path / "key.pem"
    key_path.write_bytes(pem)
    text = openssl("pkey", "-noout", "-text", "-in", str(key_path))
    assert "Private-Key: (2048 bit" in text


def test_deterministic_serial_is_positive_and_stable():
    seed = seed_hex()
    a = ca_provisioning.deterministic_serial(seed, "fms-01")
    b = ca_provisioning.deterministic_serial(seed, "fms-01")
    other = ca_provisioning.deterministic_serial(seed, "hmi-01")
    assert a == b
    assert a > 0
    assert a != other
    # DER INTEGER encoding: a serial whose top bit is set would be misread as
    # negative unless padded, which some strict validators reject outright.
    assert a.bit_length() <= 159


# ---------------------------------------------------------------------------
# render.py: writes deterministic key material as part of the normal render.
# ---------------------------------------------------------------------------


def test_render_writes_deterministic_key_material_for_root_and_leaves_only():
    build = run_render()
    assert (build / "ca" / "ca.key").exists()
    for node in LEAF_NODES:
        assert (build / "tls" / node / "key.pem").exists(), f"no key.pem for {node}"
    # sim-host is trust-only (digital.md §2.7): it must never receive a private
    # key, so it must not have a build/tls/sim-host directory at all.
    assert not (build / "tls" / "sim-host").exists()


def test_render_key_material_is_byte_identical_across_independent_renders():
    """Not merely "unchanged inputs are skipped" (that's a caching property) —
    two independent renders, with build/ removed in between, must produce the
    exact same key bytes. This is the property the attacker-with-world.yaml
    argument actually depends on."""
    build = run_render()
    ca_key_before = (build / "ca" / "ca.key").read_bytes()
    fms_key_before = (build / "tls" / "fms-01" / "key.pem").read_bytes()

    shutil.rmtree(ROOT / "build")
    build = run_render()

    assert (build / "ca" / "ca.key").read_bytes() == ca_key_before
    assert (build / "tls" / "fms-01" / "key.pem").read_bytes() == fms_key_before


def test_render_key_material_changes_with_a_different_seed(tmp_path: Path):
    w = world()
    original_seed = w["crypto"]["password_salt_seed"]
    mutated = dict(w)
    mutated["crypto"] = dict(w["crypto"])
    mutated["crypto"]["password_salt_seed"] = "ff" * 16
    assert mutated["crypto"]["password_salt_seed"] != original_seed
    World.model_validate(mutated)  # the mutated file must still be schema-valid

    world_copy = tmp_path / "world.yaml"
    world_copy.write_text(yaml.safe_dump(mutated))

    default_build = run_render()
    default_key = (default_build / "ca" / "ca.key").read_bytes()

    mutated_build = run_render(world_path=world_copy)
    mutated_key = (mutated_build / "ca" / "ca.key").read_bytes()

    assert default_key != mutated_key

    # Leave build/ rendered against the default world for later tests.
    run_render()


def test_render_preserves_ca_key_material_across_a_re_render():
    """render.py wipes and regenerates build/ on every run (deployment.md §5.2);
    build/ca and build/tls are the one exception, so build-ca.sh's own
    input-hash cache (Tier below) is not defeated by every `make render`."""
    build = run_render()
    (build / "ca").mkdir(exist_ok=True)
    sentinel = build / "ca" / "not-owned-by-render.marker"
    sentinel.write_text("still here?")

    run_render()

    assert sentinel.exists(), "render.py deleted build/ca instead of preserving it"
    sentinel.unlink()


# ---------------------------------------------------------------------------
# build-ca.sh: builds the X.509 structure from render.py's key material.
# ---------------------------------------------------------------------------


def test_build_ca_produces_a_valid_chain_with_hostname_only_sans():
    build, _ = full_pipeline()
    ca_pem = build / "ca" / "ca.pem"
    assert ca_pem.exists()
    # The root private key must exist on the build host but is never a rendered
    # artefact pushed anywhere — this only asserts it stays put, in build/ca/.
    assert (build / "ca" / "ca.key").exists()

    for hostname in LEAF_NODES:
        leaf = build / "tls" / hostname / "cert.pem"
        assert leaf.exists(), f"build-ca.sh did not sign a leaf for {hostname}"
        # Plain `openssl verify` is lenient and does not reproduce HAProxy's own
        # live handshake rejection of a chain whose CA lacks an explicit
        # keyUsage extension. `-x509_strict -purpose sslserver` is what actually
        # exercises RFC 5280 conformance the way a real TLS stack does — this is
        # the check that is supposed to catch the class of bug already seen
        # once in this feature's history, before it ever reaches a live
        # deployment.
        openssl(
            "verify",
            "-x509_strict",
            "-purpose",
            "sslserver",
            "-CAfile",
            str(ca_pem),
            str(leaf),
        )

        sans = openssl("x509", "-in", str(leaf), "-noout", "-ext", "subjectAltName")
        assert f"DNS:{hostname}" in sans, f"{hostname} leaf missing DNS:{hostname} SAN"
        assert "IP Address:" not in sans, f"{hostname} leaf must not carry an IP SAN"

    # sim-host is trust-only: no leaf certificate is signed for it.
    assert not (build / "tls" / "sim-host").exists()


def test_build_ca_root_has_ca_extensions():
    build, _ = full_pipeline()
    ca_pem = build / "ca" / "ca.pem"
    constraints = openssl(
        "x509", "-in", str(ca_pem), "-noout", "-ext", "basicConstraints"
    )
    key_usage = openssl("x509", "-in", str(ca_pem), "-noout", "-ext", "keyUsage")
    assert "CA:TRUE" in constraints
    assert "Certificate Sign" in key_usage


def test_build_ca_is_idempotent():
    build, _ = full_pipeline()
    ca_pem = build / "ca" / "ca.pem"
    fms_leaf = build / "tls" / "fms-01" / "cert.pem"
    ca_before = ca_pem.read_bytes()
    fms_leaf_before = fms_leaf.read_bytes()

    out = subprocess.run(
        ["bash", "scripts/build-ca.sh"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode == 0, out.stderr
    assert "unchanged, skipping" in out.stdout

    assert ca_pem.read_bytes() == ca_before
    assert fms_leaf.read_bytes() == fms_leaf_before


def test_build_ca_certificates_are_byte_identical_across_independent_runs():
    """The determinism requirement extends past the key: with a pinned serial
    and validity window (this feature's decision), the *certificate* bytes —
    not just the key inside it — must also be reproducible from world.yaml
    alone, on a completely independent run (build/ removed in between)."""
    build, _ = full_pipeline()
    ca_before = (build / "ca" / "ca.pem").read_bytes()
    leaves_before = {
        node: (build / "tls" / node / "cert.pem").read_bytes() for node in LEAF_NODES
    }

    shutil.rmtree(ROOT / "build")
    build, _ = full_pipeline()

    assert (build / "ca" / "ca.pem").read_bytes() == ca_before
    for node in LEAF_NODES:
        assert (build / "tls" / node / "cert.pem").read_bytes() == leaves_before[
            node
        ], f"{node}'s leaf certificate is not byte-identical across independent runs"


def test_build_ca_regenerates_when_the_seed_changes(tmp_path: Path):
    build, _ = full_pipeline()
    ca_before = (build / "ca" / "ca.pem").read_bytes()

    w = world()
    mutated = dict(w)
    mutated["crypto"] = dict(w["crypto"])
    mutated["crypto"]["password_salt_seed"] = "ff" * 16
    world_copy = tmp_path / "world.yaml"
    world_copy.write_text(yaml.safe_dump(mutated))

    run_render(world_path=world_copy)
    out = subprocess.run(
        ["bash", "scripts/build-ca.sh"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode == 0, out.stderr
    assert "unchanged, skipping" not in out.stdout, (
        "build-ca.sh treated a changed seed as unchanged input"
    )
    ca_after = (ROOT / "build" / "ca" / "ca.pem").read_bytes()
    assert ca_after != ca_before

    # Restore the default world so later tests in this session see the real CA.
    run_render()
    subprocess.run(
        ["bash", "scripts/build-ca.sh"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_certificates_change_with_a_different_not_before_but_keys_do_not(
    tmp_path: Path,
):
    """crypto.ca_not_before feeds only the signed validity window (params.json),
    never the key derivation seed — so changing it must change the resulting
    certificate bytes while leaving the private keys untouched. Asserting both
    halves catches either direction of regression: a not_before that silently
    fails to move the certificate, or one that unexpectedly perturbs the key."""
    build, _ = full_pipeline()
    ca_key_before = (build / "ca" / "ca.key").read_bytes()
    ca_cert_before = (build / "ca" / "ca.pem").read_bytes()

    w = world()
    mutated = dict(w)
    mutated["crypto"] = dict(w["crypto"])
    original_not_before = mutated["crypto"]["ca_not_before"]
    mutated["crypto"]["ca_not_before"] = "2026-09-02"
    assert mutated["crypto"]["ca_not_before"] != original_not_before
    World.model_validate(mutated)
    world_copy = tmp_path / "world.yaml"
    world_copy.write_text(yaml.safe_dump(mutated))

    run_render(world_path=world_copy)
    out = subprocess.run(
        ["bash", "scripts/build-ca.sh"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode == 0, out.stderr
    assert "unchanged, skipping" not in out.stdout, (
        "build-ca.sh treated a changed not_before as unchanged input"
    )

    assert (ROOT / "build" / "ca" / "ca.key").read_bytes() == ca_key_before, (
        "ca_not_before must not affect key derivation"
    )
    assert (ROOT / "build" / "ca" / "ca.pem").read_bytes() != ca_cert_before, (
        "ca_not_before must change the signed certificate's validity window"
    )

    # Restore the default world so later tests in this session see the real CA.
    run_render()
    subprocess.run(
        ["bash", "scripts/build-ca.sh"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_render_refuses_a_stale_not_before(tmp_path: Path):
    """A ca_not_before old enough that the leaf certificates it would produce
    are already expired must fail the render outright (ca_provisioning.py's
    _check_not_stale), not silently build already-invalid certificates —
    the exact failure mode a hardcoded source constant was exposed to."""
    w = world()
    mutated = dict(w)
    mutated["crypto"] = dict(w["crypto"])
    mutated["crypto"]["ca_not_before"] = "2000-01-01"
    World.model_validate(mutated)  # stale, but still a well-formed date
    world_copy = tmp_path / "world.yaml"
    world_copy.write_text(yaml.safe_dump(mutated))

    out = subprocess.run(
        [
            sys.executable,
            "scripts/render.py",
            "--profile",
            "ci-minimal",
            "--world",
            str(world_copy),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode != 0, "render.py must refuse a stale ca_not_before"
    assert "stale" in out.stderr.lower()

    # Leave build/ rendered against the default world for later tests.
    run_render()


# ---------------------------------------------------------------------------
# Rendered artefacts that depend on the CA.
# ---------------------------------------------------------------------------


def test_dock_operator_backend_validates_hmi_01_instead_of_verify_none():
    build, _ = full_pipeline()
    cfg = (build / "haproxy" / "sim-host.cfg").read_text()

    server_lines = [line for line in cfg.splitlines() if "server hmi-01 " in line]
    assert server_lines, "dock_operator_target's hmi-01 server line not found"
    server_line = server_lines[0]

    assert "verify none" not in server_line
    assert "ssl verify required" in server_line
    assert "ca-file" in server_line
    assert "verifyhost hmi-01" in server_line
    # Not optional: observed directly on a live deployment that without an
    # explicit SNI, this HAProxy build's `verifyhost` silently fails against a
    # certificate that `openssl verify -x509_strict` and a live TLS handshake
    # both accept — the exact bug this feature's test suite was built to catch,
    # and did not catch until it was exercised against a real deployment
    # (test_smoke.sh's dock-operator reproduction). Static checks alone (this
    # one included) cannot prove sni's absence would fail; they can only pin
    # down that removing it here would be a regression once it is known to
    # matter.
    assert "sni str(hmi-01)" in server_line


def test_hmi_addresses_fms_by_hostname_not_ip():
    """fms-01's certificate carries a DNS SAN of 'fms-01' only, so the HMI's
    HTTPS client must reach it by that name for certificate validation to
    succeed — an IP-literal URL would fail hostname verification."""
    build, _ = full_pipeline()
    env = (build / "env" / "hmi-01.env").read_text()
    assert "HMI_FMS_URL=https://fms-01" in env

    hosts = (build / "hosts" / "hmi-01").read_text()
    assert "fms-01" in hosts, "hmi-01 has no /etc/hosts entry to resolve fms-01"


def test_fms_resolves_hist_01_by_hostname():
    """hist-01's certificate carries a DNS SAN of 'hist-01' only (digital.md
    §2.7, hist.md §3.1), so the FMS's pushes into vmagent/vlagent must reach it
    by that name for certificate validation to succeed — an IP-literal target
    would fail hostname verification."""
    build, _ = full_pipeline()
    env = (build / "env" / "fms-01.env").read_text()
    assert "FMS_VMAGENT_URL=https://hist-01:8429/api/v1/import" in env
    assert "FMS_VLAGENT_JSONLINE_URL=https://hist-01:9429/insert/jsonline" in env
    assert "FMS_VLAGENT_SYSLOG_HOST=hist-01" in env

    hosts = (build / "hosts" / "fms-01").read_text()
    assert "hist-01" in hosts, "fms-01 has no /etc/hosts entry to resolve hist-01"


def test_hmi_resolves_hist_01_by_hostname():
    """hist-01's Grafana is reachable from any OT host, including hmi-01
    (hist-tests.md §7); its certificate is hostname-only, so hmi-01 needs the
    matching resolve entry alongside its existing one for fms-01."""
    build, _ = full_pipeline()
    hosts = (build / "hosts" / "hmi-01").read_text()
    assert "hist-01" in hosts, "hmi-01 has no /etc/hosts entry to resolve hist-01"


def test_recon_01_already_resolves_hist_dmz_01_by_hostname():
    """topology.yaml already gives recon-01 a resolve: [hist-dmz-01] entry,
    predating this feature — but now that hist-dmz-01's certificate carries a
    hostname-only SAN, that entry is load-bearing for the reconciliation user's
    browser to validate it, not just a convenience. This is a regression guard:
    if it were ever removed, certificate validation from recon-01 would start
    failing with no test elsewhere to catch it."""
    build = run_render()
    hosts = (build / "hosts" / "recon-01").read_text()
    assert "hist-dmz-01" in hosts, (
        "recon-01 has no /etc/hosts entry to resolve hist-dmz-01"
    )


def test_fms_resolves_corp_it_by_hostname():
    """corp-it's certificate carries a DNS SAN of 'corp-it' only
    (corp-it.md §5), so the FMS's operator directory bind must reach it by
    that name for certificate validation to succeed — an IP-literal LDAPS URI
    would fail hostname verification."""
    build = run_render()
    env = (build / "env" / "fms-01.env").read_text()
    assert "FMS_LDAP_URI=ldaps://corp-it" in env, (
        "fms-01.env does not address the directory as ldaps://corp-it"
    )

    hosts = (build / "hosts" / "fms-01").read_text()
    assert "corp-it" in hosts, "fms-01 has no /etc/hosts entry to resolve corp-it"


def test_recon_01_resolves_corp_it_by_hostname():
    """corp-it's certificate carries a DNS SAN of 'corp-it' only
    (corp-it.md §5), so the reconciliation workstation's directory client must
    reach it by that name for certificate validation to succeed
    (recon.md §3, §7.1.1)."""
    build = run_render()
    hosts = (build / "hosts" / "recon-01").read_text()
    assert "corp-it" in hosts, "recon-01 has no /etc/hosts entry to resolve corp-it"


@pytest.mark.parametrize("node", ALL_NODES)
def test_every_node_installs_the_ca_root_in_its_trust_store(node: str):
    """deployment-config.md §4: the CA root is projected 'into each node's
    trusted certificate store' — literally every node, not only the three that
    make a CA-dependent TLS connection today."""
    build = run_render()
    script = (build / "node-build" / f"{node}.sh").read_text()
    assert "ca.pem" in script, f"{node}'s build script never installs the CA root"


@pytest.mark.parametrize("node", ALL_NODES)
def test_every_node_installs_the_ca_root_where_update_ca_certificates_reads_it(
    node: str,
):
    """`recon.md` §6.4 records the one way this step fails while appearing to
    have been taken: `update-ca-certificates` reads only files ending `.crt`
    and ignores any other extension without complaint. The destination is
    therefore load-bearing, and the preceding test — which asks only that the
    root is referenced somewhere — passes either way.

    Stated over the system store alone. A node may also hold the root at a
    `.pem` path for an application's own trust configuration, as `hmi-01` does
    at `/etc/hmi/tls/ca.pem`, and that copy is not read by
    `update-ca-certificates`.
    """
    build = run_render()
    text = (build / "node-build" / f"{node}.sh").read_text()
    # The push spans a line continuation in `_trust_ca.sh.j2`, so the source
    # and destination are not on one line in the rendered script.
    script = re.sub(r"\\\n\s*", " ", text)

    installed = re.search(
        r"incus file push\b[^\n]*\bbuild/ca/ca\.pem\b"
        r"[^\n]*?/usr/local/share/ca-certificates/([^\s\"']+)",
        script,
    )
    assert installed, (
        f"{node}'s build script does not push build/ca/ca.pem into "
        "/usr/local/share/ca-certificates/, the directory "
        "update-ca-certificates reads"
    )
    assert installed.group(1).endswith(".crt"), (
        f"{node} installs the CA root as {installed.group(1)!r}; "
        "update-ca-certificates reads only files ending '.crt', so the root "
        "would be ignored and the trust bundle left without it"
    )
    assert "update-ca-certificates" in script, (
        f"{node} places the CA root in the system store but never runs "
        "update-ca-certificates, so the bundle is never rebuilt to include it"
    )


def test_no_node_build_artefact_carries_another_nodes_private_key():
    """Least-knowledge (deployment-config.md §5), extended to key material: a
    node's rendered build script must reference only its own leaf key, never
    another node's."""
    build = run_render()
    for owner in LEAF_NODES:
        others = [n for n in LEAF_NODES if n != owner]
        script = (build / "node-build" / f"{owner}.sh").read_text()
        for other in others:
            assert f"tls/{other}/key.pem" not in script, (
                f"{owner}'s build script references {other}'s private key"
            )


def test_ca_root_private_key_is_never_referenced_by_any_node_build_script():
    build = run_render()
    for node in ALL_NODES:
        script_path = build / "node-build" / f"{node}.sh"
        if not script_path.exists():
            continue
        script = script_path.read_text()
        assert "ca.key" not in script, (
            f"{node}'s build script references the CA root private key"
        )


def test_sim_host_trusts_the_ca_root_but_holds_no_key_material():
    """sim-host has no build_script entry (it uses build-sim-image.sh, not
    render.py's per-node build scripts) so it is exercised separately here. It
    is the third TLS-terminating component in digital.md §2.7, but as a client
    only: it trusts the CA root for the dock-operator backend's `verify
    required`, and must never hold — or reference — a private key of its own."""
    script = (ROOT / "scripts" / "build-sim-image.sh").read_text()
    assert "ca.pem" in script, "build-sim-image.sh never installs the CA root"
    assert "ca.key" not in script, (
        "build-sim-image.sh references the CA root private key"
    )


# ---------------------------------------------------------------------------
# The leaf set is declared in more than one place — ca_provisioning.LEAF_LABELS
# derives the keys, build-ca.sh's LEAVES signs the certificates, and each
# node's build script installs its own. Nothing reconciles them, so a new leaf
# can be added to one and not the others without any check noticing. These two tests assert the invariant rather than
# the present two-node instance, so that coverage follows an addition instead
# of having to be remembered alongside it.
# ---------------------------------------------------------------------------


def _build_ca_leaves() -> set[str]:
    """The leaf set as `build-ca.sh` declares it, read from its `LEAVES=` line
    rather than restated here — the value of the comparison below depends on
    both sides being read from their source."""
    for line in (ROOT / "scripts" / "build-ca.sh").read_text().splitlines():
        if line.startswith("LEAVES="):
            return set(line.split("=", 1)[1].strip().strip('"').split())
    raise AssertionError("build-ca.sh no longer has a LEAVES= line to compare against")


def test_build_ca_and_ca_provisioning_agree_on_the_leaf_set():
    """render.py derives a private key for every node in `LEAF_LABELS`;
    build-ca.sh signs a certificate for every node in `LEAVES`. A node in the
    first but not the second receives a key and no certificate; a node in the
    second but not the first makes build-ca.sh exit on a key it cannot find."""
    assert _build_ca_leaves() == set(ca_provisioning.LEAF_LABELS)


def test_a_node_given_its_certificate_and_key_separately_verifies_they_pair():
    """render.py rewrites `key.pem` on every render but preserves `cert.pem`,
    which build-ca.sh owns (main() in render.py excludes build/ca and build/tls
    from the wipe). A render following a changed `password_salt_seed` or
    `ca_not_before` therefore leaves a new key beside a certificate signed from
    the previous one: both files present, both valid PEM, no longer a pair.
    Running a node build script directly — as tests/integration/test_image_build.sh
    does, bypassing the Makefile's build-ca dependency — would bake that
    mismatch into the image, where rustls rejects it at FMS startup.

    Any node receiving the two halves as separate files must therefore compare
    them before the image is built. `hmi-01` is exempt for as long as it
    receives a single concatenated bundle, since that file has one producer and
    cannot diverge from itself. The exemption is derived from what each script
    pushes rather than named here, so converting any node to separate files
    brings it into scope without this test being edited."""
    build = run_render()

    checked: list[str] = []
    for node in ca_provisioning.LEAF_LABELS:
        script = (build / "node-build" / f"{node}.sh").read_text()
        pushed = [line for line in script.splitlines() if "incus file push" in line]
        if not (
            any(f"build/tls/{node}/cert.pem" in line for line in pushed)
            and any(f"build/tls/{node}/key.pem" in line for line in pushed)
        ):
            continue  # receives a bundle, not two separate files

        checked.append(node)
        modulus_lines = [line for line in script.splitlines() if "-modulus" in line]
        assert any(f"tls/{node}/cert.pem" in line for line in modulus_lines) and any(
            f"tls/{node}/key.pem" in line for line in modulus_lines
        ), (
            f"{node} is given cert.pem and key.pem as separate files, but its build "
            "script never compares their moduli — a certificate left over from a "
            "previous seed would be installed alongside the current key unnoticed"
        )

    assert checked, (
        "no node was found receiving cert.pem and key.pem as separate files — either "
        "every leaf now receives a bundle, or the build scripts changed shape and "
        "this test has become vacuous"
    )
