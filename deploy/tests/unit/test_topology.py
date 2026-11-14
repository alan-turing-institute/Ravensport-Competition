"""Renderer unit tests. Run with: pytest tests/unit -q"""

import json
import subprocess
import sys
import warnings
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_deploy_is_docker_free():
    """The deployment is Docker-free: build-sim-image.sh builds the sim-host
    image with Incus, and lima.yaml provisions no Docker. Guards against silent
    reintroduction — dockerd's FORWARD-policy changes break Incus egress.

    The image leaves no Docker trace at all (the rationale lives in git history,
    not in the files), so any mention — including a comment — fails the check.
    """
    for rel in ("lima.yaml", "scripts/build-sim-image.sh"):
        offenders = [
            line
            for line in (ROOT / rel).read_text().splitlines()
            if "docker" in line.lower()
        ]
        assert not offenders, f"{rel} references Docker in: {offenders}"


def run_render(profile: str) -> Path:
    out = subprocess.run(
        [sys.executable, "scripts/render.py", "--profile", profile],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert out.returncode == 0, out.stderr
    return ROOT / "build"


def test_adversary_it_address_does_not_collide_with_admin():
    """The adversary activates its IT address only while a stage runs, but it
    still shares vnet-it with the always-addressed administration workstation."""
    topology = yaml.safe_load((ROOT / "topology.yaml").read_text())
    adversary_it = next(
        nic
        for nic in topology["nodes"]["adversary"]["nics"]
        if nic["network"] == "vnet-it"
    )
    admin_it = topology["nodes"]["admin-01"]["nics"][0]

    assert adversary_it["ipv4"] == "10.30.0.50"
    assert admin_it["ipv4"] == "10.30.0.40"
    assert adversary_it["ipv4"] != admin_it["ipv4"]


def test_full_renders():
    build = run_render("full")
    manifest = json.loads((build / "manifest.json").read_text())
    assert "fms-01" in manifest["nodes"]
    assert "vnet-ot" in manifest["networks"]
    assert manifest["profile"] == "full"


def test_full_uses_vms():
    build = run_render("full")
    cmds = (build / "incus-commands.sh").read_text()
    assert "incus init --vm" in cmds


def test_ci_minimal_forces_container():
    build = run_render("ci-minimal")
    cmds = (build / "incus-commands.sh").read_text()
    assert "incus init --vm" not in cmds
    # The RFID readers are networked TCP services, so no node hosts a serial
    # link; the retired raw.qemu/pci-serial machinery must not reappear.
    assert "raw.qemu" not in cmds
    assert "pci-serial" not in cmds


def test_zone_bridges_are_isolated():
    """The uplink bridge has been removed. No zone bridge should have NAT or a
    host IP — all bridges are isolated (the host has no address on the subnet)."""
    build = run_render("full")
    cmds = (build / "incus-commands.sh").read_text()
    for line in cmds.splitlines():
        if "incus network create" not in line:
            continue
        assert "ipv4.nat=false" in line, f"bridge has nat=true: {line}"
        assert "ipv4.address=none" in line, f"bridge has a host IP: {line}"


def test_static_ip_via_network_config():
    """Per-node network-config files are generated for all nodes that have a
    build script. These are consumed by the per-node image build scripts.
    Bridges have DHCP disabled — IPs are baked into the images."""
    build = run_render("full")
    cmds = (build / "incus-commands.sh").read_text()
    assert "ipv4.dhcp=false" in cmds

    manifest = json.loads((build / "manifest.json").read_text())
    for node_name in manifest["build_script_nodes"]:
        nc_path = build / "network-config" / f"{node_name}.yaml"
        assert nc_path.exists(), f"missing network-config for {node_name}"
        content = nc_path.read_text()
        assert "ethernets:" in content
        if node_name == "adversary":
            # The adversary is the *sole* exception: its NICs boot down,
            # addressless, with an empty route table, and are brought up only for
            # the duration of a stage (adversary.md §1; the `activate: runtime`
            # topology flag renders to `activation-mode: 'off'`). Assert it is
            # genuinely addressless rather than merely exempting it, so this
            # exception cannot mask a real node silently losing its baked address.
            assert "activation-mode: 'off'" in content, (
                f"{node_name} network-config lacks activation-mode: 'off'; its "
                f"NICs must boot down (adversary.md §1)"
            )
            assert "addresses:" not in content, (
                f"{node_name} must boot addressless, but its network-config "
                f"carries addresses:\n{content}"
            )
        else:
            # Every other node bakes a static address into its image.
            assert "addresses:" in content, (
                f"{node_name} network-config has no baked addresses:\n{content}"
            )


def test_every_build_script_is_executable_bash():
    """Every rendered build script must start with a bash shebang."""
    build = run_render("full")
    node_build_dir = build / "node-build"
    assert node_build_dir.exists(), "build/node-build/ directory missing"
    scripts = list(node_build_dir.iterdir())
    assert scripts, "no build scripts were rendered"
    for f in scripts:
        assert f.suffix == ".sh", f"unexpected file in node-build: {f.name}"
        content = f.read_text()
        assert content.startswith("#!/usr/bin/env bash"), (
            f"{f.name} does not start with bash shebang"
        )


def test_per_node_hosts_contain_own_entry():
    """Each node with a build script must have its own per-node /etc/hosts file
    containing at least loopback and its own name→IP mapping.

    Nodes whose first NIC carries a hostname field (e.g. sim-host) have no
    build_script, so no per-node file is generated for them.
    """
    build = run_render("full")
    t = yaml.safe_load((ROOT / "topology.yaml").read_text())
    manifest = json.loads((build / "manifest.json").read_text())
    for node_name in manifest["build_script_nodes"]:
        hosts_file = build / "hosts" / node_name
        assert hosts_file.exists(), f"missing per-node hosts file for {node_name}"
        content = hosts_file.read_text()
        assert "127.0.0.1 localhost" in content, (
            f"{node_name} hosts file missing loopback entry"
        )
        node = t["nodes"][node_name]
        nics = node.get("nics", [])
        if nics and not nics[0].get("hostname"):
            assert node_name in content, f"{node_name} missing from its own hosts file"


def test_fms_deployed_as_rust_binary():
    """The FMS is the Rust binary baked into the image at build/node-build/fms-01.sh.
    The retired Python/uvicorn stub must not return."""
    build = run_render("ci-minimal")
    fms_build = (build / "node-build" / "fms-01.sh").read_text()
    assert "ExecStart=/usr/local/bin/fms" in fms_build
    # The Python stub must not return.
    assert "uvicorn" not in fms_build
    assert "fastapi" not in fms_build.lower()
    # Env file must exist and declare the database URL.
    fms_env = (build / "env" / "fms-01.env").read_text()
    assert "FMS_DATABASE_URL" in fms_env


def test_fms_reads_rfid_over_tcp():
    """The FMS connects to the vessel RFID reader's OT address over TCP; the
    retired socat serial bridge must not return."""
    build = run_render("ci-minimal")
    fms_env = (build / "env" / "fms-01.env").read_text()
    assert "FMS_VESSEL_RFID_HOST=" in fms_env
    assert "9001" in fms_env
    assert "socat" not in fms_env
    assert "fms-serial.service" not in fms_env


def test_systemd_unit_files_are_well_formed():
    """Multi-line systemd unit files must keep one directive per line.
    Catches Jinja whitespace-stripping bugs that collapse `[Service]` and
    `ExecStart=` onto the same line."""
    build = run_render("ci-minimal")
    fms_build = (build / "node-build" / "fms-01.sh").read_text()
    # ExecStart must be on its own line, not concatenated with [Service]
    for line in fms_build.splitlines():
        if "ExecStart=" in line:
            stripped = line.strip()
            assert stripped.startswith("ExecStart="), (
                f"ExecStart line has prefix: {stripped!r}"
            )


def test_firewall_rules_have_valid_proto():
    """Every firewall_rules entry must use proto 'tcp' or 'udp'."""
    sys.path.insert(0, str(ROOT / "scripts"))
    from render import load_yaml

    t = load_yaml(ROOT / "topology.yaml")
    valid = {"tcp", "udp"}
    for rule in t.get("firewall_rules", []):
        assert rule["proto"] in valid, (
            f"firewall_rules: invalid proto '{rule['proto']}' "
            f"in rule {rule} — must be 'tcp' or 'udp'"
        )


def test_firewall_rules_reference_valid_nodes_and_networks():
    """Every firewall_rules entry must name valid networks, the destination
    must be either a known node name or a literal IP address, and an optional
    source must be either a literal IP address or a known node with a NIC on
    the rule's `from` network."""
    import ipaddress

    sys.path.insert(0, str(ROOT / "scripts"))
    from render import load_yaml

    t = load_yaml(ROOT / "topology.yaml")
    nodes = set(t["nodes"])
    networks = set(t["networks"])
    for rule in t.get("firewall_rules", []):
        assert rule["from"] in networks, (
            f"firewall_rules: unknown network '{rule['from']}'"
        )
        assert rule["to"] in networks, f"firewall_rules: unknown network '{rule['to']}'"
        dst = rule["dst"]
        try:
            ipaddress.ip_address(dst)
        except ValueError:
            assert dst in nodes, (
                f"firewall_rules: dst '{dst}' is neither a node name nor a valid IP"
            )
        src = rule.get("src")
        if src is None:
            continue
        try:
            ipaddress.ip_address(src)
        except ValueError:
            assert src in nodes, (
                f"firewall_rules: src '{src}' is neither a node name nor a valid IP"
            )
            assert any(
                nic["network"] == rule["from"] for nic in t["nodes"][src]["nics"]
            ), f"firewall_rules: src '{src}' has no NIC on '{rule['from']}'"


def test_only_atg_relay_reaches_the_atg_console():
    """digital.md §1.2: DMZ → OT admits TCP 10001 from atg-relay-01 to the ATG
    console only. The fw-ot-dmz rule must carry atg-relay-01's DMZ address as
    its source, or any DMZ host could reach the console."""
    build = run_render("ci-minimal")
    fw_ot_dmz = (build / "nftables" / "fw-ot-dmz.nft").read_text()
    console_rules = [
        line.strip() for line in fw_ot_dmz.splitlines() if "tcp dport 10001" in line
    ]
    assert console_rules == [
        'iifname "dmz0" oifname "ot0" ip saddr 10.20.0.10 ip daddr 10.40.0.51 '
        "tcp dport 10001 accept"
    ], f"fw-ot-dmz console rules: {console_rules}"


def test_transit_rules_render_on_correct_fw_node():
    """LDAPS transit rules must appear in fw-ot-dmz (hop 1, ot0→dmz0) and
    fw-dmz-it (hop 2, dmz0→it0) with corp-it's IP as the destination. The
    directory is reachable only over TLS (corp-it.md §3): port 389 must not
    appear on either hop, or a client could reach a plaintext bind even if
    corp-it itself never configures one. No Kerberos service is deployed
    (digital.md §8 deviation 4), so port 88 must not appear either."""
    build = run_render("full")
    corp_it_ip = "10.30.0.20"

    fw_ot_dmz = (build / "nftables" / "fw-ot-dmz.nft").read_text()
    assert corp_it_ip in fw_ot_dmz, "corp-it IP missing from fw-ot-dmz nftables"
    assert 'iifname "ot0" oifname "dmz0"' in fw_ot_dmz
    assert "tcp dport 636" in fw_ot_dmz, "fw-ot-dmz missing tcp dport 636 for corp-it"
    assert "tcp dport 389" not in fw_ot_dmz, (
        "fw-ot-dmz still admits plaintext LDAP (tcp dport 389) to corp-it"
    )
    for proto in ("tcp", "udp"):
        assert f"{proto} dport 88" not in fw_ot_dmz, (
            f"fw-ot-dmz admits Kerberos ({proto} dport 88), which is not deployed"
        )

    fw_dmz_it = (build / "nftables" / "fw-dmz-it.nft").read_text()
    assert corp_it_ip in fw_dmz_it, "corp-it IP missing from fw-dmz-it nftables"
    assert 'iifname "dmz0" oifname "it0"' in fw_dmz_it
    assert "tcp dport 636" in fw_dmz_it, "fw-dmz-it missing tcp dport 636 for corp-it"
    assert "tcp dport 389" not in fw_dmz_it, (
        "fw-dmz-it still admits plaintext LDAP (tcp dport 389) to corp-it"
    )
    for proto in ("tcp", "udp"):
        assert f"{proto} dport 88" not in fw_dmz_it, (
            f"fw-dmz-it admits Kerberos ({proto} dport 88), which is not deployed"
        )


def test_udp_rules_render_in_nftables():
    """NTP (UDP 123) must appear on both transit fw nodes (two-hop
    OT → DMZ → IT path to corp-it, digital.md §1.3)."""
    build = run_render("full")

    fw_dmz_it = (build / "nftables" / "fw-dmz-it.nft").read_text()
    assert "udp dport 123" in fw_dmz_it, "fw-dmz-it missing NTP udp dport 123"

    fw_ot_dmz = (build / "nftables" / "fw-ot-dmz.nft").read_text()
    assert "udp dport 123" in fw_ot_dmz, (
        "fw-ot-dmz missing NTP udp dport 123 (OT hop of the two-hop path)"
    )


def test_chrony_anchor_serves_the_site_without_an_upstream():
    """corp-it is the site time source (digital.md §1.3): it offers its own
    free-running clock at stratum 10 and has no upstream of any kind. A `server`
    or `pool` line here would silently subordinate the site clock to something
    outside it."""
    build = run_render("full")
    conf = (build / "chrony" / "corp-it.conf").read_text()

    assert "local stratum 10" in conf, "corp-it does not declare itself the anchor"
    for directive in ("server", "pool"):
        assert not any(line.startswith(directive) for line in conf.splitlines()), (
            f"corp-it has an upstream '{directive}' line; the site clock must be free-running"
        )


def test_chrony_allow_list_matches_firewall_policy():
    """chrony's access control must admit exactly the zones the firewall admits
    to port 123, plus corp-it's own zone (same-zone clients transit no fw node,
    so no rule names them). Asserted against topology.yaml rather than against
    literal subnets, so that replacing the derivation with a hard-coded list
    fails here."""
    build = run_render("full")
    topo = yaml.safe_load((ROOT / "topology.yaml").read_text())
    anchor = topo["time_source"]

    expected_nets = {
        rule["from"]
        for rule in topo["firewall_rules"]
        if rule["dst"] == anchor and rule["port"] == 123
    } | {topo["nodes"][anchor]["nics"][0]["network"]}
    expected = {topo["networks"][net]["cidr"] for net in expected_nets}

    conf = (build / "chrony" / f"{anchor}.conf").read_text()
    allowed = {
        line.split(None, 1)[1]
        for line in conf.splitlines()
        if line.startswith("allow ")
    }
    assert allowed == expected, (
        f"chrony allow list {allowed} != policy-derived {expected}"
    )


def test_chrony_does_not_serve_the_external_zone():
    """The supplier zone has no business reading the site clock: no firewall rule
    admits vnet-ext to port 123, so no allow line may name it."""
    build = run_render("full")
    topo = yaml.safe_load((ROOT / "topology.yaml").read_text())
    ext_cidr = topo["networks"]["vnet-ext"]["cidr"]

    conf = (build / "chrony" / f"{topo['time_source']}.conf").read_text()
    assert f"allow {ext_cidr}" not in conf, "chrony serves time to vnet-ext"


def test_chrony_clients_point_at_the_anchor_by_address():
    """Every managed node other than the anchor is a client of it. chrony
    addresses the anchor by IP regardless of which nodes separately resolve
    the corp-it name for their own directory bind (per-node minimal
    /etc/hosts, test_ca_provisioning.py) — a name here would not resolve for
    every chrony client fleet-wide."""
    build = run_render("full")
    topo = yaml.safe_load((ROOT / "topology.yaml").read_text())
    anchor = topo["time_source"]
    anchor_ip = topo["nodes"][anchor]["nics"][0]["ipv4"]

    clients = [p for p in (build / "chrony").glob("*.conf") if p.stem != anchor]
    assert clients, "no chrony client configs rendered"
    for path in clients:
        conf = path.read_text()
        assert f"server {anchor_ip} iburst" in conf, (
            f"{path.stem} does not point at the anchor at {anchor_ip}"
        )
        assert "local stratum" not in conf, f"{path.stem} declares itself a time source"
        assert "allow " not in conf, f"{path.stem} serves time; only the anchor may"


def test_chrony_rendered_for_managed_nodes_only():
    """Sealed appliances are not chrony clients: sim-host is the
    simulated-physical world and carries no build_script.

    Every managed node is one, `recon-01` included. Its clock does not decide
    which month the audit covers (`recon.md` §9), but it does decide whether a
    certificate verifies and whether the audit's shell history can be ordered
    against the rest of the fleet's records (§6.4, §7.2)."""
    build = run_render("full")
    rendered = {p.stem for p in (build / "chrony").glob("*.conf")}
    topo = yaml.safe_load((ROOT / "topology.yaml").read_text())

    expected = {
        name
        for name, node in topo["nodes"].items()
        if node.get("build_script") and node["build_script"] != "stub"
    }
    assert rendered == expected, (
        f"chrony configs {rendered} != managed nodes {expected}"
    )
    assert "sim-host" not in rendered, (
        "sim-host is a sealed appliance, not a time client"
    )


def _managed_nodes() -> list[str]:
    """Nodes that carry a real build script (chrony clients or the server)."""
    topo = yaml.safe_load((ROOT / "topology.yaml").read_text())
    return sorted(
        name
        for name, node in topo["nodes"].items()
        if node.get("build_script") and node["build_script"] != "stub"
    )


# Nodes that carry a real build script but are not part of the asset and so run
# no time client. The adversary models a host entirely outside RavensPort and
# installs no chrony (adversary.md §2); it remains a managed node for the
# hash-input and clock-control checks, which apply to any real build script.
_NODES_WITHOUT_CHRONY = {"adversary"}


def _chrony_client_nodes() -> list[str]:
    return [n for n in _managed_nodes() if n not in _NODES_WITHOUT_CHRONY]


def _hash_inputs(script: str) -> set[str]:
    """The file list inside the `current_hash=$(sha256sum ... | sha256sum ...)`
    block of a rendered build script. Parsing the block specifically (rather than
    a substring search over the whole script) is what makes the membership check
    meaningful: a file that is pushed but not hashed must fail."""
    lines = script.splitlines()
    start = next(
        i for i, line in enumerate(lines) if "current_hash=$(sha256sum" in line
    )
    inputs: set[str] = set()
    for line in lines[start + 1 :]:
        token = line.strip().rstrip("\\").strip()
        if token.startswith("| sha256sum") or token.startswith("|sha256sum"):
            break
        if token.startswith("build/"):
            inputs.add(token)
    return inputs


def _pushed_build_files(script: str) -> set[str]:
    """Every `build/...` file the script pushes into the image via incus file push."""
    pushed: set[str] = set()
    for line in script.splitlines():
        s = line.strip()
        if s.startswith("incus file push"):
            for tok in s.split():
                if tok.startswith("build/"):
                    pushed.add(tok)
    return pushed


@pytest.mark.parametrize("node", _chrony_client_nodes())
def test_build_script_configures_chrony(node):
    """Every managed node's build script installs chrony, pushes its rendered
    config to /etc/chrony/chrony.conf, and enables the service. Without these the
    rendered config is inert (digital.md §1.3)."""
    build = run_render("full")
    script = (build / "node-build" / f"{node}.sh").read_text()

    assert "apt-get install -y chrony" in script, f"{node}: chrony not installed"
    assert f'build/chrony/{node}.conf "$BUILD_CTR/etc/chrony/chrony.conf"' in script, (
        f"{node}: rendered chrony.conf not pushed to /etc/chrony/chrony.conf"
    )
    enable_lines = [line for line in script.splitlines() if "systemctl enable" in line]
    assert any("chrony" in line for line in enable_lines), (
        f"{node}: chrony not enabled at boot"
    )


@pytest.mark.parametrize("node", _managed_nodes())
def test_pushed_build_files_are_hashed(node):
    """Every config file a build script pushes into the image must appear in the
    `current_hash` input list, otherwise editing that file leaves the hash
    unchanged and the selective-rebuild guard serves a stale image. This is the
    class of defect behind the FMS_ADMIN_HASH regression; asserted for every
    pushed file, not only chrony's."""
    build = run_render("full")
    script = (build / "node-build" / f"{node}.sh").read_text()

    pushed = _pushed_build_files(script)
    hashed = _hash_inputs(script)
    missing = pushed - hashed
    assert not missing, f"{node}: pushed but not in current_hash: {sorted(missing)}"


def _chronyd_has_x(script: str) -> bool:
    """True if the script runs chronyd with -x (clock control disabled)"""
    return any(
        not line.lstrip().startswith("#")
        and "chrony" in line.lower()
        and "-x" in line.split()
        for line in script.splitlines()
    )


@pytest.mark.parametrize("node", _managed_nodes())
def test_build_scripts_do_not_force_clock_control(node):
    """No build script forces chronyd's clock-control mode (-x)."""
    build = run_render("full")
    script = (build / "node-build" / f"{node}.sh").read_text()
    assert not _chronyd_has_x(script), (
        f"{node} build script forces chronyd -x; clock control is the chrony "
        f"package's job (SYNC_IN_CONTAINER=no), not the build script's"
    )


def test_fw_nodes_have_transit_routes_in_network_config():
    """fw-ot-dmz must carry a static route to vnet-it via fw-dmz-it's DMZ IP,
    and fw-dmz-it must carry a static route to vnet-ot via fw-ot-dmz's DMZ IP.
    Both are required for transparent two-hop routing."""
    build = run_render("full")
    import yaml as _yaml

    nc_fw_ot = _yaml.safe_load(
        (build / "network-config" / "fw-ot-dmz.yaml").read_text()
    )
    dmz_routes = nc_fw_ot["ethernets"]["dmz0"].get("routes", [])
    assert any(
        r["to"] == "10.30.0.0/24" and r["via"] == "10.20.0.3" for r in dmz_routes
    ), "fw-ot-dmz dmz0 missing route to 10.30.0.0/24 via 10.20.0.3"

    nc_fw_dmz = _yaml.safe_load(
        (build / "network-config" / "fw-dmz-it.yaml").read_text()
    )
    dmz_routes = nc_fw_dmz["ethernets"]["dmz0"].get("routes", [])
    assert any(
        r["to"] == "10.40.0.0/24" and r["via"] == "10.20.0.4" for r in dmz_routes
    ), "fw-dmz-it dmz0 missing route to 10.40.0.0/24 via 10.20.0.4"


def test_corp_it_has_ot_return_route():
    """corp-it must have a static route to vnet-ot via fw-dmz-it's IT interface
    so that LDAP and NTP reply packets can reach OT clients."""
    build = run_render("full")
    import yaml as _yaml

    nc = _yaml.safe_load((build / "network-config" / "corp-it.yaml").read_text())
    routes = nc["ethernets"]["eth0"].get("routes", [])
    assert any(r["to"] == "10.40.0.0/24" for r in routes), (
        "corp-it eth0 missing return route to 10.40.0.0/24"
    )


def test_full_profile_non_stub_vms_have_real_build_script():
    """Every VM in the full profile that is not explicitly a stub must have
    a build script other than 'stub', so it actually configures its service
    rather than booting as a bare host."""
    sys.path.insert(0, str(ROOT / "scripts"))
    from render import load_yaml, merge_topology

    t = load_yaml(ROOT / "topology.yaml")
    o = load_yaml(ROOT / "overrides" / "full.yaml")
    merged = merge_topology(t, o)
    for name, node in merged["nodes"].items():
        if node.get("type") == "vm" and node.get("build_script") == "stub":
            reason = node.get("stub_reason")
            assert reason, (
                f"{name} is a VM with build_script=stub in the full profile; "
                "either give it a real build script or document why stub is correct "
                "by adding a 'stub_reason' field to the node in topology.yaml"
            )
            warnings.warn(f"{name} is a VM stub: {reason}", UserWarning, stacklevel=2)


def test_build_scripts_hash_correct_artefacts():
    """Each rendered node build script must hash its own rendered artefacts and
    must not hash artefacts belonging to other nodes.

    Omitting a file from the hash block means changes to that file go undetected
    and the image is not rebuilt. Including a foreign file causes spurious rebuilds
    whenever a different node's artefacts change.
    """
    build = run_render("full")
    manifest = json.loads((build / "manifest.json").read_text())
    fw_nodes = {n for n in manifest["build_script_nodes"] if n.startswith("fw-")}

    # Artefacts specific to each build-script type that must appear in the hash.
    type_required: dict[str, list[str]] = {
        "fms-01": ["build/env/fms-01.env"],
        "hmi-01": ["build/env/hmi-01.env"],
        "atg-relay-01": ["build/haproxy/atg-relay-01.cfg"],
        "corp-it": ["build/ldif/corp-it.ldif"],
    }

    for node_name in manifest["build_script_nodes"]:
        script = (build / "node-build" / f"{node_name}.sh").read_text()

        # Every build script must hash its own script, network config, and hosts file.
        for path in (
            f"build/node-build/{node_name}.sh",
            f"build/network-config/{node_name}.yaml",
            f"build/hosts/{node_name}",
        ):
            assert path in script, (
                f"{node_name}.sh must include {path} in its hash computation"
            )

        if node_name in fw_nodes:
            nft_path = f"build/nftables/{node_name}.nft"
            assert nft_path in script, (
                f"{node_name}.sh must include {nft_path} in its hash computation"
            )

        for artefact in type_required.get(node_name, []):
            assert artefact in script, (
                f"{node_name}.sh must include {artefact} in its hash computation"
            )

    # Cross-contamination: no node may include another node's env file in its hash.
    for node_name in manifest["build_script_nodes"]:
        script = (build / "node-build" / f"{node_name}.sh").read_text()
        for other in manifest["build_script_nodes"]:
            if other == node_name:
                continue
            foreign_env = f"build/env/{other}.env"
            assert foreign_env not in script, (
                f"{node_name}.sh must not hash {foreign_env}: "
                "that would cause spurious rebuilds when {other} changes"
            )


def test_build_scripts_implement_selective_rebuild():
    """Every rendered node build script must implement the content-hash skip
    pattern: compute a current hash, read the stored hash from the image
    property, and exit early if they match. The hash must be stored via the
    ravensport.input-hash image property on publish.

    This guard prevents unnecessary image rebuilds when only unrelated inputs
    have changed.
    """
    build = run_render("full")
    manifest = json.loads((build / "manifest.json").read_text())
    for node_name in manifest["build_script_nodes"]:
        script = (build / "node-build" / f"{node_name}.sh").read_text()
        assert "current_hash" in script, (
            f"{node_name}.sh missing current_hash computation"
        )
        assert "stored_hash" in script, (
            f"{node_name}.sh missing stored_hash retrieval from image"
        )
        assert "inputs unchanged, skipping" in script, (
            f"{node_name}.sh missing skip-if-unchanged early exit"
        )
        assert "ravensport.input-hash" in script, (
            f"{node_name}.sh must store/read the hash via ravensport.input-hash"
        )


def test_sim_image_build_implements_selective_rebuild():
    """build-sim-image.sh must implement the same content-hash skip pattern as
    the per-node Ubuntu build scripts."""
    script = (ROOT / "scripts" / "build-sim-image.sh").read_text()
    assert "CURRENT_HASH" in script, "build-sim-image.sh missing CURRENT_HASH"
    assert "STORED_HASH" in script, "build-sim-image.sh missing STORED_HASH"
    assert "inputs unchanged, skipping" in script, (
        "build-sim-image.sh missing skip-if-unchanged early exit"
    )
    assert "ravensport.input-hash" in script, (
        "build-sim-image.sh must store/read hash via ravensport.input-hash"
    )


def test_world_yaml_renders_fms_admin_hash():
    """FMS env must contain an argon2id admin password hash derived from world.yaml."""
    try:
        import argon2  # noqa: F401
    except ImportError:
        pytest.skip("argon2-cffi not installed")
    build = run_render("ci-minimal")
    fms_env = (build / "env" / "fms-01.env").read_text()
    # The key name must match the mandatory variable the FMS loader reads
    # (fms/src/config.rs require("FMS_ADMIN_PASSWORD_HASH")). The env-contract
    # tests in test_fms_env_contract.py enforce that coupling directly.
    assert "FMS_ADMIN_PASSWORD_HASH=" in fms_env
    hash_line = next(
        (
            line
            for line in fms_env.splitlines()
            if line.startswith("FMS_ADMIN_PASSWORD_HASH=")
        ),
        None,
    )
    assert hash_line is not None, "FMS_ADMIN_PASSWORD_HASH line not found in fms-01.env"
    hash_value = hash_line.split("=", 1)[1]
    assert hash_value.startswith("$argon2id$"), (
        f"FMS_ADMIN_PASSWORD_HASH does not start with $argon2id$: {hash_line}"
    )


def test_world_yaml_renders_ldif():
    """corp-it LDIF must contain the people organisational unit and at least one
    operator entry derived from world.yaml."""
    build = run_render("ci-minimal")
    ldif_path = build / "ldif" / "corp-it.ldif"
    assert ldif_path.exists(), "build/ldif/corp-it.ldif not found"
    ldif = ldif_path.read_text()
    assert "ou=people" in ldif, "people OU missing from LDIF"
    assert "objectClass: inetOrgPerson" in ldif, (
        "inetOrgPerson objectClass missing from LDIF"
    )
    assert "uid: jrenard" in ldif, "operator jrenard missing from LDIF"


# ── sim-host topology tests ──────────────────────────────────────────────────


def test_sim_host_readonly_root():
    """sim-host must render with a read-only root disk plus tmpfs mounts over the
    writable paths the init system and services need (sim-network.md §8); nodes
    without the flag must not get the override."""
    build = run_render("full")
    cmds = (build / "incus-commands.sh").read_text()
    assert "incus config device override sim-host root readonly=true" in cmds
    for path in ("tmpfs run tmpfs", "tmpfs tmp tmpfs", "tmpfs var/log tmpfs"):
        assert f"lxc.mount.entry = {path}" in cmds, (
            f"sim-host missing tmpfs mount: {path}"
        )
    # A node without readonly_root must not get a read-only root override.
    assert "override fms-01 root readonly=true" not in cmds


def test_sim_host_nics():
    """sim-host must present the eight NICs of the single-container design: five
    vnet-ot appliance/actor NICs, two vnet-it actor NICs, and the ext-supplier
    egress NIC. Interfaces sharing a zone use macvlan for L2 separation."""
    sys.path.insert(0, str(ROOT / "scripts"))
    from render import load_yaml

    t = load_yaml(ROOT / "topology.yaml")
    host = t["nodes"].get("sim-host")
    assert host is not None, "sim-host missing from topology"

    nics_by_name = {nic["name"]: nic for nic in host["nics"]}
    assert len(host["nics"]) == 8, "sim-host must have exactly eight NICs"

    expected = {
        "ot-atg": ("vnet-ot", "10.40.0.51", "macvlan", "atg-console"),
        "ot-disp": ("vnet-ot", "10.40.0.52", "macvlan", "dispenser"),
        "ot-vessel-rfid": ("vnet-ot", "10.40.0.53", "macvlan", "vessel-rfid"),
        "ot-op-rfid": ("vnet-ot", "10.40.0.54", "macvlan", "operator-rfid"),
        "ot-operator": ("vnet-ot", "10.40.0.55", "macvlan", "dock-operator"),
        "ext-supplier": ("vnet-ext", "10.10.0.10", None, "fuel-supplier-01"),
        "it-recon-user": ("vnet-it", "10.30.0.11", "macvlan", None),
        "it-sysadmin": ("vnet-it", "10.30.0.12", "macvlan", None),
    }
    for name, (net, ip, nic_type, hostname) in expected.items():
        assert name in nics_by_name, f"sim-host missing {name} NIC"
        nic = nics_by_name[name]
        assert nic["network"] == net, f"{name} on wrong network"
        assert nic["ipv4"] == ip, f"{name} wrong IP"
        # macvlan gives every shared-subnet OT/IT NIC native L2 separation; the
        # single-NIC External zone stays bridged (default).
        assert nic.get("nic_type") == nic_type, f"{name} wrong nic_type"
        assert nic.get("hostname") == hostname, f"{name} wrong hostname"


def test_recon_01_is_a_node():
    """recon-01 is now a real IT-zone node — the reconciliation user's RDP
    target — with a route to the DMZ for its historian access."""
    sys.path.insert(0, str(ROOT / "scripts"))
    from render import load_yaml

    t = load_yaml(ROOT / "topology.yaml")
    recon = t["nodes"].get("recon-01")
    assert recon is not None, "recon-01 missing from topology"
    nic = recon["nics"][0]
    assert nic["network"] == "vnet-it"
    assert nic["ipv4"] == "10.30.0.10"
    assert any(r["to"] == "10.20.0.0/24" for r in nic.get("routes", [])), (
        "recon-01 missing route to vnet-dmz (10.20.0.0/24)"
    )


def test_removed_nodes_absent():
    """sim-layer, sim-relay, and fuel-supplier-01 must not appear as nodes."""
    sys.path.insert(0, str(ROOT / "scripts"))
    from render import load_yaml

    t = load_yaml(ROOT / "topology.yaml")
    for removed in ("sim-layer", "sim-relay", "fuel-supplier-01"):
        assert removed not in t["nodes"], (
            f"{removed} should have been removed from topology"
        )
    assert "vnet-sim-fence" not in t["networks"], (
        "vnet-sim-fence should have been removed from topology"
    )


def test_atg_firewall_rule_targets_sim_host_atg_ip():
    """The DMZ→OT ATG firewall rule must target the ATG NIC IP, now on sim-host."""
    sys.path.insert(0, str(ROOT / "scripts"))
    from render import load_yaml

    t = load_yaml(ROOT / "topology.yaml")
    atg_rules = [
        r
        for r in t.get("firewall_rules", [])
        if r["from"] == "vnet-dmz" and r["to"] == "vnet-ot" and r["port"] == 10001
    ]
    assert atg_rules, "No DMZ→OT ATG rule found"
    assert atg_rules[0]["dst"] == "10.40.0.51", (
        f"ATG firewall rule dst is {atg_rules[0]['dst']!r}, expected 10.40.0.51"
    )


def test_log_server_replication_rules_are_the_specified_ports():
    """OT→DMZ rules to `hist-dmz-01`: 8428/TCP for metric replication
    (`hist-01` vmagent → VictoriaMetrics) and 9428/TCP for event replication
    (`hist-01` vlagent → VictoriaLogs), matching the remote-write targets in `host_specs/hist.md` §6.


    `topology.yaml` previously carried a single 5432/TCP rule which we are checking is now
    not in affect
    """
    sys.path.insert(0, str(ROOT / "scripts"))
    from render import load_yaml

    t = load_yaml(ROOT / "topology.yaml")
    rules = t.get("firewall_rules", [])

    replication = {
        (r["port"], r["proto"])
        for r in rules
        if r["from"] == "vnet-ot"
        and r["to"] == "vnet-dmz"
        and r["dst"] == "hist-dmz-01"
    }
    assert replication == {(8428, "tcp"), (9428, "tcp")}, (
        f"OT→DMZ rules to hist-dmz-01 are {sorted(replication)}; deployment.md §4.2 "
        "specifies exactly 8428/tcp (metric replication) and 9428/tcp (event "
        "replication)"
    )

    postgres = [r for r in rules if r["port"] == 5432]
    assert not postgres, (
        f"firewall_rules still carry a PostgreSQL 5432 rule: {postgres}. The log "
        "server is VictoriaMetrics/VictoriaLogs (hist.md §2); nothing in the "
        "deployment listens on 5432"
    )


def test_replication_rules_render_on_the_ot_dmz_hop():
    """Both replication rules must survive into the nftables ruleset on the single
    OT→DMZ hop, with `hist-dmz-01`'s DMZ address as the destination. Asserting the
    rendered output as well as the source rule catches a `_resolve_fw_rules`
    regression that drops or mis-addresses a rule the topology declares."""
    build = run_render("full")
    topo = yaml.safe_load((ROOT / "topology.yaml").read_text())
    hist_dmz_ip = topo["nodes"]["hist-dmz-01"]["nics"][0]["ipv4"]

    fw_ot_dmz = (build / "nftables" / "fw-ot-dmz.nft").read_text()
    for port, purpose in ((8428, "metric replication"), (9428, "event replication")):
        expected = f'iifname "ot0" oifname "dmz0" ip daddr {hist_dmz_ip} tcp dport {port} accept'
        assert expected in fw_ot_dmz, (
            f"fw-ot-dmz nftables missing the {purpose} rule: {expected!r}"
        )

    # No fw node anywhere may still forward to the retired PostgreSQL port.
    for nft in (build / "nftables").glob("*.nft"):
        assert "dport 5432" not in nft.read_text(), (
            f"{nft.name} still forwards to 5432; the historian is not a database"
        )


def test_log_server_names_are_resolved_only_by_their_named_clients():
    """Every log-server hostname is hostname-only-SAN certified (digital.md
    §2.7), so it is resolved by exactly the clients hist.md names it to, and by
    no one else — carrying an unused resolve entry leaks topology in the
    direction the per-node minimal-resolve work removed.

    `hist-01` is resolved by `fms-01` (its ingestion pushes, hist.md §3.1/§5),
    `hmi-01` (Grafana reachability from any OT host, hist-tests.md §7), and
    `admin-01` (the named SSH destination, digital.md §4.6), and
    `dmz-bastion-01` (the forwarding destination in `PermitOpen`).
    `hist-dmz-01` is resolved by `hist-01` (the replication targets of hist.md
    §6) and `recon-01` (the reconciliation user's browser, digital.md §5.2).
    """
    build = run_render("full")
    expected_resolvers = {
        "hist-01": {"fms-01", "hmi-01", "admin-01", "dmz-bastion-01"},
        "hist-dmz-01": {"hist-01", "recon-01"},
    }

    for hosts_file in (build / "hosts").iterdir():
        node_name = hosts_file.name
        content = hosts_file.read_text()
        for server, resolvers in expected_resolvers.items():
            if server == node_name:
                continue
            holds = server in content
            should_hold = node_name in resolvers
            assert holds == should_hold, (
                f"{node_name} {'resolves' if holds else 'does not resolve'} {server!r}, "
                f"but hist.md §3.1/§6/§7 says it {'should' if should_hold else 'should not'}"
            )


def test_per_node_hosts_enforce_minimal_resolve():
    """Per-node /etc/hosts files must contain only loopback, the node's own
    entry, and hostnames listed in the node's 'resolve:' field.

    Nodes may carry resolve entries for other node names (e.g. hist-dmz-01,
    corp-it) when those names appear in application configuration. However,
    sim-host NIC hostnames (atg-console, vessel-rfid, operator-rfid, etc.)
    must never appear in other nodes' hosts files — those addresses are always
    referenced by IP from env files.
    """
    build = run_render("full")
    manifest = json.loads((build / "manifest.json").read_text())

    # atg-relay-01 must resolve atg-console by name (HAProxy backend).
    relay_hosts = (build / "hosts" / "atg-relay-01").read_text()
    assert "10.40.0.51 atg-console" in relay_hosts, (
        "atg-relay-01 hosts file missing atg-console entry"
    )

    # Every other node must not carry any sim-host NIC hostname or node names
    # that are not its own — those services are always addressed by IP.
    topology_hostnames = {
        "atg-console",
        "dispenser",
        "vessel-rfid",
        "operator-rfid",
        "dock-operator",
        "fuel-supplier-01",
    }
    for node_name in manifest["build_script_nodes"]:
        if node_name == "atg-relay-01":
            continue
        content = (build / "hosts" / node_name).read_text()
        for hostname in topology_hostnames:
            assert hostname not in content, (
                f"{node_name} must not carry {hostname!r} in its hosts file; "
                "it must use the IP address from its env file instead"
            )


def test_sim_host_haproxy_cfg_propagates_topology_ips():
    """build/haproxy/sim-host.cfg must contain every IP address that topology.yaml
    assigns to sim-host's NICs and to the nodes sim-host connects to outbound.

    A change to any of these addresses in topology.yaml must be reflected in the
    rendered config; this test guards that the render function reads from topology
    rather than using hard-coded literals."""
    sys.path.insert(0, str(ROOT / "scripts"))
    from render import load_yaml

    t = load_yaml(ROOT / "topology.yaml")
    build = run_render("ci-minimal")
    cfg = (build / "haproxy" / "sim-host.cfg").read_text()

    nics = t["nodes"]["sim-host"]["nics"]
    checks = [
        (nics[0]["ipv4"], "ATG console bind address"),
        (nics[1]["ipv4"], "dispenser bind address"),
        (nics[2]["ipv4"], "vessel RFID bind address"),
        (nics[3]["ipv4"], "operator RFID bind address"),
        (nics[4]["ipv4"], "dock operator source address"),
        (nics[5]["ipv4"], "fuel supplier source address"),
        (nics[6]["ipv4"], "reconciliation user source address"),
        (t["nodes"]["atg-relay-01"]["nics"][0]["ipv4"], "ATG relay server address"),
        (t["nodes"]["hmi-01"]["nics"][0]["ipv4"], "HMI server address"),
        (
            t["nodes"]["recon-01"]["nics"][0]["ipv4"],
            "reconciliation workstation server address",
        ),
    ]
    for ip, label in checks:
        assert ip in cfg, (
            f"sim-host HAProxy config missing {label} ({ip}) from topology.yaml"
        )


def test_sim_image_build_hashes_haproxy_cfg():
    """build-sim-image.sh must include build/haproxy/sim-host.cfg in its content
    hash so that a re-render that changes the HAProxy config triggers a sim image
    rebuild."""
    script = (ROOT / "scripts" / "build-sim-image.sh").read_text()
    assert "build/haproxy/sim-host.cfg" in script, (
        "build-sim-image.sh must include build/haproxy/sim-host.cfg in its "
        "content hash (a topology IP change must trigger a sim image rebuild)"
    )


def test_seen_ips_covers_multi_nic_nodes():
    """Duplicate-IP check must catch IPs across all NICs, including nodes with
    multiple NICs on the same bridge."""
    sys.path.insert(0, str(ROOT / "scripts"))
    from render import load_yaml, merge_topology, validate

    t = load_yaml(ROOT / "topology.yaml")
    o = load_yaml(ROOT / "overrides" / "full.yaml")
    m = merge_topology(t, o)
    # Introduce a duplicate: give sim-host's second NIC the same IP as its first.
    m["nodes"]["sim-host"]["nics"][1]["ipv4"] = "10.40.0.51"
    try:
        validate(m)
    except SystemExit as e:
        assert "duplicate IP" in str(e)
    else:
        raise AssertionError("validate() should have rejected the duplicate IP")


@pytest.mark.parametrize("profile", ["full", "ci-minimal"])
def test_sim_host_is_a_container(profile):
    """The coupled sim reads its start time once from its host's clock
    (sim.md §2.1). As a container, sim-host reads the same kernel clock as
    corp-it, the digital world's time source, so the sim and the digital world
    start on one time base. As a VM it would have a clock of its own and no time
    synchronisation (the sim-host image carries none), so its start time could
    be anything."""
    build = run_render(profile)
    cmds = (build / "incus-commands.sh").read_text()
    init = [
        ln
        for ln in cmds.splitlines()
        if ln.startswith("incus init ") and ln.endswith(" sim-host")
    ]
    assert len(init) == 1, f"expected one incus init for sim-host, found {init}"
    assert "--vm" not in init[0], f"sim-host is launched as a VM: {init[0]}"
