#!/usr/bin/env python3
"""Render topology.yaml + world.yaml + a profile override into a build/ directory.

Outputs:
  build/hosts                       — /etc/hosts fragment per node
  build/network-config/<node>.yaml  — static netplan config per Ubuntu node
  build/nftables/<node>.nft         — rendered nftables rules per fw node
  build/haproxy/<node>.cfg          — rendered haproxy config for HAProxy nodes (atg-relay-01, sim-host)
  build/env/<node>.env              — rendered env file per service node
  build/suricata/<node>.env         — AF_PACKET interface list per firewall node
  build/ldif/corp-it.ldif           — LDAP seed LDIF
  build/node-build/<node>.sh        — image build script per node
  build/incus-commands.sh           — deploy: create networks + launch from images
  build/teardown-commands.sh        — destroy everything
  build/manifest.json               — node/network list for tests
  build/inputs/                     — copies of the topology, world and override rendered

Usage:
  python scripts/render.py --profile full
  python scripts/render.py --profile ci-minimal --world deploy/world.yaml
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import ipaddress
import json
import shutil
import string
import struct
import subprocess
import sys
import textwrap
from copy import deepcopy
from pathlib import Path, PurePosixPath
from typing import Any

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined

from world_schema import COMPRESSION_FACTOR, PROJECTION_MAP, PROJECTION_SCALING, World

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "build"
BUILD_SCRIPTS_DIR = ROOT / "build-scripts"

# The twelve monitored hosts (siem.md §1): every deployed host except sim-host,
# which reports nothing about itself (siem.md §4.4).
SIEM_MONITORED_NODES: tuple[str, ...] = (
    "fms-01",
    "hmi-01",
    "hist-01",
    "hist-dmz-01",
    "atg-relay-01",
    "dmz-bastion-01",
    "corp-it",
    "recon-01",
    "admin-01",
    "fw-ext-dmz",
    "fw-dmz-it",
    "fw-ot-dmz",
)

SSH_SERVER_NODES: tuple[str, ...] = (
    "admin-01",
    "dmz-bastion-01",
    "fms-01",
    "hist-01",
    "hmi-01",
)
SSH_MANAGED_NODES: tuple[str, ...] = (
    "dmz-bastion-01",
    "fms-01",
    "hist-01",
    "hmi-01",
)

try:
    from argon2.low_level import Type as ArgonType
    from argon2.low_level import hash_secret

    _ARGON2_AVAILABLE = True
except ImportError:
    _ARGON2_AVAILABLE = False


# ── Credential helpers ────────────────────────────────────────────────────────


def _derive_salt(seed_hex: str, username: str) -> bytes:
    seed = bytes.fromhex(seed_hex)
    return hmac.new(seed, username.encode(), hashlib.sha256).digest()[:16]


def compute_argon2id_hash(password: str, seed_hex: str, username: str) -> str:
    if not _ARGON2_AVAILABLE:
        sys.exit(
            "argon2-cffi is required to render world.yaml credentials. "
            "Run: pip install argon2-cffi"
        )
    salt = _derive_salt(seed_hex, username)
    encoded: bytes = hash_secret(
        secret=password.encode(),
        salt=salt,
        time_cost=2,
        memory_cost=65536,
        parallelism=1,
        hash_len=32,
        type=ArgonType.ID,
    )
    return encoded.decode()


def compute_ssha_password(password: str, seed_hex: str, username: str) -> str:
    salt = _derive_salt(seed_hex, username)
    digest = hashlib.sha1(password.encode() + salt).digest()
    encoded = base64.b64encode(digest + salt).decode()
    return f"{{SSHA}}{encoded}"


_SHADOW_SALT_ALPHABET = string.ascii_letters + string.digits + "./"


def compute_sha512_crypt_hash(password: str, seed_hex: str, username: str) -> str:
    """A deterministic SHA-512 crypt verifier suitable for `/etc/shadow`.

    OpenSSL is already a deployment-render dependency for the internal CA. The
    password is supplied on stdin rather than argv, and only the verifier is
    rendered into `admin-01`'s build script.
    """
    raw = _derive_salt(seed_hex, username)
    salt = "".join(_SHADOW_SALT_ALPHABET[b % len(_SHADOW_SALT_ALPHABET)] for b in raw)
    try:
        result = subprocess.run(
            ["openssl", "passwd", "-6", "-salt", salt, "-stdin"],
            input=f"{password}\n",
            capture_output=True,
            text=True,
            check=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as error:
        raise RuntimeError("OpenSSL with SHA-512 crypt support is required") from error
    encoded = result.stdout.strip()
    if not encoded.startswith(f"$6${salt}$"):
        raise RuntimeError("OpenSSL returned an invalid SHA-512 crypt verifier")
    return encoded


# Grafana's EncodePassword parameters (pkg/util/encoding.go) and the shape of the
# salt its own first-start path writes (util.GetRandomString: 10 alphanumerics).
_GRAFANA_PBKDF2_ITERATIONS = 10_000
_GRAFANA_PBKDF2_KEY_LENGTH = 50
_GRAFANA_SALT_LENGTH = 10
_GRAFANA_SALT_ALPHABET = string.ascii_letters + string.digits


def _derive_grafana_salt(seed_hex: str, username: str) -> str:
    """Grafana holds the salt in a `salt` text column, so it must be a printable
    string rather than the raw bytes the argon2id and SSHA paths use. Mapping the
    derived bytes onto Grafana's own alphabet keeps the stored row
    indistinguishable in form from one Grafana wrote itself."""
    raw = _derive_salt(seed_hex, username)
    return "".join(
        _GRAFANA_SALT_ALPHABET[b % len(_GRAFANA_SALT_ALPHABET)]
        for b in raw[:_GRAFANA_SALT_LENGTH]
    )


def compute_pbkdf2_grafana_hash(
    password: str, seed_hex: str, username: str
) -> tuple[str, str]:
    """Grafana's `EncodePassword`: PBKDF2-HMAC-SHA256, 10,000 iterations, a
    50-byte derived key, hex-encoded. Returns the hash and the salt that produced
    it; the image build writes both into Grafana's user table.

    Grafana's own password-setting paths generate a fresh random salt at the
    moment they run, which is the non-determinism `deployment-config.md` §6 rules
    out. Deriving the salt from `crypto.password_salt_seed` and the username
    instead makes the seeded row reproducible across rebuilds, and gives each
    identity a distinct salt."""
    salt = _derive_grafana_salt(seed_hex, username)
    derived = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode(),
        salt.encode(),
        _GRAFANA_PBKDF2_ITERATIONS,
        _GRAFANA_PBKDF2_KEY_LENGTH,
    )
    return derived.hex(), salt


# Wazuh 4.14.7's bundled Werkzeug 3.1.6 `generate_password_hash` default
# (deployment-config.md §6): scrypt, stored as scrypt:n:r:p$salt$hex in the
# management interface's rbac.db (table `users`, column `password`).
_WAZUH_SCRYPT_N = 32768
_WAZUH_SCRYPT_R = 8
_WAZUH_SCRYPT_P = 1
_WAZUH_SCRYPT_DKLEN = 64  # Werkzeug's scrypt method fixes a 64-byte derived key.
_WAZUH_SALT_LENGTH = 16
_WAZUH_SALT_ALPHABET = string.ascii_letters + string.digits


def _derive_wazuh_salt(seed_hex: str, username: str) -> str:
    """rbac.db holds the salt embedded in the stored string (unlike Grafana's
    separate column), in Werkzeug's own 16-character alphanumeric alphabet.
    `_derive_salt` already returns 16 bytes, so every byte maps onto the
    alphabet with no truncation, the same technique `_derive_grafana_salt`
    uses for Grafana's shorter, differently-shaped salt."""
    raw = _derive_salt(seed_hex, username)
    return "".join(_WAZUH_SALT_ALPHABET[b % len(_WAZUH_SALT_ALPHABET)] for b in raw)


def compute_wazuh_scrypt_hash(password: str, seed_hex: str, username: str) -> str:
    """The complete stored string for rbac.db's `password` column:
    `scrypt:32768:8:1$<salt>$<hex-digest>`.

    The manager's own `add_user`/first-start paths hash at the moment they
    run with a fresh random salt, which is the non-determinism
    `deployment-config.md` §6 rules out; this is computed once, deterministically,
    at render time, and loaded directly into rbac.db during the image build."""
    salt = _derive_wazuh_salt(seed_hex, username)
    digest = hashlib.scrypt(
        password.encode(),
        salt=salt.encode(),
        n=_WAZUH_SCRYPT_N,
        r=_WAZUH_SCRYPT_R,
        p=_WAZUH_SCRYPT_P,
        dklen=_WAZUH_SCRYPT_DKLEN,
        # OpenSSL's scrypt requires roughly 128*r*n bytes of working memory
        # (~32 MB for these parameters); hashlib's own default maxmem is
        # smaller than that and raises rather than raising the limit itself.
        maxmem=256 * 1024 * 1024,
    )
    return (
        f"scrypt:{_WAZUH_SCRYPT_N}:{_WAZUH_SCRYPT_R}:{_WAZUH_SCRYPT_P}"
        f"${salt}${digest.hex()}"
    )


def derive_agent_key(seed_hex: str, node_name: str) -> str:
    """A monitored node's Wazuh agent pre-shared key (siem.md §2.1/§8.5): the
    full HMAC-SHA256 digest of the node name under `crypto.password_salt_seed`,
    hex-encoded — 64 characters, matching the key length Wazuh's `client.keys`
    format expects.

    Deliberately not `_derive_salt`, which truncates to 16 bytes for a salt;
    an agent key is a live authentication secret rather than a salt, and
    needs the full 32-byte digest."""
    seed = bytes.fromhex(seed_hex)
    return hmac.new(seed, node_name.encode(), hashlib.sha256).hexdigest()


def derive_ssh_seed(seed_hex: str, subject: str, context: str) -> bytes:
    """Derive one deterministic Ed25519 seed for the simulated SSH estate.

    The context keeps a user's authentication key cryptographically separate
    from every host key, even when a node and identity happen to share a name.
    """
    seed = bytes.fromhex(seed_hex)
    label = f"ravensport:ssh:{context}:v1:{subject}".encode()
    return hmac.new(seed, label, hashlib.sha256).digest()


def _ssh_string(value: bytes) -> bytes:
    return struct.pack(">I", len(value)) + value


def derive_openssh_ed25519_keypair(
    seed_hex: str, subject: str, context: str, comment: str
) -> tuple[str, str]:
    """Return a deterministic unencrypted OpenSSH private/public keypair.

    PyCryptodome derives the public half from the 32-byte Ed25519 seed. The
    private half is encoded in OpenSSH's documented ``openssh-key-v1`` format
    so both sshd host keys and the workstation identity use the native format.
    """
    from Crypto.PublicKey import ECC

    private_seed = derive_ssh_seed(seed_hex, subject, context)
    key = ECC.construct(curve="Ed25519", seed=private_seed)
    public = key.public_key().export_key(format="OpenSSH")
    public_parts = public.split()
    if len(public_parts) != 2 or public_parts[0] != "ssh-ed25519":
        raise ValueError("PyCryptodome did not emit an Ed25519 OpenSSH public key")
    public_blob = base64.b64decode(public_parts[1], validate=True)

    offset = 0
    fields: list[bytes] = []
    for _ in range(2):
        if offset + 4 > len(public_blob):
            raise ValueError("truncated Ed25519 public key")
        length = struct.unpack(">I", public_blob[offset : offset + 4])[0]
        offset += 4
        fields.append(public_blob[offset : offset + length])
        offset += length
    if (
        fields[0] != b"ssh-ed25519"
        or len(fields[1]) != 32
        or offset != len(public_blob)
    ):
        raise ValueError("invalid Ed25519 public key emitted by PyCryptodome")
    public_bytes = fields[1]

    check = struct.unpack(">I", private_seed[:4])[0]
    private_block = b"".join(
        (
            struct.pack(">II", check, check),
            _ssh_string(b"ssh-ed25519"),
            _ssh_string(public_bytes),
            _ssh_string(private_seed + public_bytes),
            _ssh_string(comment.encode()),
        )
    )
    padding_length = 8 - (len(private_block) % 8)
    private_block += bytes(range(1, padding_length + 1))

    encoded = b"".join(
        (
            b"openssh-key-v1\0",
            _ssh_string(b"none"),
            _ssh_string(b"none"),
            _ssh_string(b""),
            struct.pack(">I", 1),
            _ssh_string(public_blob),
            _ssh_string(private_block),
        )
    )
    body = "\n".join(textwrap.wrap(base64.b64encode(encoded).decode(), 70))
    private_key = (
        "-----BEGIN OPENSSH PRIVATE KEY-----\n"
        f"{body}\n"
        "-----END OPENSSH PRIVATE KEY-----\n"
    )
    public_key = f"ssh-ed25519 {public_parts[1]} {comment}\n"
    return private_key, public_key


def build_ssh_artifacts(world: World) -> dict[str, str]:
    """Render the least-knowledge SSH material for each administration host."""
    seed = world.crypto.password_salt_seed
    username = world.identities.systems_administrator.username
    identity_private, identity_public = derive_openssh_ed25519_keypair(
        seed, username, "identity", f"{username}@admin-01"
    )

    host_public: dict[str, str] = {}
    artifacts: dict[str, str] = {
        "admin-01/id_ed25519": identity_private,
        "admin-01/id_ed25519.pub": identity_public,
    }
    for node_name in SSH_SERVER_NODES:
        private_key, public_key = derive_openssh_ed25519_keypair(
            seed, node_name, "host", node_name
        )
        artifacts[f"{node_name}/ssh_host_ed25519_key"] = private_key
        artifacts[f"{node_name}/ssh_host_ed25519_key.pub"] = public_key
        host_public[node_name] = public_key
        if node_name in SSH_MANAGED_NODES:
            artifacts[f"{node_name}/authorized_keys"] = identity_public

    known_hosts = []
    for node_name in SSH_MANAGED_NODES:
        algorithm, encoded, *_ = host_public[node_name].split()
        known_hosts.append(f"{node_name} {algorithm} {encoded}")
    artifacts["admin-01/ssh_known_hosts"] = "\n".join(known_hosts) + "\n"
    artifacts["admin-01/ssh_config"] = (
        "# Generated by render.py - do not edit.\n"
        "Host dmz-bastion-01 fms-01 hist-01 hmi-01\n"
        f"    User {username}\n"
        "    IdentityFile ~/.ssh/id_ed25519\n"
        "    IdentitiesOnly yes\n"
        "    ForwardAgent no\n"
        "    StrictHostKeyChecking yes\n"
        "    UserKnownHostsFile /etc/ssh/ssh_known_hosts\n"
        "\n"
        "Host fms-01 hist-01 hmi-01\n"
        "    ProxyJump dmz-bastion-01\n"
    )
    return artifacts


