"""The settle period (episodes.md §6.6, settle period).

The settle period starts when the deployment sequence starts sim-host. Step 0
begins once the episode runner has seen the four settle conditions hold and has
then waited the settle delay.

1. Every monitoring agent is connected and past the restart that applies the
   manager's shared configuration.
2. Every agent's first file-integrity scan and first listening-port report are
   complete.
3. Packet inspection is running on all three firewall nodes.
4. The simulator is running.

The episode runner reads each condition through Incus, every POLL_S seconds.
Each check names the log line or the command that it matches, as a deployment
writes them. A simulator that stops during the settle period fails the
deployment, and so does a deployment whose conditions still fail at the settle
timeout.

In a fork of a living port the settle period starts at the fork's start, the
fork instant on the fork's clock. The deployment has run since the living
port's start and settled long before the fork instant, so the conditions that
look for an event since a start accept the living port's events from any time
before the fork instant, and the logs may have rotated them away since.
Condition 1 then holds once every agent is connected, condition 2 once each
agent's listening-port report is among the latest in archives.json, and
condition 3 once packet inspection's service is active. The episode record
keeps each agent's last restart and last scan, and each engine's last start,
where the logs still hold them. Condition 4 is the same in a fork. A fork
waits no settle delay: step 0 begins once the conditions hold (COPY_DELAY_S),
and the settle timeout stays the guard.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from competition.episode_runner.incus import Result
from competition.episode_runner.steps import Clock

# Two benign episodes with the competition's flow timeouts (episodes.md §5.3,
# timings) gave 1,840 flow records of the packet inspection. The longest lag
# from a flow's end to its record's arrival at the manager was 42.8 seconds,
# and the settle delay lies above it (episodes.md §6.6, settle period).
SETTLE_DELAY_S = 45.0
# A fork holds a deployment that settled long before the fork instant, and its
# step 0 begins once the settle conditions hold.
COPY_DELAY_S = 0.0
# The longest wait for the settle conditions, measured from the start of
# sim-host. Nine watched deployments met the conditions within 38.1 seconds,
# and the timeout is about three times that (episodes.md §6.6, settle period).
SETTLE_TIMEOUT_S = 120.0
POLL_S = 2.0

SIEM = "siem-01"
SIM_HOST = "sim-host"
# The firewall nodes, between the zones, which carry the packet inspection
# (siem.md §1, purpose and scope).
FIREWALLS = ("fw-ext-dmz", "fw-dmz-it", "fw-ot-dmz")

# Condition 1 holds when `agent_control -l` on siem-01 lists each agent as
# Active, and each agent's own log holds the line of the restart.
AGENT_CONTROL = "/var/ossec/bin/agent_control"
OSSEC_LOG = "/var/ossec/logs/ossec.log"
RELOAD = "wazuh-agentd: INFO: Agent is reloading due to shared configuration changes."
# Condition 2 holds when each agent's log holds a scan that ended after the
# restart, which rescans with the manager's shared configuration. archives.json
# must also hold a listening-port report from each agent.
SCAN_ENDED = "wazuh-syscheckd: INFO: (6009): File integrity monitoring scan ended."
ARCHIVES = "/var/ossec/logs/archives/archives.json"
PORT_REPORT = '"location":"netstat listening ports"'
# In a fork, the latest listening-port reports in archives.json: twelve agents
# each report every 72 s, so 400 reports cover the last 40 minutes.
LATEST_REPORTS = 400
LATEST = 'grep -F -e "$1" "$2" | tail -n "$3"'
# What the record of a fork's settle period holds where a log has rotated a
# living port's line away.
ROTATED = "not in the log, which has rotated since the living port settled"
# Condition 3 holds when the packet inspection's service is active on each
# firewall node, and its log holds the line of its engine's start.
SURICATA_UNIT = "ravensport-suricata"
SURICATA_LOG = "/var/log/suricata/suricata.log"
ENGINE_STARTED = "Engine started."
# Condition 4 holds when the simulator's service has started, the simulator
# has logged its start state, and every service whose failure would stop the
# simulator is active. The fuel supplier calls the tank-gauge relay. The dock
# operator calls the human-machine interface (HMI) and the fuel management
# system (FMS) behind it, which signs the operator in against the directory on
# corp-it. The reconciliation user's audit calls the workstation, whose
# directory client sssd signs the user in against the same directory, and the
# log server's replica. HAProxy on sim-host carries every connection of an
# actor out of the simulator.
#
# The deployment sequence starts sim-host only after the FMS and the HMI have
# answered their health endpoints (deployment.md §8, deployment sequence). The
# FMS answers only after it has connected to its PostgreSQL database, and the
# HMI's endpoint is reached through HAProxy on hmi-01. Those probes therefore
# check the FMS, its database, the HMI and HAProxy on hmi-01, and the list
# leaves fms-01 and hmi-01 to them.
#
# The FMS follows "No operational path waits on delivery" for its pushes to the
# log server (fms.md §5.4, log-server pusher). The audit reads the replica's
# event store alone, and the replica's Grafana keeps its own accounts
# (recon.md §7.2, the log-server account and residues). So hist-01, the
# replica's VictoriaMetrics and a directory account for Grafana stay out of
# the list.
SIM_LOG = "/var/log/sim.log"
START_STATE = " start_state: start_state "
ACTOR_SERVICES = {
    "atg-relay-01": ("haproxy",),
    "corp-it": ("slapd",),
    "recon-01": ("xrdp", "xrdp-sesman", "sssd"),
    "hist-dmz-01": ("grafana-server", "victoria-logs"),
}

_AGENT = re.compile(r"\s*ID: (\d+), Name: (\S+), IP: \S+, (\S+)$")
_STATUS = re.compile(r"status: (\w+)")
_NUMBERED = re.compile(r"(\d+):(.*)")

log = logging.getLogger(__name__)


class Incus(Protocol):
    def exec(self, node: str, *argv: str, timeout_s: float = 60.0) -> Result: ...


class SettleFailure(Exception):
    """A deployment that fails to settle (episodes.md §12, terminated and
    discarded episodes)."""


@dataclass
class Condition:
    number: int
    held: float | None = None
    lines: dict[str, list[str]] = field(default_factory=dict)


@dataclass
class Settled:
    start: float
    conditions: list[Condition]
    step_0: float
    agent_ids: dict[str, str]
    start_state: str


def agents(output: str) -> dict[str, tuple[str, str]]:
    """Each agent's identifier and status, by name, from `agent_control -l`.
    The manager's own entry is left out."""
    found = {}
    for line in output.splitlines():
        match = _AGENT.match(line)
        if match:
            found[match.group(2)] = (match.group(1), match.group(3))
    return found


