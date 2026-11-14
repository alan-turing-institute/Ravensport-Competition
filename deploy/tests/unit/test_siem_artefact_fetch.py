"""
Pinned-artefact fetch tests for security monitoring (siem-tests.md §10),
targeting siem.md §8.3/§8.4.

No deployed instance has outbound internet access; connectivity exists only
during image build (deployment.md §2.1, siem.md §8.3). Reproducibility
therefore requires that every artefact this feature pulls from outside the
repository is pinned to an exact version and verified against a recorded
digest at build time, never resolved from a moving target or trusted on
first use:

  * the Wazuh packages (manager and agent), from the vendor repository, with
    the repository signing key held in this repository rather than fetched at
    build time (siem.md §8.4);
  * the Emerging Threats Open ruleset, on the three firewall nodes, held in
    this repository at a recorded revision and verified against a recorded
    SHA-256 digest, because its publisher keeps no earlier revisions to pin
    a download to (siem.md §5/§8.4);
  * the ATT&CK for Industrial Control Systems (ICS) matrix, a Structured
    Threat Information Expression (STIX) bundle imported into siem-01's
    technique database, pinned and digest-verified the same way (siem.md
    §2.2/§8.4).

Assertions are on the rendered build/node-build/*.sh scripts rather than on
the Jinja templates, matching test_hist_artefact_fetch.py's convention: a
mistake in a shared include is only observable once expanded.

Run with: pytest tests/unit -q
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # deploy/

# siem.md §8.4 / deployment-config.md §6: the pinned Wazuh package version.
_WAZUH_VERSION = "4.14.7-1"

_FW_NODES = ("fw-ext-dmz", "fw-dmz-it", "fw-ot-dmz")


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


def _script(node: str) -> str:
    build = _render()
    path = build / "node-build" / f"{node}.sh"
    assert path.exists(), f"{path} was not rendered"
    return path.read_text()


def test_wazuh_manager_package_is_pinned():
    script = _script("siem-01")
    assert re.search(rf"wazuh-manager={re.escape(_WAZUH_VERSION)}\b", script), (
        f"siem-01.sh does not install a pinned wazuh-manager={_WAZUH_VERSION}"
    )
    assert not re.search(
        r"apt-get install\s+(-\S+\s+)*wazuh-manager\s*(\n|&&|;|$)", script
    ), "siem-01.sh installs wazuh-manager without a pinned version"


def test_wazuh_agent_package_is_pinned_on_every_monitored_host():
    monitored_nodes = (
        "corp-it",
        "fms-01",
        "hmi-01",
        "hist-01",
        "hist-dmz-01",
        "recon-01",
        "atg-relay-01",
    ) + _FW_NODES
    for node in monitored_nodes:
        script = _script(node)
        assert re.search(rf"wazuh-agent={re.escape(_WAZUH_VERSION)}\b", script), (
            f"{node}'s rendered build script does not install a pinned "
            f"wazuh-agent={_WAZUH_VERSION}"
        )


def test_wazuh_repository_signing_key_is_not_fetched_at_build_time():
    """siem.md §8.4: the repository signing key is held in this repository
    rather than fetched at build time. A build-time curl/wget of the key
    defeats the point of pinning it — a compromised or unavailable upstream
    key server would silently change what the build trusts."""
    for node in ("siem-01", "corp-it"):
        script = _script(node)
        assert not re.search(r"(curl|wget)[^\n]*packages\.wazuh\.com/key", script), (
            f"{node}.sh fetches the Wazuh signing key from the network at build time"
        )
        assert re.search(
            r"incus file push[^\n]*wazuh[^\n]*\.gpg", script, re.IGNORECASE
        ), f"{node}.sh does not push a repository-held Wazuh signing key"


def test_et_open_ruleset_is_repository_held_and_digest_verified():
    """siem.md §5/§8.4: the signature set is held in this repository at a
    recorded revision and verified against a recorded SHA-256 digest before
    it is loaded. The publisher republishes the ruleset daily at one URL per
    Suricata release line, so a build-time download cannot be pinned: the
    digest would match for at most a day."""
    for node in _FW_NODES:
        script = _script(node)
        assert "ET_OPEN_SHA256" in script, (
            f"{node}.sh does not verify the Emerging Threats Open ruleset by digest"
        )
        # Running the updater would also download; masking its timer (so the
        # ruleset is never updated at run time, siem.md §8.3) is expected.
        assert not re.search(
            r"(curl|wget)[^\n]*emergingthreats|--\s+suricata-update\b", script
        ), f"{node}.sh downloads the Emerging Threats Open ruleset at build time"
        assert "build-scripts/rulesets/" in script, (
            f"{node}.sh does not load a repository-held Emerging Threats Open ruleset"
        )


def test_et_open_ruleset_matches_recorded_digest():
    """The held copy is the revision the digest records, and carries the
    EICAR canary signature the firewall build re-enables (siem.md §8.4)."""
    import hashlib
    import tarfile

    script = _script(_FW_NODES[0])
    digest = re.search(r'^ET_OPEN_SHA256="([0-9a-f]{64})"', script, re.M).group(1)
    version = re.search(r'^ET_OPEN_SURICATA_VERSION="([^"]+)"', script, re.M).group(1)
    revision = re.search(r'^ET_OPEN_REVISION="([^"]+)"', script, re.M).group(1)
    path = (
        ROOT
        / "build-scripts"
        / "rulesets"
        / f"emerging.rules-suricata-{version}-r{revision}.tar.gz"
    )
    assert path.exists(), f"{path} is not held in the repository"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
    with tarfile.open(path) as tar:
        exploit = tar.extractfile("rules/emerging-exploit.rules").read().decode()
    assert re.search(r"^#.*sid:2022938;", exploit, re.M), (
        "sid 2022938 is not present commented out in the held ruleset"
    )


def test_ics_stix_bundle_is_pinned_and_digest_verified():
    """siem.md §2.2/§8.4: the ATT&CK for ICS matrix is imported from a pinned
    STIX bundle verified against a recorded SHA-256 digest, because the
    manager ships only the Enterprise matrix."""
    script = _script("siem-01")
    assert "ICS_STIX_SHA256" in script or re.search(r"sha256sum\s+-c", script), (
        "siem-01.sh does not verify the ATT&CK for ICS STIX bundle by digest"
    )
    assert re.search(r"stix|attack.*ics|ics.*attack", script, re.IGNORECASE), (
        "siem-01.sh does not reference an ATT&CK for ICS bundle import"
    )
