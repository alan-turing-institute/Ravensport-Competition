"""The settle period (episodes.md §6.6, settle period; episodes-tests.md §4,
start state and settle).

A fake clock drives a fake deployment whose log lines appear at given times
after the start of sim-host. The log lines are those of a deployment of the
release candidate at a compression factor of 5.

Run with: pytest competition/tests -q
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from competition.episode_runner import settle
from competition.episode_runner.incus import Result

T0 = 1000.0
MONITORED = (
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
AGENT_LIST = """Wazuh agent_control. List of available agents:
   ID: 000, Name: siem-01 (server), IP: 127.0.0.1, Active/Local
   ID: 001, Name: fms-01, IP: any, Active
   ID: 002, Name: hmi-01, IP: any, Active
   ID: 003, Name: hist-01, IP: any, Active
   ID: 004, Name: hist-dmz-01, IP: any, Active
   ID: 005, Name: atg-relay-01, IP: any, Active
   ID: 006, Name: dmz-bastion-01, IP: any, Active
   ID: 007, Name: corp-it, IP: any, Active
   ID: 008, Name: recon-01, IP: any, Active
   ID: 009, Name: admin-01, IP: any, Active
   ID: 010, Name: fw-ext-dmz, IP: any, Active
   ID: 011, Name: fw-dmz-it, IP: any, Active
   ID: 012, Name: fw-ot-dmz, IP: any, Active

