"""The example Track A entry with Packetbeat
(competition/baselines/track_a/packetbeat/README.md).

The defender reads Packetbeat's events from archives.json through the query
interface, and raises the Track A alarm at the first connection to the Wazuh
manager's management interface or enrolment service, or at a connection to
the Wazuh agents' channel from an address that is not a monitored host's. The
tests build the events from Packetbeat's own output, which the entry's build
captured in its smoke test (data/packetbeat-events.ndjson), and wrap each one
in the record that the Wazuh manager archives.

The tests of the step session need the shared step protocol,
competition/baselines/track_a/common/protocol.py, and are skipped where the
repository does not hold it.

Run with: pytest competition/tests -q
"""

from __future__ import annotations

import copy
import importlib.machinery
import importlib.util
import json
import os
import re
import select
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
ENTRY = REPO / "competition" / "baselines" / "track_a" / "packetbeat"
PROTOCOL = ENTRY.parent / "common" / "protocol.py"
SIEM_READ = REPO / "competition" / "siem_read" / "siem-read"
EVENTS = Path(__file__).resolve().parent / "data" / "packetbeat-events.ndjson"

COMPETITION = REPO / "competition" / "deploy" / "override.yaml"


def program(name: str, path: Path) -> Any:
    """A program of the entry, loaded as a module."""
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    module = importlib.util.module_from_spec(
        importlib.util.spec_from_loader(loader.name, loader)
    )
    loader.exec_module(module)
    return module


defender = program("packetbeat_defender", ENTRY / "defender")
override = program("packetbeat_override", ENTRY / "override.py")

SIEM = "10.30.0.30"
ADVERSARY = "10.30.0.50"


def captured(kind: str, port: int) -> dict[str, Any]:
    """The first event of a kind, flow or tls, that Packetbeat reported for a
    connection to a port in the entry's smoke test."""
    for line in EVENTS.read_text().splitlines():
        event = json.loads(line)
        if event["type"] == kind and event["destination"]["port"] == port:
            return event
    raise LookupError(f"no {kind} event to port {port}")


def event(kind: str, port: int, source: str, destination: str = SIEM) -> dict[str, Any]:
    """A captured event, readdressed as a connection from source to
    destination on siem-01's network."""
    e = copy.deepcopy(captured(kind, port))
    e["source"]["ip"] = source
    e["destination"]["ip"] = destination
    for side, address in (("client", source), ("server", destination)):
        if side in e:
            e[side]["ip"] = address
    return e


def as_strings(value: Any) -> Any:
    """A decoded event as the Wazuh manager writes it in archives.json, with
    each number as a string."""
    if isinstance(value, dict):
        return {k: as_strings(v) for k, v in value.items()}
    if isinstance(value, list):
        return [as_strings(v) for v in value]
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return str(value)
    return value


def archived(e: dict[str, Any], decoded: bool = True) -> dict[str, Any]:
    """The record that the Wazuh manager archives for one of Packetbeat's
    events: siem-01's own record of the log input, with the decoded fields
    under data and the original line in full_log."""
    record: dict[str, Any] = {
        "timestamp": "2026-11-13T00:00:05.000+0000",
        "agent": {"id": "000", "name": "siem-01"},
        "manager": {"name": "siem-01"},
        "id": "1794528005.1",
        "full_log": json.dumps(e, separators=(",", ":")),
        "decoder": {"name": "json"},
        "location": defender.EVENTS,
    }
    if decoded:
        record["data"] = as_strings(e)
    return record


# ── The alarm rule ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("kind", ["flow", "tls"])
def test_a_connection_to_the_management_interface_raises_the_alarm(kind: str) -> None:
    reason = defender.finding(as_strings(event(kind, 55000, ADVERSARY)))
    assert reason is not None
    assert "management interface" in reason and ADVERSARY in reason


def test_a_connection_to_the_enrolment_service_raises_the_alarm() -> None:
    reason = defender.finding(as_strings(event("flow", 1515, ADVERSARY)))
    assert reason is not None and "enrolment service" in reason


