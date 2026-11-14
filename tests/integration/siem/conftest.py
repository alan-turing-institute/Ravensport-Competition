"""
Fixtures for the security-monitoring integration suite (`siem-01` manager, IT
zone; a Wazuh agent on each of the twelve monitored hosts; Suricata on the
three firewall nodes), per siem-tests.md §0.

Environment variables (with defaults):

  SIEM_NODE_NAME       Incus node name for siem-01            (default: siem-01)
  SIEM_HOST            siem-01 address on vnet-it              (default: 10.30.0.30)
  SIEM_API_PORT        Management-interface port                (default: 55000)
  SIEM_AGENT_PORT      Agent-to-manager port                    (default: 1514)
  SIEM_WORLD_YAML      Path to world.yaml                       (default:
                       <repo>/deploy/world.yaml)
  SIEM_CA_CERT         Path to the CA's public root (build artefact, not a
                       secret)                                  (default:
                       <repo>/deploy/build/ca/ca.pem)
  SIEM_PEER_NODE       An IT-zone host other than siem-01, used as the source
                       for the §8 certificate-reachability probe (default:
                       corp-it)
  SIEM_INJECTOR_HOST   atg-relay-01's address on vnet-dmz, the TLS-protocol
                       frame injector's target                  (default:
                       10.20.0.10)
  SIEM_INJECTOR_PORT   Tank Level System port                    (default: 10001)

Prerequisites:
  - The full topology is deployed: siem-01 and every monitored host, built
    from deploy/build-scripts/, with a scenario in progress for the tests in
    §4 and §5 that assert on traffic the scenario itself produces.
  - The test runner has an `incus` client authorised against the same daemon.
  - For §5 only: the runner has joined vnet-ext —
    `make -f tests/integration/Makefile configure-siem` — since the
    boundary-inspection tests must exercise the real network path Suricata
    inspects rather than substitute incus exec (§0.5). This is the only suite
    in the repository that needs a presence on the external zone. Every other
    assertion here reads through incus exec and needs no runner placement
    (§0.3, §0.4).
"""

from __future__ import annotations

import itertools
import json
import os
import re
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.integration.siem.hosts import MONITORED_HOSTS

REPO_ROOT = Path(__file__).resolve().parents[3]


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Config:
    siem_node: str
    siem_host: str
    api_port: int
    agent_port: int
    world_yaml: Path
    ca_cert_path: Path
    peer_node: str
    injector_host: str
    injector_port: int

    @classmethod
    def from_env(cls) -> Config:
        return cls(
            siem_node=_env("SIEM_NODE_NAME", "siem-01"),
            siem_host=_env("SIEM_HOST", "10.30.0.30"),
            api_port=int(_env("SIEM_API_PORT", "55000")),
            agent_port=int(_env("SIEM_AGENT_PORT", "1514")),
            world_yaml=Path(
                _env("SIEM_WORLD_YAML", str(REPO_ROOT / "deploy" / "world.yaml"))
            ),
            ca_cert_path=Path(
                _env(
                    "SIEM_CA_CERT",
                    str(REPO_ROOT / "deploy" / "build" / "ca" / "ca.pem"),
                )
            ),
            peer_node=_env("SIEM_PEER_NODE", "corp-it"),
            injector_host=_env("SIEM_INJECTOR_HOST", "10.20.0.10"),
            injector_port=int(_env("SIEM_INJECTOR_PORT", "10001")),
        )


@pytest.fixture(scope="session")
def config() -> Config:
    return Config.from_env()