# ── YAML loading ──────────────────────────────────────────────────────────────


def load_yaml(path: Path) -> dict:
    with path.open() as f:
        return yaml.safe_load(f) or {}


def _deep_merge(base: dict, override: dict) -> dict:
    """Return `base` with `override` merged in recursively: a nested mapping is
    merged key by key, any other value (scalar, list) is replaced outright. Used
    to apply a profile's `world:` override onto `world.yaml` before validation, so
    a profile can compress a single value (e.g. `intervals.session_timeout_s`)
    without restating the rest of the section."""
    merged = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


def load_world(path: Path, world_override: dict | None = None) -> World:
    raw = load_yaml(path)
    if world_override:
        raw = _deep_merge(raw, world_override)
    try:
        world = World.model_validate(raw)
    except Exception as e:
        sys.exit(f"world.yaml validation failed: {e}")
    # A world loads at COMPRESSION_FACTOR, 1, which world.yaml does not hold,
    # and an additional override file may set another
    # (world_at_override_factor).
    try:
        return world.at_compression_factor(COMPRESSION_FACTOR)
    except ValueError as e:
        sys.exit(f"compression factor check failed: {e}")


def world_at_override_factor(world: World, profile: dict, extra: dict) -> World:
    """The world at the compression factor that the additional override file
    ``extra`` sets, or at COMPRESSION_FACTOR when it sets none. The
    competition's additional override file defines the compression factor
    (deployment.md §7.3, additional override file), so a profile's override
    file cannot set it."""
    if "compression_factor" in profile:
        sys.exit(
            "a profile's override file cannot set compression_factor: the "
            "competition's additional override file defines it (deployment.md §7.3)"
        )
    factor = extra.get("compression_factor", COMPRESSION_FACTOR)
    if isinstance(factor, bool) or not isinstance(factor, int):
        sys.exit(f"compression_factor must be a whole number, not {factor!r}")
    try:
        return world.at_compression_factor(factor)
    except ValueError as e:
        sys.exit(f"compression factor check failed: {e}")


# The flow timeouts of packet inspection that an additional override file may
# set, by protocol and flow state, as Suricata's configuration names them
# (episodes.md §5.3, timings; deployment.md §7.3, additional override file).
FLOW_TIMEOUT_STATES = {
    "tcp": ("new", "established", "closed"),
    "udp": ("new", "established"),
    "icmp": ("new", "established"),
}


def flow_timeouts_from_override(profile: dict, extra: dict, nodes: dict) -> dict | None:
    """The flow timeouts, in seconds, that the additional override file
    ``extra`` sets, or None when it sets none (deployment.md §7.3, additional
    override file). A profile's override file cannot set them. The timeouts
    change the images of the firewall nodes, so the file that sets them must
    give each firewall node an image name of its own."""
    if "flow_timeouts" in profile:
        sys.exit(
            "a profile's override file cannot set flow_timeouts: only an "
            "additional override file can (deployment.md §7.3)"
        )
    timeouts = extra.get("flow_timeouts")
    if timeouts is None:
        return None
    if not isinstance(timeouts, dict) or not timeouts:
        sys.exit(f"flow_timeouts must map protocols to flow states, not {timeouts!r}")
    for proto, states in timeouts.items():
        allowed = FLOW_TIMEOUT_STATES.get(proto)
        if allowed is None:
            sys.exit(
                f"flow_timeouts: unknown protocol {proto!r}, "
                f"expected one of {', '.join(FLOW_TIMEOUT_STATES)}"
            )
        if not isinstance(states, dict) or not states:
            sys.exit(
                f"flow_timeouts.{proto} must map flow states to seconds, not {states!r}"
            )
        for state, seconds in states.items():
            if state not in allowed:
                sys.exit(
                    f"flow_timeouts.{proto}: unknown flow state {state!r}, "
                    f"expected one of {', '.join(allowed)}"
                )
            if isinstance(seconds, bool) or not isinstance(seconds, int) or seconds < 1:
                sys.exit(
                    f"flow_timeouts.{proto}.{state} must be a whole number of "
                    f"seconds of at least 1, not {seconds!r}"
                )
    shared = sorted(
        name
        for name, node in nodes.items()
        if node.get("build_script") == "fw-zone"
        and node_image(name, node) == f"ravensport/{name}:latest"
    )
    if shared:
        sys.exit(
            "flow_timeouts change the images of the firewall nodes, so the "
            "additional override file must give each of them an image name of "
            f"its own (deployment.md §7.3): none for {', '.join(shared)}"
        )
    return timeouts


# ── Topology merge + validate ─────────────────────────────────────────────────


def merge_topology(topology: dict, override: dict) -> dict:
    merged = deepcopy(topology)
    for name, patch in (override.get("nodes") or {}).items():
        if name not in merged["nodes"]:
            sys.exit(f"override references unknown node: {name}")
        merged["nodes"][name].update(patch)
    if override.get("force_container"):
        for node in merged["nodes"].values():
            node["type"] = "container"
    replacements = override.get("build_script_replacements") or {}
    for node in merged["nodes"].values():
        bs = node.get("build_script")
        if bs in replacements:
            node["build_script"] = replacements[bs]
    # An additional override file names no profile, so its merge keeps the
    # profile's name (deployment.md §7.3).
    merged["_profile"] = override.get("profile_name", merged.get("_profile", "unknown"))
    return merged


def node_image(name: str, node: dict) -> str:
    """The image that a node is built as and created from. A node with a
    build script is built as `ravensport/<node>:latest`, unless an additional
    override file sets its `image_name` (deployment.md §7.3)."""
    if node.get("build_script"):
        return node.get("image_name", f"ravensport/{name}:latest")
    return node["image"]


def validate(t: dict) -> None:
    seen_ips: dict[str, str] = {}
    networks = t["networks"]
    for name, node in t["nodes"].items():
        node.setdefault("type", t["defaults"]["type"])
        node.setdefault("image", t["defaults"]["image"])
        for nic in node.get("nics", []):
            net = nic["network"]
            if net not in networks:
                sys.exit(f"node {name}: nic references unknown network {net}")
            ip = nic["ipv4"]
            if ip in seen_ips:
                sys.exit(f"duplicate IP {ip}: {name} and {seen_ips[ip]}")
            seen_ips[ip] = name
    valid_protos = {"tcp", "udp"}
    for rule in t.get("firewall_rules", []):
        if rule["proto"] not in valid_protos:
            sys.exit(
                f"firewall_rules: invalid proto '{rule['proto']}' "
                f"(must be 'tcp' or 'udp')"
            )
        dst = rule["dst"]
        if dst not in t["nodes"]:
            try:
                ipaddress.IPv4Address(dst)
            except ValueError:
                sys.exit(
                    f"firewall_rules: dst '{dst}' is neither a known node name "
                    f"nor a valid IPv4 address"
                )
        # `src` is optional: a rule without it admits any host on the `from`
        # network. A node named as `src` must have a NIC on that network.
        src = rule.get("src")
        if src is not None:
            if src in t["nodes"]:
                if not any(
                    nic["network"] == rule["from"] for nic in t["nodes"][src]["nics"]
                ):
                    sys.exit(
                        f"firewall_rules: src '{src}' has no NIC on '{rule['from']}'"
                    )
            else:
                try:
                    ipaddress.IPv4Address(src)
                except ValueError:
                    sys.exit(
                        f"firewall_rules: src '{src}' is neither a known node name "
                        f"nor a valid IPv4 address"
                    )
    if "time_source" not in t:
        sys.exit("time_source: missing (name the node that serves site time)")
    if t["time_source"] not in t["nodes"]:
        sys.exit(f"time_source: unknown node {t['time_source']}")


# ── Shared output builders ────────────────────────────────────────────────────


def _all_host_entries(t: dict) -> dict[str, str]:
    """Map hostname → ipv4 for every named entity in the topology."""
    entries: dict[str, str] = {}
    for name, node in t["nodes"].items():
        emitted_node_name = False
        for i, nic in enumerate(node.get("nics", [])):
            label = nic.get("hostname")
            if label:
                entries[label] = nic["ipv4"]
            elif i == 0 and not emitted_node_name:
                entries[name] = nic["ipv4"]
                emitted_node_name = True
    return entries


def build_node_hosts(node_name: str, node: dict, all_entries: dict[str, str]) -> str:
    """Per-node /etc/hosts: loopback + own entry + explicitly resolved names only.

    Each node carries only the names it must actively resolve. All other nodes
    use IP addresses from their env files and require no name-to-IP mapping.
    Nodes whose first NIC carries a hostname field have no self-entry (the node
    name is not the address any consumer uses).
    """
    lines = ["127.0.0.1 localhost", "::1 localhost"]
    nics = node.get("nics", [])
    if nics and not nics[0].get("hostname"):
        lines.append(f"{nics[0]['ipv4']} {node_name}")
    for name in node.get("resolve", []):
        if name not in all_entries:
            sys.exit(f"{node_name}: resolve: references unknown hostname '{name}'")
        lines.append(f"{all_entries[name]} {name}")
    return "\n".join(lines) + "\n"


def build_network_config(node: dict, t: dict) -> str:
    ethernets: dict = {}
    for i, nic in enumerate(node["nics"]):
        net = t["networks"][nic["network"]]
        prefix = net["cidr"].split("/")[1]
        iface = nic.get("name", f"eth{i}")
        # A runtime-activated NIC boots present but unconfigured — down, no
        # address, no routes — for a host whose workload binds its own L3 state at
        # run time (the adversary: adversary.md §1). `activation-mode: off` keeps
        # the link down; the absence of `addresses`/`routes` leaves the routing
        # table empty. The address in topology is still the one the workload
        # binds, and still feeds the host/entity maps.
        if nic.get("activate") == "runtime":
            ethernets[iface] = {
                "dhcp4": False,
                "dhcp6": False,
                "activation-mode": "off",
            }
            continue
        cfg: dict[str, Any] = {
            "dhcp4": False,
            "dhcp6": False,
            "addresses": [f"{nic['ipv4']}/{prefix}"],
        }
        if i == 0:
            gw = nic.get("gateway") or net.get("gateway")
            routes = []
            if gw:
                routes.append({"to": "default", "via": gw})
            for r in nic.get("routes", []):
                routes.append({"to": r["to"], "via": r["via"]})
            if routes:
                cfg["routes"] = routes
        else:
            extra = [{"to": r["to"], "via": r["via"]} for r in nic.get("routes", [])]
            if extra:
                cfg["routes"] = extra
        ethernets[iface] = cfg
    return yaml.safe_dump(
        {"ethernets": ethernets},
        default_flow_style=False,
        sort_keys=False,
    )


def _resolve_fw_rules(node: dict, t: dict) -> list[dict]:
    """Return firewall rules for a fw node, with interface names, destination
    IPs, and any source IP resolved from topology. A source node resolves to
    its address on the rule's `from` network. All NICs are zone NICs (no
    uplink)."""
    zone_nics = node["nics"]
    zone_nets = {nic["network"] for nic in zone_nics}
    net_to_iface = {
        nic["network"]: nic.get("name", f"eth{i}") for i, nic in enumerate(zone_nics)
    }
    rules = []
    for rule in t.get("firewall_rules", []):
        if rule["from"] in zone_nets and rule["to"] in zone_nets:
            dst_node = t["nodes"].get(rule["dst"], {})
            dst_nics = dst_node.get("nics", [])
            dst_ip = dst_nics[0]["ipv4"] if dst_nics else rule["dst"]
            src = rule.get("src")
            if src in t["nodes"]:
                src_ip = next(
                    nic["ipv4"]
                    for nic in t["nodes"][src]["nics"]
                    if nic["network"] == rule["from"]
                )
            else:
                src_ip = src
            rules.append(
                {
                    "from_iface": net_to_iface[rule["from"]],
                    "to_iface": net_to_iface[rule["to"]],
                    "src_ip": src_ip,
                    "dst_ip": dst_ip,
                    "port": rule["port"],
                    "proto": rule["proto"],
                }
            )
    return rules


# ── Per-node rendered file builders ──────────────────────────────────────────


