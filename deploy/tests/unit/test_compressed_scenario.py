"""
Compressed scenarios (deployment-config.md §3 and §4; sim.md §2.2;
episodes-tests.md §2).

A compressed scenario divides every interval and duration of the port's
operation by the compression factor and multiplies every rate of fuel flow by
it (narrative.md §6, item 3, compressed scenarios). render.py projects each
such value already scaled, so no consumer holds the factor. Most of these tests
drive the render functions with a world at a compression factor of 5, the
provisional factor of episodes.md §5.2. A render without an additional override
file uses a compression factor of 1. Every render writes the committed
infra/sim/sim-constants.toml at the values in operation, and a render at
another factor also writes the scaled constants to build/sim/sim-constants.toml
(deployment-config.md §4.1, sim projection).

Run with: pytest tests/unit -q
"""

from __future__ import annotations

import subprocess
import sys
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[2]  # deploy/
sys.path.insert(0, str(ROOT / "scripts"))
from render import (  # noqa: E402
    build_fms_env,
    build_hmi_env,
    build_sim_constants,
    check_projection_map,
    load_world,
    load_yaml,
    merge_topology,
    scale,
    siem_schedules,
)
from world_schema import COMPRESSION_FACTOR, World  # noqa: E402


def world_data() -> dict:
    return yaml.safe_load((ROOT / "world.yaml").read_text())


def world_at(compression_factor: int) -> World:
    return World.model_validate(world_data()).at_compression_factor(compression_factor)


def topology() -> dict:
    return merge_topology(
        load_yaml(ROOT / "topology.yaml"),
        load_yaml(ROOT / "overrides" / "ci-minimal.yaml"),
    )


def env_pairs(text: str) -> dict[str, str]:
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)


def node_with_build_script(t: dict, script: str) -> dict:
    return next(
        node for node in t["nodes"].values() if node.get("build_script") == script
    )


# ── Scaling (deployment-config.md §4) ─────────────────────────────────────────


def _flat(world: World) -> dict:
    return {
        key: value
        for table in tomllib.loads(build_sim_constants(world)).values()
        for key, value in table.items()
    }


def test_a_compression_factor_of_one_reproduces_the_values_in_operation():
    world = world_at(1)
    flat = _flat(world)
    assert flat["supplier_poll_interval_s"] == 900
    assert flat["lead_time_s"] == 6 * 3600
    assert flat["dock_operator_poll_s"] == 30
    assert flat["admin_check_interval_s"] == 24 * 3600
    assert flat["inflow_rate_l_per_min"] == 600
    assert flat["outflow_rate_l_per_min"] == 300
    for n in range(1, flat["vessel_count"] + 1):
        assert flat[f"vessel_{n}_mean_time_away_s"] == 20 * 3600
    assert flat["audit_period"] == "month"
    assert flat["settling_delay_s"] == 21600
    assert flat["boundary_margin_s"] == 7200
    assert siem_schedules(world) == {
        "file_integrity_s": 43200,
        "inventory_s": 3600,
        "listening_port_s": 360,
    }


def test_render_scales_each_value_in_operation():
    """At a compression factor of 5, render.py projects every timing of the
    port's operation divided by 5 and every rate of fuel flow multiplied by
    5, and each scaled value reaches its consumer: the simulator's
    constants, the FMS's and the HMI's environment, and the Wazuh agents'
    configuration (episodes-tests.md, render_scales_each_value_in_operation;
    episodes.md §5.3, timings)."""
    world = world_at(5)
    flat, operation = _flat(world), _flat(world_at(1))
    # The simulator: every interval and duration shortens by the factor, each
    # tug's mean time away among them, and stays whole seconds at the
    # provisional factor; every rate of flow rises by it.
    timings = [key for key in operation if key.endswith("_s")]
    rates = [key for key in operation if key.endswith("_per_min")]
    assert len(timings) >= 10 and len(rates) == 2
    for key in timings:
        assert flat[key] * 5 == operation[key], key
        assert float(flat[key]).is_integer(), key
    for key in rates:
        assert flat[key] == operation[key] * 5, key
    assert flat["supplier_poll_interval_s"] == 180
    assert flat["lead_time_s"] == 4320
    assert flat["dock_operator_poll_s"] == 6
    assert flat["admin_check_interval_s"] == 17280
    assert flat["session_timeout_s"] == 360
    assert flat["settling_delay_s"] == 4320
    assert flat["boundary_margin_s"] == 1440
    for n in range(1, flat["vessel_count"] + 1):
        assert flat[f"vessel_{n}_mean_time_away_s"] == 4 * 3600
    assert flat["inflow_rate_l_per_min"] == 3000
    assert flat["outflow_rate_l_per_min"] == 1500
    # The audit period becomes a number of hours: 720 for a month, divided.
    assert flat["audit_period"] == "144h"
    # The FMS and the HMI.
    t = topology()
    fms = env_pairs(build_fms_env(node_with_build_script(t, "fms-01"), t, world))
    hmi = env_pairs(build_hmi_env(node_with_build_script(t, "hmi-01"), t, world))
    assert fms["FMS_ATG_POLL_INTERVAL"] == "6"
    assert fms["FMS_MODBUS_POLL_INTERVAL"] == "1"
    assert fms["FMS_SESSION_TIMEOUT"] == "360"
    assert hmi["HMI_CHALLENGE_TIMEOUT_SECS"] == "6"
    # The Wazuh agents' schedules, as their configuration holds them.
    schedules = siem_schedules(world)
    assert schedules == {
        "file_integrity_s": 8640,
        "inventory_s": 720,
        "listening_port_s": 72,
    }
    env = Environment(
        loader=FileSystemLoader(str(ROOT / "build-scripts")), undefined=StrictUndefined
    )
    rendered = env.get_template("_host-monitoring.xml.j2").render(
        siem_schedules=schedules
    )
    config = ET.fromstring(f"<ossec_config>{rendered}</ossec_config>")
    assert config.find("syscheck").findtext("frequency") == "8640"
    syscollector = next(
        w for w in config.findall("wodle") if w.get("name") == "syscollector"
    )
    assert syscollector.findtext("interval") == "720s"