List of agentless devices:
"""
START_STATE = (
    "2026-11-04T01:13:18.827797Z  INFO start_state: start_state "
    'sim_tug_arrivals=4651467801475421837 delivery_state="no order placed" '
    "remaining_lead_time_s=None"
)


class FakeClock:
    """A clock that advances only when the settle checks wait. A wait past
    `LIMIT_S` fails the test, so a fault that keeps a deployment from settling
    ends the test at once."""

    LIMIT_S = 3600.0

    def __init__(self, now: float = T0) -> None:
        self.t = now

    def now(self) -> float:
        return self.t

    def sleep_until(self, t: float) -> None:
        if t > T0 + self.LIMIT_S:
            raise AssertionError(f"still waiting {self.LIMIT_S:g} s after the start")
        self.t = max(self.t, t)


def stamp(at: float) -> str:
    """A time of an agent's log, `at` seconds after the start of sim-host at
    01:13:18."""
    return datetime.fromtimestamp(1793754798 + at, timezone.utc).strftime(
        "%Y/%m/%d %H:%M:%S"
    )


class FakeDeployment:
    """The nodes of a deployment, whose log lines appear at their times in
    seconds after the start of sim-host."""

    def __init__(self, clock: FakeClock, **times: float) -> None:
        self.clock = clock
        self.times = {
            "active": 5.0,
            "early_scan": 10.0,
            "port_report": 12.0,
            "reload": 25.0,
            "scan": 30.0,
            "engine": 8.0,
            "start_state": 0.9,
        }
        self.times.update(times)
        self.late: dict[str, dict[str, float]] = {}
        self.status = "started"
        # OpenRC reports the simulator's service as stopped until it starts
        # the service, and as stopped again from stops_at.
        self.starts_at = 0.0
        self.stops_at: float | None = None
        # Services that an actor of the simulator calls, and the packet
        # inspection on a firewall node, as (node, unit), and the time after
        # the start of sim-host at which each becomes active.
        self.service_starts: dict[tuple[str, str], float] = {}
        self.proxy_starts_at = 0.0
        self.calls: list[tuple[str, tuple[str, ...]]] = []
        # The logs whose lines a fork's long-running deployment has rotated
        # away: "ossec" for the agents' logs, "suricata" for packet inspection.
        self.rotated: set[str] = set()

    def at(self, host: str, what: str) -> float:
        return self.late.get(host, {}).get(what, self.times[what])

    def seen(self, host: str, what: str) -> bool:
        return self.clock.now() - T0 >= self.at(host, what)

    def exec(self, node: str, *argv: str, timeout_s: float = 60.0) -> Result:
        self.calls.append((node, argv))
        if argv == (settle.AGENT_CONTROL, "-l"):
            lines = [
                line
                for line in AGENT_LIST.splitlines()
                if not any(f"Name: {h}," in line for h in MONITORED)
                or any(
                    f"Name: {h}," in line and self.seen(h, "active") for h in MONITORED
                )
            ]
            return Result(0, "\n".join(lines) + "\n", "")
        if argv[:1] == ("grep",) and argv[-1] == settle.OSSEC_LOG:
            if "ossec" in self.rotated:
                return Result(1, "", "")
            lines = [
                (self.at(node, what), text)
                for what, text in (
                    ("early_scan", settle.SCAN_ENDED),
                    ("reload", settle.RELOAD),
                    ("scan", settle.SCAN_ENDED),
                )
                if self.seen(node, what)
            ]
            out = "".join(
                f"{40 + n}:{stamp(at)} {text}\n"
                for n, (at, text) in enumerate(sorted(lines))
            )
            return Result(0 if out else 1, out, "")
        if (argv[:1] == ("grep",) and argv[-1] == settle.ARCHIVES) or argv == (
            "sh",
            "-c",
            settle.LATEST,
            "sh",
            settle.PORT_REPORT,
            settle.ARCHIVES,
            str(settle.LATEST_REPORTS),
        ):
            out = "".join(
                json.dumps(
                    {
                        "timestamp": f"2026-11-04T01:13:{30 + n:02d}.217+0000",
                        "agent": {"id": f"{n + 1:03d}", "name": h},
                        "manager": {"name": "siem-01"},
                        "full_log": "ossec: output: 'netstat listening ports':\n"
                        "tcp 0.0.0.0:22 0.0.0.0:* 1/init",
                        "location": "netstat listening ports",
                    },
                    separators=(",", ":"),
                )
                + "\n"
                for n, h in enumerate(MONITORED)
                if self.seen(h, "port_report")
            )
            return Result(0 if out else 1, out, "")
        if argv[:2] == ("systemctl", "is-active") and argv[2:] != (
            settle.SURICATA_UNIT,
        ):
            states = [
                "active"
                if self.clock.now() - T0 >= self.service_starts.get((node, unit), 0.0)
                else "inactive"
                for unit in argv[2:]
            ]
            out = "".join(f"{s}\n" for s in states)
            return Result(0 if set(states) == {"active"} else 3, out, "")
        if argv == ("rc-service", "haproxy", "status"):
            started = self.clock.now() - T0 >= self.proxy_starts_at
            return Result(0, f" * status: {'started' if started else 'stopped'}\n", "")
        if argv == ("systemctl", "is-active", settle.SURICATA_UNIT):
            unit = (node, settle.SURICATA_UNIT)
            running = self.clock.now() - T0 >= self.service_starts.get(unit, 0.0)
            state = "active" if running else "inactive"
            return Result(0 if running else 3, f"{state}\n", "")
        if argv[:1] == ("grep",) and argv[-1] == settle.SURICATA_LOG:
            if not self.seen(node, "engine") or "suricata" in self.rotated:
                return Result(1, "", "")
            line = (
                "[260 - Suricata-Main] 2026-11-04 01:13:27 Notice: threads: Threads "
                "created -> W: 4 FM: 1 FR: 1   Engine started.\n"
            )
            if "-m" not in argv:
                # Every start in the log: the image's build left one first.
                line = line.replace("01:13:27", "00:02:11") + line
            return Result(0, line, "")
        if argv[:1] == ("grep",) and argv[-1] == settle.SIM_LOG:
            ok = self.seen(node, "start_state")
            return Result(0 if ok else 1, START_STATE + "\n" if ok else "", "")
        if argv == ("rc-service", "sim", "status"):
            since = self.clock.now() - T0
            status = self.status
            if since < self.starts_at or (
                self.stops_at is not None and since >= self.stops_at
            ):
                status = "stopped"
            return Result(0 if status == "started" else 3, f" * status: {status}\n", "")
        raise AssertionError(f"an unexpected call on {node}: {argv}")


def wait(deployment: FakeDeployment, clock: FakeClock, **options):
    return settle.wait_to_settle(deployment, clock, MONITORED, T0, **options)


# When each settle condition holds, in seconds after the start of sim-host,
# with nothing withheld: the checks run every 2 seconds from that start.
HELD_WITH_NOTHING_WITHHELD = {1: 26.0, 2: 30.0, 3: 8.0, 4: 2.0}
# Each settle condition withheld in turn until 59 s after the start of
# sim-host, as the fake deployment's attributes, and when each condition that
# it moves then holds.
WITHHELD = [
    pytest.param(
        {"late": {"dmz-bastion-01": {"reload": 59.0, "scan": 61.0}}},
        {1: 60.0, 2: 62.0},
        id="a Wazuh agent held before its restart",
    ),
    pytest.param(
        {"late": {"hist-01": {"scan": 59.0}}},
        {2: 60.0},
        id="the file-integrity scan delayed at an agent's start",
    ),
    pytest.param(
        {
            "service_starts": {("fw-dmz-it", settle.SURICATA_UNIT): 59.0},
            "late": {"fw-dmz-it": {"engine": 59.0}},
        },
        {3: 60.0},
        id="packet inspection stopped on fw-dmz-it",
    ),
    pytest.param(
        {"starts_at": 59.0, "late": {"sim-host": {"start_state": 59.5}}},
        {4: 60.0},
        id="the simulator held",
    ),
    pytest.param(
        {"proxy_starts_at": 59.0}, {4: 60.0}, id="haproxy on sim-host stopped"
    ),
    *(
        pytest.param(
            {"service_starts": {(node, unit): 59.0}},
            {4: 60.0},
            id=f"{unit} on {node} stopped",
        )
        for node, units in settle.ACTOR_SERVICES.items()
        for unit in units
    ),
]


@pytest.mark.parametrize(("withheld", "held_at"), WITHHELD)
def test_step_0_waits_for_each_settle_condition(
    withheld: dict, held_at: dict[int, float]
) -> None:
    """Each settle condition withheld in turn: one Wazuh agent held before
    its restart, the file-integrity scan delayed at one agent's start, the
    packet inspection stopped on one firewall node, the simulator held, and
    each service that the settle period waits for stopped. The withheld
    condition holds at its later time, and step 0 begins the settle delay
    after the last condition holds, so it waits for the withheld one
    (episodes-tests.md, step_0_waits_for_each_settle_condition; episodes.md
    §6.6, settle period)."""
    clock = FakeClock()
    deployment = FakeDeployment(clock)
    for name, value in withheld.items():
        setattr(deployment, name, value)
    settled = wait(deployment, clock)
    held = {c.number: c.held - T0 for c in settled.conditions}
    assert held == {**HELD_WITH_NOTHING_WITHHELD, **held_at}
    assert settled.step_0 == T0 + max(held.values()) + settle.SETTLE_DELAY_S
    assert settled.step_0 >= T0 + 60.0 + settle.SETTLE_DELAY_S
    assert settled.start == T0


def test_each_condition_keeps_the_lines_it_matched() -> None:
    clock = FakeClock()
    settled = wait(FakeDeployment(clock), clock)
    c1, c2, c3, c4 = settled.conditions
    assert set(c1.lines) == set(MONITORED)
    assert c1.lines["recon-01"] == [f"{stamp(25.0)} {settle.RELOAD}"]
    assert c2.lines["recon-01"] == [
        f"{stamp(30.0)} {settle.SCAN_ENDED}",
        "2026-11-04T01:13:37.217+0000 listening-port report",
    ]
    assert set(c3.lines) == set(settle.FIREWALLS)
    assert c4.lines["sim-host"] == [START_STATE, "haproxy started"]
    assert set(c4.lines) == {"sim-host", *settle.ACTOR_SERVICES}
    assert settled.start_state == START_STATE
    assert settled.agent_ids["fms-01"] == "001"
    assert settled.agent_ids["fw-ot-dmz"] == "012"


def test_a_scan_that_ended_before_the_reload_leaves_condition_2_unmet() -> None:
    clock = FakeClock()
    deployment = FakeDeployment(clock)
    deployment.late = {"hist-01": {"scan": 61.0}}
    settled = wait(deployment, clock)
    assert settled.conditions[1].held == T0 + 62.0


def test_a_missing_listening_port_report_leaves_condition_2_unmet() -> None:
    clock = FakeClock()
    deployment = FakeDeployment(clock)
    deployment.late = {"hist-dmz-01": {"port_report": 45.0}}
    settled = wait(deployment, clock)
    assert settled.conditions[1].held == T0 + 46.0


def test_an_agent_that_is_not_active_leaves_condition_1_unmet() -> None:
    clock = FakeClock()
    deployment = FakeDeployment(clock)
    deployment.late = {"corp-it": {"active": 51.0}}
    settled = wait(deployment, clock)
    assert settled.conditions[0].held == T0 + 52.0


def test_a_simulator_that_has_not_yet_started_is_waited_for() -> None:
    """A deployment can return before OpenRC on sim-host starts the
    simulator's service."""
    clock = FakeClock()
    deployment = FakeDeployment(clock)
    deployment.starts_at = 3.0
    settled = wait(deployment, clock)
    assert settled.conditions[3].held == T0 + 4.0


