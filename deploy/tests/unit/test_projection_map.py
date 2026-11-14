"""Projection-map integrity unit tests. Run with: pytest tests/unit -q

Covers the declared source-to-destination map
(``scripts/world_schema.PROJECTION_MAP``) and the render-time check
(``scripts/render.check_projection_map``) that enforces it:

  * every declared source path resolves in the validated ``World``;
  * a consumer artefact must carry each destination key with world.yaml's
    canonical typed value, so a renamed key, a diverged value, or an ``int()``
    cast on a forced-float constant (reconciliation gap 5) aborts the render;
  * the real rendered ``fms-01.env``, ``hmi-01.env`` and the committed
    ``infra/sim/sim-constants.toml`` satisfy the map end-to-end.
"""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]  # deploy/
sys.path.insert(0, str(ROOT / "scripts"))

import render  # noqa: E402
from render import check_projection_map  # noqa: E402
from world_schema import PROJECTION_MAP, World  # noqa: E402


def load_world_model() -> World:
    return World.model_validate(yaml.safe_load((ROOT / "world.yaml").read_text()))


def canonical(world: World, dotted: str) -> str:
    """The canonical string a projection must emit for a source path: its value
    as render.py projects it, scaled by the compression factor
    (deployment-config.md §4). Raises AttributeError if the path does not
    resolve."""
    return str(render.scaled(world, dotted))


def valid_artefacts(world: World) -> dict[str, str]:
    """A minimal, correct artefact per consumer, built straight from the map."""
    lines: dict[str, list[str]] = {}
    for src, dests in PROJECTION_MAP.items():
        value = canonical(world, src)
        for consumer, key in dests.items():
            lines.setdefault(consumer, []).append(f"{key}={value}")
    return {c: "\n".join(rows) + "\n" for c, rows in lines.items()}


def test_numeric_fuel_properties_are_mapped():
    """Density reaches the FMS and the sim and must agree between them
    (deployment-config.md §1); sulphur and flashpoint reach the sim only.
    Declaring them here puts every one under the render-time mapping check."""
    assert PROJECTION_MAP["process.density_kg_per_l"] == {
        "fms_env": "FMS_DENSITY_KG_PER_L",
        "sim": "density_kg_per_l",
    }
    assert PROJECTION_MAP["process.sulphur_pct_mm"] == {"sim": "sulphur_pct_mm"}
    assert PROJECTION_MAP["process.flashpoint_c"] == {"sim": "flashpoint_c"}


def test_tug_arrival_rate_is_not_mapped():
    """The roster's arrival rate is withdrawn for each tug's mean time away
    (sim.md §6). That is a field of a roster entry, projected with the roster as
    numbered keys (deployment-config.md §4.1), not a scalar of this map."""
    assert "roster.lambda_tug" not in PROJECTION_MAP


def test_session_lifetime_is_mapped_to_the_fms_and_the_sim():
    """The FMS expires a session after this lifetime; the reconciliation user's
    retrieval reaches back by it for the LOGIN that opened a session (sim-impl.md,
    reconciliation user). The two must agree, so both are under the render-time
    mapping check (deployment-config.md §3)."""
    assert PROJECTION_MAP["intervals.session_timeout_s"] == {
        "fms_env": "FMS_SESSION_TIMEOUT",
        "sim": "session_timeout_s",
    }


def test_audit_cadence_is_mapped_to_the_sim():
    """The reconciliation audit's period and settling delay are the port's
    procedure, read only by the sim (deployment-config.md §3); declaring them
    here puts them under the render-time mapping check."""
    assert PROJECTION_MAP["audit.period"] == {"sim": "audit_period"}
    assert PROJECTION_MAP["audit.settling_delay_s"] == {"sim": "settling_delay_s"}


def test_lifted_scenario_values_are_mapped_to_the_sim():
    """Each value lifted from the sim's literals reaches the sim only, under the
    sim's own name where it differs from world.yaml's (deployment-config.md
    §4.1); declaring them here puts every one under the render-time mapping
    check."""
    expected = {
        "process.dispenser_flow_rate_l_per_min": {"sim": "outflow_rate_l_per_min"},
        "process.overfill_interlock_pct": {"sim": "overfill_interlock_pct"},
        "fuel_supplier.reorder_threshold_l": {"sim": "reorder_threshold_l"},
        "fuel_supplier.delivery_volume_l": {"sim": "delivery_volume_l"},
        "fuel_supplier.lead_time_s": {"sim": "lead_time_s"},
        "fuel_supplier.tanker_pump_rate_l_per_min": {"sim": "inflow_rate_l_per_min"},
        "fuel_supplier.poll_interval_s": {"sim": "supplier_poll_interval_s"},
        "roster.refuel_volume_min_l": {"sim": "refuel_volume_min_l"},
        "roster.refuel_volume_max_l": {"sim": "refuel_volume_max_l"},
    }
    for src, dests in expected.items():
        assert PROJECTION_MAP.get(src) == dests, src