def reload_and_scan(output: str) -> tuple[str | None, str | None]:
    """The last restart and the first scan that ended after it, from the
    numbered lines that grep -n finds in an agent's log."""
    reload: tuple[int, str] | None = None
    scans: list[tuple[int, str]] = []
    for line in output.splitlines():
        match = _NUMBERED.match(line)
        if not match:
            continue
        number, text = int(match.group(1)), match.group(2)
        if text.endswith(RELOAD):
            reload = (number, text)
        elif text.endswith(SCAN_ENDED):
            scans.append((number, text))
    if reload is None:
        return None, None
    after = [text for number, text in scans if number > reload[0]]
    return reload[1], (after[0] if after else None)


def last_reload_and_scan(output: str) -> tuple[str | None, str | None]:
    """The last restart and the last scan in an agent's log, in either order,
    from the numbered lines that grep -n finds."""
    reload = scan = None
    for line in output.splitlines():
        match = _NUMBERED.match(line)
        if not match:
            continue
        text = match.group(2)
        if text.endswith(RELOAD):
            reload = text
        elif text.endswith(SCAN_ENDED):
            scan = text
    return reload, scan


def port_reports(output: str) -> dict[str, str]:
    """The time of each agent's first listening-port report, by name, from the
    lines of archives.json that hold one."""
    found: dict[str, str] = {}
    for line in output.splitlines():
        try:
            event = json.loads(line)
            found.setdefault(event["agent"]["name"], event["timestamp"])
        except (ValueError, KeyError, TypeError):
            continue
    return found


def simulator_status(incus: Incus) -> str | None:
    """The state of the simulator's service, such as started or crashed, or
    None when sim-host cannot be read."""
    match = _STATUS.search(incus.exec(SIM_HOST, "rc-service", "sim", "status").stdout)
    return match.group(1) if match else None


def start_state_time(line: str) -> datetime:
    """The simulator's start: the time at the start of its start_state line."""
    return datetime.fromisoformat(line.split()[0])