def test_a_simulator_that_stops_after_starting_fails_the_deployment() -> None:
    clock = FakeClock()
    deployment = FakeDeployment(clock)
    deployment.stops_at = 21.0
    with pytest.raises(settle.SettleFailure, match="stopped"):
        wait(deployment, clock)


def test_a_simulator_that_has_stopped_fails_the_deployment() -> None:
    clock = FakeClock()
    deployment = FakeDeployment(clock)
    deployment.status = "crashed"
    with pytest.raises(settle.SettleFailure, match="crashed"):
        wait(deployment, clock)


def test_the_settle_timeout_fails_a_deployment_that_never_settles() -> None:
    clock = FakeClock()
    deployment = FakeDeployment(clock)
    deployment.late = {"atg-relay-01": {"reload": 1e9}}
    with pytest.raises(settle.SettleFailure, match="condition 1") as failure:
        wait(deployment, clock, timeout_s=120.0)
    assert "condition 2" in str(failure.value)
    assert clock.now() == T0 + 120.0


def test_without_a_settle_timeout_the_episode_runner_waits() -> None:
    clock = FakeClock()
    deployment = FakeDeployment(clock)
    deployment.late = {"atg-relay-01": {"reload": 900.0, "scan": 905.0}}
    settled = wait(deployment, clock)
    assert settled.step_0 == T0 + 906.0 + 45.0


