"""
Sim constants single-source agreement tests.

world.yaml is the declared single source for the shared physical constants and
RFID card numbers. The sim consumes them at build time: ``render.py`` projects
world.yaml to the committed crate input ``infra/sim/sim-constants.toml``, and the
sim's ``build.rs`` compiles those values into the binary (sim-network.md §7).
The sim's Rust source therefore no longer carries the values as literals; it
references the generated ``sim::constants`` module.

These tests guard both ends of that wiring:

  * the committed ``infra/sim/sim-constants.toml`` is exactly what ``render.py``
    would produce from the current ``world.yaml`` at the values in operation,
    which every render writes there whatever its compression factor. A change
    to world.yaml that was not re-rendered and committed therefore fails here,
    at ``make test-unit``, rather than shipping a stale constant into the sim;
  * the sim source consumes the generated constants (and has not had a literal
    re-introduced), so the build-time projection cannot be silently bypassed.

Run with: pytest tests/unit -q
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
from render import build_sim_constants, scaled  # noqa: E402
from world_schema import World  # noqa: E402

SIM_CRATE = ROOT.parent / "infra" / "sim"
COMMITTED_TOML = SIM_CRATE / "sim-constants.toml"
COMMITTED_CA_ROOT = SIM_CRATE / "ca.pem"
BUILD_RS = SIM_CRATE / "build.rs"
SIM_SRC = SIM_CRATE / "src"
MAIN_RS = SIM_SRC / "main.rs"
# The configuration of a coupled run, which the sim's binary and the
# competition's entry point both build.
COUPLED_RS = SIM_SRC / "coupled.rs"
DOCK_OPERATOR_RS = SIM_SRC / "actors" / "dock_operator" / "internals.rs"
TUG_GENERATOR_RS = SIM_SRC / "physical" / "tug_arrival_generator" / "internals.rs"
FUEL_TANK_RS = SIM_SRC / "physical" / "fuel_tank" / "internals.rs"
RECON_USER_RS = SIM_SRC / "actors" / "reconciliation_user" / "internals.rs"
SYSTEMS_ADMINISTRATOR_RS = SIM_SRC / "actors" / "systems_administrator" / "internals.rs"


def world_model() -> World:
    return World.model_validate(yaml.safe_load((ROOT / "world.yaml").read_text()))


def test_committed_sim_constants_match_world():
    """The committed crate input build.rs reads must be exactly what render.py
    projects from world.yaml at the values in operation, a compression factor
    of 1 (deployment-config.md §4.1, sim projection). Regenerate
    (``make render``) and commit it when world.yaml changes."""
    expected = build_sim_constants(world_model().at_compression_factor(1))
    actual = COMMITTED_TOML.read_text()
    assert actual == expected, (
        f"{COMMITTED_TOML} is stale relative to world.yaml — regenerate it "
        f"(run render.py / `make render`) and commit the result."
    )


def test_the_configuration_consumes_generated_scalar_constants():
    """coupled.rs must source the shared scalars (those the FMS also uses) from
    the generated module, not a re-introduced literal."""
    text = COUPLED_RS.read_text()
    for const in (
        "crate::constants::TANK_CAPACITY_L",
        "crate::constants::FLOW_METER_K_FACTOR",
    ):
        assert const in text, f"coupled.rs no longer references {const}"


def test_the_configuration_consumes_generated_roster():
    """The roster, each tug with its mean time away, is a scenario parameter
    (deployment-config.md §3): coupled.rs must take it from the generated
    module."""
    text = COUPLED_RS.read_text()
    assert "vessels: crate::constants::VESSELS" in text, (
        "coupled.rs does not take vessels from crate::constants::VESSELS"
    )
    assert "lambda_tug" not in text, "coupled.rs still configures lambda_tug"


# The scenario values lifted from the sim's literals into world.yaml
# (deployment-config.md §3), as the generated module names them.
LIFTED_CONSTANTS = (
    "INFLOW_RATE_L_PER_MIN",
    "OUTFLOW_RATE_L_PER_MIN",
    "OVERFILL_INTERLOCK_PCT",
    "REORDER_THRESHOLD_L",
    "DELIVERY_VOLUME_L",
    "LEAD_TIME",
    "SUPPLIER_POLL_INTERVAL",
    "REFUEL_VOLUME_MIN_L",
    "REFUEL_VOLUME_MAX_L",
)


def test_the_configuration_consumes_generated_scenario_values():
    """The flow rates, the interlock level, the supplier's ordering values and
    the refuel range are scenario parameters (deployment-config.md §3):
    coupled.rs must take each from the generated module, not hold its own
    literal."""
    text = COUPLED_RS.read_text()
    missing = [c for c in LIFTED_CONSTANTS if f"crate::constants::{c}" not in text]
    assert not missing, f"coupled.rs does not reference crate::constants::{missing}"


def test_build_rs_compiles_the_scenario_values_in():
    """build.rs emits every lifted value (deployment-config.md §3)."""
    text = BUILD_RS.read_text()
    missing = [c for c in LIFTED_CONSTANTS if c not in text]
    assert not missing, f"build.rs does not emit {missing}"


def test_the_sim_does_not_receive_the_cavitation_threshold():
    """The cavitation level is the FMS's (fms.md §5.1) and the sim does not
    read it (deployment-config.md §3): neither the rendered sim-constants.toml
    nor build.rs carries it."""
    flat = _toml_keys(build_sim_constants(world_model()))
    assert "cavitation_threshold_pct" not in flat
    assert "CAVITATION_THRESHOLD_PCT" not in BUILD_RS.read_text()


def test_fuel_tank_holds_no_interlock_literal():
    """The overfill interlock level is a scenario value (sim.md §1.3): the fuel
    tank takes it from its configuration, not from a literal 0.95."""
    assert "0.95" not in FUEL_TANK_RS.read_text(), (
        "fuel_tank internals still holds the interlock level as a literal"
    )


def test_tug_generator_holds_no_refuel_range_literal():
    """The refuel range is a scenario value (sim.md §6): the tug arrival
    generator takes it from its configuration, not from its own constants."""
    text = TUG_GENERATOR_RS.read_text()
    for name in ("VOLUME_MIN_L", "VOLUME_MAX_L"):
        assert name not in text, f"tug_arrival_generator internals still holds {name}"


def test_recon_user_consumes_generated_session_lifetime():
    """The look-back for a session's LOGIN is the FMS's session lifetime
    (deployment-config.md §3): the reconciliation user must take it from the
    generated module, not hold its own literal."""
    assert "crate::constants::SESSION_TIMEOUT_S" in RECON_USER_RS.read_text(), (
        "reconciliation_user internals does not reference "
        "crate::constants::SESSION_TIMEOUT_S"
    )


def test_the_configuration_consumes_generated_audit_cadence():
    """The audit's period and settling delay are scenario parameters
    (deployment-config.md §3): coupled.rs must take them from the generated
    module, not hold its own literals."""
    text = COUPLED_RS.read_text()
    for const in (
        "crate::constants::AUDIT_PERIOD",
        "crate::constants::SETTLING_DELAY",
        "crate::constants::BOUNDARY_MARGIN",
    ):
        assert const in text, f"coupled.rs does not reference {const}"


def test_build_rs_compiles_the_audit_cadence_in():
    text = BUILD_RS.read_text()
    for const in ("AUDIT_PERIOD", "SETTLING_DELAY", "BOUNDARY_MARGIN"):
        assert const in text, f"build.rs does not emit {const}"


def test_the_configuration_consumes_generated_scenario_rates_and_timings():
    """The flow rates, the fuel supplier's poll and lead time, and the
    administrator's check interval are scenario parameters that a compressed
    scenario scales (deployment-config.md §3): coupled.rs must take them from
    the generated module, which render.py fills already scaled."""
    text = COUPLED_RS.read_text()
    for const in (
        "crate::constants::INFLOW_RATE_L_PER_MIN",
        "crate::constants::OUTFLOW_RATE_L_PER_MIN",
        "crate::constants::SUPPLIER_POLL_INTERVAL",
        "crate::constants::LEAD_TIME",
        "crate::constants::ADMIN_CHECK_INTERVAL",
    ):
        assert const in text, f"coupled.rs does not reference {const}"


def test_dock_operator_consumes_generated_poll_interval():
    """The dock operator's poll of a job's status is a scenario parameter
    (deployment-config.md §3), not a literal in the actor."""
    text = (SIM_SRC / "actors" / "dock_operator" / "internals.rs").read_text()
    assert "crate::constants::DOCK_OPERATOR_POLL_INTERVAL" in text


def test_recon_user_consumes_generated_k_factor():
    """The ±0.5% check crosses from pulses to litres with the K-factor, a
    projected world fact (deployment-config.md §3); a literal in the actor would
    reintroduce the drift the compiled constants prevent."""
    assert "crate::constants::FLOW_METER_K_FACTOR" in RECON_USER_RS.read_text(), (
        "reconciliation_user internals does not reference "
        "crate::constants::FLOW_METER_K_FACTOR"
    )


def test_actors_consume_generated_card_constants():
    """The RFID card actors must source their card numbers from the generated
    module, not a re-introduced literal."""
    assert "crate::constants::OPERATOR_CARD" in DOCK_OPERATOR_RS.read_text(), (
        "dock_operator internals no longer references crate::constants::OPERATOR_CARD"
    )


def test_tug_generator_holds_no_vessel_of_its_own():
    """The tug arrival generator takes the roster at spawn (sim-impl.md, tug
    arrival generator), so it holds no vessel constants of its own: each tug's
    IMO number, name and card come together from one world.yaml roster entry,
    as the FMS requires of a card and the vessel the job names."""
    for path in (TUG_GENERATOR_RS, TUG_GENERATOR_RS.with_name("mod.rs")):
        assert "crate::constants::VESSEL" not in path.read_text(), (
            f"{path} still reads a vessel from the generated constants"
        )


def test_build_rs_compiles_the_roster_in():
    """build.rs reads `vessel_count` and the numbered vessel keys and emits the
    roster as VESSELS (deployment-config.md §4.1); the single-vessel keys and
    the arrival rate are no longer read."""
    text = BUILD_RS.read_text()
    assert "vessel_count" in text, "build.rs does not read vessel_count"
    assert "VESSELS" in text, "build.rs does not emit VESSELS"
    for withdrawn in (
        "vessel_cards",
        "vessel_imo_number",
        "lambda_tug",
        "LAMBDA_TUG",
        "VESSEL_IMO_NUMBER",
        "VESSEL_RFID_CARD",
    ):
        assert withdrawn not in text, f"build.rs still refers to {withdrawn}"


def test_dock_operator_consumes_generated_credentials():
    """The operator username and password must be sourced from the generated
    module, not re-introduced as Rust literals — the login credentials are
    projected from world.yaml the same way the card is."""
    text = DOCK_OPERATOR_RS.read_text()
    assert "crate::constants::OPERATOR_USERNAME" in text, (
        "dock_operator internals no longer references crate::constants::OPERATOR_USERNAME"
    )
    assert "crate::constants::OPERATOR_PASSWORD" in text, (
        "dock_operator internals no longer references crate::constants::OPERATOR_PASSWORD"
    )


def test_systems_administrator_consumes_generated_world_facts():
    """The workstation credential, pinned host key and cadence must all come
    from world.yaml through the generated constants module, and the first
    check from the start state."""
    actor = SYSTEMS_ADMINISTRATOR_RS.read_text()
    configuration = COUPLED_RS.read_text()
    for const in (
        "crate::constants::SYSTEMS_ADMINISTRATOR_USERNAME",
        "crate::constants::SYSTEMS_ADMINISTRATOR_PASSWORD",
        "crate::constants::ADMIN_HOST_KEY",
    ):
        assert const in actor, f"systems administrator no longer references {const}"
    assert "admin_check_interval: crate::constants::ADMIN_CHECK_INTERVAL," in (
        configuration
    )
    assert "admin_first_check_after: start.first_check_after," in configuration


# ── Selective rebuild wiring ───────────────────────────────────────────────────
#
# The sim image is only rebuilt when the sim binary changes. The binary changes
# when sim-constants.toml changes (Cargo detects this via rerun-if-changed and
# re-runs build.rs, producing a different world_constants.rs). The tests below
# guard both ends of that wiring: that build.rs declares the directive, and that
# only sim-relevant world.yaml fields affect sim-constants.toml output.


def test_build_rs_declares_rerun_if_changed():
    """build.rs must declare cargo:rerun-if-changed for sim-constants.toml so
    that Cargo recompiles the binary whenever the projection changes, making the
    image-build content hash change and triggering a sim image rebuild."""
    text = BUILD_RS.read_text()
    assert "cargo:rerun-if-changed=" in text, (
        "build.rs contains no cargo:rerun-if-changed directive"
    )
    assert "sim-constants.toml" in text, (
        "build.rs does not reference sim-constants.toml in its rerun-if-changed path"
    )


# ── The internal certificate authority's root (deployment-config.md §3) ────────
#
# The reconciliation user verifies recon-01's remote-desktop certificate against
# the deployment's root, which is compiled into the binary like the constants
# above. The root is not a world.yaml value but derived from it by build-ca.sh,
# so build-ca.sh, not render.py, writes the committed copy.


def test_the_committed_root_is_the_one_build_ca_signs():
    """The committed copy is byte-identical to the root build-ca.sh signs from
    world.yaml, and build-ca.sh writes that root into the sim crate. The root is
    deterministic (deployment.md §9.1), so a change to
    `crypto.password_salt_seed` or `crypto.ca_not_before` changes it, and the
    copy must be rewritten by `make build-ca` and committed with world.yaml.

    build-ca.sh overwrites the copy, so the committed bytes are read before it
    runs and restored after: a stale copy fails the test rather than being
    replaced by it."""
    committed = COMMITTED_CA_ROOT.read_bytes()
    try:
        for step in (
            [sys.executable, "scripts/render.py", "--profile", "ci-minimal"],
            ["bash", "scripts/build-ca.sh"],
        ):
            out = subprocess.run(step, cwd=ROOT, capture_output=True, text=True)
            assert out.returncode == 0, out.stderr
        built = (ROOT / "build" / "ca" / "ca.pem").read_bytes()
        written = COMMITTED_CA_ROOT.read_bytes()
    finally:
        COMMITTED_CA_ROOT.write_bytes(committed)
    assert written == built, (
        f"build-ca.sh did not write the root it signed to {COMMITTED_CA_ROOT}"
    )
    assert committed == built, (
        f"{COMMITTED_CA_ROOT} is not the root build-ca.sh signs from world.yaml: "
        "run `make build-ca` and commit the result"
    )


def test_build_rs_compiles_the_ca_root_in():
    """build.rs reads the committed root, recompiles when it changes, and emits
    it into the generated constants module."""
    text = BUILD_RS.read_text()
    assert "ca.pem" in text, "build.rs does not read the committed ca.pem"
    assert "CA_ROOT_PEM" in text, "build.rs does not emit a CA_ROOT_PEM constant"


def test_sim_constants_change_when_sim_relevant_field_changes():
    """Changing a world.yaml field that is projected to sim-constants.toml must
    produce different TOML output. Cargo detects the changed file via
    rerun-if-changed and recompiles the binary, so the image-build hash changes
    and the sim image is rebuilt."""
    world = world_model()
    baseline = build_sim_constants(world)
    mutated_process = world.process.model_copy(
        update={"tank_capacity_l": world.process.tank_capacity_l + 1.0}
    )
    assert (
        build_sim_constants(world.model_copy(update={"process": mutated_process}))
        != baseline
    )


def test_sim_constants_unchanged_when_sim_irrelevant_field_changes():
    """Changing a world.yaml field that is not projected to sim-constants.toml
    must leave the TOML output identical, so no spurious Cargo recompile or sim
    image rebuild is triggered."""
    world = world_model()
    baseline = build_sim_constants(world)
    mutated_intervals = world.intervals.model_copy(
        update={"atg_poll_s": world.intervals.atg_poll_s + 1}
    )
    assert (
        build_sim_constants(world.model_copy(update={"intervals": mutated_intervals}))
        == baseline
    )


# ── Fuel, ship and interactive actors (deployment-config.md §3) ───────────────
#
# The sim's slice gains the fuel's grade and properties (scenario parameters
# written onto the bunker delivery note), the receiving ship's name and IMO
# number, the reconciliation user's three credentials, and the systems
# administrator's reused local-workstation credential and cadence. The TOML is
# parsed with tomllib here, independently of build.rs's line scan, so these tests
# check what the file says rather than what the scan happens to find.

ROAD_TANKER_DRIVER_RS = SIM_SRC / "actors" / "road_tanker_driver" / "internals.rs"

# Each world.yaml source and the key build.rs reads it by. Key names are unique
# across the whole file (deployment-config.md §4.1): build.rs's line scan
# ignores sections and returns the first match.
SIM_SLICE_KEYS = {
    "grade": lambda w: w.process.grade,
    "density_kg_per_l": lambda w: w.process.density_kg_per_l,
    "sulphur_pct_mm": lambda w: w.process.sulphur_pct_mm,
    "flashpoint_c": lambda w: w.process.flashpoint_c,
    # Scenario parameters a compressed scenario scales, as render.py projects
    # them at the compression factor (deployment-config.md §4).
    "session_timeout_s": lambda w: scaled(w, "intervals.session_timeout_s"),
    "audit_period": lambda w: scaled(w, "audit.period"),
    "settling_delay_s": lambda w: scaled(w, "audit.settling_delay_s"),
    "dock_operator_poll_s": (
        lambda w: w.intervals.dock_operator_poll_s / w.compression_factor
    ),
    "admin_check_interval_s": (
        lambda w: w.intervals.admin_check_interval_h * 3600 / w.compression_factor
    ),
    "boundary_margin_s": lambda w: w.audit.boundary_margin_s / w.compression_factor,
    "recon_username": lambda w: w.identities.reconciliation_user.username,
    "recon_password": lambda w: w.identities.reconciliation_user.password,
    "recon_workstation_password": (
        lambda w: w.identities.reconciliation_user.workstation_password
    ),
    "systems_administrator_username": (
        lambda w: w.identities.systems_administrator.username
    ),
    "systems_administrator_password": (lambda w: w.identities.admin.password),
    # The lifted scenario values, under the sim's names where they differ
    # (deployment-config.md §4.1). The flow rates, the poll interval and the
    # lead time are projected at the compression factor; the levels and
    # volumes are not scaled.
    "overfill_interlock_pct": lambda w: w.process.overfill_interlock_pct,
    "outflow_rate_l_per_min": (
        lambda w: scaled(w, "process.dispenser_flow_rate_l_per_min")
    ),
    "inflow_rate_l_per_min": (
        lambda w: scaled(w, "fuel_supplier.tanker_pump_rate_l_per_min")
    ),
    "reorder_threshold_l": lambda w: w.fuel_supplier.reorder_threshold_l,
    "delivery_volume_l": lambda w: w.fuel_supplier.delivery_volume_l,
    "lead_time_s": lambda w: scaled(w, "fuel_supplier.lead_time_s"),
    "supplier_poll_interval_s": lambda w: scaled(w, "fuel_supplier.poll_interval_s"),
    "refuel_volume_min_l": lambda w: w.roster.refuel_volume_min_l,
    "refuel_volume_max_l": lambda w: w.roster.refuel_volume_max_l,
}


def _toml_keys(text: str) -> dict[str, object]:
    """Every key in the rendered TOML, flattened across sections; raises if a
    key appears in more than one section."""
    flat: dict[str, object] = {}
    for section, table in tomllib.loads(text).items():
        for key, value in table.items():
            assert key not in flat, (
                f"key {key!r} appears in more than one section (again in "
                f"[{section}]); build.rs would read the first occurrence for both"
            )
            flat[key] = value
    return flat


def test_no_key_repeats_across_sections():
    """deployment-config.md §4.1: a key reused in a second section silently
    compiles the first section's value — the build and the agreement test both
    pass, and the first symptom is a failed login on the VM."""
    _toml_keys(build_sim_constants(world_model()))


def test_sim_slice_carries_fuel_vessel_and_actor_credentials():
    world = world_model()
    flat = _toml_keys(build_sim_constants(world))
    for key, source in SIM_SLICE_KEYS.items():
        assert key in flat, f"sim-constants.toml has no {key!r}"
        assert flat[key] == source(world), (
            f"sim-constants.toml {key}={flat[key]!r} != world.yaml {source(world)!r}"
        )


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(
            lambda w: w.identities.reconciliation_user.model_copy(
                update={"password": 'Pass"word'}
            ),
            id="recon_password",
        ),
        pytest.param(
            lambda w: w.identities.reconciliation_user.model_copy(
                update={"workstation_password": 'Work"station'}
            ),
            id="recon_workstation_password",
        ),
    ],
)
def test_build_sim_constants_refuses_a_double_quote_in_recon_credentials(mutate):
    """build.rs strips the outer quotes of a TOML string without a parser, so a
    double quote inside the value cannot be represented; render refuses it rather
    than emit a value the sim build mis-parses."""
    world = world_model()
    identities = world.identities.model_copy(
        update={"reconciliation_user": mutate(world)}
    )
    with pytest.raises(ValueError):
        build_sim_constants(world.model_copy(update={"identities": identities}))


def test_sim_slice_carries_the_roster_as_numbered_keys():
    """deployment-config.md §4.1: the roster under [vessels], as `vessel_count`
    and, for each tug in the order of `roster.vessels`, its IMO number, name,
    RFID card and mean time away; nothing else in the section."""
    world = world_model()
    expected: dict[str, object] = {"vessel_count": len(world.roster.vessels)}
    for n, vessel in enumerate(world.roster.vessels, start=1):
        expected |= {
            f"vessel_{n}_imo_number": vessel.imo_number,
            f"vessel_{n}_name": vessel.name,
            f"vessel_{n}_rfid_card": vessel.rfid_card,
            f"vessel_{n}_mean_time_away_s": (
                vessel.mean_time_away_s / world.compression_factor
            ),
        }
    sections = tomllib.loads(build_sim_constants(world))
    assert sections.get("vessels") == expected


def test_single_vessel_keys_are_withdrawn():
    """The numbered roster replaces the single vessel's keys, its card in
    [rfid], and the arrival rate (deployment-config.md §4.1). Left in the file,
    each would still be found by build.rs's scan."""
    flat = _toml_keys(build_sim_constants(world_model()))
    for key in ("vessel_cards", "vessel_imo_number", "vessel_name", "lambda_tug"):
        assert key not in flat, f"sim-constants.toml still carries {key!r}"