def build_nftables_conf(node: dict, t: dict) -> str:
    """nftables ruleset for one monitored host (siem.md §4.1, §8.2).

    The three firewall nodes carry a stateful filter table enforcing
    cross-zone policy (deployment.md §4.2); every monitored host, firewall
    nodes included, additionally carries a `siem_log` table that logs new
    connections it makes or accepts to NFLOG group 1, for `ulogd` (§8.2) to
    turn into the file each host's agent collects. On a firewall node this
    table is placed at a higher priority than the filter table, so it only
    sees packets the filter table did not already drop; on every other
    monitored host it is the only table, so priority is immaterial. Loopback
    traffic is excluded: it is never a conversation between two of this
    environment's monitored components.
    """
    is_firewall = node.get("build_script") == "fw-zone"
    lines = ["#!/usr/sbin/nft -f", "flush ruleset", ""]
    if is_firewall:
        fw_rules = _resolve_fw_rules(node, t)
        lines += [
            "table inet filter {",
            "  chain input {",
            "    type filter hook input priority 0; policy drop;",
            "    ct state established,related accept",
            "    ct state invalid drop",
            '    iifname "lo" accept',
            "    icmp type echo-request accept",
            "    tcp dport 22 accept",
            "  }",
            "",
            "  chain forward {",
            "    type filter hook forward priority 0; policy drop;",
            "    ct state established,related accept",
            "    ct state invalid drop",
        ]
        for rule in fw_rules:
            saddr = f"ip saddr {rule['src_ip']} " if rule["src_ip"] else ""
            lines.append(
                f'    iifname "{rule["from_iface"]}" oifname "{rule["to_iface"]}" '
                f"{saddr}ip daddr {rule['dst_ip']} {rule['proto']} dport "
                f"{rule['port']} accept"
            )
        lines += [
            "  }",
            "",
            "  chain output {",
            "    type filter hook output priority 0; policy accept;",
            "  }",
            "}",
            "",
        ]
    log_priority = "filter + 10" if is_firewall else "filter"
    lines += [
        "table inet siem_log {",
        "  chain input {",
        f"    type filter hook input priority {log_priority}; policy accept;",
        '    iifname "lo" accept',
        '    ct state new log prefix "conn-in: " group 1',
        "  }",
        "",
        "  chain output {",
        f"    type filter hook output priority {log_priority}; policy accept;",
        '    oifname "lo" accept',
        '    ct state new log prefix "conn-out: " group 1',
        "  }",
        "}",
    ]
    return "\n".join(lines) + "\n"


def build_suricata_interfaces_env(node: dict) -> str:
    """The AF_PACKET interface list for one firewall node (siem.md §5, §8.4).

    Each firewall node's own two zone network interface cards are exactly
    the interfaces Suricata must capture on; boundary inspection observes the
    real traffic on those interfaces regardless of the underlying substrate
    (siem.md §5), so no other input is needed. The packaged Suricata unit is
    masked in favour of one that reads this file (fw-zone.sh.j2), rather than
    baking the interface list into `suricata.yaml` itself, so the same
    `suricata.yaml` is shared across all three firewall nodes.
    """
    interfaces = " ".join(nic["name"] for nic in node["nics"])
    return f'SURICATA_INTERFACES="{interfaces}"\n'


def fms_seed_cards(world: World) -> str:
    """Project the RFID cards the FMS must accept into the FMS_SEED_CARDS value.

    The FMS validates two card classes against its card_table: the operator
    card at login and the vessel card at the job RFID gate. On a fresh deploy
    the table is empty, so the FMS enrols this set at startup (see fms/src). The
    value is ``number:label`` entries joined by commas; the label is the
    operator username or vessel IMO number from world.yaml. A comma in a label would
    collide with the entry delimiter, so it is rejected at render time rather
    than producing a value the FMS would mis-split.
    """
    entries: list[tuple[int, str]] = [
        (op.rfid_card, op.username) for op in world.identities.operators
    ] + [(v.rfid_card, str(v.imo_number)) for v in world.roster.vessels]
    parts = []
    for number, label in entries:
        if "," in label:
            raise ValueError(
                f"card label {label!r} contains a comma, the FMS_SEED_CARDS "
                f"entry delimiter"
            )
        parts.append(f"{number}:{label}")
    return ",".join(parts)


def build_chrony_conf(node_name: str, node: dict, t: dict) -> str:
    """chrony config for one node. The time source serves; every other node is a
    client of it (digital.md §1.3)."""
    anchor = t["time_source"]
    lines = [
        "# Generated by render.py.",
        "driftfile /var/lib/chrony/chrony.drift",
        "logdir /var/log/chrony",
        "log tracking measurements",
        "",
    ]
    if node_name == anchor:
        # The zones chrony serves are the zones the firewall admits to port 123,
        # plus the source's own zone (same-zone traffic transits no fw node and
        # so has no rule). Deriving the set here keeps it from drifting out of
        # step with the policy that carries the traffic.
        client_nets = {
            rule["from"]
            for rule in t.get("firewall_rules", [])
            if rule["dst"] == anchor and rule["port"] == 123
        } | {node["nics"][0]["network"]}
        # No upstream source: the site clock is free-running, offered at
        # stratum 10.
        lines += ["local stratum 10", ""]
        lines += [f"allow {t['networks'][net]['cidr']}" for net in sorted(client_nets)]
    else:
        anchor_ip = t["nodes"][anchor]["nics"][0]["ipv4"]
        lines += [f"server {anchor_ip} iburst", "makestep 1 3"]
    return "\n".join(lines) + "\n"


def build_fms_env(node: dict, t: dict, world: World) -> str:
    atg_ip = t["nodes"]["sim-host"]["nics"][0]["ipv4"]
    disp_ip = t["nodes"]["sim-host"]["nics"][1]["ipv4"]
    rfid_ip = t["nodes"]["sim-host"]["nics"][2]["ipv4"]

    admin_hash = compute_argon2id_hash(
        world.identities.admin.password,
        world.crypto.password_salt_seed,
        world.identities.admin.username,
    )

    return (
        "\n".join(
            [
                "# Generated by render.py — do not edit.",
                f"FMS_DATABASE_URL=postgresql:///{world.database.fms.name}",
                "FMS_LISTEN_PORT=443",
                "FMS_TLS_CERT=/etc/fms/tls/cert.pem",
                "FMS_TLS_KEY=/etc/fms/tls/key.pem",
                f"FMS_ADMIN_USERNAME={world.identities.admin.username}",
                f"FMS_ADMIN_PASSWORD_HASH={admin_hash}",
                "FMS_LDAP_URI=ldaps://corp-it/",
                f"FMS_LDAP_BASE_DN={world.ldap.base_dn}",
                f"FMS_SEED_CARDS={fms_seed_cards(world)}",
                f"FMS_ATG_HOST={atg_ip}",
                "FMS_ATG_PORT=10001",
                f"FMS_ATG_POLL_INTERVAL={scaled(world, 'intervals.atg_poll_s')}",
                f"FMS_DISPENSER_HOST={disp_ip}",
                "FMS_DISPENSER_PORT=502",
                f"FMS_MODBUS_POLL_INTERVAL={scaled(world, 'intervals.modbus_poll_s')}",
                f"FMS_VESSEL_RFID_HOST={rfid_ip}",
                "FMS_VESSEL_RFID_TCP_PORT=9001",
                # Hostname, not IP: hist-01's leaf certificate carries a DNS SAN
                # of "hist-01" only (digital.md §2.7), and this node resolves it
                # via /etc/hosts (topology.yaml fms-01.resolve, hist.md §3.1).
                "FMS_VMAGENT_URL=https://hist-01:8429/api/v1/import",
                "FMS_VLAGENT_JSONLINE_URL=https://hist-01:9429/insert/jsonline",
                "FMS_VLAGENT_SYSLOG_HOST=hist-01",
                "FMS_VLAGENT_SYSLOG_PORT=6514",
                f"FMS_SESSION_TIMEOUT={scaled(world, 'intervals.session_timeout_s')}",
                f"FMS_TANK_CAPACITY_L={world.process.tank_capacity_l}",
                f"FMS_CAVITATION_THRESHOLD_PCT={world.process.cavitation_threshold_pct}",
                f"FMS_K_FACTOR={world.process.k_factor}",
                f"FMS_GRADE={world.process.grade}",
                f"FMS_DENSITY_KG_PER_L={world.process.density_kg_per_l}",
            ]
        )
        + "\n"
    )


def build_hmi_env(node: dict, t: dict, world: World) -> str:
    op_rfid_ip = t["nodes"]["sim-host"]["nics"][3]["ipv4"]
    return (
        "\n".join(
            [
                "# Generated by render.py — do not edit.",
                "HMI_LISTEN_ADDR=127.0.0.1:8080",
                # Hostname, not IP: fms-01's leaf certificate carries a DNS SAN of
                # "fms-01" only (digital.md §8 decision 7), and this node resolves
                # it via /etc/hosts (topology.yaml hmi-01.resolve).
                "HMI_FMS_URL=https://fms-01",
                "HMI_CA_CERT_PATH=/etc/hmi/tls/ca.pem",
                f"HMI_OPERATOR_RFID_ADDR={op_rfid_ip}:9002",
                f"HMI_CHALLENGE_TIMEOUT_SECS={scaled(world, 'intervals.challenge_timeout_s')}",
                "RUST_LOG=info",
            ]
        )
        + "\n"
    )


def build_haproxy_atg_cfg(t: dict) -> str:
    supplier_ip = next(
        nic["ipv4"]
        for nic in t["nodes"]["sim-host"]["nics"]
        if nic.get("name") == "ext-supplier"
    )
    return (
        "\n".join(
            [
                "global",
                "  log /dev/log local0",
                "  maxconn 200",
                "defaults",
                "  mode tcp",
                "  timeout connect 5s",
                "  timeout client  60s",
                "  timeout server  60s",
                "  log global",
                "frontend atg_in",
                "  bind *:10001",
                f"  tcp-request connection reject if !{{ src {supplier_ip} }}",
                "  default_backend atg_out",
                "backend atg_out",
                "  server atg atg-console:10001 check inter 10s init-addr libc,none",
            ]
        )
        + "\n"
    )


def build_haproxy_sim_cfg(t: dict) -> str:
    nics = t["nodes"]["sim-host"]["nics"]
    by_name = {nic["name"]: nic for nic in nics}
    atg_ip = by_name["ot-atg"]["ipv4"]
    disp_ip = by_name["ot-disp"]["ipv4"]
    v_rfid_ip = by_name["ot-vessel-rfid"]["ipv4"]
    op_rfid_ip = by_name["ot-op-rfid"]["ipv4"]
    dock_op_ip = by_name["ot-operator"]["ipv4"]
    supplier_ip = by_name["ext-supplier"]["ipv4"]
    recon_ip = by_name["it-recon-user"]["ipv4"]
    systems_administrator_ip = by_name["it-sysadmin"]["ipv4"]
    atg_relay_ip = t["nodes"]["atg-relay-01"]["nics"][0]["ipv4"]
    hmi_ip = t["nodes"]["hmi-01"]["nics"][0]["ipv4"]
    recon_01_ip = t["nodes"]["recon-01"]["nics"][0]["ipv4"]
    admin_01_ip = t["nodes"]["admin-01"]["nics"][0]["ipv4"]
    return (
        "\n".join(
            [
                "# Generated by render.py — do not edit.",
                "# HAProxy reverse proxy for the sim<->digital boundary (sim-network.md §6).",
                "#",
                "# The sim binds and originates only on 127.0.0.1; HAProxy owns every zone",
                "# interface and address. Inbound frontends bind the entity's zone address and",
                "# forward to the sim's loopback port (§4); outbound frontends listen on a",
                "# loopback egress port and dial the real target with the connection source",
                "# bound to the entity's zone address (§5). The dock operator path is the only",
                "# encrypted boundary: plain HTTP on loopback, TLS originated to the HMI.",
                "#",
                "# Production defaults, no throttling: a short connect timeout but generous",
                "# inactivity timeouts paired with TCP keepalives, because OT connections are",
                "# long-lived and mostly idle. Threads auto-scale to the available CPUs",
                "# (HAProxy's default) and each backend is health-checked. No stick-tables or",
                "# connection-rate limits.",
                "global",
                "    log stdout format raw local0 info",
                "    maxconn 2048",
                "defaults",
                "    log     global",
                "    mode    tcp",
                "    timeout connect 5s",
                "    timeout client  1h",
                "    timeout server  1h",
                "    option  clitcpka",
                "    option  srvtcpka",
                "    default-server check inter 5s",
                "# ── Inbound (digital world → sim): zone address → loopback (§4) ──────────────",
                "frontend atg_in",
                f"    bind {atg_ip}:10001",
                "    default_backend atg_sim",
                "backend atg_sim",
                "    server sim 127.0.0.1:10001",
                "frontend dispenser_in",
                f"    bind {disp_ip}:502",
                "    default_backend dispenser_sim",
                "backend dispenser_sim",
                "    server sim 127.0.0.1:10502",
                "frontend vessel_rfid_in",
                f"    bind {v_rfid_ip}:9001",
                "    default_backend vessel_rfid_sim",
                "backend vessel_rfid_sim",
                "    server sim 127.0.0.1:19001",
                "frontend operator_rfid_in",
                f"    bind {op_rfid_ip}:9002",
                "    default_backend operator_rfid_sim",
                "backend operator_rfid_sim",
                "    server sim 127.0.0.1:19002",
                "# ── Outbound (sim → digital world): loopback → target, source-bound (§5) ─────",
                "frontend fuel_supplier_out",
                "    bind 127.0.0.1:20001",
                "    default_backend fuel_supplier_target",
                "backend fuel_supplier_target",
                f"    server atg-relay-01 {atg_relay_ip}:10001 source {supplier_ip} no-check",
                "frontend recon_out",
                "    bind 127.0.0.1:20003",
                "    default_backend recon_target",
                "backend recon_target",
                f"    server recon-01 {recon_01_ip}:3389 source {recon_ip} no-check",
                "frontend systems_administrator_out",
                "    bind 127.0.0.1:20004",
                "    default_backend systems_administrator_target",
                "backend systems_administrator_target",
                (
                    f"    server admin-01 {admin_01_ip}:22 "
                    f"source {systems_administrator_ip} no-check"
                ),
                "# Dock operator: HTTP on loopback, TLS originated to the HMI (sim-network.md §5).",
                "frontend dock_operator_out",
                "    bind 127.0.0.1:20002",
                "    mode http",
                "    default_backend dock_operator_target",
                "backend dock_operator_target",
                "    mode http",
                # sni is not optional here: without an explicit SNI, this
                # HAProxy build's `verifyhost` silently fails against a
                # perfectly valid certificate (observed directly — the same
                # chain validates fine via `openssl verify -x509_strict`, and
                # the backend succeeds immediately once sni is added), even
                # though hmi-01 has only one certificate to present regardless
                # of what SNI it receives.
                f"    server hmi-01 {hmi_ip}:443 ssl verify required "
                "ca-file /etc/haproxy/tls/ca.pem sni str(hmi-01) verifyhost hmi-01 "
                f"source {dock_op_ip} no-check",
            ]
        )
        + "\n"
    )