def test_a_monitored_host_may_reach_the_management_interface_neither() -> None:
    # No process of the port uses the interface, so its source does not matter.
    assert defender.finding(event("flow", 55000, "10.30.0.20")) is not None


@pytest.mark.parametrize("address", sorted(defender.MONITORED_HOSTS))
def test_an_agent_connection_from_a_monitored_host_raises_no_alarm(
    address: str,
) -> None:
    assert defender.finding(as_strings(event("flow", 1514, address))) is None


@pytest.mark.parametrize("address", [ADVERSARY, "10.30.0.11", "10.30.0.12", SIEM])
def test_an_agent_connection_from_another_address_raises_the_alarm(
    address: str,
) -> None:
    reason = defender.finding(as_strings(event("flow", 1514, address)))
    assert reason is not None and "agents' channel" in reason


def test_a_port_with_a_fractional_part_is_read_as_a_port() -> None:
    e = as_strings(event("flow", 55000, ADVERSARY))
    e["destination"]["port"] = "55000.000000"
    assert defender.finding(e) is not None


def test_a_flow_of_another_transport_raises_no_alarm() -> None:
    e = event("flow", 55000, ADVERSARY)
    e["network"]["transport"] = "udp"
    assert defender.finding(e) is None


def test_an_event_without_a_destination_raises_no_alarm() -> None:
    e = event("flow", 55000, ADVERSARY)
    del e["destination"]
    assert defender.finding(e) is None


# ── Reading the archived records ──────────────────────────────────────────────


def test_the_decoded_fields_give_the_event() -> None:
    record = archived(event("flow", 55000, ADVERSARY))
    assert defender.packetbeat_event(record) == record["data"]


def test_the_original_line_gives_the_event_where_no_field_was_decoded() -> None:
    e = event("tls", 55000, ADVERSARY)
    assert defender.packetbeat_event(archived(e, decoded=False)) == e


@pytest.mark.parametrize(
    "record",
    [
        # A connection record of a monitored host's Wazuh agent.
        {
            "agent": {"name": "recon-01"},
            "location": "/var/log/ulog/connections.json",
            "full_log": '{"src_ip":"10.30.0.10","dest_ip":"10.30.0.30","dest_port":1514}',
            "data": {
                "src_ip": "10.30.0.10",
                "dest_ip": "10.30.0.30",
                "dest_port": "1514",
            },
        },
        # A command's output on siem-01.
        {
            "agent": {"name": "siem-01"},
            "location": "df -P",
            "full_log": "ossec: output: 'df -P'",
        },
        {"agent": {"name": "siem-01"}, "location": "journald", "full_log": "{not json"},
    ],
)
def test_a_record_of_another_kind_holds_no_event(record: dict[str, Any]) -> None:
    assert defender.packetbeat_event(record) is None


def test_the_detector_raises_the_alarm_at_the_first_finding_and_keeps_it() -> None:
    detector = defender.Detector()
    benign = [
        archived(event("flow", 1514, address)) for address in defender.MONITORED_HOSTS
    ]
    assert detector.observe(benign) is False
    assert detector.finding is None
    attack = archived(event("flow", 1515, ADVERSARY))
    assert detector.observe(benign + [attack, archived(event("tls", 55000, ADVERSARY))])
    assert "enrolment service" in detector.finding
    assert detector.observe([]) is True


# ── Consistency of the entry's files ──────────────────────────────────────────


def test_the_defender_reads_what_its_telemetry_specification_declares() -> None:
    telemetry = json.loads((ENTRY / "telemetry.json").read_text())
    assert telemetry == {
        "reads": [
            {"output": "archives", "host": defender.HOST, "input": defender.EVENTS}
        ]
    }


