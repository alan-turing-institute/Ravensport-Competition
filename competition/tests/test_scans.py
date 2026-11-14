"""Scan requests (episodes.md §6.2, first occurrences; episodes-tests.md §3,
start state, living ports and forks).

Run with: pytest competition/tests -q
"""

from __future__ import annotations

from competition.episode_runner import scans, schedule
from competition.episode_runner.incus import Result

SEED = bytes(range(32)).hex()
INTERVAL_S = 8640
HOSTS = ("fms-01", "hmi-01", "hist-01", "atg-relay-01", "corp-it", "fw-ot-dmz")
AGENT_IDS = {host: f"{n:03d}" for n, host in enumerate(HOSTS, start=1)}


class FakeIncus:
    def __init__(self, result: Result) -> None:
        self.result = result
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    def exec(self, node: str, *argv: str, timeout_s: float = 60.0) -> Result:
        self.calls.append((node, argv))
        return self.result


class FakeClock:
    def __init__(self, now: float) -> None:
        self.t = now

    def now(self) -> float:
        return self.t


def test_only_the_scans_inside_the_episode_are_requested() -> None:
    times = schedule.first_scans(SEED, HOSTS, INTERVAL_S)
    # An episode whose steps cover a stretch that holds some of the drawn
    # times, from 500 seconds after the simulator's start.
    sim_start, step_0 = 10_000.0, 10_500.0
    end = step_0 + sorted(times.values())[3] - 400.0
    requests = scans.plan(SEED, HOSTS, AGENT_IDS, INTERVAL_S, sim_start, step_0, end)
    inside = {h for h, t in times.items() if step_0 <= sim_start + t < end}
    assert inside and inside != set(HOSTS)
    assert {r.host for r in requests} == inside
    for r in requests:
        assert r.due == sim_start + times[r.host]
        assert r.agent_id == AGENT_IDS[r.host]
    assert [r.due for r in requests] == sorted(r.due for r in requests)


def test_a_scan_request_runs_agent_control_on_siem_01() -> None:
    incus = FakeIncus(
        Result(
            0,
            "\nWazuh agent_control: Restarting Syscheck/Rootcheck on agent: 007\n",
            "",
        )
    )
    clock = FakeClock(12_345.0)
    request = scans.ScanRequest("corp-it", "007", due=12_345.0)
    (event,) = scans.events(incus, clock, [request])
    assert event.at == 12_345.0
    event.action()
    assert incus.calls == [
        ("siem-01", ("/var/ossec/bin/agent_control", "-r", "-u", "007"))
    ]
    assert request.sent == 12_345.0
    assert request.ok
    assert (
        request.output
        == "Wazuh agent_control: Restarting Syscheck/Rootcheck on agent: 007"
    )


def test_a_failed_scan_request_is_recorded() -> None:
    incus = FakeIncus(Result(1, "", "Error: Instance not running"))
    request = scans.ScanRequest("fms-01", "001", due=1.0)
    (event,) = scans.events(incus, FakeClock(1.0), [request])
    event.action()
    assert request.ok is False
    assert request.output == "Error: Instance not running"