# Directory-allocated POSIX identifiers start here, clear of every range a host
# allocates locally: 0-99 static system accounts, 100-999 those created by
# package post-install scripts, 1000 upwards local users.
#
# Measured rather than assumed. A directory group at 1000 resolved on `recon-01`
# as `lxd`, the container runtime's group, because `files` precedes `sss` and
# the local entry wins — so the session ran with the host's virtualisation
# group. Only `id` on a deployed host shows that.
DIRECTORY_ID_BASE = 10000

# The primary group every directory account claims. It had no object behind it
# while nothing logged in to a host; a POSIX login resolves it.
#
# **Not `staff`.** That is a distribution group at GID 50, so a directory group
# of the same name is shadowed by it — `getent group staff` answers
# `staff:x:50:` and never consults the directory.
DIRECTORY_GROUP_CN = "employees"
DIRECTORY_GID_NUMBER = DIRECTORY_ID_BASE

# One identifier per directory account, allocated above the group.
RECON_UID_NUMBER = DIRECTORY_ID_BASE + 1
OPERATOR_UID_BASE = DIRECTORY_ID_BASE + 2


def systems_administrator_uid(world: World) -> int:
    """Allocate the admin-01 local account after directory operators."""
    return OPERATOR_UID_BASE + len(world.identities.operators)


def directory_admin_uid(world: World) -> int:
    """Allocate the FMS administrator's directory entry after the local admin."""
    return systems_administrator_uid(world) + 1


def build_sssd_conf(world: World) -> str:
    """The reconciliation workstation's directory client (`recon.md` §7.1).

    Anonymous search with a bind as the authenticating user, so the workstation
    holds no service credential. The access rules `slapd`'s packaged initial
    configuration creates on `corp-it` allow `by anonymous auth` against
    `userPassword` and read elsewhere: enough to resolve an account and verify a
    password by binding as it, not enough to read a verifier.

    Those rules are inherited rather than authored — `corp-it`'s build installs
    `slapd` non-interactively and writes no `olcAccess` rule — so a change there
    would alter what this client can do, with nothing here to say so.

    Addressed by hostname, as the FMS's `FMS_LDAP_URI` is: `corp-it`'s leaf
    certificate carries a hostname-only subject alternative name, so the name
    dialled has to be the name the certificate asserts. The fleet runs no name
    service, so `recon-01`'s minimal `/etc/hosts` carries the entry
    (`recon.md` §3).

    **The transport is TLS** on 636, consistent with the FMS. The certificate
    is validated against the internal certificate authority's root, installed
    in this host's system trust store as it is on every node
    (`recon.md` §7.1.1, `digital.md` §2.7).
    """
    # The directory's own first naming component, so the sssd domain and the
    # base DN cannot drift apart.
    domain = world.ldap.base_dn.split(",")[0].split("=")[1]
    return (
        "\n".join(
            [
                "# Generated by render.py — do not edit.",
                "[sssd]",
                "config_file_version = 2",
                "services = nss, pam",
                f"domains = {domain}",
                "",
                f"[domain/{domain}]",
                "id_provider = ldap",
                "auth_provider = ldap",
                # The directory is authoritative; the workstation does not offer
                # to change a password it never holds.
                "chpass_provider = none",
                "ldap_uri = ldaps://corp-it/",
                f"ldap_search_base = {world.ldap.base_dn}",
                f"ldap_user_search_base = {world.ldap.people_ou},{world.ldap.base_dn}",
                "ldap_schema = rfc2307",
                # Nothing enumerates the directory: a workstation resolves the
                # account in front of it, and listing every account in the port
                # is not a capability this host needs.
                "enumerate = false",
                # A real domain workstation authenticates when the directory is
                # unreachable. It also makes corp-it's availability a modelled
                # dependency rather than a hard one.
                "cache_credentials = true",
                # The ldaps:// URI encrypts the channel before the first bind,
                # so there is no plain connection for StartTLS to upgrade.
                "ldap_id_use_start_tls = false",
                # The root installed by every node's build (_trust_ca.sh.j2),
                # named rather than left to libldap's compiled-in default.
                "ldap_tls_cacert = /etc/ssl/certs/ca-certificates.crt",
                # Stated rather than relied upon as sssd's default: this is the
                # setting that decides whether a host answering on corp-it's
                # address can collect the workstation password.
                "ldap_tls_reqcert = demand",
            ]
        )
        + "\n"
    )


def build_ldif(world: World) -> str:
    base_dn = world.ldap.base_dn
    people_dn = f"{world.ldap.people_ou},{base_dn}"
    groups_dn = f"ou=groups,{base_dn}"
    entries = [
        f"dn: {people_dn}",
        "objectClass: top",
        "objectClass: organizationalUnit",
        "ou: people",
        "",
        # The primary group the accounts below claim. Without an object behind
        # the number a POSIX login resolves an identifier that names nothing,
        # which was invisible while no directory account logged in to a host.
        f"dn: {groups_dn}",
        "objectClass: top",
        "objectClass: organizationalUnit",
        "ou: groups",
        "",
        f"dn: cn={DIRECTORY_GROUP_CN},{groups_dn}",
        "objectClass: top",
        "objectClass: posixGroup",
        f"cn: {DIRECTORY_GROUP_CN}",
        f"gidNumber: {DIRECTORY_GID_NUMBER}",
        "",
    ]
    for i, op in enumerate(world.identities.operators):
        pw_hash = compute_ssha_password(
            op.password, world.crypto.password_salt_seed, op.username
        )
        uid_number = OPERATOR_UID_BASE + i
        entries += [
            f"dn: uid={op.username},{people_dn}",
            "objectClass: inetOrgPerson",
            "objectClass: posixAccount",
            "objectClass: shadowAccount",
            f"uid: {op.username}",
            f"cn: {op.username}",
            f"sn: {op.username}",
            f"userPassword: {pw_hash}",
            f"uidNumber: {uid_number}",
            f"gidNumber: {DIRECTORY_GID_NUMBER}",
            f"homeDirectory: /home/{op.username}",
            "",
        ]

    # The reconciliation user's workstation login (recon.md §7.1). A directory
    # account rather than a local one, so the workstation holds no verifier.
    #
    # Salted from the seed and username as every other SSHA entry is. The same
    # identity's Grafana credential uses `_derive_grafana_salt`, a different
    # derivation, so one person's two verifiers cannot collide.
    recon = world.identities.reconciliation_user
    entries += [
        f"dn: uid={recon.username},{people_dn}",
        "objectClass: inetOrgPerson",
        "objectClass: posixAccount",
        "objectClass: shadowAccount",
        f"uid: {recon.username}",
        f"cn: {recon.username}",
        f"sn: {recon.username}",
        f"userPassword: "
        f"{compute_ssha_password(recon.workstation_password, world.crypto.password_salt_seed, recon.username)}",
        f"uidNumber: {RECON_UID_NUMBER}",
        f"gidNumber: {DIRECTORY_GID_NUMBER}",
        f"homeDirectory: /home/{recon.username}",
        "loginShell: /bin/bash",
        "",
    ]

    # The application administrator also exists in the corporate directory.
    # The FMS verifies the same identity independently against its Argon2 hash;
    # the distinct local systems-administrator account on admin-01 deliberately
    # reuses this password (digital.md §4.6).
    admin = world.identities.admin
    admin_password = compute_ssha_password(
        admin.password,
        world.crypto.password_salt_seed,
        admin.username,
    )
    entries += [
        f"dn: uid={admin.username},{people_dn}",
        "objectClass: inetOrgPerson",
        "objectClass: posixAccount",
        "objectClass: shadowAccount",
        f"uid: {admin.username}",
        f"cn: {admin.username}",
        f"sn: {admin.username}",
        f"userPassword: {admin_password}",
        f"uidNumber: {directory_admin_uid(world)}",
        f"gidNumber: {DIRECTORY_GID_NUMBER}",
        f"homeDirectory: /home/{admin.username}",
        "",
    ]
    return "\n".join(entries)


def _grafana_identity(build_script: str, world: World):
    """The single Grafana account each log server holds (hist.md §7). Selected by
    build script, as the env-file projections are, so a node renamed in
    topology.yaml keeps its own credential and neither node can acquire the
    other's."""
    return {
        "hist-01": world.identities.hist_admin,
        "hist-dmz-01": world.identities.reconciliation_user,
    }[build_script]


def build_grafana_user_seed(build_script: str, world: World) -> str:
    """SQL applied once at image-build time to the database Grafana creates on its
    first start, then deleted from the image (hist.md §10.2).

    The default account is renamed rather than supplemented. Grafana creates one
    user, id 1, and one organisation membership referencing it; adding a second
    user would leave the well-known admin/admin credential live and the membership
    attached to the wrong account.
    """
    identity = _grafana_identity(build_script, world)
    pw_hash, salt = compute_pbkdf2_grafana_hash(
        identity.password, world.crypto.password_salt_seed, identity.username
    )
    domain = ".".join(part.split("=", 1)[1] for part in world.ldap.base_dn.split(","))
    return "\n".join(
        [
            f"-- Grafana account for {build_script}: {identity.username} (hist.md §7).",
            "-- Applied at image build time with sqlite3, then removed from the",
            "-- image. Grafana's own password paths are not used: each generates a",
            "-- fresh random salt when it runs, which would discard this row and",
            "-- break the reproducibility guarantee of deployment.md §9.1.",
            "",
            "UPDATE user",
            f"   SET login = '{identity.username}',",
            f"       email = '{identity.username}@{domain}',",
            f"       name = '{identity.username}',",
            f"       password = '{pw_hash}',",
            f"       salt = '{salt}',",
            "       is_admin = 1",
            " WHERE id = 1;",
            "",
            "-- Organisation Admin in the default organisation (hist.md §7). The",
            "-- account administers its own instance, and without this membership",
            "-- /api/datasources returns 403 rather than the datasources below.",
            "UPDATE org_user",
            "   SET role = 'Admin'",
            " WHERE org_id = 1 AND user_id = 1;",
            "",
        ]
    )


def build_wazuh_admin_seed(world: World) -> str:
    """SQL applied once at image-build time to the RBAC database the Wazuh
    manager creates on its first start, then deleted from the image, in the
    same manner as `build_grafana_user_seed` above.

    The manager ships two default superusers with well-known passwords
    (`wazuh`/`wazuh`, `wazuh-wui`/`wazuh-wui`, per its own
    `rbac/default/users.yaml`). The first (id 1) is renamed rather than
    supplemented — the same reasoning as Grafana's default admin: adding a
    second user would leave the well-known credential live. The second
    (`wazuh-wui`) exists to serve Wazuh's own web dashboard, which is not
    deployed here (siem.md §9); nothing in this environment authenticates as
    it, and world.yaml carries no identity for it, so it is removed rather
    than left as a second live default credential.
    """
    identity = world.identities.siem_admin
    pw_hash = compute_wazuh_scrypt_hash(
        identity.password, world.crypto.password_salt_seed, identity.username
    )
    return "\n".join(
        [
            "-- siem-01 management-interface account (siem.md §8.5,",
            "-- deployment-config.md §6). Applied at image build time with",
            "-- sqlite3, then removed from the image. The manager's own",
            "-- add_user path is not used: it hashes at the moment it runs,",
            "-- which would discard this row and break the reproducibility",
            "-- guarantee of deployment.md §9.1.",
            "",
            "-- user_roles references users with ON DELETE CASCADE, which",
            "-- sqlite3 enforces only with foreign keys enabled; without it the",
            "-- DELETE below leaves the removed user's administrator role row.",
            "PRAGMA foreign_keys = ON;",
            "",
            "BEGIN;",
            "",
            "UPDATE users",
            f"   SET username = '{identity.username}',",
            f"       password = '{pw_hash}'",
            " WHERE id = 1;",
            "",
            "-- The second default superuser serves the web dashboard, which",
            "-- is not deployed (siem.md §9); removed rather than left live.",
            "DELETE FROM users WHERE id = 2;",
            "",
            "-- Neither statement fails when it matches no row, so the result",
            "-- is checked: the management-interface account is the only user",
            "-- and no role assignment names a user that does not exist. A",
            "-- failed check stops sqlite3 -bail before the COMMIT. With no",
            "-- users the comparison is NULL, which IS 1 rejects and = 1 would",
            "-- not.",
            "CREATE TEMP TABLE seed_result (",
            "    ok INTEGER CONSTRAINT only_the_management_account_remains",
            "        CHECK (ok IS 1)",
            ");",
            "INSERT INTO seed_result",
            f"SELECT (SELECT group_concat(username) FROM users) = '{identity.username}'",
            "   AND NOT EXISTS (SELECT 1 FROM user_roles",
            "                    WHERE user_id NOT IN (SELECT id FROM users));",
            "",
            "COMMIT;",
            "",
        ]
    )


