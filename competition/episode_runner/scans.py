"""Scan requests (episodes.md §6.2, first occurrences).

Where the time of a monitored host's first file-integrity scan falls inside the
episode, the episode runner requests the scan from the manager of the security
monitoring on siem-01 at that time. The manager's command-line tool
agent_control, with its option -r, requests a scan from one agent, which runs
it at its next check-in with the manager. The episode runner runs the tool
through its own incus exec into siem-01, apart from the step session.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from competition.episode_runner import schedule
from competition.episode_runner.settle import AGENT_CONTROL, SIEM, Incus
from competition.episode_runner.steps import Event

log = logging.getLogger(__name__)


@dataclass
class ScanRequest:
    host: str
    agent_id: str
    due: float
    sent: float | None = None
    ok: bool | None = None
    output: str = ""


def plan(
    episode_seed_hex: str,
    monitored: Sequence[str],
    agent_ids: dict[str, str],
    interval_s: float,
    sim_start: float,
    step_0: float,
    end: float,
) -> list[ScanRequest]:
    """The scan requests of the episode whose steps run from step_0 to end,
    in time order. Times are on the episode runner's clock, and the simulator
    started at sim_start."""
    times = schedule.first_scans(episode_seed_hex, monitored, interval_s)
    inside = schedule.scans_inside(times, step_0 - sim_start, end - sim_start)
    return [ScanRequest(host, agent_ids[host], sim_start + t) for t, host in inside]


def events(incus: Incus, clock, requests: Sequence[ScanRequest]) -> list[Event]:
    """One timed event of the step schedule for each scan request."""

    def send(request: ScanRequest) -> None:
        result = incus.exec(SIEM, AGENT_CONTROL, "-r", "-u", request.agent_id)
        request.sent = clock.now()
        request.ok = result.ok
        request.output = (result.stdout + result.stderr).strip()
        if not result.ok:
            log.warning(
                "the scan request for %s failed: %s", request.host, request.output
            )

    return [Event(r.due, lambda r=r: send(r)) for r in requests]