def test_every_check_runs_through_incus_exec_without_su() -> None:
    clock = FakeClock()
    deployment = FakeDeployment(clock)
    wait(deployment, clock)
    assert deployment.calls
    assert not any("su" in argv or argv[0] == "sudo" for _, argv in deployment.calls)


def test_the_agents_are_read_from_agent_control() -> None:
    agents = settle.agents(
        AGENT_LIST.replace("hmi-01, IP: any, Active", "hmi-01, IP: any, Disconnected")
    )
    assert agents["fms-01"] == ("001", "Active")
    assert agents["hmi-01"] == ("002", "Disconnected")
    assert "siem-01" not in agents


def test_the_simulators_start_is_the_time_of_its_start_state_line() -> None:
    assert settle.start_state_time(START_STATE) == datetime(
        2026, 11, 4, 1, 13, 18, 827797, timezone.utc
    )


def test_condition_4_waits_for_the_services_that_the_actors_call() -> None:
    """The fuel supplier polls the tank gauge through the relay on
    atg-relay-01, so condition 4 holds once the relay runs."""
    clock = FakeClock()
    deployment = FakeDeployment(clock)
    deployment.service_starts = {("atg-relay-01", "haproxy"): 19.0}
    settled = wait(deployment, clock)
    assert settled.conditions[3].held == T0 + 20.0
    assert settled.conditions[3].lines["atg-relay-01"] == ["haproxy active"]
    assert settled.conditions[3].lines["sim-host"][1] == "haproxy started"


