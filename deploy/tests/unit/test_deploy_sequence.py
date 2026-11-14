"""
The start order of `make deploy` (deployment.md §8): sim-host starts only after
the FMS, the HMI and the directory on corp-it have each passed a readiness
probe.

The sim's dock operator logs in at the HMI as soon as the sim starts. The HMI
forwards the login to the FMS, which verifies the password with a bind against
the directory on corp-it (sim-network.md §7, fms.md §4), and a 401 at login is
fatal to the dock operator (sim-impl.md, error taxonomy). With probes of the FMS
and the HMI alone, one of six fresh deployments on a Linux test host failed:
the dock operator logged in 3.4 s after sim-host started, before slapd on
corp-it listened on port 636, and the simulator stopped.

deploy.sh runs here in a copy of deploy/ against two stub commands. `incus`
records every command it is given and answers the three probes; `sleep` records
its argument and returns at once. The stub's directory probe behaves as
`openssl s_client` does against slapd: a refused connection exits 1, and a
completed handshake exits 0 once standard input ends. On an open standard input
s_client keeps the connection open until `timeout` stops it (exit 124), so every
run here gives deploy.sh an open standard input, as a terminal does.

Run with: pytest tests/unit -q
"""

from __future__ import annotations

import itertools
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]  # deploy/

FMS_PROBE = (
    "incus exec fms-01 -- curl -fsk --max-time 3 https://127.0.0.1/api/v1/health"
)
HMI_PROBE = "incus exec fms-01 -- curl -fsk --max-time 3 https://10.40.0.12/health"
DIRECTORY_PROBE = (
    "incus exec fms-01 -- timeout 3 openssl s_client -connect corp-it:636 -brief"
)
SIM_START = "incus start sim-host"
# Five-second intervals for up to 120 seconds (deployment.md §8).
ATTEMPTS = 24

# The end of build/incus-commands.sh as render.py writes it: every instance is
# started, sim-host included, and deploy.sh removes sim-host's line.
INCUS_COMMANDS = """#!/usr/bin/env bash
set -euo pipefail
# --- Start ---
incus start fw-ot-dmz
incus start fw-dmz-it
incus start corp-it
incus start fms-01
incus start hmi-01
incus start sim-host
"""

# The stub `incus`. The state file holds the number of connections the
# directory refuses before slapd listens, or null if it never listens.
STUB_INCUS = r"""
import json
import os
import select
import sys
from pathlib import Path

LOG = Path(os.environ["STUB_LOG"])
STATE = Path(os.environ["STUB_STATE"])


def directory_probe(state):
    refusals = state["directory_refusals"]
    if refusals is None or refusals > 0:
        if refusals is not None:
            state["directory_refusals"] = refusals - 1
            STATE.write_text(json.dumps(state))
        print("connect:errno=111", file=sys.stderr)
        return 1
    # The handshake has completed. s_client closes the connection and exits
    # when its standard input ends; on an open input, timeout stops it.
    readable, _, _ = select.select([sys.stdin], [], [], 0.5)
    if not readable or os.read(sys.stdin.fileno(), 1):
        return 124
    return 0


def main(args):
    with LOG.open("a") as log:
        log.write("incus " + " ".join(args) + "\n")
    state = json.loads(STATE.read_text())
    match args:
        case ["start", _]:
            return 0
        case ["exec", "fms-01", "--", "curl", *rest] if (
            "https://127.0.0.1/api/v1/health" in rest
            or "https://10.40.0.12/health" in rest
        ):
            print('{"status":"ok"}')
            return 0
        case ["exec", "fms-01", "--", "timeout", _, "openssl", "s_client", *rest] if (
            "corp-it:636" in rest
        ):
            return directory_probe(state)
    print(f"Error: the stub does not model incus {' '.join(args)}", file=sys.stderr)
    return 1


sys.exit(main(sys.argv[1:]))
"""

STUB_SLEEP = """#!/bin/sh
echo "sleep $*" >>"$STUB_LOG"
"""