def build_grafana_ini() -> str:
    """Grafana terminates TLS itself on 443 with the CA-signed keypair shared by
    every TLS-terminating service on the host, installed once at /etc/hist-tls/
    (hist.md §9/§10.2/§10.3). No admin credential appears here: setting one would
    have Grafana re-hash it with a random salt at startup, overwriting the seed.

    The [plugins] block stops Grafana fetching applications from grafana.com at
    first start. Left at the default it installs four — exploretraces, lokiexplore,
    metricsdrilldown and pyroscope — none of which this deployment has a backend
    for, none pinned or checksummed against §2.1, and none covered by the image's
    input hash. It also defeats the reason §10.2 gives for unpacking the
    VictoriaLogs plugin from an archive: the build already depends on the plugin
    catalogue being reachable, just invisibly.

    `preinstall_disabled` rather than an empty `preinstall` list. Grafana's
    defaults.ini ships `preinstall =` empty already and the four applications
    install regardless, so the list is defaulted in Grafana's code and emptying it
    here would change nothing.

    The remaining settings stop every other connection Grafana makes to a service
    outside the port (narrative §4, hist.md §9.1): the version and plugin update
    checks, usage reporting, plugin signing-key retrieval (the key built into
    Grafana verifies the VictoriaLogs plugin instead), the plugin catalogue, and
    external snapshot publishing; and, in the viewer's browser, the news feed and
    Gravatar images.
    """
    return "\n".join(
        [
            "# Rendered by render.py. Grafana's [server] block, hist.md §9.",
            "[server]",
            "protocol = https",
            "http_addr = 0.0.0.0",
            "http_port = 443",
            "cert_file = /etc/hist-tls/cert.pem",
            # cert_key. N/B Grafana ignores an unrecognised setting silently
            "cert_key = /etc/hist-tls/key.pem",
            "",
            "# hist.md §2.1: every plugin is pinned and fetched through the",
            "# artefact cache. Grafana's own preinstall reaches grafana.com at",
            "# first start and is disabled here; the build asserts the outcome.",
            "[plugins]",
            "preinstall_disabled = true",
            "public_key_retrieval_disabled = true",
            "plugin_admin_enabled = false",
            "",
            "# narrative §4, hist.md §9.1: no connection to a service outside the port.",
            "[analytics]",
            "reporting_enabled = false",
            "check_for_updates = false",
            "check_for_plugin_updates = false",
            "",
            "[snapshots]",
            "external_enabled = false",
            "",
            "[news]",
            "news_feed_enabled = false",
            "",
            "[security]",
            "disable_gravatar = true",
            "",
        ]
    )


# Where hist-01's scrape configuration sits inside the image. Read by
# victoria-metrics.service, which runs unprivileged and must be able to read it.
SCRAPE_CONFIG_PATH = "/etc/victoria-metrics/scrape.yml"

# The four local endpoints of hist.md §9.1, one job per component. The job name
# becomes the `job` label on every series collected from it, which is what §7.1's
# "ingestion rate per source" is read by.
_SCRAPE_TARGETS = {
    "victoria-metrics": "127.0.0.1:8428",
    "vmagent": "127.0.0.1:8429",
    "victoria-logs": "127.0.0.1:9428",
    "vlagent": "127.0.0.1:9429",
}


def build_victoriametrics_scrape_config() -> str:
    """
    The self-monitoring scrape configuration for `hist-01` (hist.md §7.1, §9.1)

    This is probably most open to change - the length of retention could mean that
    this becomes unwieldly.

    §7.1's operations dashboard shows ingestion rate, remote-write queue depth and
    store health. Those figures are exposed on the four `/metrics` endpoints below..

    Both stores and both agents are scraped, because the three classes of figure
    sit on different processes: uptime and disk usage on the stores, ingestion and
    replication on the agents.

    Every target is loopback. `hist-01`'s stores bind loopback only (§9.2), and
    the agents are scraped the same way.

    This file is read by `victoria-metrics`, not by `vmagent`. VictoriaMetrics
    stores what it scrapes locally and originates no remote-write, so the scraped
    series cannot reach `hist-dmz-01`; vmagent would forward every one of them to
    both of §6's targets, the second being the replica. This is a structural guard
    that the OTs host's internal telemetry doesn't cross into the hist-dmz. This
    was a DECISION RE expected behaviour and needs of the recon user.
    """
    # vmagent/vlagent serve /metrics on the same TLS-only listener as their
    # ingestion endpoints (hist.md §9.1) — self-monitoring scrapes them over
    # HTTPS accordingly. The scrape stays loopback-only either way, and the
    # certificate's SAN is the hostname "hist-01", not "127.0.0.1", so strict
    # verification would fail on a mismatch it need not care about here;
    # `insecure_skip_verify` is scoped to this internal, loopback-only scrape,
    # not to any client-facing connection. victoria-metrics/victoria-logs keep
    # plain HTTP: their own loopback listeners carry no TLS flags at all.
    _TLS_SCRAPE_JOBS = {"vmagent", "vlagent"}

    def _scrape_job(job: str, target: str) -> dict:
        config: dict = {"job_name": job, "static_configs": [{"targets": [target]}]}
        if job in _TLS_SCRAPE_JOBS:
            config["scheme"] = "https"
            config["tls_config"] = {"insecure_skip_verify": True}
        return config

    return yaml.safe_dump(
        {
            # The upstream default, stated rather than left implicit because it
            # sets the storage cost of §8's three-year retention. Nothing §7.1
            # displays resolves faster: a rate is read over minutes, and queue
            # depth, uptime and disk usage all move slowly.
            "global": {"scrape_interval": "60s"},
            "scrape_configs": [
                _scrape_job(job, target) for job, target in _SCRAPE_TARGETS.items()
            ],
        },
        sort_keys=False,
    )


# The identifier of the VictoriaLogs datasource plugin pinned in hist.md §2.1. It
# is both the provisioned datasource's `type` and the directory name the plugin
# archive expands to, so a dashboard naming it must agree with the datasource.
_VICTORIALOGS_PLUGIN = "victoriametrics-logs-datasource"

# Datasource identifiers referenced by the provisioned dashboards (hist.md §7.1),
# named for the task each instance serves rather than for the store behind it:
# `hist-dmz-01` carries the reconciliation user's month-end audit, `hist-01` the
# administrator's operational checks. Distinct per host on purpose — see
# build_grafana_datasources.
DATASOURCE_UIDS = {
    "hist-dmz-01": {
        "VictoriaMetrics": "monthly-audit-metrics",
        "VictoriaLogs": "monthly-audit-logs",
    },
    "hist-01": {
        "VictoriaMetrics": "dockside-check-metrics",
        "VictoriaLogs": "dockside-check-logs",
    },
}


#  hist-01: each store has one listener, so Grafana addresses it directly.
#  hist-dmz-01: each store answers two listeners on one port (hist.md §9.2) — the
#  zone address, TLS, for hist-01's replication traffic, and loopback, plain
#  HTTP, on a second, dedicated port, since 0.0.0.0 and 127.0.0.1 cannot both be
#  bound on the same one. Grafana addresses the dedicated pair instead.
_GRAFANA_DATASOURCE_PORTS = {
    "hist-01": {"VictoriaMetrics": 8428, "VictoriaLogs": 9428},
    "hist-dmz-01": {"VictoriaMetrics": 18428, "VictoriaLogs": 19428},
}


def build_grafana_datasources(node: str) -> str:
    """The two datasources of hist.md §7, both on loopback: VictoriaMetrics
    implements the Prometheus HTTP API directly, and VictoriaLogs is read through
    its own Grafana plugin. Both hosts provision the same pair, each pointing at
    its own local stores, so no Grafana query crosses a firewall boundary.

    Marked non-editable: a provisioned datasource that can be repointed from the
    browser would defeat the loopback constraint without any rendered file
    changing.

    Both carry an explicit `uid`, and the two hosts use different ones.

    Pinning is what makes a dashboard reproducible: Grafana generates a uid when
    the file omits it, and regenerates it on every rebuild, so a dashboard would
    reference a value that no longer exists. A panel whose datasource cannot be
    resolved renders empty rather than failing, so that break would be silent.

    Differing between hosts is a second, separate guard. The two instances carry
    different dashboards for different users (`hist.md` §7.1). Were the uids
    shared, `hist-01`'s operations dashboard would bind successfully if it were
    ever provisioned to `hist-dmz-01`, showing its panels against the replica's
    data without complaint. Distinct uids make that mistake fail visibly instead.
    The names describe the task each instance serves, not the store behind it,
    which is the thing a reader needs to know when looking at either.
    """
    uids = DATASOURCE_UIDS[node]
    ports = _GRAFANA_DATASOURCE_PORTS[node]
    return yaml.safe_dump(
        {
            "apiVersion": 1,
            "datasources": [
                {
                    "uid": uids["VictoriaMetrics"],
                    "name": "VictoriaMetrics",
                    "type": "prometheus",
                    "access": "proxy",
                    "url": f"http://127.0.0.1:{ports['VictoriaMetrics']}",
                    "isDefault": True,
                    "editable": False,
                },
                {
                    "uid": uids["VictoriaLogs"],
                    "name": "VictoriaLogs",
                    "type": _VICTORIALOGS_PLUGIN,
                    "access": "proxy",
                    "url": f"http://127.0.0.1:{ports['VictoriaLogs']}",
                    "isDefault": False,
                    "editable": False,
                },
            ],
        },
        sort_keys=True,
    )


# Where the dashboard files sit inside the image. The provider below reads this
# directory and the build script fills it (hist.md §7.1). Grafana's own data
# directory is used rather than /etc/grafana: the provisioning tree holds the
# provider that names a dashboard directory, not the dashboards themselves.
GRAFANA_DASHBOARD_DIR = "/var/lib/grafana/dashboards"

# One dashboard per log server (hist.md §7.1). The uids differ between hosts for
# the reason build_grafana_datasources gives about the datasource uids, and the
# titles are §7.1's.
DASHBOARDS = {
    "hist-dmz-01": {
        "uid": "ravensport-bunkering-ops",
        "title": "Bunkering operations",
        "tag": "reconciliation",
    },
    "hist-01": {
        "uid": "ravensport-log-server-ops",
        "title": "Log server operations",
        "tag": "operations",
    },
}

# The columns of the events table, in the order the reconciliation user reads
# them: when, what, who, then the quantities and the references that tie a record
# to a paper document. Every name is a hist.md §4.2 field.
_BUNKERING_TABLE_FIELDS = [
    "_time",
    "event_type",
    "type",
    "operator",
    "counterparty",
    "grade",
    "preset_volume_l",
    "volume_dispensed_l",
    "delivered_volume_l",
    "ticket_number",
    "bdn_number",
    "job_id",
]

# Of those columns, the ones holding a quantity in litres. VictoriaLogs stores
# every field as a string, so each arrives as "701.01" rather than 701.01 and is
# converted at the panel (see build_grafana_dashboard).
_BUNKERING_TABLE_LITRE_FIELDS = [
    "preset_volume_l",
    "volume_dispensed_l",
    "delivered_volume_l",
]


def build_grafana_dashboard_provider() -> str:
    """The file provider Grafana reads at start (hist.md §7.1).

    `allowUiUpdates` is false so the deployed dashboard cannot be saved over from
    the browser. That is what keeps it equal to the rendered file the image's
    input hash covers; §7.1's allowance for the account to keep dashboards of its
    own is unaffected, since the provider owns only the files it manages.
    """
    return yaml.safe_dump(
        {
            "apiVersion": 1,
            "providers": [
                {
                    "name": "ravensport",
                    "orgId": 1,
                    "folder": "",
                    "type": "file",
                    "disableDeletion": True,
                    "allowUiUpdates": False,
                    # The files change only when the image is rebuilt, so there is
                    # nothing for a shorter poll to find.
                    "updateIntervalSeconds": 86400,
                    "options": {
                        "path": GRAFANA_DASHBOARD_DIR,
                        "foldersFromFilesStructure": False,
                    },
                }
            ],
        },
        sort_keys=True,
    )


def _bunkering_tank_volume_panel(metrics_uid: str) -> dict:
    """Tank volume over the selected period (hist.md §4.1, §7.1).

    The series is dense — one push per 30-second poll cycle — so it interpolates
    linearly. §7.1's step-interpolation constraint applies to `atg_alarm_active`,
    which is written on transition only; no panel here carries it.
    """
    datasource = {"type": "prometheus", "uid": metrics_uid}
    return {
        "id": 1,
        "type": "timeseries",
        "title": "Tank volume (litres)",
        "description": (
            "Tank 01 level over the selected period, as the automatic tank gauge "
            "reported it (hist.md §4.1). The fills and dispenses in the table "
            "below are the recorded explanation for its movements."
        ),
        "datasource": datasource,
        "gridPos": {"h": 9, "w": 24, "x": 0, "y": 0},
        "fieldConfig": {
            "defaults": {
                "custom": {
                    "drawStyle": "line",
                    "lineInterpolation": "linear",
                    "lineWidth": 1,
                    "fillOpacity": 5,
                    "showPoints": "never",
                    "spanNulls": False,
                    "axisLabel": "litres",
                },
                "decimals": 1,
                "min": 0,
            },
            "overrides": [],
        },
        "options": {
            "legend": {
                "displayMode": "list",
                "placement": "bottom",
                "showLegend": True,
            },
            "tooltip": {"mode": "single", "sort": "none"},
        },
        "targets": [
            {
                "refId": "A",
                "datasource": datasource,
                "editorMode": "code",
                "expr": "atg_tank_volume_litres",
                "legendFormat": "tank {{tank_id}}",
                "range": True,
            }
        ],
    }


