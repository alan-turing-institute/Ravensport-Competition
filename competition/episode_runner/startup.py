"""The simulator's start-up input, startup.json (episodes.md §4.3, start-up
input; episode-runner-impl.md §2, start-up input).

The first version starts every episode from the simulator's own start state
(sim-impl.md, "Start state"), with the episode's seed of the generator
sim_tug_arrivals: 35,000 litres in the tank, no order placed, both totalisers
at zero, the first bunker delivery note and the first delivery ticket numbered
1, and no paper held. The fuel supplier's first poll and the systems
administrator's first routine check come one compressed interval after the
simulator's start. The next audit comes one compressed audit period and one
settling delay after the episode's plan, so that the first audit window runs
one whole period, and no audit falls inside an episode. The start-up input
holds no paper, and the simulator's first window starts at its own start.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from competition.episode_runner.seeds import SIM_TUG_ARRIVALS, derive_generator_seed

if TYPE_CHECKING:
    from competition.episode_runner.predicted_events import Timings

# The fields of the start-up input, in the order of episode-runner-impl.md §2
# (start-up input), as the competition's entry point lists them in
# competition/sim/src/input.rs.
FIELDS = (
    "sim_tug_arrivals",
    "initial_volume_l",
    "order_pending_s",
    "first_poll_after_s",
    "first_check_after_s",
    "next_audit_at",
    "initial_cumulative_pulses",
    "initial_meter_totaliser_l",
    "first_bdn_number",
    "first_ticket_number",
    "held_since",
    "held_artefacts",
)
# The simulator's own initial volume, INITIAL_VOLUME_L in infra/sim/src/coupled.rs.
INITIAL_VOLUME_L = 35_000.0
# An audit period in hours, as render.py projects it in a compressed scenario.
_HOURS = re.compile(r"\A(\d+(?:\.\d+)?)h\Z")


def start_state(timings: Timings) -> dict[str, Any]:
    """The simulator's own start state at the competition's timings, as the
    fields of the start-up input that give it, other than the seed and the
    next audit."""
    return {
        "initial_volume_l": INITIAL_VOLUME_L,
        "order_pending_s": None,
        "first_poll_after_s": timings.poll_s,
        "first_check_after_s": timings.check_s,
        "initial_cumulative_pulses": 0,
        "initial_meter_totaliser_l": 0.0,
        "first_bdn_number": 1,
        "first_ticket_number": 1,
        "held_since": None,
        "held_artefacts": [],
    }


def audit_period_s(period: str) -> float:
    """The audit period in seconds. The competition's entry point takes only an
    audit period in hours, such as 144h, and refuses a calendar period."""
    found = _HOURS.match(period)
    if found is None or float(found.group(1)) <= 0.0:
        raise ValueError(f"the audit period {period!r} is not a number of hours")
    return float(found.group(1)) * 3600.0


def startup_input(episode_seed_hex: str, timings: Timings, planned: datetime) -> str:
    """The start-up input of an episode planned at `planned`."""
    next_audit = planned.astimezone(timezone.utc).replace(microsecond=0) + timedelta(
        seconds=audit_period_s(timings.audit_period) + timings.settling_delay_s
    )
    given = {
        **start_state(timings),
        SIM_TUG_ARRIVALS: derive_generator_seed(episode_seed_hex, SIM_TUG_ARRIVALS),
        "next_audit_at": f"{next_audit:%Y-%m-%dT%H:%M:%SZ}",
    }
    return json.dumps({field: given[field] for field in FIELDS}) + "\n"