class Deployment:
    """A copy of deploy/scripts/ beside a rendered build/incus-commands.sh, with
    the stub commands first on the path."""

    def __init__(self, path: Path, directory_refusals: int | None = 0):
        self.root = path / "deploy"
        scripts = self.root / "scripts"
        scripts.mkdir(parents=True)
        # Every shell script, so that a script deploy.sh sources is present.
        for script in (ROOT / "scripts").glob("*.sh"):
            shutil.copy(script, scripts / script.name)
        (self.root / "build").mkdir()
        (self.root / "build" / "incus-commands.sh").write_text(INCUS_COMMANDS)

        bin_dir = path / "bin"
        bin_dir.mkdir()
        stub = path / "stub_incus.py"
        stub.write_text(STUB_INCUS)
        (bin_dir / "incus").write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "{stub}" "$@"\n'
        )
        (bin_dir / "sleep").write_text(STUB_SLEEP)
        for command in bin_dir.iterdir():
            command.chmod(0o755)

        self.log_file = path / "commands.log"
        self.log_file.touch()
        state_file = path / "incus-state.json"
        state_file.write_text(json.dumps({"directory_refusals": directory_refusals}))
        self.env = {
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "STUB_LOG": str(self.log_file),
            "STUB_STATE": str(state_file),
        }

    def run(self) -> subprocess.CompletedProcess:
        """Run deploy.sh with a standard input that stays open until it exits."""
        read_end, write_end = os.pipe()
        try:
            return subprocess.run(
                ["bash", str(self.root / "scripts" / "deploy.sh")],
                cwd=self.root,
                env=self.env,
                stdin=read_end,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        finally:
            os.close(read_end)
            os.close(write_end)

    @property
    def log(self) -> list[str]:
        return self.log_file.read_text().splitlines()

    def indices(self, command: str) -> list[int]:
        """The positions in the log of every run of exactly `command`."""
        return [i for i, line in enumerate(self.log) if line == command]

    def index(self, command: str) -> int:
        """The position in the log of the first run of exactly `command`."""
        found = self.indices(command)
        assert found, f"{command!r} was not run; the log is:\n" + "\n".join(self.log)
        return found[0]


@pytest.fixture
def deployment(tmp_path: Path) -> Deployment:
    return Deployment(tmp_path)


def test_sim_host_starts_after_the_fms_hmi_and_directory_probes(
    deployment: Deployment,
):
    """The dock operator's first login needs the HMI, the FMS and the
    directory, so sim-host's start follows all three probes, and is the only
    start of sim-host."""
    result = deployment.run()
    assert result.returncode == 0, result.stderr

    fms = deployment.index(FMS_PROBE)
    hmi = deployment.index(HMI_PROBE)
    directory = deployment.index(DIRECTORY_PROBE)
    sim_start = deployment.index(SIM_START)
    assert fms < hmi < directory < sim_start, "\n".join(deployment.log)
    assert deployment.indices(SIM_START) == [len(deployment.log) - 1], (
        "sim-host must be started once, by deploy.sh after the probes, and "
        "never by build/incus-commands.sh:\n" + "\n".join(deployment.log)
    )
    # The directory accepted the first connection. A probe that read deploy.sh's
    # open standard input would have waited for timeout and been retried.
    assert deployment.indices(DIRECTORY_PROBE) == [directory]


def test_sim_host_waits_while_the_directory_refuses_connections(tmp_path: Path):
    """The failure seen on the test host: corp-it running, and slapd not yet
    listening, so a connection to port 636 is refused. The probe retries at
    five-second intervals, as the FMS and HMI probes do, and sim-host starts
    after the first connection the directory accepts."""
    deployment = Deployment(tmp_path, directory_refusals=3)
    result = deployment.run()
    assert result.returncode == 0, result.stderr

    probes = deployment.indices(DIRECTORY_PROBE)
    assert len(probes) == 4, "\n".join(deployment.log)
    for before, after in itertools.pairwise(probes):
        assert deployment.log[before + 1 : after] == ["sleep 5"]
    assert probes[-1] < deployment.index(SIM_START)


def test_sim_host_is_not_started_while_the_directory_never_listens(tmp_path: Path):
    """After 120 seconds without a connection to the directory the deployment
    fails, naming corp-it, and sim-host is never started."""
    deployment = Deployment(tmp_path, directory_refusals=None)
    result = deployment.run()

    assert result.returncode != 0, result.stdout
    assert "corp-it" in result.stderr, result.stderr
    assert len(deployment.indices(DIRECTORY_PROBE)) == ATTEMPTS
    assert deployment.indices(SIM_START) == [], "\n".join(deployment.log)