def test_the_wazuh_manager_reads_the_file_that_packetbeat_writes() -> None:
    localfile = (ENTRY / "localfile.xml").read_text()
    assert f"<location>{defender.EVENTS}</location>" in localfile
    assert "<log_format>json</log_format>" in localfile
    unit = (ENTRY / "packetbeat.service").read_text()
    assert f"StandardOutput=append:{defender.EVENTS}\n" in unit
    template = (ENTRY / "build-scripts" / "siem-01-packetbeat.sh.j2").read_text()
    assert f"install -m 0640 /dev/null {defender.EVENTS}" in template


def test_the_capture_passes_the_ports_of_the_alarm_rule() -> None:
    config = yaml.safe_load((ENTRY / "packetbeat.yml").read_text())
    (interface,) = config["packetbeat.interfaces"]
    assert interface["device"] == "eth0" and interface["type"] == "af_packet"
    ports = {int(p) for p in re.findall(r"port (\d+)", interface["bpf_filter"])}
    assert ports == set(defender.INTERFACES) | {defender.AGENTS_PORT}
    # Each clause names siem-01's address and the direction of its port, so
    # that a probe from source port 55000 or 1515 to another port of siem-01
    # does not pass.
    clauses = re.findall(r"\((dst|src) host (\S+) and", interface["bpf_filter"])
    assert {address for _, address in clauses} == {SIEM}
    assert "tcp port" not in interface["bpf_filter"]
    (tls,) = config["packetbeat.protocols"]
    assert tls["type"] == "tls" and set(tls["ports"]) == set(defender.INTERFACES)
    assert "output.console" in config


def test_siem_01_has_one_interface_named_eth0() -> None:
    topology = yaml.safe_load((REPO / "deploy" / "topology.yaml").read_text())
    (nic,) = topology["nodes"]["siem-01"]["nics"]
    assert "name" not in nic and nic["ipv4"] == SIEM


def test_the_monitored_hosts_are_the_hosts_with_a_wazuh_agent() -> None:
    topology = yaml.safe_load((REPO / "deploy" / "topology.yaml").read_text())
    scripts = REPO / "deploy" / "build-scripts"
    with_agent = {
        name
        for name, node in topology["nodes"].items()
        if node.get("build_script")
        and "_wazuh-agent.sh.j2"
        in (scripts / f"{node['build_script']}.sh.j2").read_text()
    }
    assert set(defender.MONITORED_HOSTS.values()) == with_agent
    assert len(with_agent) == 12
    for address, name in defender.MONITORED_HOSTS.items():
        nics = topology["nodes"][name]["nics"]
        # A host's one address; for a firewall node, its address on the IT
        # segment, or else the one whose route reaches that segment.
        on_path = (
            nics
            if len(nics) == 1
            else [n for n in nics if n["network"] == "vnet-it"]
            or [
                n
                for n in nics
                if any(r["to"] == "10.30.0.0/24" for r in n.get("routes", []))
            ]
        )
        assert [n["ipv4"] for n in on_path] == [address], name


def test_the_entry_override_file_is_the_competitions_with_the_entrys_siem_01(
    tmp_path: Path,
) -> None:
    output = tmp_path / "dist" / "override.yaml"
    entry = override.generate(output=output)
    assert yaml.safe_load(output.read_text()) == entry
    templates = (output.parent / entry.pop("template_dir")).resolve()
    assert templates == (ENTRY / "build-scripts").resolve()
    assert entry["nodes"].pop("siem-01") == {
        "build_script": "siem-01-packetbeat",
        "image_name": "ravensport/siem-01-packetbeat:latest",
    }
    competition = yaml.safe_load(COMPETITION.read_text())
    competition.pop("template_dir")
    assert competition["nodes"].pop("siem-01")["build_script"] == "siem-01-competition"
    assert entry == competition


