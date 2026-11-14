"""
No self-update, no external lookups

No port host updates itself or contacts a service outside the port (narrative
§4). Every per-node build script must therefore remove the stock Ubuntu
services that would: purge ``unattended-upgrades``, mask the apt, motd-news and
Ubuntu Pro timers, and empty systemd-resolved's fallback DNS list
(deployment.md §6.1 step 7). Both profiles are rendered, and every node
template is checked for the shared step, so a template no profile currently
renders (the stub) is covered as well.

This unit test checks the *rendered* build scripts statically (no Incus needed).
The runtime counterpart — that each built image carries the change — lives in
tests/integration/test_image_build.sh.

Run with: pytest tests/unit -q
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = ROOT / "build-scripts"

TIMERS = (
    "apt-daily.timer",
    "apt-daily-upgrade.timer",
    "motd-news.timer",
    "ua-timer.timer",
)


def render(profile: str) -> list[Path]:
    out = subprocess.run(
        [sys.executable, "scripts/render.py", "--profile", profile],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode == 0, out.stderr
    build = ROOT / "build"
    manifest = json.loads((build / "manifest.json").read_text())
    scripts = [build / "node-build" / f"{n}.sh" for n in manifest["build_script_nodes"]]
    assert scripts, "no per-node build scripts were rendered"
    for script in scripts:
        assert script.is_file(), f"{script.name} was not rendered"
    return scripts


@pytest.fixture(scope="module", params=["ci-minimal", "full"])
def build_scripts(request) -> dict[str, str]:
    return {s.name: s.read_text() for s in render(request.param)}


def published_part(text: str) -> str:
    """The part of a build script that runs before the container is stopped
    for publishing; a change made after that point is not in the image."""
    stop = text.index('incus stop "$BUILD_CTR"')
    return text[:stop]


def test_build_scripts_purge_unattended_upgrades(build_scripts):
    for name, text in build_scripts.items():
        assert "apt-get purge -y -o DPkg::Lock::Timeout=600 unattended-upgrades" in (
            published_part(text)
        ), f"{name} does not purge unattended-upgrades before publishing"


def test_build_scripts_mask_external_timers(build_scripts):
    for name, text in build_scripts.items():
        part = published_part(text)
        mask = part[part.index("systemctl mask --now") :]
        mask = mask[: mask.index("\nincus ")]
        for timer in TIMERS:
            assert timer in mask, f"{name} does not mask {timer}"


def test_build_scripts_empty_resolved_fallback_dns(build_scripts):
    for name, text in build_scripts.items():
        part = published_part(text)
        assert "/etc/systemd/resolved.conf.d/90-ravensport.conf" in part, (
            f"{name} does not install the systemd-resolved drop-in"
        )
        assert "[Resolve]\nFallbackDNS=\n" in part, (
            f"{name} does not set an empty FallbackDNS list"
        )


def test_network_config_names_no_dns_server(build_scripts):
    """Nodes resolve names from /etc/hosts and have no DNS server (deployment.md
    §2.1). A server in the rendered network configuration becomes a `DNS=` line
    in the node's systemd-networkd unit, and every lookup /etc/hosts does not
    answer is then sent to it."""
    for path in sorted((ROOT / "build" / "network-config").glob("*.yaml")):
        assert "nameservers" not in path.read_text(), (
            f"{path.name} configures a DNS server"
        )


def test_every_node_template_includes_the_step():
    templates = sorted(
        t for t in TEMPLATES.glob("*.sh.j2") if not t.name.startswith("_")
    )
    assert templates, "no node build templates found"
    for template in templates:
        assert '{% include "_no-external-services.sh.j2" %}' in template.read_text(), (
            f"{template.name} does not include _no-external-services.sh.j2"
        )
