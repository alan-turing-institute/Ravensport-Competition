"""Systems-administration path renderer tests (digital.md §4.6)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from render import compute_sha512_crypt_hash, compute_ssha_password  # noqa: E402


def _render(profile: str = "ci-minimal") -> Path:
    out = subprocess.run(
        [sys.executable, "scripts/render.py", "--profile", profile],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode == 0, out.stderr
    return ROOT / "build"


def _topology() -> dict:
    return yaml.safe_load((ROOT / "topology.yaml").read_text())


def test_administration_nodes_and_actor_address_are_deployed() -> None:
    topology = _topology()
    assert topology["nodes"]["dmz-bastion-01"]["nics"][0]["ipv4"] == "10.20.0.30"
    assert topology["nodes"]["admin-01"]["nics"][0]["ipv4"] == "10.30.0.40"
    sysadmin_nic = next(
        nic
        for nic in topology["nodes"]["sim-host"]["nics"]
        if nic.get("name") == "it-sysadmin"
    )
    assert sysadmin_nic == {
        "network": "vnet-it",
        "ipv4": "10.30.0.12",
        "nic_type": "macvlan",
        "name": "it-sysadmin",
    }

    manifest = json.loads((_render() / "manifest.json").read_text())
    assert "admin-01" in manifest["nodes"]
    assert "dmz-bastion-01" in manifest["nodes"]


def test_firewall_admits_only_the_two_stage_ssh_path() -> None:
    build = _render("full")
    fw_dmz_it = (build / "nftables/fw-dmz-it.nft").read_text()
    assert (
        'iifname "it0" oifname "dmz0" ip saddr 10.30.0.40 '
        "ip daddr 10.20.0.30 tcp dport 22 accept"
    ) in fw_dmz_it

    fw_ot_dmz = (build / "nftables/fw-ot-dmz.nft").read_text()
    for address in ("10.40.0.10", "10.40.0.11", "10.40.0.12"):
        assert (
            'iifname "dmz0" oifname "ot0" ip saddr 10.20.0.30 '
            f"ip daddr {address} tcp dport 22 accept"
        ) in fw_ot_dmz
    assert 'iifname "it0" oifname "ot0"' not in fw_ot_dmz


def test_sim_haproxy_source_binds_the_admin_egress() -> None:
    cfg = (_render() / "haproxy/sim-host.cfg").read_text()
    assert "bind 127.0.0.1:20004" in cfg
    assert "server admin-01 10.30.0.40:22 source 10.30.0.12 no-check" in cfg


def test_ssh_key_material_is_deterministic_valid_and_least_knowledge() -> None:
    build = _render()
    ssh = build / "ssh"
    identity = ssh / "admin-01/id_ed25519"
    assert identity.exists()
    assert (identity.stat().st_mode & 0o777) == 0o600
    assert [path.parent.name for path in ssh.glob("*/id_ed25519")] == ["admin-01"]

    first = identity.read_bytes()
    _render()
    assert identity.read_bytes() == first

    ssh_keygen = shutil.which("ssh-keygen")
    assert ssh_keygen is not None, "ssh-keygen is required to validate rendered keys"
    for node in ("admin-01", "dmz-bastion-01", "fms-01", "hist-01", "hmi-01"):
        private = ssh / node / "ssh_host_ed25519_key"
        public = (ssh / node / "ssh_host_ed25519_key.pub").read_text().split()[:2]
        derived = subprocess.run(
            [ssh_keygen, "-y", "-f", str(private)],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()[:2]
        assert derived == public

    derived_identity = subprocess.run(
        [ssh_keygen, "-y", "-f", str(identity)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()[:2]
    assert derived_identity == (ssh / "admin-01/id_ed25519.pub").read_text().split()[:2]


def test_workstation_bastion_and_managed_host_policies_are_distinct() -> None:
    build = _render()
    admin = (build / "node-build/admin-01.sh").read_text()
    assert "PasswordAuthentication yes" in admin
    assert "PubkeyAuthentication no" in admin
    assert "AllowTcpForwarding no" in admin
    assert "sssd" not in admin.lower()
    assert "useradd --uid" in admin
    assert (
        'incus exec "$BUILD_CTR" -- chown root:root \\\n'
        "    /etc/ssh/ssh_known_hosts "
        "/etc/ssh/ssh_config.d/50-ravensport-admin.conf"
    ) in admin

    bastion = (build / "node-build/dmz-bastion-01.sh").read_text()
    assert 'ADMIN_SHELL="/usr/sbin/nologin"' in bastion
    assert "PasswordAuthentication no" in bastion
    assert "AllowTcpForwarding local" in bastion
    assert "PermitOpen fms-01:22 hist-01:22 hmi-01:22" in bastion
    assert "PermitTTY no" in bastion

    for node in ("fms-01", "hist-01", "hmi-01"):
        script = (build / "node-build" / f"{node}.sh").read_text()
        assert "PasswordAuthentication no" in script
        assert "AllowTcpForwarding no" in script
        assert "NOPASSWD: ALL" in script
        assert 'env_keep += "SYSTEMD_PAGER"' in script
        assert "export SYSTEMD_PAGER=cat" in script
        assert "AllowAgentForwarding no" in script
        assert "X11Forwarding no" in script


def test_workstation_client_uses_proxyjump_and_pinned_host_keys() -> None:
    build = _render()
    config = (build / "ssh/admin-01/ssh_config").read_text()
    assert "ProxyJump dmz-bastion-01" in config
    assert "StrictHostKeyChecking yes" in config
    assert "ForwardAgent no" in config

    known_hosts = (build / "ssh/admin-01/ssh_known_hosts").read_text().splitlines()
    assert [line.split()[0] for line in known_hosts] == [
        "dmz-bastion-01",
        "fms-01",
        "hist-01",
        "hmi-01",
    ]


def test_local_workstation_account_reuses_the_directory_admin_password() -> None:
    build = _render()
    world = yaml.safe_load((ROOT / "world.yaml").read_text())
    identity = world["identities"]["systems_administrator"]
    admin_identity = world["identities"]["admin"]
    ldif = (build / "ldif/corp-it.ldif").read_text()
    script = (build / "node-build/admin-01.sh").read_text()

    assert f"dn: uid={admin_identity['username']}," in ldif
    assert f"dn: uid={identity['username']}," not in ldif
    expected_directory_hash = compute_ssha_password(
        admin_identity["password"],
        world["crypto"]["password_salt_seed"],
        admin_identity["username"],
    )
    assert f"userPassword: {expected_directory_hash}" in ldif
    assert not (build / "sssd/admin-01.conf").exists()
    expected = compute_sha512_crypt_hash(
        admin_identity["password"],
        world["crypto"]["password_salt_seed"],
        identity["username"],
    )
    assert expected in script
    assert admin_identity["password"] not in script


def test_ssh_login_hosts_keep_records_in_the_system_journal() -> None:
    build = _render()
    for node in ("admin-01", "dmz-bastion-01", "fms-01", "hist-01", "hmi-01"):
        script = (build / "node-build" / f"{node}.sh").read_text()
        assert "SplitMode=none" in script, f"{node} can create per-user journals"


def test_private_key_is_not_embedded_in_other_rendered_node_inputs() -> None:
    build = _render()
    private_key = (build / "ssh/admin-01/id_ed25519").read_text()
    for path in build.rglob("*"):
        if not path.is_file() or path == build / "ssh/admin-01/id_ed25519":
            continue
        if path.name == "manifest.json":
            continue
        assert private_key not in path.read_text(errors="ignore"), (
            f"systems administrator private key leaked into {os.fspath(path)}"
        )