def test_condition_4_leaves_the_fms_and_the_hmi_to_the_deployment_sequence() -> None:
    """The deployment sequence's readiness probes check the FMS, its database,
    the HMI and HAProxy on hmi-01 before sim-host starts, so condition 4 reads
    the services on the other hosts."""
    clock = FakeClock()
    deployment = FakeDeployment(clock)
    settled = wait(deployment, clock)
    assert set(settled.conditions[3].lines) == {
        "sim-host",
        "atg-relay-01",
        "corp-it",
        "recon-01",
        "hist-dmz-01",
    }


def test_condition_4_waits_for_the_directory_and_the_workstations_client() -> None:
    """The FMS signs the dock operator in against the directory on corp-it,
    and sssd on recon-01 signs the reconciliation user in against it, so
    condition 4 holds once both run."""
    clock = FakeClock()
    deployment = FakeDeployment(clock)
    deployment.service_starts = {("corp-it", "slapd"): 23.0, ("recon-01", "sssd"): 27.0}
    settled = wait(deployment, clock)
    assert settled.conditions[3].held == T0 + 28.0
    assert settled.conditions[3].lines["corp-it"] == ["slapd active"]
    assert settled.conditions[3].lines["recon-01"] == [
        "xrdp active",
        "xrdp-sesman active",
        "sssd active",
    ]


def test_a_service_that_never_runs_fails_the_deployment() -> None:
    clock = FakeClock()
    deployment = FakeDeployment(clock)
    deployment.service_starts = {("recon-01", "xrdp"): 1e9}
    with pytest.raises(settle.SettleFailure, match="condition 4"):
        wait(deployment, clock, timeout_s=120.0)


def test_condition_4_waits_for_haproxy_on_sim_host() -> None:
    """HAProxy on sim-host carries every connection of an actor out of the
    simulator."""
    clock = FakeClock()
    deployment = FakeDeployment(clock)
    deployment.proxy_starts_at = 11.0
    settled = wait(deployment, clock)
    assert settled.conditions[3].held == T0 + 12.0


# A fork of a living port: the deployment settled an hour before the fork's
# start, T0, and every condition's event lies before it.
LONG_AGO = {
    what: -3600.0
    for what in (
        "active",
        "early_scan",
        "port_report",
        "reload",
        "scan",
        "engine",
        "start_state",
    )
}


def copy_of(clock: FakeClock) -> FakeDeployment:
    deployment = FakeDeployment(clock, **LONG_AGO)
    deployment.starts_at = -3600.0
    return deployment


def test_a_copy_settles_at_its_start_on_a_deployment_that_settled_long_ago() -> None:
    clock = FakeClock()
    settled = wait(copy_of(clock), clock, copy=True)
    assert {c.number: c.held for c in settled.conditions} == {
        n: T0 for n in (1, 2, 3, 4)
    }
    assert settled.start == T0
    assert settled.step_0 == T0 + settle.SETTLE_DELAY_S
    c1, c2, _, _ = settled.conditions
    assert c1.lines["recon-01"] == [f"{stamp(-3600.0)} {settle.RELOAD}"]
    assert c2.lines["recon-01"][0] == f"{stamp(-3600.0)} {settle.SCAN_ENDED}"