def pytest_runtest_setup(item: pytest.Item) -> None:
    """Skip with a usable message rather than failing every test with a
    subprocess or fixture error when there is no deployment to test.

    A `pytest_runtest_setup` hook, not an autouse fixture: pytest guarantees
    this runs before any fixture for the item is set up, whereas an autouse
    fixture has no guaranteed order relative to other same-scope fixtures
    (`world`/`siem_admin_credential` here) — those raise `KeyError` reading
    `identities.siem_admin` from a `world.yaml` that does not carry it yet,
    which happened even with an autouse fixture in place, before this fix.

    Exempts test_00_tls_frame_injector.py: that module tests the injector's
    frame-building and checksum logic in isolation, with no Incus
    infrastructure needed at all, the same "harness tests itself" property
    tests/integration/hist/test_00_pusher_wire_format.py has — it must run on
    a development machine with no deployment, not only on the deployment
    host."""
    if item.module.__name__.endswith("test_00_tls_frame_injector"):
        return
    if shutil.which("incus") is None:
        pytest.skip("incus client not found on the test runner")
    config = Config.from_env()
    probe = subprocess.run(
        ["incus", "exec", config.siem_node, "--", "true"],
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode != 0:
        pytest.skip(
            f"{config.siem_node} is not running: "
            f"{probe.stderr.strip() or 'unknown error'}"
        )


@pytest.fixture(scope="session")
def world(config: Config) -> dict:
    if not config.world_yaml.exists():
        pytest.skip(f"world.yaml not found at {config.world_yaml}")
    return yaml.safe_load(config.world_yaml.read_text())


@dataclass(frozen=True)
class Credential:
    username: str
    password: str


@pytest.fixture(scope="session")
def siem_admin_credential(world: dict) -> Credential:
    identity = world["identities"]["siem_admin"]
    return Credential(username=identity["username"], password=identity["password"])


# ---------------------------------------------------------------------------
# incus exec helper (the same Node pattern as tests/integration/recon)
# ---------------------------------------------------------------------------


class Node:
    """Runs commands inside an Incus instance."""

    def __init__(self, name: str) -> None:
        self.name = name

    def run(
        self, *args: str, input: str | None = None, check: bool = False
    ) -> subprocess.CompletedProcess:
        result = subprocess.run(
            ["incus", "exec", self.name, "--", *args],
            input=input,
            capture_output=True,
            text=True,
            check=False,
        )
        if check and result.returncode != 0:
            raise AssertionError(
                f"{self.name}: {' '.join(args)} failed ({result.returncode})\n"
                f"stdout: {result.stdout}\nstderr: {result.stderr}"
            )
        return result

    def stdout(self, *args: str, input: str | None = None) -> str:
        return self.run(*args, input=input, check=True).stdout

    def read(self, path: str) -> str:
        return self.stdout("cat", path)

    def path_exists(self, path: str) -> bool:
        return self.run("test", "-e", path).returncode == 0

    def can_reach(self, host: str, port: int, timeout: int = 5) -> bool:
        """True if a TCP connection to host:port completes from inside this
        instance. Uses Python's socket module rather than a network tool,
        since the images install none and adding one would change what is
        being tested."""
        script = (
            "import socket,sys\n"
            f"s=socket.socket(); s.settimeout({timeout})\n"
            f"sys.exit(0 if s.connect_ex(({host!r},{port}))==0 else 1)\n"
        )
        return self.run("python3", "-c", script).returncode == 0


@pytest.fixture(scope="session")
def siem(config: Config) -> Node:
    return Node(config.siem_node)


@pytest.fixture(scope="session")
def peer(config: Config) -> Node:
    probe = subprocess.run(
        ["incus", "exec", config.peer_node, "--", "true"],
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode != 0:
        pytest.skip(f"peer node {config.peer_node} is not running")
    return Node(config.peer_node)


@pytest.fixture(scope="session")
def monitored_nodes() -> dict[str, Node]:
    return {name: Node(name) for name in MONITORED_HOSTS}


@pytest.fixture(params=MONITORED_HOSTS)
def monitored_node(request: pytest.FixtureRequest) -> Node:
    """Parametrising fixture: one Node per monitored host, for tests that must
    run once per host rather than once per suite."""
    return Node(request.param)


# ---------------------------------------------------------------------------
# Reading alerts.json / archives.json (§0.3)
# ---------------------------------------------------------------------------


class OutputReader:
    """Reads siem-01's two output files via incus exec (siem.md §6). Each
    line is parsed independently; a line that fails to decode is skipped
    rather than failing the whole read, since a record mid-write when the
    file is sampled is not itself a defect."""

    _ALERTS_PATH = "/var/ossec/logs/alerts/alerts.json"
    _ARCHIVES_PATH = "/var/ossec/logs/archives/archives.json"
    _ALERTS_LOG_PATH = "/var/ossec/logs/alerts/alerts.log"

    def __init__(self, node: Node) -> None:
        self._node = node

    def _read_records(self, path: str) -> list[dict[str, Any]]:
        raw = self._node.run("cat", path).stdout
        records = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return records

    def alerts(self) -> list[dict[str, Any]]:
        return self._read_records(self._ALERTS_PATH)

    def archives(self) -> list[dict[str, Any]]:
        return self._read_records(self._ARCHIVES_PATH)

    def alerts_log_lines(self) -> list[str]:
        return self._node.run("cat", self._ALERTS_LOG_PATH).stdout.splitlines()

    def snapshot(self) -> tuple[int, int]:
        """Current (alerts, archives) record counts, so a later `wait_for_*`
        search can be bounded to records written after this point."""
        return (len(self.alerts()), len(self.archives()))

    def wait_for_alert(
        self,
        predicate: Callable[[dict[str, Any]], bool],
        *,
        since: int = 0,
        timeout: float = 15.0,
        poll_interval: float = 1.0,
    ) -> dict[str, Any] | None:
        """Poll alerts.json for a record matching `predicate` at index >=
        `since`. Returns the first match, or None on timeout."""
        deadline = time.monotonic() + timeout
        while True:
            for record in self.alerts()[since:]:
                if predicate(record):
                    return record
            if time.monotonic() >= deadline:
                return None
            time.sleep(poll_interval)

    def wait_for_archive(
        self,
        predicate: Callable[[dict[str, Any]], bool],
        *,
        since: int = 0,
        timeout: float = 15.0,
        poll_interval: float = 1.0,
    ) -> dict[str, Any] | None:
        deadline = time.monotonic() + timeout
        while True:
            for record in self.archives()[since:]:
                if predicate(record):
                    return record
            if time.monotonic() >= deadline:
                return None
            time.sleep(poll_interval)


@pytest.fixture(scope="session")
def outputs(siem: Node) -> OutputReader:
    return OutputReader(siem)


# ---------------------------------------------------------------------------
# Deliberate events on a monitored host (§0.4)
# ---------------------------------------------------------------------------


_next_auth_failure_port = itertools.count(50000)

# Hosts whose agent has already been seen past its first-connection reload.
_agents_past_reload: set[str] = set()

# The shared configuration's merged file, which the manager pushes to each
# agent on first connection.
_SHARED_CONFIGURATION = "/var/ossec/etc/shared/merged.mg"

# How long after the merged file is written the reload is complete: the
# agent restarts its daemons as soon as it has written the file, and the
# restarted log collector reopens the journal within about 7 s (confirmed
# live).
_RELOAD_COMPLETE_AFTER_S = 30


def await_agent_past_first_reload(node: Node, timeout: float = 120.0) -> None:
    """Wait until the host's agent has completed the one reload the product
    performs after first connecting.

    About 20 s after first connecting, the agent receives the shared
    configuration from the manager (an empty agent.conf, with the default
    group's files), writes it to the merged file, and applies it by
    restarting its own daemons. A line written to the journal while the log
    collector is restarting is never read, so a deliberate event triggered
    in that window on a newly deployed host is lost. The reload is complete
    once the merged file exists and was written long enough ago.

    Both times are read from the host's own clock. The start time `ps`
    reports for a process in these containers is offset from it (confirmed
    live: 14 s early), so it cannot be compared with the file's."""
    if node.name in _agents_past_reload:
        return
    script = (
        f"pushed=$(stat -c %Y {_SHARED_CONFIGURATION}) || exit 1; "
        f"[ $(( $(date +%s) - pushed )) -ge {_RELOAD_COMPLETE_AFTER_S} ]"
    )
    deadline = time.monotonic() + timeout
    while node.run("sh", "-c", script, check=False).returncode != 0:
        assert time.monotonic() < deadline, (
            f"{node.name}: agent has not completed its first-connection reload"
        )
        time.sleep(2.0)
    _agents_past_reload.add(node.name)


def trigger_failed_authentication(node: Node, source_ip: str = "10.30.0.99") -> str:
    """A failed authentication event, injected as a realistic sshd log line
    via `logger` rather than attempted through `sudo`/`su`.

    Both `sudo -S` and `su` skip password verification entirely when the
    invoking process already holds root — which every `incus exec` command
    does — so neither can be made to produce a genuine failure this way
    (confirmed: piping a wrong password to `sudo -S` under `incus exec`
    still exits 0). `logger` writes the exact line sshd itself would for a
    failed login, tagged and prioritised the way sshd tags it; Wazuh's
    stock decoder and rules key on that line's content, not on the
    mechanism that produced it. A monotonically increasing source port
    keeps repeated calls distinct while holding `source_ip` fixed, so a
    frequency rule keyed on `same_source_ip` (e.g. rule 5763, siem.md
    §2.2) can still correlate them. Returns the line logged, so a caller can
    match its own event rather than any record from the host."""
    await_agent_past_first_reload(node)
    port = next(_next_auth_failure_port)
    line = f"Failed password for root from {source_ip} port {port} ssh2"
    node.run("logger", "-p", "auth.warning", "-t", "sshd", "--", line, check=True)
    return line


def trigger_service_restart(node: Node, unit: str = "cron") -> int:
    """Restart a unit, by default one present on every monitored host's base
    image, so this helper needs no per-host unit table. Returns the restarted
    unit's new main process identifier, so a caller can match the new
    process's own output rather than any record from the host."""
    await_agent_past_first_reload(node)
    node.run("systemctl", "restart", unit, check=True)
    return int(
        node.run(
            "systemctl", "show", "--property=MainPID", "--value", unit, check=True
        ).stdout
    )


# Every monitored host reports continuously (connection records, scheduled
# jobs, its own agent), so a deliberate event is matched by its own record,
# never by any record from the host.


def carries_line(host: str, line: str) -> Callable[[dict[str, Any]], bool]:
    """Matches the record of a line the test itself wrote on `host`, such as
    the one `trigger_failed_authentication` returns."""

    def match(record: dict[str, Any]) -> bool:
        return record.get("agent", {}).get("name") == host and line in str(
            record.get("full_log", "")
        )

    return match


def systemd_reports(host: str, message: str) -> Callable[[dict[str, Any]], bool]:
    """Matches systemd's own record on `host` containing `message`, such as
    `Started cron.service` after `trigger_service_restart`."""

    def match(record: dict[str, Any]) -> bool:
        return (
            record.get("agent", {}).get("name") == host
            and record.get("predecoder", {}).get("program_name") == "systemd"
            and message in str(record.get("full_log", ""))
        )

    return match


def process_output(
    host: str, program: str, pid: int
) -> Callable[[dict[str, Any]], bool]:
    """Matches a line the process `pid` on `host` wrote to the journal under
    its own program name."""

    def match(record: dict[str, Any]) -> bool:
        return (
            record.get("agent", {}).get("name") == host
            and record.get("predecoder", {}).get("program_name") == program
            and f" {program}[{pid}]: " in str(record.get("full_log", ""))
        )

    return match


# ---------------------------------------------------------------------------
# wazuh-logtest (§0.6)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LogtestResult:
    rule_id: str | None
    # The decoder that matched in phase 2, and the fields it extracted.
    # Phase 1 (pre-decoding) prints `full event`, `timestamp` and others for
    # every line, decoded or not, so it is excluded.
    decoder: str | None
    decoded_fields: dict[str, str]
    # The fields phase 3 reports for the matched rule (`level`, `mitre.id`,
    # and others); empty when no rule matched.
    rule_fields: dict[str, str]
    raw_output: str


class WazuhLogtest:
    """Wraps `wazuh-logtest`, fed one line on stdin, run via incus exec on
    siem-01 (§0.6). Needs no live agent connection or generated traffic."""

    _RULE_ID = re.compile(r"id:\s*'?(\d+)'?")
    _PHASE_2 = re.compile(
        r"^\*\*Phase 2: Completed decoding\.\n(.*?)(?=^\*\*|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    _PHASE_3 = re.compile(
        r"^\*\*Phase 3: Completed filtering \(rules\)\.\n(.*?)(?=^\*\*|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    _FIELD = re.compile(r"^\s*(\S+):\s*'(.*)'\s*$", re.MULTILINE)

    def __init__(self, node: Node) -> None:
        self._node = node

    def decode(self, line: str) -> LogtestResult:
        result = self._node.run("/var/ossec/bin/wazuh-logtest", input=line + "\n")
        output = result.stdout + result.stderr
        rule_match = self._RULE_ID.search(output)
        phase_2 = self._PHASE_2.search(output)
        fields = dict(self._FIELD.findall(phase_2.group(1))) if phase_2 else {}
        decoder = fields.pop("name", None)
        fields.pop("parent", None)
        phase_3 = self._PHASE_3.search(output)
        return LogtestResult(
            rule_id=rule_match.group(1) if rule_match else None,
            decoder=decoder,
            decoded_fields=fields,
            rule_fields=dict(self._FIELD.findall(phase_3.group(1))) if phase_3 else {},
            raw_output=output,
        )

    def rule_ids_in_one_session(self, lines: list[str]) -> list[str | None]:
        """Feed `lines` to one `wazuh-logtest` run, which evaluates them in a
        single session, so a frequency rule correlates across them. Returns
        the rule each line matched, in order."""
        result = self._node.run(
            "/var/ossec/bin/wazuh-logtest", input="".join(f"{ln}\n" for ln in lines)
        )
        output = result.stdout + result.stderr
        blocks = output.split("**Phase 1: Completed pre-decoding.")[1:]
        assert len(blocks) == len(lines), output
        ids = []
        for block in blocks:
            phase_3 = self._PHASE_3.search(block)
            ids.append(
                dict(self._FIELD.findall(phase_3.group(1))).get("id")
                if phase_3
                else None
            )
        return ids


@pytest.fixture(scope="session")
def logtest(siem: Node) -> WazuhLogtest:
    return WazuhLogtest(siem)


# ---------------------------------------------------------------------------
# Management-interface API client (§0.2)
#
# Executed via incus exec inside siem-01 itself, against its own name —
# consistent with §0.3's "the management interface's own state is
# read via incus exec siem-01" — rather than depending on the runner having a
# route to vnet-it, which this suite's only configure- step does not
# establish (§0.5 places the runner on vnet-ext, for the frame injector only).
# The one exception is §8's certificate-reachability test, which deliberately
# probes from a different IT-zone host to exercise a real network path.
# ---------------------------------------------------------------------------


class WazuhAPI:
    """Thin client for the management interface (TCP 55000), wrapping only
    what the tests need: authentication, agent connection state, rule and
    decoder listing. It cannot retrieve alerts (siem.md §6) — see
    OutputReader for those."""

    def __init__(
        self,
        node: Node,
        base_url: str,
        username: str,
        password: str,
        *,
        verify_cert: bool = False,
    ) -> None:
        self._node = node
        self._base_url = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._verify_cert = verify_cert
        self._token: str | None = None

    def _curl(self, args: list[str]) -> subprocess.CompletedProcess:
        insecure = [] if self._verify_cert else ["--insecure"]
        return self._node.run(
            "curl", "--silent", "--show-error", *insecure, "-w", "\n%{http_code}", *args
        )

    def _authenticate(self) -> str:
        result = self._curl(
            [
                "--user",
                f"{self._username}:{self._password}",
                "-X",
                "POST",
                f"{self._base_url}/security/user/authenticate",
            ]
        )
        body, _, status = result.stdout.rpartition("\n")
        if not status.strip().startswith("2"):
            raise RuntimeError(f"authentication failed (HTTP {status.strip()}): {body}")
        return json.loads(body)["data"]["token"]

    def _get_once(self, path: str, params: dict[str, str] | None) -> tuple[int, Any]:
        args = [
            "-H",
            f"Authorization: Bearer {self._token}",
            "--get",
            f"{self._base_url}{path}",
        ]
        for key, value in (params or {}).items():
            args += ["--data-urlencode", f"{key}={value}"]
        result = self._curl(args)
        body, _, status = result.stdout.rpartition("\n")
        return int(status.strip() or "0"), (json.loads(body) if body else None)

    def get(self, path: str, params: dict[str, str] | None = None) -> tuple[int, Any]:
        """A cached token (`security.auth_token_exp_timeout`, 900s by default)
        does not survive this suite's own longer waits (the periodic
        listening-port scan alone is a multi-minute window), so a 401 here is
        expected to mean an expired token, not a rejected request — retried
        once against a freshly authenticated token before it is reported."""
        if self._token is None:
            self._token = self._authenticate()
        status, payload = self._get_once(path, params)
        if status == 401:
            self._token = self._authenticate()
            status, payload = self._get_once(path, params)
        return status, payload

    def agents(self) -> list[dict[str, Any]]:
        status, payload = self.get("/agents", {"limit": "100"})
        if status != 200:
            raise RuntimeError(f"GET /agents returned {status}: {payload}")
        return payload["data"]["affected_items"]

    def agent_status(self, name: str) -> str | None:
        for agent in self.agents():
            if agent["name"] == name:
                return agent["status"]
        return None

    def rules(self) -> list[dict[str, Any]]:
        status, payload = self.get("/rules", {"limit": "500"})
        if status != 200:
            raise RuntimeError(f"GET /rules returned {status}: {payload}")
        return payload["data"]["affected_items"]

    def decoders(self) -> list[dict[str, Any]]:
        status, payload = self.get("/decoders", {"limit": "500"})
        if status != 200:
            raise RuntimeError(f"GET /decoders returned {status}: {payload}")
        return payload["data"]["affected_items"]


@pytest.fixture(scope="session")
def wazuh_api(
    siem: Node, config: Config, siem_admin_credential: Credential
) -> WazuhAPI:
    # siem-01 resolves its own name to its own address and trusts the
    # RavensPort internal CA, so every call validates the presented
    # certificate against the CA's root and the name siem-01 (§0.2).
    return WazuhAPI(
        siem,
        f"https://siem-01:{config.api_port}",
        siem_admin_credential.username,
        siem_admin_credential.password,
        verify_cert=True,
    )