def test_the_entry_override_file_follows_a_change_to_the_competitions(
    tmp_path: Path,
) -> None:
    competition = yaml.safe_load(COMPETITION.read_text())
    competition["compression_factor"] = 4
    competition["nodes"]["siem-01"]["memory"] = "8GB"
    changed = tmp_path / "competition.yaml"
    changed.write_text(yaml.safe_dump(competition))
    entry = override.generate(competition=changed, output=tmp_path / "override.yaml")
    assert entry["compression_factor"] == 4
    assert entry["nodes"]["siem-01"] == {
        "build_script": "siem-01-packetbeat",
        "image_name": "ravensport/siem-01-packetbeat:latest",
        "memory": "8GB",
    }


def test_every_file_that_the_template_pushes_is_owned_by_root() -> None:
    """incus file push keeps the source file's owner unless told otherwise,
    and Packetbeat refuses a configuration file that root does not own: the
    build of the entry's image failed at `packetbeat test config` with the
    configuration owned by the build machine's user, uid 1000."""
    template = (ENTRY / "build-scripts" / "siem-01-packetbeat.sh.j2").read_text()
    pushes = re.findall(r"^incus file push (.*)$", template, re.MULTILINE)
    assert len(pushes) == 4
    assert all(push.startswith("--uid 0 --gid 0 ") for push in pushes)


def test_the_entry_template_directory_holds_the_competition_templates_it_names() -> (
    None
):
    """The entry's override file replaces the competition's template
    directory with the entry's, which therefore links to each template that
    the competition's file names, and to no other: siem-01's competition
    template alone, since the adversary host has no competition image
    (episodes.md §3.4, competition image)."""
    directory = ENTRY / "build-scripts"
    template = (directory / "siem-01-packetbeat.sh.j2").read_text()
    assert '{% extends "siem-01-competition.sh.j2" %}' in template
    competition = yaml.safe_load(
        (REPO / "competition" / "deploy" / "override.yaml").read_text()
    )
    named = {
        f"{node['build_script']}.sh.j2"
        for node in competition["nodes"].values()
        if isinstance(node, dict) and "build_script" in node
    }
    assert named == {"siem-01-competition.sh.j2"}
    links = {path.name for path in directory.iterdir() if path.is_symlink()}
    assert links == named
    for name in links:
        target = REPO / "competition" / "deploy" / "build-scripts" / name
        assert (directory / name).resolve() == target and target.is_file()


def test_the_digests_of_both_binaries_are_recorded() -> None:
    build = (ENTRY / "build-packetbeat.sh").read_text()
    version = re.search(r'^BEATS_TAG="v([\d.]+)"$', build, re.MULTILINE).group(1)
    lines = (ENTRY / "packetbeat.sha256").read_text().splitlines()
    names = [line.split("  ")[1] for line in lines]
    assert names == [
        f"packetbeat-oss-{version}-linux-{arch}" for arch in ("amd64", "arm64")
    ]
    assert all(re.fullmatch(r"[0-9a-f]{64}  \S+", line) for line in lines)


# ── The step session, with the shared step protocol ──────────────────────────

needs_protocol = pytest.mark.skipif(
    not PROTOCOL.is_file(), reason=f"{PROTOCOL} is not in this repository"
)