def wait_to_settle(
    incus: Incus,
    clock: Clock,
    monitored: Sequence[str],
    start: float,
    *,
    delay_s: float = SETTLE_DELAY_S,
    poll_s: float = POLL_S,
    timeout_s: float | None = None,
    copy: bool = False,
) -> Settled:
    """Wait for the four settle conditions, from the start of sim-host at
    `start`, or in a fork from the fork's start. Step 0 begins delay_s after
    they hold. With no timeout_s, the episode runner waits for as long as the
    conditions take."""
    conditions = [Condition(n) for n in (1, 2, 3, 4)]
    reloads: dict[str, str] = {}
    scans: dict[str, str] = {}
    reports: dict[str, str] = {}
    engines: dict[str, str] = {}
    start_state: str | None = None
    agent_ids: dict[str, str] = {}
    started = False
    actor_nodes = [node for node in ACTOR_SERVICES if node in monitored]
    services: dict[str, list[str]] = {}
    proxy: str | None = None
    while True:
        # OpenRC reports the simulator's service as stopped until it starts
        # the service, which can follow the end of the deployment. A service
        # whose process has died is crashed.
        status = simulator_status(incus)
        started = started or status == "started"
        if status == "crashed" or (started and status not in (None, "started")):
            raise SettleFailure(f"the simulator's service is {status}")
        if start_state is None:
            found = incus.exec(SIM_HOST, "grep", "-m", "1", "-F", START_STATE, SIM_LOG)
            start_state = found.stdout.strip() or None
        listed = agents(incus.exec(SIEM, AGENT_CONTROL, "-l").stdout)
        for host in monitored:
            if copy and host not in scans:
                # Read once: the living port's agents restarted and scanned
                # long before the fork.
                reload, scan = last_reload_and_scan(
                    incus.exec(
                        host,
                        "grep",
                        "-n",
                        "-F",
                        "-e",
                        RELOAD,
                        "-e",
                        SCAN_ENDED,
                        OSSEC_LOG,
                    ).stdout
                )
                reloads[host] = reload or f"the agent's restart: {ROTATED}"
                scans[host] = scan or f"the agent's scan: {ROTATED}"
            elif host not in scans:
                reload, scan = reload_and_scan(
                    incus.exec(
                        host,
                        "grep",
                        "-n",
                        "-F",
                        "-e",
                        RELOAD,
                        "-e",
                        SCAN_ENDED,
                        OSSEC_LOG,
                    ).stdout
                )
                if reload:
                    reloads[host] = reload
                if scan:
                    scans[host] = scan
        if any(host not in reports for host in monitored):
            if copy:
                found = incus.exec(
                    SIEM,
                    "sh",
                    "-c",
                    LATEST,
                    "sh",
                    PORT_REPORT,
                    ARCHIVES,
                    str(LATEST_REPORTS),
                )
            else:
                found = incus.exec(SIEM, "grep", "-F", PORT_REPORT, ARCHIVES)
            for host, at in port_reports(found.stdout).items():
                reports.setdefault(host, at)
        for node in FIREWALLS:
            if (
                node not in engines
                and incus.exec(
                    node, "systemctl", "is-active", SURICATA_UNIT
                ).stdout.strip()
                == "active"
            ):
                # The last start is the deployment's: the image's build leaves
                # an earlier one in the log.
                found = incus.exec(node, "grep", "-F", ENGINE_STARTED, SURICATA_LOG)
                last_start = found.stdout.strip().splitlines()[-1:]
                if last_start:
                    engines[node] = last_start[0]
                elif copy:
                    engines[node] = (
                        f"{SURICATA_UNIT} active; its engine's start: {ROTATED}"
                    )

        now = clock.now()
        c1, c2, c3, c4 = conditions
        if c1.held is None and all(
            listed.get(host, ("", ""))[1] == "Active" and host in reloads
            for host in monitored
        ):
            c1.held = now
            c1.lines = {host: [reloads[host]] for host in monitored}
            agent_ids = {host: listed[host][0] for host in monitored}
        if c2.held is None and all(
            host in scans and host in reports for host in monitored
        ):
            c2.held = now
            c2.lines = {
                host: [scans[host], f"{reports[host]} listening-port report"]
                for host in monitored
            }
        if c3.held is None and all(node in engines for node in FIREWALLS):
            c3.held = now
            c3.lines = {node: [engines[node]] for node in FIREWALLS}
        if c4.held is None:
            for node in actor_nodes:
                units = ACTOR_SERVICES[node]
                if node not in services and incus.exec(
                    node, "systemctl", "is-active", *units
                ).stdout.split() == ["active"] * len(units):
                    services[node] = [f"{unit} active" for unit in units]
            if proxy is None:
                proxy_status = _STATUS.search(
                    incus.exec(SIM_HOST, "rc-service", "haproxy", "status").stdout
                )
                if proxy_status and proxy_status.group(1) == "started":
                    proxy = "haproxy started"
        if (
            c4.held is None
            and status == "started"
            and start_state
            and proxy
            and all(node in services for node in actor_nodes)
        ):
            c4.held = now
            c4.lines = {SIM_HOST: [start_state, proxy], **services}

        for c in conditions:
            if c.held == now:
                log.info("settle condition %d holds", c.number)
        if all(c.held is not None for c in conditions):
            break
        if timeout_s is not None and now - start >= timeout_s:
            unmet = ", ".join(
                f"condition {c.number}" for c in conditions if c.held is None
            )
            raise SettleFailure(
                f"{unmet} still failed at the settle timeout of {timeout_s:g} s"
            )
        clock.sleep_until(now + poll_s)

    held = max(c.held for c in conditions if c.held is not None)
    assert start_state is not None
    return Settled(start, conditions, held + delay_s, agent_ids, start_state)
