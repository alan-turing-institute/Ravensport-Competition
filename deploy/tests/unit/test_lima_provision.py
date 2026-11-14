"""lima.yaml provisioning checks. Run with: pytest tests/unit -q"""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def _system_scripts() -> list[str]:
    lima = yaml.safe_load((ROOT / "lima.yaml").read_text())
    return [p["script"] for p in lima["provision"] if p["mode"] == "system"]


def _incus_preseed() -> dict:
    """The YAML that the provision script pipes into `incus admin init
    --preseed`, reassembled from its single-quoted printf arguments."""
    for script in _system_scripts():
        match = re.search(
            r"printf '%s\\n' \\\n(.*?)\| incus admin init --preseed",
            script,
            re.DOTALL,
        )
        if match:
            lines = re.findall(r"^\s*'(.*)' \\$", match.group(1), re.MULTILINE)
            return yaml.safe_load("\n".join(lines))
    raise AssertionError("lima.yaml has no `incus admin init --preseed` block")


def test_default_pool_is_btrfs_loop():
    """The default pool is copy-on-write, so each instance is a snapshot of an
    image volume unpacked once, not a full root filesystem written per deploy.
    A sized loop file keeps the pool within the VM disk."""
    pools = {p["name"]: p for p in _incus_preseed()["storage_pools"]}
    assert pools["default"]["driver"] == "btrfs", pools["default"]
    assert pools["default"].get("config", {}).get("size") == "60GiB", pools["default"]


def test_default_profile_roots_on_default_pool():
    profiles = {p["name"]: p for p in _incus_preseed()["profiles"]}
    root = profiles["default"]["devices"]["root"]
    assert root == {"path": "/", "pool": "default", "type": "disk"}, root


def test_btrfs_progs_installed():
    """The btrfs driver needs the userspace tools to create the loop pool."""
    assert any(
        re.search(r"apt-get install[^\n]*(?:\\\n[^\n]*)*\bbtrfs-progs\b", s)
        for s in _system_scripts()
    ), "lima.yaml does not install btrfs-progs"