class Manager:
    """The Wazuh manager's output files below a temporary /var/ossec/logs, the
    current day's dated files with alerts.json and archives.json as their
    second names, and a siem-read program that serves them."""

    def __init__(self, root: Path) -> None:
        self.logs = root / "logs"
        self.state = root / "state"
        for directory, current, prefix in (
            ("alerts", "alerts.json", "ossec-alerts-"),
            ("archives", "archives.json", "ossec-archive-"),
        ):
            dated = self.logs / directory / "2026" / "Nov" / f"{prefix}13.json"
            dated.parent.mkdir(parents=True)
            dated.touch()
            os.link(dated, self.logs / directory / current)
        self.state.mkdir()
        self.program = root / "siem-read"
        self.calls = root / "calls.jsonl"
        # The stand-in runs through /bin/sh, since an interpreter's path with
        # a space, such as a virtual environment's in this repository, cannot
        # follow #! in a script.
        script = root / "siem_read.py"
        self.program.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
        script.write_text(
            "import importlib.machinery, importlib.util, json, sys\n"
            f"with open({str(self.calls)!r}, 'a') as f:\n"
            "    f.write(json.dumps(sys.argv[1:]) + '\\n')\n"
            f"loader = importlib.machinery.SourceFileLoader('siem_read', {str(SIEM_READ)!r})\n"
            "spec = importlib.util.spec_from_loader(loader.name, loader)\n"
            "module = importlib.util.module_from_spec(spec)\n"
            "loader.exec_module(module)\n"
            "from pathlib import Path\n"
            f"sys.exit(module.main(sys.argv[1:], logs=Path({str(self.logs)!r}), "
            f"state=Path({str(self.state)!r})))\n"
        )
        self.program.chmod(0o755)

    def archive(self, *records: dict[str, Any]) -> None:
        with open(self.logs / "archives" / "archives.json", "a") as f:
            f.writelines(
                json.dumps(record, separators=(",", ":")) + "\n" for record in records
            )

    def start(self) -> None:
        subprocess.run([str(self.program), "--start", "--step-s", "5"], check=True)

    def requests(self) -> list[list[str]]:
        return [json.loads(line) for line in self.calls.read_text().splitlines()]


def step_message(step: int, ended: bool = False) -> str:
    """A step message of evaluation mode, in which no step message carries a
    reward (episode-runner-impl.md §6, step messages)."""
    return (
        json.dumps(
            {
                "track": "A",
                "step": step,
                "previous_reward": None,
                "ended": ended,
            }
        )
        + "\n"
    )


def answer(proc: subprocess.Popen[str], timeout_s: float = 10.0) -> dict[str, Any]:
    ready, _, _ = select.select([proc.stdout], [], [], timeout_s)
    assert ready, f"the defender sent no decision message within {timeout_s} s"
    return json.loads(proc.stdout.readline())


@pytest.fixture
def volume(tmp_path: Path) -> Path:
    """The entry's storage volume, as deploy-entry.sh writes it."""
    target = tmp_path / "volume"
    subprocess.run([str(ENTRY / "deploy-entry.sh"), "volume", str(target)], check=True)
    return target


@needs_protocol
def test_the_defender_answers_no_action_until_an_interface_is_used(
    tmp_path: Path, volume: Path
) -> None:
    manager = Manager(tmp_path / "siem-01")
    # A record from before step 0, which no read returns.
    manager.archive(archived(event("flow", 55000, ADVERSARY)))
    manager.start()
    manager.archive(
        archived(event("flow", 1514, "10.40.0.10")),
        {
            "agent": {"name": "siem-01"},
            "location": "df -P",
            "full_log": "ossec: output",
        },
        {
            "agent": {"name": "fms-01"},
            "location": defender.EVENTS,
            "data": {"type": "flow"},
        },
    )
    proc = subprocess.Popen(
        [str(volume / "defender")],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        env={**os.environ, "SIEM_READ": str(manager.program)},
    )
    try:
        proc.stdin.write(step_message(0))
        proc.stdin.flush()
        assert answer(proc) == {"decision": "no action"}
        manager.archive(archived(event("flow", 55000, ADVERSARY)))
        proc.stdin.write(step_message(1))
        proc.stdin.flush()
        assert answer(proc) == {"decision": "alarm"}
        proc.stdin.write(step_message(2, ended=True))
        proc.stdin.close()
        assert proc.wait(timeout=10) == 0
    finally:
        proc.kill()
    declared = ["archives", "--host", defender.HOST, "--input", defender.EVENTS]
    assert manager.requests()[1:] == [declared, declared]


@needs_protocol
def test_the_volume_holds_the_defender_and_what_it_imports(volume: Path) -> None:
    assert sorted(p.name for p in volume.iterdir()) == [
        "defender",
        "protocol.py",
        "telemetry.json",
    ]
    assert os.access(volume / "defender", os.X_OK)
