"""
Fixtures for the reconciliation workstation integration suite (`recon-01`, IT
zone), per `recon-tests.md` §0.

Environment variables (with defaults):

  RECON_NODE_NAME    Incus node name for recon-01        (default: recon-01)
  RECON_HOST         recon-01 address on vnet-it         (default: 10.30.0.10)
  RECON_RDP_PORT     xrdp inbound port                   (default: 3389)
  RECON_PEER_NODE    A vnet-it host used as the probe    (default: corp-it)
                     source for reachability assertions
  RECON_SIM_HOST     sim-host address on vnet-it         (default: 10.30.0.11)
  HIST_DMZ_HOST      hist-dmz-01 address on vnet-dmz     (default: 10.20.0.20)
  HIST_NODE_NAME     Incus node name for hist-01, inside (default: hist-01)
                     which §5's records are seeded
  HIST_HOST          hist-01 address on vnet-ot, where   (default: 10.40.0.11)
                     its ingestion listener answers
  CORP_IT_HOST       corp-it address on vnet-it          (default: 10.30.0.20)
  RECON_WORLD_YAML   Path to world.yaml                  (default:
                     <repo>/deploy/world.yaml)

Prerequisites:
  - `recon-01` is running an Incus image built from
    `deploy/build-scripts/recon-01.sh.j2`.
  - The test runner has an `incus` client authorised against the same daemon.

**This suite requires no zone membership and adds none.** Every assertion is
made through `incus exec`, inside `recon-01` or inside a same-zone peer, and
§5's records are seeded through `incus exec` inside `hist-01`, so there is no
`configure-` step.

That is not a claim that the runner is isolated. It may already be on `vnet-ot`
with a route to `vnet-it`, from `configure-fms` or `configure-corp-it`; and
`incus exec` runs as root inside any instance on any zone, which is strictly
more capability than sitting on a zone. The runner is not access-constrained,
and nothing here makes it so.

What probing from inside the instance buys is a correct measurement. The
reachability assertions are about what `recon-01` can and cannot reach — a
property of `recon-01`'s position, not the runner's. Probed from the runner they
would answer a different question, and for the negative cases they would answer
it wrongly: `test_cannot_reach_the_operational_technology_zone` run on a VM where
`configure-fms` has been applied would find the OT zone reachable, because the
runner is on it, and would fail while `recon-01` was correctly isolated.

**What this suite does not cover.** `recon-tests.md` §2–§4 — the negotiated
security layer, the desktop session, and the browser's behaviour — need a client
that speaks Remote Desktop Protocol, which does not exist yet. §5's retrieval
needs none: it is run through `incus exec`, less its clipboard pipe. The modules here
assert what the image carries and what the host can reach; they do not assert
what a session does. Where a test is the image-level counterpart of a pending
behavioural assertion, its docstring says so, because the two are not
interchangeable: a correct configuration file is not evidence that the running
service uses it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@pytest.fixture(scope="session")
def recon_node() -> str:
    return _env("RECON_NODE_NAME", "recon-01")


@pytest.fixture(scope="session")
def recon_host() -> str:
    return _env("RECON_HOST", "10.30.0.10")


@pytest.fixture(scope="session")
def rdp_port() -> int:
    return int(_env("RECON_RDP_PORT", "3389"))


@pytest.fixture(scope="session")
def peer_node() -> str:
    """A host on `vnet-it` other than `recon-01`, used as the source for
    reachability probes. `corp-it` is the natural choice: it is a real member of
    the zone, so a probe from it exercises the same path a real neighbour would
    (`recon.md` §3.1)."""
    return _env("RECON_PEER_NODE", "corp-it")


@pytest.fixture(scope="session")
def hist_dmz_host() -> str:
    return _env("HIST_DMZ_HOST", "10.20.0.20")


@pytest.fixture(scope="session")
def corp_it_host() -> str:
    return _env("CORP_IT_HOST", "10.30.0.20")


@pytest.fixture(scope="session")
def sim_host_it_address() -> str:
    """The sim's `vnet-it` interface — the address the reconciliation user actor
    dials out from. `recon.md` §3.2 requires that `recon-01` cannot dial *it*."""
    return _env("RECON_SIM_HOST", "10.30.0.11")


@pytest.fixture(scope="session")
def world() -> dict:
    path = Path(_env("RECON_WORLD_YAML", str(REPO_ROOT / "deploy" / "world.yaml")))
    if not path.exists():
        pytest.skip(f"world.yaml not found at {path}")
    return yaml.safe_load(path.read_text())


@pytest.fixture(scope="session", autouse=True)
def _require_incus_and_node(recon_node: str) -> None:
    """Skip the whole suite with a usable message rather than failing every test
    with a subprocess error when there is no deployment to test."""
    if shutil.which("incus") is None:
        pytest.skip("incus client not found on the test runner")
    probe = subprocess.run(
        ["incus", "exec", recon_node, "--", "true"], capture_output=True, text=True
    )
    if probe.returncode != 0:
        pytest.skip(
            f"{recon_node} is not running: {probe.stderr.strip() or 'unknown error'}"
        )


class Node:
    """Runs commands inside an Incus instance."""

    def __init__(self, name: str) -> None:
        self.name = name

    def run(self, *args: str, check: bool = False) -> subprocess.CompletedProcess:
        result = subprocess.run(
            ["incus", "exec", self.name, "--", *args],
            capture_output=True,
            text=True,
        )
        if check and result.returncode != 0:
            raise AssertionError(
                f"{self.name}: {' '.join(args)} failed ({result.returncode})\n"
                f"stdout: {result.stdout}\nstderr: {result.stderr}"
            )
        return result

    def stdout(self, *args: str) -> str:
        return self.run(*args, check=True).stdout

    def read(self, path: str) -> str:
        return self.stdout("cat", path)

    def path_exists(self, path: str) -> bool:
        return self.run("test", "-e", path).returncode == 0

    def can_reach(self, host: str, port: int, timeout: int = 5) -> bool:
        """True if a TCP connection to host:port completes from inside this
        instance. Uses Python's socket module rather than a network tool, since
        the images install none and adding one would change what is being
        tested."""
        script = (
            "import socket,sys\n"
            f"s=socket.socket(); s.settimeout({timeout})\n"
            f"sys.exit(0 if s.connect_ex(({host!r},{port}))==0 else 1)\n"
        )
        return self.run("python3", "-c", script).returncode == 0


@pytest.fixture(scope="session")
def recon(recon_node: str) -> Node:
    return Node(recon_node)


@pytest.fixture(scope="session")
def hist_host() -> str:
    return _env("HIST_HOST", "10.40.0.11")


@pytest.fixture(scope="session")
def hist() -> Node:
    """`hist-01`, inside which §5's records are seeded so that they reach the
    replica by replication, as the FMS's do."""
    name = _env("HIST_NODE_NAME", "hist-01")
    probe = subprocess.run(
        ["incus", "exec", name, "--", "true"], capture_output=True, text=True
    )
    if probe.returncode != 0:
        pytest.skip(f"{name} is not running, so no records can be seeded")
    return Node(name)


@pytest.fixture(scope="session")
def peer(peer_node: str) -> Node:
    probe = subprocess.run(
        ["incus", "exec", peer_node, "--", "true"], capture_output=True, text=True
    )
    if probe.returncode != 0:
        pytest.skip(f"peer node {peer_node} is not running")
    return Node(peer_node)