def test_cavitation_threshold_reaches_only_the_fms():
    """The FMS refuses a dispense at the cavitation level (fms.md §5.1); the sim
    does not read it, so it is projected to the FMS alone
    (deployment-config.md §3)."""
    assert PROJECTION_MAP["process.cavitation_threshold_pct"] == {
        "fms_env": "FMS_CAVITATION_THRESHOLD_PCT",
    }


def test_grade_reaches_the_fms():
    """The FMS refuses a job for any grade but the configured one (fms.md §7.4),
    so it must receive world.yaml's grade, under the same render-time mapping
    check as the density it records for that grade. The sim's quoted copy is
    checked by test_sim_constants_agreement.py."""
    assert PROJECTION_MAP["process.grade"] == {"fms_env": "FMS_GRADE"}


def test_projection_map_sources_resolve():
    """Every PROJECTION_MAP source path resolves in the validated World."""
    world = load_world_model()
    for src in PROJECTION_MAP:
        canonical(world, src)  # raises AttributeError on drift


def test_check_passes_for_valid_artefacts():
    world = load_world_model()
    check_projection_map(world, valid_artefacts(world))  # must not raise


def test_check_aborts_on_missing_destination_key():
    world = load_world_model()
    art = valid_artefacts(world)
    art["fms_env"] = (
        "\n".join(
            r for r in art["fms_env"].splitlines() if not r.startswith("FMS_K_FACTOR=")
        )
        + "\n"
    )
    with pytest.raises(SystemExit):
        check_projection_map(world, art)


def test_check_aborts_on_diverged_value():
    world = load_world_model()
    art = valid_artefacts(world)
    art["fms_env"] = art["fms_env"].replace("FMS_K_FACTOR=100.0", "FMS_K_FACTOR=999.0")
    with pytest.raises(SystemExit):
        check_projection_map(world, art)


def test_check_aborts_on_int_cast_of_forced_float():
    """A forced-float constant emitted as a bare int (reconciliation gap 5)
    diverges from world.yaml's canonical float and must abort."""
    world = load_world_model()
    art = valid_artefacts(world)
    art["fms_env"] = art["fms_env"].replace("FMS_K_FACTOR=100.0", "FMS_K_FACTOR=100")
    with pytest.raises(SystemExit):
        check_projection_map(world, art)


def test_check_aborts_on_unresolvable_source(monkeypatch):
    world = load_world_model()
    art = valid_artefacts(world)  # built from the clean map before patching
    monkeypatch.setitem(render.PROJECTION_MAP, "process.nonexistent", {"fms_env": "X"})
    with pytest.raises(SystemExit):
        check_projection_map(world, art)


# ── End-to-end: the real render must satisfy the map ──────────────────────────

# Absolute paths to the artefact each consumer identifier refers to.
# fms_env and hmi_env are written under build/ on every render; sim-constants.toml
# is written to the committed crate path (infra/sim/) — not under build/.
CONSUMER_FILES: dict[str, Path] = {
    "fms_env": ROOT / "build" / "env" / "fms-01.env",
    "hmi_env": ROOT / "build" / "env" / "hmi-01.env",
    "sim": ROOT.parent / "infra" / "sim" / "sim-constants.toml",
}


def render_ci_minimal() -> None:
    out = subprocess.run(
        [sys.executable, "scripts/render.py", "--profile", "ci-minimal"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert out.returncode == 0, out.stderr


def artefact_pairs(text: str) -> dict[str, str]:
    pairs: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        pairs[key.strip()] = value.strip()
    return pairs


def toml_pairs(text: str) -> dict[str, str]:
    """The sim's artefact read as the TOML it is, flattened across sections,
    each value in its canonical string form: a string unquoted, as its source."""
    return {
        key: str(value)
        for table in tomllib.loads(text).values()
        for key, value in table.items()
    }


def test_rendered_artefacts_satisfy_projection_map():
    world = load_world_model()
    render_ci_minimal()
    parsed = {
        consumer: (toml_pairs if consumer == "sim" else artefact_pairs)(
            path.read_text()
        )
        for consumer, path in CONSUMER_FILES.items()
    }
    for src, dests in PROJECTION_MAP.items():
        expected = canonical(world, src)
        for consumer, key in dests.items():
            assert key in parsed[consumer], f"{consumer} artefact missing {key}"
            assert parsed[consumer][key] == expected, (
                f"{consumer}:{key}={parsed[consumer][key]!r} != {expected!r}"
            )