def _bunkering_events_panel(logs_uid: str) -> dict:
    """The month's dispense and fill events in time order (hist.md §4.2, §7.1).

    `event_type:*` selects the records carrying that field, which is exactly
    §4.2's set: the §4.3 syslog records share the same VictoriaLogs instance and
    carry `msg_id` instead, and §7.1 keeps them off this dashboard.

    Three transformations, and their order makes a difference
    """
    datasource = {"type": _VICTORIALOGS_PLUGIN, "uid": logs_uid}
    return {
        "id": 2,
        "type": "table",
        "title": "Dispense and fill events",
        "description": (
            "Every job and transaction event in the selected period, oldest "
            "first (hist.md §4.2). This is what the digital record holds; the "
            "comparison against the Bunker Delivery Notes and delivery tickets "
            "is the reconciliation user's, and no panel here makes it."
        ),
        "datasource": datasource,
        "gridPos": {"h": 15, "w": 24, "x": 0, "y": 9},
        "fieldConfig": {
            "defaults": {"custom": {"align": "auto", "filterable": True}},
            "overrides": [],
        },
        "options": {
            "showHeader": True,
            "cellHeight": "sm",
            "footer": {"show": False},
        },
        "transformations": [
            {
                "id": "extractFields",
                "options": {"source": "labels", "replace": False, "keepTime": True},
            },
            {
                "id": "convertFieldType",
                "options": {
                    "conversions": [
                        {"targetField": field, "destinationType": "number"}
                        for field in _BUNKERING_TABLE_LITRE_FIELDS
                    ]
                },
            },
            {
                "id": "organize",
                "options": {
                    "excludeByName": {"Line": True, "id": True, "labels": True},
                    "renameByName": {},
                    # Column order. The timestamp is addressed as `Time`: the
                    # plugin renames `_time` to it when building the frame, so
                    # an entry for `_time` would match no column and leave the
                    # order to chance.
                    "indexByName": {
                        name: index
                        for index, name in enumerate(
                            ["Time"]
                            + [f for f in _BUNKERING_TABLE_FIELDS if f != "_time"]
                        )
                    },
                },
            },
        ],
        "targets": [
            {
                "refId": "A",
                "datasource": datasource,
                # "Raw Logs" in the query editor: the query type that returns
                # individual records rather than an aggregate.
                "queryType": "instant",
                "editorMode": "code",
                "expr": (
                    "event_type:*"
                    f" | fields {', '.join(_BUNKERING_TABLE_FIELDS)}"
                    " | sort by (_time)"
                ),
                "maxLines": 5000,
            }
        ],
    }


# ── hist-01: the operations dashboard (hist.md §7.1) ──────────────────────────
#
# The series below are the log server's own telemetry, collected by a
# VictoriaMetrics scrape. Their names come from the four pinned products
# rather than from hist.md, and were read off the deployed host's /metrics
# endpoints.
#
# test_hist_grafana_dashboards.py uses the same transcription and fails if a
# panel names a series absent from it. It cannot detect a product renaming a
# series across a §2.1 version bump: the panel and the transcription would be
# corrected together, agree with each other, and leave the panel rendering empty
# on the deployed host.
#
# Both agents mask their remote-write URLs, so the `url` label reads
# `1:secret-url` rather than an address. The ordinal is the position of the
# `-remoteWrite.url` flag, so 1 is the local store and 2 is the replica (§6,
# implemented in build-scripts/hist-01.sh.j2).
#
# The masking is retained rather than disabled with `-remoteWrite.showURL`.
# /metrics is served on the same port the FMS pushes to, which binds every address
# by necessity (§9.1), so the agents cannot expose one without the other:
# unmasking would publish the replica's address to every host in the
# operational-technology zone.
#
# The mapping below therefore encodes the flag order of a different file.
# Reordering those two lines would leave every replication panel reporting each
# queue as the other one — rendering normally, with real values and exchanged
# legends. test_the_remote_write_ordinals_match_the_flag_order reads the order out
# of the rendered build script and fails if it changes.
_REMOTE_WRITE_TARGETS = [
    ("1:secret-url", "local store"),
    ("2:secret-url", "hist-dmz-01 replica"),
]


def _timeseries_panel(
    panel_id: int,
    title: str,
    description: str,
    metrics_uid: str,
    grid: dict,
    unit: str,
    targets: list[tuple[str, str]],
) -> dict:
    """A timeseries panel over the metrics datasource, from (expression, legend)
    pairs. The operations panels differ only in their queries, so the shared shape
    is built once rather than repeated seven times."""
    datasource = {"type": "prometheus", "uid": metrics_uid}
    return {
        "id": panel_id,
        "type": "timeseries",
        "title": title,
        "description": description,
        "datasource": datasource,
        "gridPos": grid,
        "fieldConfig": {
            "defaults": {
                "custom": {
                    "drawStyle": "line",
                    "lineInterpolation": "linear",
                    "lineWidth": 1,
                    "fillOpacity": 0,
                    "showPoints": "never",
                    "spanNulls": False,
                },
                "unit": unit,
                "min": 0,
            },
            "overrides": [],
        },
        "options": {
            "legend": {
                "displayMode": "list",
                "placement": "bottom",
                "showLegend": True,
            },
            "tooltip": {"mode": "multi", "sort": "desc"},
        },
        "targets": [
            {
                "refId": chr(ord("A") + index),
                "datasource": datasource,
                "editorMode": "code",
                "expr": expr,
                "legendFormat": legend,
                "range": True,
            }
            for index, (expr, legend) in enumerate(targets)
        ],
    }


def _replication_panels(metrics_uid: str) -> list[dict]:
    """§7.1's replication panels: queue depth and push activity, for each of §6's
    two targets on each of the two agents.

    One panel per agent per quantity rather than one panel per quantity. The two
    agents' series would otherwise share a legend, in which two entries would both
    read "local store" and neither would say which agent it belonged to.

    §7.1 asks for "time since the last successful push", and no such metric
    exists. Both agents were checked on the deployed host: they expose
    `blocks_sent_total`, `requests_total`, `errors_total` and
    `send_duration_seconds_total`, all counters, and nothing carrying a
    last-success timestamp. The panel therefore plots the increase in blocks sent
    over the preceding five minutes, which answers whether pushes are happening;
    §7.1 is amended to that quantity rather than left specifying one that cannot be
    rendered.
    """
    panels = []
    quantities = [
        (
            "pending_data_bytes",
            "Remote-write queue depth",
            "bytes",
            'sum({metric}{{url="{url}"}})',
            "Data buffered on disk for each target (§6). A queue that grows and "
            "does not drain means the target is not accepting writes; the buffer "
            "is what keeps that from losing records.",
        ),
        (
            "blocks_sent_total",
            "Blocks sent",
            "short",
            'increase({metric}{{url="{url}"}}[5m])',
            "Blocks delivered to each target over the preceding five minutes. "
            "§7.1 asks for time since the last successful push; neither agent "
            "exposes a last-success timestamp, so this is the quantity that "
            "answers whether pushes are happening at all.",
        ),
    ]
    panel_id = 2
    for row, (suffix, title, unit, template, description) in enumerate(quantities):
        for column, (agent, carries) in enumerate(
            [("vmagent", "metrics"), ("vlagent", "logs")]
        ):
            panels.append(
                _timeseries_panel(
                    panel_id=panel_id,
                    title=f"{title} — {carries} ({agent})",
                    description=description,
                    metrics_uid=metrics_uid,
                    grid={"h": 8, "w": 12, "x": column * 12, "y": 8 + row * 8},
                    unit=unit,
                    targets=[
                        (
                            template.format(
                                metric=f"{agent}_remotewrite_{suffix}", url=url
                            ),
                            legend,
                        )
                        for url, legend in _REMOTE_WRITE_TARGETS
                    ],
                )
            )
            panel_id += 1
    return panels


def _operations_panels(metrics_uid: str) -> list[dict]:
    """The seven panels of `hist-01`'s operations dashboard, covering the three
    classes §7.1 names: ingestion rate, replication state, and store health."""
    return [
        _timeseries_panel(
            panel_id=1,
            title="Ingestion rate",
            description=(
                "Records entering the log server per second, by the path each "
                "arrives on (hist.md §5, §6.1). The two `fms-01` series are the "
                "ingestion this host exists for; the scrape series is this "
                "dashboard's own collection."
            ),
            metrics_uid=metrics_uid,
            grid={"h": 8, "w": 24, "x": 0, "y": 0},
            unit="cps",
            targets=[
                (
                    'rate(vmagent_rows_inserted_total{type="promremotewrite"}[5m])',
                    "metrics from fms-01",
                ),
                (
                    'rate(vl_rows_ingested_total{type="syslog_tcp"}[5m])',
                    "syslog from fms-01",
                ),
                (
                    'rate(vl_rows_ingested_total{type="nativeinsert"}[5m])',
                    "events written into victoria-logs",
                ),
                (
                    'rate(vm_rows_inserted_total{type="promscrape"}[5m])',
                    "self-monitoring scrape",
                ),
            ],
        ),
        *_replication_panels(metrics_uid),
        _timeseries_panel(
            panel_id=6,
            title="Store disk usage",
            description=(
                "What each store holds against the space left to it. §8 retains "
                "three years, and §6.1's self-monitoring series are the bulk of "
                "what accumulates here."
            ),
            metrics_uid=metrics_uid,
            grid={"h": 8, "w": 12, "x": 0, "y": 24},
            unit="bytes",
            # Summed rather than plotted raw. Both stores break their stored size
            # down by internal part — in-memory, small and big merge parts, and the
            # index — so the bare series is several lines that would all take the
            # same legend and be indistinguishable. The total is the quantity the
            # panel is about, and the sum is the identity for the free-space series
            # that carry one value each.
            targets=[
                ("sum(vm_data_size_bytes)", "victoria-metrics — stored"),
                ("sum(vm_free_disk_space_bytes)", "victoria-metrics — free"),
                ("sum(vl_data_size_bytes)", "victoria-logs — stored"),
                ("sum(vl_free_disk_space_bytes)", "victoria-logs — free"),
            ],
        ),
        _timeseries_panel(
            panel_id=7,
            title="Component uptime",
            description=(
                "Time since each of §9.1's four components last started. All four "
                "expose the same series name, separated by the `job` label §6.1's "
                "scrape configuration sets. A line dropping to zero is a restart."
            ),
            metrics_uid=metrics_uid,
            grid={"h": 8, "w": 12, "x": 12, "y": 24},
            unit="s",
            targets=[("vm_app_uptime_seconds", "{{job}}")],
        ),
    ]


# The panel set each host's dashboard carries, and the two properties that differ
# with it: the period a reader arrives on, and whether the view refreshes itself.
_DASHBOARD_PANELS = {
    "hist-dmz-01": (
        lambda uids: [
            _bunkering_tank_volume_panel(uids["VictoriaMetrics"]),
            _bunkering_events_panel(uids["VictoriaLogs"]),
        ],
        # The rolling 30 days rather than the previous calendar month. The audit is
        # a month-end task (narrative.md:161) and the user selects the period, but
        # a calendar-month default renders empty for most of the month, which is
        # indistinguishable from a dashboard that is not working.
        {"from": "now-30d", "to": "now"},
        # No refresh: the month's records do not change while being read.
        "",
    ),
    "hist-01": (
        lambda uids: _operations_panels(uids["VictoriaMetrics"]),
        # Six hours, and refreshing. This is the view an administrator opens
        # because something is wrong now (§7), so it opens on the recent past and
        # keeps up with the host while being watched.
        {"from": "now-6h", "to": "now"},
        "1m",
    ),
}


def build_grafana_dashboard(node: str) -> str:
    """The dashboard provisioned to `node` (hist.md §7.1).

    Bound to its host's datasource uids, which differ per host so that a
    dashboard provisioned to the wrong log server fails to resolve them rather
    than rendering the other instance's data without complaint.

    The panel set, the period a reader arrives on and the refresh interval come
    from _DASHBOARD_PANELS, which records why each host's differ.
    """
    meta = DASHBOARDS[node]
    uids = DATASOURCE_UIDS[node]
    panels, default_range, refresh = _DASHBOARD_PANELS[node]
    return (
        json.dumps(
            {
                "uid": meta["uid"],
                "title": meta["title"],
                "tags": ["ravensport", meta["tag"]],
                # UTC rather than the browser's zone: the retained record is the
                # evidence for a MARPOL-period audit (§8), and a reader in a
                # different zone must not see different timestamps from the same row.
                # Applied to both hosts so that the administrator troubleshooting an
                # ingestion gap and the auditor reading the records it left behind are
                # looking at the same clock.
                "timezone": "utc",
                "editable": True,
                "schemaVersion": 41,
                "version": 1,
                "refresh": refresh,
                "time": default_range,
                "timepicker": {},
                "annotations": {"list": []},
                "templating": {"list": []},
                "panels": panels(uids),
            },
            indent=2,
            # The section signs in the panel descriptions stay as themselves rather
            # than § escapes, so the file reads in review as it does in Grafana.
            ensure_ascii=False,
        )
        + "\n"
    )