@pytest.mark.parametrize("index", [0, 1], ids=["first tug", "second tug"])
def test_build_sim_constants_refuses_a_double_quote_in_vessel_name(index):
    """Every tug's name is emitted as a quoted string, not only the first's. The
    roster is two tugs whatever world.yaml holds; `model_copy` does not
    re-validate, so the second may repeat the first's identity."""
    world = world_model()
    first = world.roster.vessels[0]
    vessels = [first, first]
    vessels[index] = first.model_copy(update={"name": 'Tug "Alpha"'})
    roster = world.roster.model_copy(update={"vessels": vessels})
    with pytest.raises(ValueError):
        build_sim_constants(world.model_copy(update={"roster": roster}))


@pytest.mark.parametrize(
    "path", [DOCK_OPERATOR_RS, ROAD_TANKER_DRIVER_RS], ids=lambda p: p.parent.name
)
def test_actors_consume_generated_grade(path):
    """Both actors write the grade onto a paper artefact; each must take it from
    the generated module rather than hold its own literal."""
    text = path.read_text()
    assert "crate::constants::FUEL_GRADE" in text, (
        f"{path.parent.name} does not reference crate::constants::FUEL_GRADE"
    )
    assert '"MGO"' not in text, f"{path.parent.name} still holds a grade literal"


def test_road_tanker_driver_consumes_generated_density():
    """The density on the delivery ticket must be the world constant the FMS
    also receives (FMS_DENSITY_KG_PER_L), not a literal that can drift from it."""
    text = ROAD_TANKER_DRIVER_RS.read_text()
    assert "crate::constants::DENSITY_KG_PER_L" in text, (
        "road_tanker_driver does not reference crate::constants::DENSITY_KG_PER_L"
    )
    assert "0.840" not in text, "road_tanker_driver still holds a density literal"


def test_main_builds_a_coupled_clock():
    """The deployed sim runs against the digital world, so it must use the
    coupled clock — 1:1 pace, starting at the current UTC time — not an isolated
    one with a fixed start date (sim.md §2.1). An isolated clock dated 2025 put
    every paper artefact some 21 months behind the records it describes, which
    breaks sim.md §8.3's same-instant rule for every transfer."""
    text = MAIN_RS.read_text()
    assert "SimClock::coupled()" in text, "main.rs does not build SimClock::coupled()"
    assert "SimClock::isolated(" not in text, (
        "main.rs builds an isolated clock; isolated mode is not valid against the "
        "digital world"
    )
