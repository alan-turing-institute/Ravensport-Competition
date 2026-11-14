"""Fixtures of the episode runner's suite (episodes-tests.md §0.1, two
locations; §0.6, the first version).

The suite runs one benign suppressed episode of the first version on the full
topology through the episode runner's own code, with the idle defender,
test_02_adversarial_episode.py runs an adversarial one, and test_03_track_a.py
runs four Track A episodes, none suppressed. Two hooks read the
deployment during each episode. The first runs at the end of the settle
period, before the step session starts. The second runs after the export,
before the reset. Every read goes through Incus with its standard input
closed, and none uses su. The suite needs incus and the images of RavensPort,
and it needs every RavensPort node to be absent at its start, because each
episode deploys and resets. Where the deployed topology holds the adversary
host, the suite also needs the attack chains' binaries from `make build-attacks`.
The deployed topology is deploy/topology.yaml with the profile's override file
and the competition's additional override file (deploy.topology).
"""

from __future__ import annotations

import gzip
import hashlib
import json
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from competition.episode_runner import deploy, episode, export, schedule, settle
from competition.episode_runner.incus import Incus
from competition.episode_runner.steps import MonotonicClock, ProcessSession

REPO = Path(__file__).resolve().parents[3]
TRACK = "A"
ADVERSARY = "adversary"
# A first scan drawn within this span after the simulator's start falls inside
# every episode, whose steps run from about 115 seconds to about 835 seconds
# after the simulator's start.
SCAN_SPAN_S = (200.0, 450.0)
SCAN_STARTED = "wazuh-syscheckd: INFO: (6008): File integrity monitoring scan started."


def pytest_runtest_setup() -> None:
    if shutil.which("incus") is None:
        pytest.skip("incus client not found on the test runner")


def node(name: str, *argv: str, user: str | None = None) -> subprocess.CompletedProcess:
    """Run a command on a node, as user when given, by its numeric identifier."""
    as_user = ["--user", user] if user else []
    return subprocess.run(
        ["incus", "exec", name, *as_user, "--", *argv],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )


def query(path: str) -> dict[str, Any]:
    """An object of the Incus application programming interface (API), such as
    /1.0/instances/sim-host."""
    out = subprocess.run(
        ["incus", "query", path],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(out.stdout)


def image_store() -> dict[str, str]:
    """The full fingerprint of each image in the store, by alias."""
    out = subprocess.run(
        ["incus", "image", "list", "--format", "json"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=True,
    )
    return {
        alias["name"]: image["fingerprint"]
        for image in json.loads(out.stdout)
        for alias in image.get("aliases") or []
    }


def adversary_addresses() -> dict[str, dict[str, Any]]:
    """Each network interface of the adversary host other than loopback, with
    its state and its IPv4 addresses, from `ip -json address`."""
    out = node(ADVERSARY, "ip", "-json", "address", "show")
    return {
        link["ifname"]: {
            "up": "UP" in link.get("flags", []),
            "ipv4": [
                a["local"]
                for a in link.get("addr_info", [])
                if a.get("family") == "inet"
            ],
        }
        for link in json.loads(out.stdout or "[]")
        if link["ifname"] != "lo"
    }


def adversary_binaries() -> dict[str, str]:
    """The SHA-256 digest of each file in the adversary host's /root, by
    name."""
    out = node(
        ADVERSARY,
        "sh",
        "-c",
        "cd /root && find . -maxdepth 1 -type f -exec sha256sum {} +",
    )
    return {
        name.removeprefix("./"): digest
        for digest, name in (line.split(maxsplit=1) for line in out.stdout.splitlines())
    }


def suite_seed(interval_s: float, hosts: list[str]) -> str:
    """The first episode seed of a fixed sequence that draws a first scan
    within SCAN_SPAN_S for at least one monitored host."""
    for n in range(10_000):
        seed = hashlib.sha256(f"episodes suite {n}".encode()).hexdigest()
        times = schedule.first_scans(seed, hosts, interval_s)
        if any(SCAN_SPAN_S[0] <= t <= SCAN_SPAN_S[1] for t in times.values()):
            return seed
    raise AssertionError("no seed of the sequence draws a scan within the span")


def exported(record: Path, output: str) -> str:
    """The records of the exported dated files of `output`, alerts or
    archives, oldest day first, each compressed file decompressed."""
    months = export.MONTHS

    def day(path: Path) -> tuple[int, int, int]:
        year, month, name = path.relative_to(record / output).parts
        return int(year), months.index(month), int(name.split("-")[-1][:2])

    text = ""
    for path in sorted((record / output).glob("*/*/*.json*"), key=day):
        data = path.read_bytes()
        if path.suffix == ".gz":
            data = gzip.decompress(data)
        text += data.decode(errors="replace")
    return text


def parse(text: str) -> datetime:
    """A time of the episode record or of archives.json."""
    if text.endswith("+0000"):
        text = text[:-5] + "+00:00"
    return datetime.fromisoformat(text)


def ossec_time(line: str) -> datetime:
    """The time at the start of a line of an agent's ossec.log, such as
    2026/11/04 01:13:45, which the containers write in Coordinated Universal
    Time (UTC) to the second."""
    day, clock = line.split()[:2]
    return datetime.fromisoformat(f"{day.replace('/', '-')}T{clock}+00:00")


@dataclass
class EpisodeRun:
    seed: str
    outcome: episode.Outcome
    record: dict[str, Any]
    steps: list[dict[str, Any]]
    records: Path
    topology: dict[str, Any]
    at_step_0: dict[str, Any] = field(default_factory=dict)
    before_reset: dict[str, Any] = field(default_factory=dict)
    after_reset: dict[str, Any] = field(default_factory=dict)

    @property
    def path(self) -> Path:
        return self.outcome.record


@pytest.fixture(scope="session")
def episode_run(tmp_path_factory: pytest.TempPathFactory) -> EpisodeRun:
    topology = deploy.topology()
    present = {i["name"] for i in Incus().instances()} & set(topology["nodes"])
    if present:
        pytest.fail(
            f"RavensPort nodes already exist ({', '.join(sorted(present))}); "
            "run `make down` in deploy/ first"
        )
    records = tmp_path_factory.mktemp("records")
    scenario = episode.Scenario.from_files()
    monitored = [n for n in episode.SIEM_MONITORED_NODES if n in topology["nodes"]]
    seed = suite_seed(scenario.file_integrity_s, monitored)
    observed: dict[str, dict[str, Any]] = {
        "at_step_0": {},
        "before_reset": {},
        "after_reset": {},
    }

    def session(argv: list[str]) -> ProcessSession:
        rendered = deploy.manifest()
        at = observed["at_step_0"]
        at["instances"] = {n: query(f"/1.0/instances/{n}") for n in rendered["nodes"]}
        at["networks"] = {n: query(f"/1.0/networks/{n}") for n in rendered["networks"]}
        uid = node("sim-host", "id", "-u", "sim").stdout.strip()
        at["startup_as_sim_user"] = node(
            "sim-host", "cat", "/var/lib/sim/startup/startup.json", user=uid
        ).stdout
        at["state_dir"] = node(
            "sim-host", "ls", "-A", "/var/lib/sim/startup"
        ).stdout.split()
        if ADVERSARY in rendered["nodes"]:
            at["adversary_addresses"] = adversary_addresses()
            at["adversary_binaries"] = adversary_binaries()
        return ProcessSession(argv)

    def down(clock: MonotonicClock) -> deploy.MakeRun:
        (path,) = records.iterdir()
        requests = json.loads((path / "episode.json").read_text()).get(
            "scan_requests", []
        )
        observed["before_reset"]["scans_started"] = {
            r["host"]: node(
                r["host"], "grep", "-F", SCAN_STARTED, settle.OSSEC_LOG
            ).stdout.splitlines()
            for r in requests
        }
        if ADVERSARY in deploy.manifest()["nodes"]:
            observed["before_reset"]["adversary_addresses"] = adversary_addresses()
        made = deploy.down(clock)
        names = {i["name"] for i in Incus().instances()}
        observed["after_reset"]["remaining"] = sorted(names & set(topology["nodes"]))
        return made

    testbed = episode.Testbed(
        incus=Incus(), clock=MonotonicClock(), session=session, down=down
    )
    outcome = episode.run_episode(
        testbed, scenario, seed, TRACK, records, suppressed=True
    )
    if outcome.outcome != "horizon":
        pytest.fail(f"the episode ended {outcome.outcome}: {outcome.cause}")
    lines = (outcome.record / "steps.jsonl").read_text().splitlines()
    return EpisodeRun(
        seed=seed,
        outcome=outcome,
        record=json.loads((outcome.record / "episode.json").read_text()),
        steps=[json.loads(line) for line in lines],
        records=records,
        topology=topology,
        **observed,
    )