def build_sim_constants(world: World) -> str:
    op_cards = [op.rfid_card for op in world.identities.operators]
    # The sim models a single operator (build.rs consumes the first card) and
    # every tug on the roster. The operator's username and password are
    # presented at the HMI login; the reconciliation user's three credentials at
    # the workstation and the log server; each tug's name and IMO number are
    # written onto the bunker delivery note.
    operator = world.identities.operators[0]
    recon = world.identities.reconciliation_user
    # Each value a compressed scenario scales is projected already scaled, so
    # the sim holds no compression factor of its own (sim.md §2.2).
    factor = world.compression_factor
    intervals = world.intervals
    systems_administrator = world.identities.systems_administrator
    # build.rs reads this TOML with a line scan, which strips a string's outer
    # quotes without a parser, so no string value may contain a double quote;
    # reject that here rather than emit a value the sim build then mis-parses.
    # The same scan ignores sections and returns the first line beginning with
    # a key, so every key below is unique across the whole file
    # (deployment-config.md §4.1) — hence recon_username, not username.
    strings = {
        "operator username": operator.username,
        "operator password": operator.password,
        "reconciliation user username": recon.username,
        "reconciliation user password": recon.password,
        "reconciliation user workstation_password": recon.workstation_password,
        "systems administrator username": systems_administrator.username,
        "systems administrator workstation password": world.identities.admin.password,
        "process grade": world.process.grade,
    }
    for n, vessel in enumerate(world.roster.vessels, start=1):
        strings[f"vessel {n} name"] = vessel.name
    for label, value in strings.items():
        if '"' in value:
            raise ValueError(
                f"{label} {value!r} contains a double quote, which the "
                f"sim-constants.toml line scan cannot represent"
            )
    # The roster as numbered keys (deployment-config.md §4.1): vessel_count,
    # then each tug's IMO number, name, card and mean time away, numbered in the
    # order of roster.vessels. A compressed scenario divides each mean time away
    # by the compression factor (sim.md §2.2). The IMO number is what the dock operator enters
    # on the job and what the FMS matches against the vessel card's label; the
    # BDN carries both the IMO number and the name (MARPOL Annex VI Appendix V).
    vessel_lines = [f"vessel_count = {len(world.roster.vessels)}"]
    for n, vessel in enumerate(world.roster.vessels, start=1):
        vessel_lines += [
            f"vessel_{n}_imo_number = {vessel.imo_number}",
            f'vessel_{n}_name = "{vessel.name}"',
            f"vessel_{n}_rfid_card = {vessel.rfid_card}",
            f"vessel_{n}_mean_time_away_s = {vessel.mean_time_away_s / factor}",
        ]
    _, admin_host_public = derive_openssh_ed25519_keypair(
        world.crypto.password_salt_seed,
        "admin-01",
        "host",
        "admin-01",
    )
    admin_host_key = " ".join(admin_host_public.split()[:2])
    return (
        "\n".join(
            [
                "# Generated by render.py — do not edit.",
                "# Consumed at build time by infra/sim/build.rs, which compiles these",
                "# constants into the sim binary. Committed at infra/sim/sim-constants.toml",
                "# so the crate builds standalone; kept in step with world.yaml by",
                "# tests/unit/test_sim_constants_agreement.py.",
                "",
                "[process]",
                f"tank_capacity_l = {world.process.tank_capacity_l}",
                f"k_factor = {world.process.k_factor}",
                f'grade = "{world.process.grade}"',
                f"density_kg_per_l = {world.process.density_kg_per_l}",
                f"sulphur_pct_mm = {world.process.sulphur_pct_mm}",
                f"flashpoint_c = {world.process.flashpoint_c}",
                f"overfill_interlock_pct = {world.process.overfill_interlock_pct}",
                "outflow_rate_l_per_min = "
                f"{scaled(world, 'process.dispenser_flow_rate_l_per_min')}",
                "",
                # The supplier's ordering and the road tanker's pump rate, under
                # the sim's names (deployment-config.md §4.1).
                "[fuel_supplier]",
                f"reorder_threshold_l = {world.fuel_supplier.reorder_threshold_l}",
                f"delivery_volume_l = {world.fuel_supplier.delivery_volume_l}",
                f"lead_time_s = {scaled(world, 'fuel_supplier.lead_time_s')}",
                "inflow_rate_l_per_min = "
                f"{scaled(world, 'fuel_supplier.tanker_pump_rate_l_per_min')}",
                "supplier_poll_interval_s = "
                f"{scaled(world, 'fuel_supplier.poll_interval_s')}",
                "",
                "[rfid]",
                f"operator_cards = {op_cards}",
                "",
                "[operator]",
                f'username = "{operator.username}"',
                f'password = "{operator.password}"',
                "",
                "[roster]",
                f"refuel_volume_min_l = {world.roster.refuel_volume_min_l}",
                f"refuel_volume_max_l = {world.roster.refuel_volume_max_l}",
                "",
                "[vessels]",
                *vessel_lines,
                "",
                "[intervals]",
                f"session_timeout_s = {scaled(world, 'intervals.session_timeout_s')}",
                f"dock_operator_poll_s = {intervals.dock_operator_poll_s / factor}",
                f"admin_check_interval_s = {intervals.admin_check_interval_h * 3600 / factor}",
                "",
                "[audit]",
                f'audit_period = "{scaled(world, "audit.period")}"',
                f"settling_delay_s = {scaled(world, 'audit.settling_delay_s')}",
                f"boundary_margin_s = {world.audit.boundary_margin_s / factor}",
                "",
                "[reconciliation_user]",
                f'recon_username = "{recon.username}"',
                f'recon_password = "{recon.password}"',
                f'recon_workstation_password = "{recon.workstation_password}"',
                "",
                "[systems_administrator]",
                f'systems_administrator_username = "{systems_administrator.username}"',
                f'systems_administrator_password = "{world.identities.admin.password}"',
                f'admin_host_key = "{admin_host_key}"',
            ]
        )
        + "\n"
    )


# ── Compressed scenarios (deployment-config.md §4) ───────────────────────────

# A calendar audit period in operation, in hours (deployment-config.md §4).
AUDIT_PERIOD_HOURS = {"month": 720, "week": 168, "day": 24}


def scale(world: World, value: Any, how: str | None) -> Any:
    """``value`` as render.py projects it at the compression factor: an
    interval or duration divided by it, in whole seconds where its consumer
    reads whole seconds; a rate multiplied by it; a calendar audit period
    converted to hours and divided by it. ``how`` is the source's entry in
    PROJECTION_SCALING; a source with none is projected unchanged. A
    compression factor of 1 projects every value in operation, and keeps a
    calendar audit period."""
    factor = world.compression_factor
    if how is None:
        return value
    if how == "whole_seconds":
        # World.at_compression_factor refuses a compression factor that leaves
        # a remainder.
        return value // factor
    if how == "seconds":
        return value / factor
    if how == "rate":
        return value * factor
    if how == "hours":
        if factor == 1:
            return value
        return f"{AUDIT_PERIOD_HOURS[value] / factor:.12g}h"
    raise ValueError(f"unknown scaling {how!r}")


def scaled(world: World, dotted: str) -> Any:
    """The projected value of the world.yaml source ``dotted``."""
    return scale(world, _resolve_source(world, dotted), PROJECTION_SCALING.get(dotted))


def siem_schedules(world: World) -> dict[str, int]:
    """The agents' three schedules in whole seconds at the compression factor:
    the file integrity scan, the inventory scan and the listening-port report
    (siem.md §2.1)."""
    factor = world.compression_factor
    intervals = world.intervals
    return {
        "file_integrity_s": intervals.file_integrity_interval_h * 3600 // factor,
        "inventory_s": intervals.inventory_interval_h * 3600 // factor,
        "listening_port_s": intervals.listening_port_interval_s // factor,
    }


# ── Projection-mapping integrity ──────────────────────────────────────────────


def _resolve_source(world: World, dotted: str) -> Any:
    """Resolve a PROJECTION_MAP dotted source path against the validated World."""
    cur: Any = world
    for part in dotted.split("."):
        if not hasattr(cur, part):
            raise AttributeError(dotted)
        cur = getattr(cur, part)
    return cur


def _artefact_pairs(text: str) -> dict[str, str]:
    """Parse the ``key=value`` / ``key = value`` lines of a rendered artefact."""
    pairs: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        pairs[key.strip()] = value.strip()
    return pairs


def check_projection_map(world: World, artefacts: dict[str, str]) -> None:
    """Enforce the declared source-to-destination map before any file is written.

    For every mapping in ``world_schema.PROJECTION_MAP`` this asserts that the
    source path resolves in the validated ``world`` and that the consumer
    artefact carries the destination key with the source's canonical typed value
    (``str(value)`` — the form the emitters produce). A renamed key on either
    side, a diverged value, or a projection that changes the value's type (an
    ``int()`` cast on a forced-float constant) therefore aborts the render before
    anything is emitted. ``artefacts`` maps a consumer id to its rendered text; a
    consumer absent from the dict (not present in this profile) still has its
    source resolved, but its destination keys are not asserted against a text.
    """
    parsed = {consumer: _artefact_pairs(text) for consumer, text in artefacts.items()}
    errors: list[str] = []
    for src, dests in PROJECTION_MAP.items():
        try:
            source = _resolve_source(world, src)
        except AttributeError:
            errors.append(
                f"PROJECTION_MAP source {src!r} does not resolve in world.yaml"
            )
            continue
        projected = scale(world, source, PROJECTION_SCALING.get(src))
        expected = str(projected)
        # A string is written bare in an env file and quoted in TOML; either
        # form carries the same value.
        accepted = (
            {expected, f'"{expected}"'} if isinstance(projected, str) else {expected}
        )
        for consumer, key in dests.items():
            pairs = parsed.get(consumer)
            if pairs is None:
                continue
            if key not in pairs:
                errors.append(
                    f"{src} -> {consumer}:{key} not found in the rendered "
                    f"{consumer} artefact"
                )
            elif pairs[key] not in accepted:
                errors.append(
                    f"{src} -> {consumer}:{key}={pairs[key]!r} but world.yaml "
                    f"declares {expected!r} at compression factor "
                    f"{world.compression_factor}"
                )
    if errors:
        sys.exit(
            "world.yaml projection-mapping check failed:\n  " + "\n  ".join(errors)
        )


# ── Build script + incus commands rendering ───────────────────────────────────