def test_levels_and_volumes_are_not_scaled():
    """A compressed scenario scales the port's timings and flow rates, not its
    volumes or levels (narrative.md §6, item 3, compressed scenarios)."""
    one, five = _flat(world_at(1)), _flat(world_at(5))
    for key in (
        "tank_capacity_l",
        "reorder_threshold_l",
        "delivery_volume_l",
        "refuel_volume_min_l",
        "refuel_volume_max_l",
        "overfill_interlock_pct",
    ):
        assert five[key] == one[key], key


@pytest.mark.parametrize("factor", [1, 5])
def test_a_delivery_fits_below_the_overfill_interlock_at_both_factors(factor):
    """The render's check that a tanker arriving with the tank at the reorder
    threshold fits below the interlock level (deployment-config.md §5) holds
    for the values that the sim compiles at each factor."""
    flat = _flat(world_at(factor))
    interlock_l = flat["tank_capacity_l"] * flat["overfill_interlock_pct"] / 100
    assert flat["reorder_threshold_l"] + flat["delivery_volume_l"] <= interlock_l


@pytest.mark.parametrize(
    "period, hours", [("month", "144h"), ("week", "33.6h"), ("day", "4.8h")]
)
def test_a_compressed_audit_period_is_its_calendar_hours_divided(period, hours):
    assert scale(world_at(5), period, "hours") == hours
    assert scale(world_at(1), period, "hours") == period


def test_the_projection_check_holds_at_a_compression_factor_of_five():
    world = world_at(5)
    t = topology()
    check_projection_map(
        world,
        {
            "sim": build_sim_constants(world),
            "fms_env": build_fms_env(node_with_build_script(t, "fms-01"), t, world),
            "hmi_env": build_hmi_env(node_with_build_script(t, "hmi-01"), t, world),
        },
    )


# ── Render-time validation (deployment-config.md §5) ──────────────────────────


def test_render_rejects_a_factor_that_breaks_whole_seconds():
    # 30-second polls and windows divide by 5 but not by 7.
    with pytest.raises(ValueError, match="whole seconds"):
        world_at(7)


def test_a_compression_factor_below_one_is_refused():
    with pytest.raises(ValueError, match="below 1"):
        world_at(0)


def test_world_yaml_holds_no_compression_factor():
    # The compression factor is no fact about the fictional asset (episodes.md
    # §17, parameters), so world.yaml refuses one.
    data = world_data()
    assert "compression_factor" not in data
    data["compression_factor"] = COMPRESSION_FACTOR
    with pytest.raises(ValidationError, match="compression_factor"):
        World.model_validate(data)


def test_without_an_additional_override_file_the_compression_factor_is_one():
    assert COMPRESSION_FACTOR == 1
    assert World.model_validate(world_data()).compression_factor == 1
    assert load_world(ROOT / "world.yaml").compression_factor == 1


def _render(*extra_args: str) -> None:
    out = subprocess.run(
        [sys.executable, "scripts/render.py", "--profile", "ci-minimal", *extra_args],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert out.returncode == 0, f"render failed:\n{out.stderr}"


def test_a_compressed_render_writes_the_scaled_constants_beside_the_committed_ones(
    tmp_path: Path,
):
    """The committed constants keep the values in operation at every factor,
    and only a render at another factor writes the scaled copy that the
    competition image of sim-host compiles against."""
    committed = ROOT.parent / "infra" / "sim" / "sim-constants.toml"
    scaled_copy = ROOT / "build" / "sim" / "sim-constants.toml"
    override = tmp_path / "override.yaml"
    override.write_text("compression_factor: 5\n")
    try:
        _render("--extra-override", str(override))
        assert committed.read_text() == build_sim_constants(world_at(1))
        assert scaled_copy.read_text() == build_sim_constants(world_at(5))
    finally:
        # The shared build/ directory is left as a render without an
        # additional override file leaves it, for the rest of the suite.
        _render()
    assert committed.read_text() == build_sim_constants(world_at(1))
    assert not scaled_copy.exists()


def test_a_settling_delay_shorter_than_the_margin_is_refused():
    data = world_data()
    data["audit"]["boundary_margin_s"] = data["audit"]["settling_delay_s"] + 1
    with pytest.raises(ValidationError, match="boundary_margin_s"):
        World.model_validate(data)


@pytest.mark.parametrize(
    "section, key",
    [
        ("process", "dispenser_flow_rate_l_per_min"),
        ("fuel_supplier", "tanker_pump_rate_l_per_min"),
        ("fuel_supplier", "poll_interval_s"),
        ("fuel_supplier", "lead_time_s"),
        ("intervals", "dock_operator_poll_s"),
        ("intervals", "admin_check_interval_h"),
        ("intervals", "listening_port_interval_s"),
        ("audit", "boundary_margin_s"),
    ],
)
def test_a_scaled_value_must_be_positive(section, key):
    data = world_data()
    data[section][key] = 0
    with pytest.raises(ValidationError):
        World.model_validate(data)