def test_a_copy_accepts_logs_that_have_rotated_since_the_living_port_settled() -> None:
    clock = FakeClock()
    deployment = copy_of(clock)
    deployment.rotated = {"ossec", "suricata"}
    settled = wait(deployment, clock, copy=True)
    c1, c2, c3, _ = settled.conditions
    assert settled.step_0 == T0 + settle.SETTLE_DELAY_S
    assert c1.lines["fms-01"] == [f"the agent's restart: {settle.ROTATED}"]
    assert c2.lines["fms-01"][0] == f"the agent's scan: {settle.ROTATED}"
    assert c3.lines["fw-ext-dmz"] == [
        f"{settle.SURICATA_UNIT} active; its engine's start: {settle.ROTATED}"
    ]


def test_a_copy_records_the_living_ports_own_start_of_packet_inspection() -> None:
    clock = FakeClock()
    settled = wait(copy_of(clock), clock, copy=True)
    (line,) = settled.conditions[2].lines["fw-dmz-it"]
    assert "01:13:27" in line


def test_at_fork_0_condition_3_reads_the_last_start_of_packet_inspection() -> None:
    """Each firewall image's build leaves an earlier start in the log, so
    condition 3 reads the last line at fork 0, as it does in a fork."""
    clock = FakeClock()
    deployment = FakeDeployment(clock)
    settled = wait(deployment, clock)
    (line,) = settled.conditions[2].lines["fw-dmz-it"]
    assert "01:13:27" in line
    suricata = [argv for _, argv in deployment.calls if argv[-1] == settle.SURICATA_LOG]
    assert suricata and all("-m" not in argv for argv in suricata)


def test_packet_inspection_that_starts_before_the_simulator_leaves_it_waited_for() -> (
    None
):
    """The engines of packet inspection can start before OpenRC on sim-host
    starts the simulator's service, which the settle checks still wait for."""
    clock = FakeClock()
    deployment = FakeDeployment(clock)
    deployment.starts_at = 12.0
    settled = wait(deployment, clock)
    held = {c.number: c.held for c in settled.conditions}
    assert (held[3], held[4]) == (T0 + 8.0, T0 + 12.0)


def test_a_copy_still_waits_for_an_agent_to_connect_and_report() -> None:
    clock = FakeClock()
    deployment = copy_of(clock)
    deployment.late = {"admin-01": {"active": 9.0, "port_report": 15.0}}
    settled = wait(deployment, clock, copy=True)
    held = {c.number: c.held for c in settled.conditions}
    assert held == {1: T0 + 10.0, 2: T0 + 16.0, 3: T0, 4: T0}
    assert settled.step_0 == T0 + 16.0 + settle.SETTLE_DELAY_S


def test_a_copy_reads_only_the_latest_listening_port_reports() -> None:
    clock = FakeClock()
    deployment = copy_of(clock)
    wait(deployment, clock, copy=True)
    reads = [argv for node, argv in deployment.calls if node == settle.SIEM]
    assert ("grep", "-F", settle.PORT_REPORT, settle.ARCHIVES) not in reads
    assert (
        "sh",
        "-c",
        settle.LATEST,
        "sh",
        settle.PORT_REPORT,
        settle.ARCHIVES,
        str(settle.LATEST_REPORTS),
    ) in reads
    # Each agent's log is read once, for the record.
    ossec = [n for n, argv in deployment.calls if argv[-1:] == (settle.OSSEC_LOG,)]
    assert sorted(ossec) == sorted(MONITORED)


def test_a_copy_whose_agent_stays_disconnected_fails_at_the_timeout() -> None:
    clock = FakeClock()
    deployment = copy_of(clock)
    deployment.late = {"corp-it": {"active": 1e9}}
    with pytest.raises(settle.SettleFailure, match="condition 1 still failed"):
        wait(deployment, clock, copy=True, timeout_s=120.0)
    assert clock.now() == T0 + 120.0


def test_at_fork_0_a_log_without_the_restart_keeps_conditions_1_and_2() -> None:
    """The fork-0 path still looks for the restart that follows the start of
    sim-host."""
    clock = FakeClock()
    deployment = copy_of(clock)
    deployment.rotated = {"ossec"}
    with pytest.raises(settle.SettleFailure, match="condition 1, condition 2"):
        wait(deployment, clock, timeout_s=60.0)