def render_build_scripts(t: dict, world: World) -> None:
    env = Environment(
        loader=FileSystemLoader(t.get("_template_dirs", [str(BUILD_SCRIPTS_DIR)])),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    out_dir = BUILD / "node-build"
    out_dir.mkdir(parents=True, exist_ok=True)
    administrator_password_hash = compute_sha512_crypt_hash(
        world.identities.admin.password,
        world.crypto.password_salt_seed,
        world.identities.systems_administrator.username,
    )

    for name, node in t["nodes"].items():
        bs = node.get("build_script")
        if not bs:
            continue
        template_name = f"{bs}.sh.j2"
        try:
            template = env.get_template(template_name)
        except Exception as e:
            sys.exit(f"{name}: cannot load build-script template {template_name}: {e}")
        ctx = {
            "node_name": name,
            "node": node,
            "topology": t,
            # The validated world facts, so a build script can project a
            # credential (e.g. corp-it's slapd rootDN password) from the single
            # source rather than hard-coding it.
            "world": world,
            "profile": t["_profile"],
            "image_name": node_image(name, node),
            # The flow timeouts that an additional override file sets, which
            # the firewall nodes' build script writes into Suricata's
            # configuration; None when no file sets them (deployment.md §7.3).
            "flow_timeouts": t.get("_flow_timeouts"),
            # None for a node that carries no provisioned dashboard (hist.md §7.1).
            "dashboard": DASHBOARDS.get(bs),
            # The directory the provider reads, so the push and the provider
            # cannot name different ones.
            "dashboard_dir": GRAFANA_DASHBOARD_DIR,
            # Likewise for the scrape configuration: the unit's flag and the
            # build's push are rendered from one value (hist.md §7.1).
            "scrape_config_path": SCRAPE_CONFIG_PATH,
            "scrape_config_dir": str(PurePosixPath(SCRAPE_CONFIG_PATH).parent),
            # The security monitoring's three schedules, in seconds, scaled by
            # the compression factor (siem.md §2.1, §8.5).
            "siem_schedules": siem_schedules(world),
            "systems_administrator": world.identities.systems_administrator,
            "systems_administrator_uid": systems_administrator_uid(world),
            "systems_administrator_password_hash": administrator_password_hash,
        }
        rendered = template.render(**ctx)
        out_path = out_dir / f"{name}.sh"
        out_path.write_text(rendered)
        out_path.chmod(0o755)


def emit_incus_commands(t: dict) -> str:
    s: list[str] = ["#!/usr/bin/env bash", "set -euo pipefail", ""]
    s.append(f'PROFILE="{t["_profile"]}"')
    s.append('echo "Deploying with profile: $PROFILE"')
    s.append("")

    # Pre-flight
    s.append("# --- Pre-flight ---")
    s.append("if ! command -v dnsmasq >/dev/null 2>&1; then")
    s.append('  echo "ERROR: dnsmasq binary not found." >&2')
    s.append('  echo "Incus requires it to create managed bridges." >&2')
    s.append('  echo "Install with: sudo apt install -y dnsmasq-base" >&2')
    s.append("  exit 1")
    s.append("fi")
    s.append("maxkeys=$(cat /proc/sys/kernel/keys/maxkeys 2>/dev/null || echo 0)")
    s.append('if [ "$maxkeys" -lt 2000 ]; then')
    s.append('  echo "ERROR: kernel.keys.maxkeys is $maxkeys (need >= 2000)." >&2')
    s.append('  echo "Fix: sudo sysctl kernel.keys.maxkeys=2000" >&2')
    s.append("  exit 1")
    s.append("fi")
    s.append("")

    # Networks (no uplink — all bridges are isolated zone bridges)
    s.append("# --- Networks ---")
    for name, net in t["networks"].items():
        nat = net.get("nat", False)
        if nat:
            addr = f"ipv4.address={net['gateway']}/{net['cidr'].split('/')[1]}"
        else:
            addr = "ipv4.address=none"
        nat_flag = f"ipv4.nat={'true' if nat else 'false'}"
        s.append(
            f"incus network show {name} >/dev/null 2>&1 || "
            f"incus network create {name} --type=bridge "
            f"{addr} "
            f"ipv4.dhcp=false {nat_flag} ipv6.address=none dns.mode=none"
        )
    s.append("")

    # Instances — launch from pre-built images
    s.append("# --- Instances ---")
    for name, node in t["nodes"].items():
        kind = "--vm " if node["type"] == "vm" else ""
        deploy_image = node_image(name, node)
        s.append(f"# {name}  ({node['type']}, image: {deploy_image})")
        s.append(f"incus init {kind}{deploy_image} {name}")

        if "cpu" in node:
            s.append(f"incus config set {name} limits.cpu={node['cpu']}")
        if "memory" in node:
            s.append(f"incus config set {name} limits.memory={node['memory']}")

        s.append(f"incus config device remove {name} eth0 2>/dev/null || true")
        for i, nic in enumerate(node["nics"]):
            iface = nic.get("name", f"eth{i}")
            nictype = nic.get("nic_type", "bridged")
            s.append(
                f"incus config device add {name} {iface} nic "
                f"nictype={nictype} parent={nic['network']} name={iface}"
            )

        if node.get("readonly_root") and node["type"] == "container":
            s.append(f"incus config device override {name} root readonly=true")
            s.append(f"incus config set {name} raw.lxc - <<'RAWLXC'")
            s.append(
                "lxc.mount.entry = tmpfs run tmpfs "
                "rw,nosuid,nodev,mode=0755,create=dir 0 0"
            )
            s.append(
                "lxc.mount.entry = tmpfs tmp tmpfs "
                "rw,nosuid,nodev,mode=1777,create=dir 0 0"
            )
            s.append(
                # mode=1777 (as /tmp): a service runs as its own non-root user and
                # supervise-daemon opens that service's log file as that user, so
                # the log directory must be writable by unprivileged users or the
                # daemon fails to start. The sticky bit keeps each user's log its own.
                "lxc.mount.entry = tmpfs var/log tmpfs "
                "rw,nosuid,nodev,mode=1777,create=dir 0 0"
            )
            s.append("RAWLXC")
        s.append("")

    # Start all nodes together (no ordering dependency without cloud-init)
    s.append("# --- Start ---")
    for name in t["nodes"]:
        s.append(f"incus start {name}")

    return "\n".join(s) + "\n"


def emit_teardown(t: dict) -> str:
    s: list[str] = ["#!/usr/bin/env bash", "set -u", ""]
    for name in t["nodes"]:
        s.append(f"incus delete --force {name} 2>/dev/null || true")
    for name in t["networks"]:
        s.append(f"incus network delete {name} 2>/dev/null || true")
    return "\n".join(s) + "\n"


# ── Main ──────────────────────────────────────────────────────────────────────


def main() -> None:
    ap = argparse.ArgumentParser()
    # Profiles are discovered from the overrides directory, so adding an
    # overrides/<name>.yaml is all it takes to define a new profile.
    profiles = sorted(p.stem for p in (ROOT / "overrides").glob("*.yaml"))
    ap.add_argument("--profile", required=True, choices=profiles)
    ap.add_argument("--topology", default=str(ROOT / "topology.yaml"))
    ap.add_argument("--world", default=str(ROOT / "world.yaml"))
    ap.add_argument(
        "--extra-override",
        help="an additional override file, applied after the profile's, which "
        "may also set the compression factor (deployment.md §7.3)",
    )
    args = ap.parse_args()

    topology = load_yaml(Path(args.topology))
    override = load_yaml(ROOT / "overrides" / f"{args.profile}.yaml")
    merged = merge_topology(topology, override)
    template_dirs = [BUILD_SCRIPTS_DIR]
    extra: dict = {}
    if args.extra_override:
        extra_path = Path(args.extra_override).resolve()
        extra = load_yaml(extra_path)
        merged = merge_topology(merged, extra)
        # A template directory is named relative to its override file.
        if extra.get("template_dir"):
            template_dirs.append(extra_path.parent / extra["template_dir"])
    merged["_template_dirs"] = [str(d) for d in template_dirs]
    validate(merged)
    merged["_flow_timeouts"] = flow_timeouts_from_override(
        override, extra, merged["nodes"]
    )

    # A profile may carry a `world:` section overriding world.yaml facts (e.g. a
    # compressed session timeout for attack testing); it is deep-merged before
    # validation so realistic profiles keep world.yaml's values unchanged. The
    # additional override file's compression factor then scales the result
    # (deployment.md §7.3).
    world = world_at_override_factor(
        load_world(Path(args.world), override.get("world")), override, extra
    )

    # Projection-mapping integrity: assert the declared source-to-destination
    # map holds — each shared world fact reaches every consumer artefact under
    # its declared key with its canonical type — before anything is written.
    projection_artefacts: dict[str, str] = {"sim": build_sim_constants(world)}
    for node_name, node in merged["nodes"].items():
        bs = node.get("build_script")
        if bs == "fms-01":
            projection_artefacts["fms_env"] = build_fms_env(node, merged, world)
        elif bs == "hmi-01":
            projection_artefacts["hmi_env"] = build_hmi_env(node, merged, world)
    check_projection_map(world, projection_artefacts)

    # "ca" and "tls" are owned by build-ca.sh (via the deterministic key
    # material this step writes into them), not by this wipe-and-regenerate
    # cycle (digital.md §8 decision 7) — preserved across a render rather than
    # wiped, so a render run never discards the CA root or the certificates
    # build-ca.sh signed from it.
    if BUILD.exists():
        for entry in BUILD.iterdir():
            if entry.name in {"ca", "tls"}:
                continue
            if entry.is_dir():
                shutil.rmtree(entry)
            else:
                entry.unlink()
    else:
        BUILD.mkdir()

    # Deterministic CA/leaf private key material (digital.md §2.7, §8 decision
    # 7): derived from crypto.password_salt_seed the same way every other
    # secret in the pipeline is (deployment-config.md §6), so it is cheap and
    # side-effect-free to (re)write unconditionally on every render. The
    # X.509 structure — the actual certificates — is built from this material
    # by build-ca.sh, which owns its own, separately cached, regeneration.
    #
    # Imported here, not at module level: this is render.py's only use of
    # pycryptodome, and other tooling imports individual functions from this
    # module (e.g. compute_pbkdf2_grafana_hash, from a separate virtualenv
    # under tests/integration/ that has no reason to carry a CA-generation
    # dependency) without ever calling main(). A module-level import would
    # make pycryptodome a hard dependency of importing this file at all.
    import ca_provisioning

    ca_provisioning.write_key_material(
        world.crypto.password_salt_seed, world.crypto.ca_not_before, BUILD
    )

    # Per-node /etc/hosts files: loopback + own entry + resolve: list only.
    # Each node carries the minimum set of name-to-IP mappings it requires.
    all_entries = _all_host_entries(merged)
    hosts_dir = BUILD / "hosts"
    hosts_dir.mkdir(exist_ok=True)
    for name, node in merged["nodes"].items():
        if node.get("build_script"):
            (hosts_dir / name).write_text(build_node_hosts(name, node, all_entries))

    # network-config for all Ubuntu nodes (those with a build_script)
    netcfg_dir = BUILD / "network-config"
    netcfg_dir.mkdir(exist_ok=True)
    for name, node in merged["nodes"].items():
        if node.get("build_script"):
            (netcfg_dir / f"{name}.yaml").write_text(build_network_config(node, merged))

    # nftables configs: the three firewall nodes' filter table, and the
    # siem_log connection-logging table every monitored host carries (§4.1).
    nft_dir = BUILD / "nftables"
    nft_dir.mkdir(exist_ok=True)
    for name, node in merged["nodes"].items():
        if name in SIEM_MONITORED_NODES:
            (nft_dir / f"{name}.nft").write_text(build_nftables_conf(node, merged))
    # chrony configs
    chrony_dir = BUILD / "chrony"
    chrony_dir.mkdir(exist_ok=True)
    for name, node in merged["nodes"].items():
        if node.get("build_script") and node["build_script"] != "stub":
            (chrony_dir / f"{name}.conf").write_text(
                build_chrony_conf(name, node, merged)
            )

    # haproxy configs
    haproxy_dir = BUILD / "haproxy"
    haproxy_dir.mkdir(exist_ok=True)
    (haproxy_dir / "atg-relay-01.cfg").write_text(build_haproxy_atg_cfg(merged))
    (haproxy_dir / "sim-host.cfg").write_text(build_haproxy_sim_cfg(merged))

    # env files
    env_dir = BUILD / "env"
    env_dir.mkdir(exist_ok=True)
    for name, node in merged["nodes"].items():
        bs = node.get("build_script")
        if bs == "fms-01":
            (env_dir / f"{name}.env").write_text(build_fms_env(node, merged, world))
        elif bs == "hmi-01":
            (env_dir / f"{name}.env").write_text(build_hmi_env(node, merged, world))

    # Suricata AF_PACKET interface list, one per firewall node (siem.md §8.4).
    suricata_dir = BUILD / "suricata"
    suricata_dir.mkdir(exist_ok=True)
    for name, node in merged["nodes"].items():
        if node.get("build_script") == "fw-zone":
            (suricata_dir / f"{name}.env").write_text(
                build_suricata_interfaces_env(node)
            )

    # LDIF
    ldif_dir = BUILD / "ldif"
    ldif_dir.mkdir(exist_ok=True)
    (ldif_dir / "corp-it.ldif").write_text(build_ldif(world))

    # recon-01 is the only workstation directory client. Remove configurations
    # from an earlier render before emitting the current profile so the move of
    # admin-01 to a local account cannot leave a stale SSSD input behind.
    sssd_dir = BUILD / "sssd"
    sssd_dir.mkdir(exist_ok=True)
    for stale in sssd_dir.glob("*.conf"):
        stale.unlink()
    if "recon-01" in merged["nodes"]:
        (sssd_dir / "recon-01.conf").write_text(build_sssd_conf(world))

    # Deterministic SSH identity and host keys. Private material is written only
    # beneath the node directory that consumes it; admin-01 alone receives the
    # systems administrator's private key.
    ssh_dir = BUILD / "ssh"
    ssh_dir.mkdir(exist_ok=True)
    for relative_path, contents in build_ssh_artifacts(world).items():
        path = ssh_dir / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents)
        if path.name in {"id_ed25519", "ssh_host_ed25519_key"}:
            path.chmod(0o600)

    # Grafana seed and provisioning, one set per log server (hist.md §7, §9).
    grafana_dir = BUILD / "grafana"
    grafana_dir.mkdir(exist_ok=True)
    for name, node in merged["nodes"].items():
        bs = node.get("build_script")
        if bs not in ("hist-01", "hist-dmz-01"):
            continue
        (grafana_dir / f"{name}-user-seed.sql").write_text(
            build_grafana_user_seed(bs, world)
        )
        (grafana_dir / f"{name}-grafana.ini").write_text(build_grafana_ini())
        (grafana_dir / f"{name}-datasources.yaml").write_text(
            build_grafana_datasources(bs)
        )
        # hist-01 scrapes its own stack so that §7.1's operations dashboard has
        # something to draw; hist-dmz-01 runs neither agent and carries the
        # bunkering dashboard instead, so it scrapes nothing.
        if bs == "hist-01":
            scrape_dir = BUILD / "victoria-metrics"
            scrape_dir.mkdir(exist_ok=True)
            (scrape_dir / f"{name}-scrape.yml").write_text(
                build_victoriametrics_scrape_config()
            )
        # The provisioned dashboard, for the hosts that carry one (hist.md §7.1).
        if bs in DASHBOARDS:
            (grafana_dir / f"{name}-dashboards.yaml").write_text(
                build_grafana_dashboard_provider()
            )
            (grafana_dir / f"{name}-dashboard.json").write_text(
                build_grafana_dashboard(bs)
            )

    # Security monitoring agent keys and management credential (siem.md §2.1,
    # §8.1, §8.5; deployment-config.md §6). One pre-shared key per monitored
    # node, derived from crypto.password_salt_seed and the node name; siem-01
    # receives the complete set (client.keys), each monitored node receives
    # only its own (<node>.key).
    if "siem-01" in merged["nodes"]:
        siem_dir = BUILD / "siem"
        siem_dir.mkdir(exist_ok=True)
        seed = world.crypto.password_salt_seed
        client_keys_lines = []
        for i, node_name in enumerate(SIEM_MONITORED_NODES, start=1):
            if node_name not in merged["nodes"]:
                continue
            key = derive_agent_key(seed, node_name)
            line = f"{i:03d} {node_name} any {key}"
            client_keys_lines.append(line)
            (siem_dir / f"{node_name}.key").write_text(line + "\n")
        (siem_dir / "client.keys").write_text("\n".join(client_keys_lines) + "\n")
        (siem_dir / "siem-01-admin-seed.sql").write_text(build_wazuh_admin_seed(world))

    # sim-constants.toml. Written at the values in operation, on every render,
    # to the committed crate input infra/sim/sim-constants.toml, which build.rs
    # compiles into the sim binary (the crate must build without a prior
    # render, so its input is version-controlled). Regenerate and commit the
    # crate copy whenever world.yaml changes; test_sim_constants_agreement.py
    # fails if it drifts. A render at another compression factor also writes
    # the constants it scales to build/sim/sim-constants.toml, against which
    # the build of the competition image of sim-host compiles the simulator
    # through SIM_CONSTANTS (deployment-config.md §4.1, sim projection).
    (ROOT.parent / "infra" / "sim" / "sim-constants.toml").write_text(
        build_sim_constants(world.at_compression_factor(1))
    )
    if world.compression_factor != 1:
        sim_dir = BUILD / "sim"
        sim_dir.mkdir(exist_ok=True)
        (sim_dir / "sim-constants.toml").write_text(build_sim_constants(world))

    # build scripts
    render_build_scripts(merged, world)

    # incus-commands.sh and teardown
    (BUILD / "incus-commands.sh").write_text(emit_incus_commands(merged))
    (BUILD / "incus-commands.sh").chmod(0o755)
    (BUILD / "teardown-commands.sh").write_text(emit_teardown(merged))
    (BUILD / "teardown-commands.sh").chmod(0o755)

    # manifest
    (BUILD / "manifest.json").write_text(
        json.dumps(
            {
                "profile": merged["_profile"],
                "nodes": list(merged["nodes"].keys()),
                "networks": list(merged["networks"].keys()),
                "build_script_nodes": [
                    name
                    for name, node in merged["nodes"].items()
                    if node.get("build_script")
                ],
                "images": {
                    name: node_image(name, node)
                    for name, node in merged["nodes"].items()
                },
            },
            indent=2,
        )
    )

    # The inputs of this render, kept beside its outputs so that a snapshot
    # set records how the deployment it was taken from was rendered
    # (deployment.md §10.3).
    inputs_dir = BUILD / "inputs"
    (inputs_dir / "overrides").mkdir(parents=True)
    shutil.copyfile(args.world, inputs_dir / "world.yaml")
    shutil.copyfile(args.topology, inputs_dir / "topology.yaml")
    shutil.copyfile(
        ROOT / "overrides" / f"{args.profile}.yaml",
        inputs_dir / "overrides" / f"{args.profile}.yaml",
    )

    print(f"Rendered {len(merged['nodes'])} nodes for profile '{args.profile}'")
    print(f"  → {BUILD}/")


if __name__ == "__main__":
    main()
