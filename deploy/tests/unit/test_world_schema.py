"""world.yaml declared-schema unit tests. Run with: pytest tests/unit -q

Covers the strict schema + forced types in scripts/world_schema.py:
  * world.yaml conforms to world_schema.World;
  * forced types — the process constants are coerced to float (the form the FMS
    env_f64 / sim f64 consumers require) and the intervals to int;
  * the strict base rejects unknown keys and the schema rejects invalid inputs
    (duplicate cards/usernames, unknown grade, malformed salt, out-of-range
    values);
  * the fuel's properties and the receiving ship's IMO number
    (deployment-config.md §3, §5): present, forced to float, range-checked, and
    the IMO check digit verified;
  * the scenario values lifted from the sim — the dispenser's flow rate, the
    overfill interlock level, the fuel supplier's ordering values and the refuel
    range (deployment-config.md §3, §5): present, forced to float or int, each
    positive and finite on its own, and consistent with one another.
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import pydantic
import pytest
import yaml
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[2]  # deploy/
sys.path.insert(0, str(ROOT / "scripts"))
from world_schema import World  # noqa: E402


def load_world() -> dict:
    return yaml.safe_load((ROOT / "world.yaml").read_text())


def test_world_yaml_validates():
    World.model_validate(load_world())


def test_process_constants_forced_to_float():
    """tank_capacity_l, cavitation_threshold_pct, k_factor must be float even
    when authored as bare integers in world.yaml — the FMS reads them via
    env_f64 and the sim as f64."""
    w = World.model_validate(load_world())
    assert isinstance(w.process.tank_capacity_l, float)
    assert isinstance(w.process.cavitation_threshold_pct, float)
    assert isinstance(w.process.k_factor, float)


def test_intervals_forced_to_int():
    w = World.model_validate(load_world())
    assert isinstance(w.intervals.atg_poll_s, int)
    assert isinstance(w.intervals.modbus_poll_s, int)
    assert isinstance(w.intervals.session_timeout_s, int)
    assert isinstance(w.intervals.challenge_timeout_s, int)
    assert isinstance(w.intervals.admin_check_interval_h, int)


# Each mutation makes world.yaml invalid in exactly one way the schema must
# reject. duplicate_username copies the first operator (same username) with a
# fresh card, so the username check fires rather than the card check.
INVALID_CASES = {
    "duplicate_rfid_card": lambda d: d["roster"]["vessels"][0].__setitem__(
        "rfid_card", d["identities"]["operators"][0]["rfid_card"]
    ),
    # Appends an operator with the same username as the first but a fresh card,
    # so the username check fires rather than the card-uniqueness check.
    "duplicate_username": lambda d: d["identities"]["operators"].append(
        {**d["identities"]["operators"][0], "rfid_card": 9999999}
    ),
    # Off-by-one from the valid world.yaml value, so the check digit no
    # longer matches; the range bound is not what rejects it.
    "bad_imo_check_digit": lambda d: d["roster"]["vessels"][0].__setitem__(
        "imo_number", d["roster"]["vessels"][0]["imo_number"] + 1
    ),
    "duplicate_imo_number": lambda d: d["roster"]["vessels"].append(
        {**d["roster"]["vessels"][0], "rfid_card": 1999999}
    ),
    "unknown_grade": lambda d: d["process"].__setitem__("grade", "DIESEL"),
    "malformed_salt_seed": lambda d: d["crypto"].__setitem__(
        "password_salt_seed", "xyz"
    ),
    "cavitation_over_100": lambda d: d["process"].__setitem__(
        "cavitation_threshold_pct", 150
    ),
    "non_positive_k_factor": lambda d: d["process"].__setitem__("k_factor", 0),
    "unknown_extra_key": lambda d: d.__setitem__("surprise", 1),
    # empty_operators: tests Field(min_length=1) on the operators list; deploying
    # with no operators leaves no one able to authenticate.
    "empty_operators": lambda d: d["identities"].__setitem__("operators", []),
    # zero_atg_poll_s: tests Field(gt=0) on interval fields; a zero poll interval
    # would cause the FMS to spin requests without any sleep.
    "zero_atg_poll_s": lambda d: d["intervals"].__setitem__("atg_poll_s", 0),
    "zero_admin_check_interval_h": lambda d: d["intervals"].__setitem__(
        "admin_check_interval_h", 0
    ),
    # non_positive_rfid_card: tests Field(gt=0) on RFID card number fields;
    # card number 0 is not a valid issued card.
    "non_positive_rfid_card": lambda d: d["identities"]["operators"][0].__setitem__(
        "rfid_card", 0
    ),
    # unknown_nested_key: tests that extra="forbid" is enforced on nested models,
    # not only at the root World level.
    "unknown_nested_key": lambda d: d["process"].__setitem__("surprise", 1),
    # duplicate_hist_admin_username: hist_admin's username collides with an
    # operator's. Username uniqueness spans all identities, not just admin/
    # operators, so this must fire the same duplicate-username check.
    "duplicate_hist_admin_username": lambda d: d["identities"][
        "hist_admin"
    ].__setitem__("username", d["identities"]["operators"][0]["username"]),
    # missing_reconciliation_user: hist-dmz-01's Grafana seed has no identity to
    # project without this field; its absence must fail schema validation.
    "missing_reconciliation_user": lambda d: d["identities"].pop("reconciliation_user"),
    "missing_systems_administrator": lambda d: d["identities"].pop(
        "systems_administrator"
    ),
    "duplicate_systems_administrator_username": lambda d: d["identities"][
        "systems_administrator"
    ].__setitem__("username", d["identities"]["operators"][0]["username"]),
    "admin_password_not_ascii": lambda d: d["identities"]["admin"].__setitem__(
        "password", d["identities"]["admin"]["password"] + "£"
    ),
    # hist_admin/reconciliation_user field-level requireds (hist-tests.md §8):
    # each seeded Grafana identity must carry both username and password, or the
    # render has nothing to hash into the user row. One case drops the password
    # on hist_admin and the other the username on reconciliation_user, so both
    # fields are covered across both identities.
    "hist_admin_missing_password": lambda d: d["identities"]["hist_admin"].pop(
        "password"
    ),
    "reconciliation_user_missing_username": lambda d: d["identities"][
        "reconciliation_user"
    ].pop("username"),
    # reconciliation_user_missing_workstation_password: recon-01's shadow entry
    # has nothing to project without this field (recon.md §7.1). Required rather
    # than optional: a workstation whose account cannot be seeded cannot be
    # logged into, and the remote desktop session is the host's only purpose.
    "reconciliation_user_missing_workstation_password": lambda d: d["identities"][
        "reconciliation_user"
    ].pop("workstation_password"),
    # reconciliation_user_password_reuse: the workstation account and the Grafana
    # account are separate systems with separate user stores, and recon.md §7.1
    # requires that compromising one does not yield the other. Reuse is rejected
    # by the schema rather than left to review because it is invisible in a
    # deployed environment — both logins go on working.
    "reconciliation_user_password_reuse": lambda d: d["identities"][
        "reconciliation_user"
    ].__setitem__(
        "workstation_password", d["identities"]["reconciliation_user"]["password"]
    ),
    # Typable credentials (deployment-config.md §5): the username and Grafana
    # password are typed into a terminal as `username:password` inside the
    # single-quoted `-u` argument of the audit command, through a scancode table
    # covering printable ASCII. A single quote ends the argument; a character
    # outside printable ASCII has no scancode; a colon in the username moves the
    # boundary Basic authentication reads between username and password. Each
    # fault is appended to a valid value, so this rule alone rejects it.
    "reconciliation_user_username_single_quote": lambda d: d["identities"][
        "reconciliation_user"
    ].__setitem__("username", d["identities"]["reconciliation_user"]["username"] + "'"),
    "reconciliation_user_username_colon": lambda d: d["identities"][
        "reconciliation_user"
    ].__setitem__("username", d["identities"]["reconciliation_user"]["username"] + ":"),
    "reconciliation_user_username_not_ascii": lambda d: d["identities"][
        "reconciliation_user"
    ].__setitem__("username", d["identities"]["reconciliation_user"]["username"] + "é"),
    "reconciliation_user_password_single_quote": lambda d: d["identities"][
        "reconciliation_user"
    ].__setitem__("password", d["identities"]["reconciliation_user"]["password"] + "'"),
    "reconciliation_user_password_not_ascii": lambda d: d["identities"][
        "reconciliation_user"
    ].__setitem__("password", d["identities"]["reconciliation_user"]["password"] + "£"),
    "reconciliation_user_password_control_character": lambda d: d["identities"][
        "reconciliation_user"
    ].__setitem__(
        "password", d["identities"]["reconciliation_user"]["password"] + "\t"
    ),
    # The workstation password is printable ASCII.
    "reconciliation_user_workstation_password_not_ascii": lambda d: d["identities"][
        "reconciliation_user"
    ].__setitem__(
        "workstation_password",
        d["identities"]["reconciliation_user"]["workstation_password"] + "é",
    ),
    # siem_admin field-level requireds (siem-tests.md §10): the management
    # interface credential (identities.siem_admin, deployment-config.md §6) has
    # nothing to hash into rbac.db without both fields.
    "siem_admin_missing_username": lambda d: (
        d["identities"].__setitem__(
            "siem_admin", {"username": "siem_admin", "password": "x"}
        ),
        d["identities"]["siem_admin"].pop("username"),
    ),
    "siem_admin_missing_password": lambda d: (
        d["identities"].__setitem__(
            "siem_admin", {"username": "siem_admin", "password": "x"}
        ),
        d["identities"]["siem_admin"].pop("password"),
    ),
    # duplicate_siem_admin_username: siem_admin's username collides with an
    # operator's. Username uniqueness spans all identities (World's
    # _unique_cards_and_usernames validator), not just admin/operators.
    "duplicate_siem_admin_username": lambda d: d["identities"].__setitem__(
        "siem_admin",
        {
            "username": d["identities"]["operators"][0]["username"],
            "password": "x",
        },
    ),
}


def test_world_schema_accepts_siem_admin_identity():
    """identities.siem_admin (deployment-config.md §6, siem.md §8.5) is a plain
    username/password pair, seeded into the manager's own credential store at
    build time — the same shape as hist_admin, not the two-password shape
    reconciliation_user needs."""
    d = copy.deepcopy(load_world())
    d["identities"]["siem_admin"] = {
        "username": "siem_admin",
        "password": "a-siem-management-password",
    }
    w = World.model_validate(d)
    assert w.identities.siem_admin.username == "siem_admin"
    assert w.identities.siem_admin.password == "a-siem-management-password"


def test_world_schema_rejects_siem_admin_extra_key():
    """The strict base must reject an unknown key on siem_admin, the same as
    every other identity (extra="forbid" applies at every nesting level)."""
    d = copy.deepcopy(load_world())
    d["identities"]["siem_admin"] = {
        "username": "siem_admin",
        "password": "x",
        "surprise": 1,
    }
    with pytest.raises(pydantic.ValidationError):
        World.model_validate(d)


def test_reconciliation_user_passwords_are_distinct():
    """The two credentials the reconciliation user holds must not be the same
    value (recon.md §7.1).

    The rejection case above proves the schema refuses reuse; this proves the
    authored world.yaml does not rely on that refusal.
    """
    w = World.model_validate(load_world())
    recon = w.identities.reconciliation_user
    assert recon.password != recon.workstation_password


def test_workstation_password_may_carry_a_single_quote():
    """The workstation password is sent in the remote-desktop logon, not typed
    inside a quoted argument, so the single-quote refusal does not extend to it
    (deployment-config.md §5)."""
    d = copy.deepcopy(load_world())
    recon = d["identities"]["reconciliation_user"]
    recon["workstation_password"] = recon["workstation_password"] + "'"
    World.model_validate(d)


@pytest.mark.parametrize("name", sorted(INVALID_CASES))
def test_schema_rejects_invalid_world(name):
    d = copy.deepcopy(load_world())
    INVALID_CASES[name](d)
    with pytest.raises(pydantic.ValidationError):
        World.model_validate(d)


# ── Fuel properties and the receiving ship (deployment-config.md §3, §5) ──────
#
# These fields are new, so a bare pytest.raises would pass against a schema that
# does not declare them yet: `extra="forbid"` rejects the unknown key before any
# range check runs. Each case therefore names the location the rejection must
# come from and refuses an `extra_forbidden` error as the reason.


def test_fuel_properties_present_and_forced_to_float():
    """The fuel's properties are authored in world.yaml beside the grade and
    reach every consumer as float."""
    w = World.model_validate(load_world())
    assert isinstance(w.process.density_kg_per_l, float)
    assert isinstance(w.process.sulphur_pct_mm, float)
    assert isinstance(w.process.flashpoint_c, float)


def test_vessel_carries_its_imo_number():
    """The bunker delivery note names the receiving ship by name and IMO number
    (MARPOL Annex VI Appendix V item 1)."""
    w = World.model_validate(load_world())
    assert all(v.imo_number for v in w.roster.vessels)


def test_each_tug_carries_its_mean_time_away():
    """Each tug's mean time away is authored in world.yaml in whole seconds
    (deployment-config.md §3); the sim draws that tug's times away from it
    (sim.md §6)."""
    w = World.model_validate(load_world())
    assert all(isinstance(v.mean_time_away_s, int) for v in w.roster.vessels)


def test_tug_arrival_rate_is_withdrawn():
    """Each tug's mean time away replaces the roster's arrival rate (sim.md §6):
    world.yaml no longer carries `roster.lambda_tug`, and the schema refuses it
    as an unknown key."""
    d = copy.deepcopy(load_world())
    assert "lambda_tug" not in d["roster"]
    d["roster"]["lambda_tug"] = 1.0
    with pytest.raises(ValidationError) as excinfo:
        World.model_validate(d)
    assert [
        e["type"]
        for e in excinfo.value.errors()
        if tuple(e["loc"]) == ("roster", "lambda_tug")
    ] == ["extra_forbidden"]


def _process(field, value):
    return lambda d: d["process"].__setitem__(field, value)


def _vessel(field, value):
    return lambda d: d["roster"]["vessels"][0].__setitem__(field, value)


def _audit(**fields):
    return lambda d: d["audit"].update(fields)


def test_audit_cadence_is_authored():
    """The reconciliation audit's period and settling delay are authored in
    world.yaml (deployment-config.md §3): a period name and whole seconds."""
    w = World.model_validate(load_world())
    assert w.audit.period in {"month", "week", "day"}
    assert isinstance(w.audit.settling_delay_s, int)


@pytest.mark.parametrize(
    "period, settling_delay_s",
    [("day", 86_399), ("week", 7 * 86_400 - 1), ("month", 28 * 86_400 - 1)],
)
def test_a_settling_delay_just_short_of_its_shortest_period_is_accepted(
    period, settling_delay_s
):
    d = copy.deepcopy(load_world())
    _audit(period=period, settling_delay_s=settling_delay_s)(d)
    World.model_validate(d)


def _drop(section, field):
    def mutate(d):
        target = d["roster"]["vessels"][0] if section == "vessel" else d[section]
        target.pop(field, None)

    return mutate


# name -> (mutation, location the rejection must be reported at)
FIELD_CASES = {
    "density_non_positive": (
        _process("density_kg_per_l", 0),
        ("process", "density_kg_per_l"),
    ),
    "density_missing": (
        _drop("process", "density_kg_per_l"),
        ("process", "density_kg_per_l"),
    ),
    "sulphur_non_positive": (
        _process("sulphur_pct_mm", 0),
        ("process", "sulphur_pct_mm"),
    ),
    # 0.50 % m/m is the regulation 14.1 limit the bunker delivery note declares
    # against; a fuel above it could not truthfully carry that declaration.
    "sulphur_above_regulation_14_1": (
        _process("sulphur_pct_mm", 0.51),
        ("process", "sulphur_pct_mm"),
    ),
    "sulphur_missing": (
        _drop("process", "sulphur_pct_mm"),
        ("process", "sulphur_pct_mm"),
    ),
    # 60 °C is the ISO 8217 minimum flashpoint for distillate marine fuels.
    "flashpoint_below_iso_8217": (
        _process("flashpoint_c", 59.9),
        ("process", "flashpoint_c"),
    ),
    "flashpoint_missing": (
        _drop("process", "flashpoint_c"),
        ("process", "flashpoint_c"),
    ),
    # The check digit and uniqueness cases are in INVALID_CASES; these name the
    # field the rejection must come from.
    "imo_six_digits": (
        _vessel("imo_number", 199999),
        ("roster", "vessels", 0, "imo_number"),
    ),
    "imo_missing": (
        _drop("vessel", "imo_number"),
        ("roster", "vessels", 0, "imo_number"),
    ),
    # Each tug's mean time away is a positive whole number of seconds
    # (deployment-config.md §5, "Range"): with a mean of zero the tug would
    # arrive again the instant it left.
    "mean_time_away_zero": (
        _vessel("mean_time_away_s", 0),
        ("roster", "vessels", 0, "mean_time_away_s"),
    ),
    "mean_time_away_negative": (
        _vessel("mean_time_away_s", -72000),
        ("roster", "vessels", 0, "mean_time_away_s"),
    ),
    "mean_time_away_fractional": (
        _vessel("mean_time_away_s", 72000.5),
        ("roster", "vessels", 0, "mean_time_away_s"),
    ),
    "mean_time_away_missing": (
        _drop("vessel", "mean_time_away_s"),
        ("roster", "vessels", 0, "mean_time_away_s"),
    ),
    # The audit's cadence (deployment-config.md §5, "Audit cadence"): one of the
    # three periods the port sets, and a delay shorter than the shortest period
    # of its kind — 28, 7 or 1 days — or each audit would start after the
    # following window had ended.
    "audit_period_unknown": (
        _audit(period="fortnight"),
        ("audit", "period"),
    ),
    "audit_period_missing": (
        _drop("audit", "period"),
        ("audit", "period"),
    ),
    "settling_delay_non_positive": (
        _audit(settling_delay_s=0),
        ("audit", "settling_delay_s"),
    ),
    "settling_delay_missing": (
        _drop("audit", "settling_delay_s"),
        ("audit", "settling_delay_s"),
    ),
    "settling_delay_of_a_day_on_a_day_period": (
        _audit(period="day", settling_delay_s=86_400),
        ("audit",),
    ),
    "settling_delay_of_a_week_on_a_week_period": (
        _audit(period="week", settling_delay_s=7 * 86_400),
        ("audit",),
    ),
    "settling_delay_of_28_days_on_a_month_period": (
        _audit(period="month", settling_delay_s=28 * 86_400),
        ("audit",),
    ),
}


def _errors_at(d: dict, loc: tuple) -> list[dict]:
    """Validate ``d``, which must fail, and return the errors reported at
    ``loc``. Refuses an ``extra_forbidden`` error as the reason, which would
    mean the schema does not declare the field rather than that its own check
    rejected the value."""
    with pytest.raises(ValidationError) as excinfo:
        World.model_validate(d)
    at_loc = [e for e in excinfo.value.errors() if tuple(e["loc"]) == loc]
    assert at_loc, f"no error reported at {loc}: {excinfo.value.errors()}"
    assert all(e["type"] != "extra_forbidden" for e in at_loc), (
        f"{loc} was rejected as an unknown key, not by its own check — the "
        f"schema does not declare the field"
    )
    return at_loc


@pytest.mark.parametrize("name", sorted(FIELD_CASES))
def test_schema_rejects_invalid_fuel_or_vessel_field(name):
    mutate, loc = FIELD_CASES[name]
    d = copy.deepcopy(load_world())
    mutate(d)
    _errors_at(d, loc)


# ── Scenario values lifted from the sim (deployment-config.md §3, §5) ─────────
#
# The dispenser's flow rate, the overfill interlock level, the fuel supplier's
# ordering values and the refuel range move from literals in the sim into
# world.yaml. As for the fuel properties above, each rejection names the
# location it must come from. The helpers create a missing section, so that
# before the schema declares these values a case fails on its assertion rather
# than on a KeyError.


def _set(section, field, value):
    return lambda d: d.setdefault(section, {}).__setitem__(field, value)


def _remove(section, field):
    return lambda d: d.get(section, {}).pop(field, None)


# Each lifted value and the type the sim reads it as: the volumes, rates and
# percentages as f64, the two durations as whole seconds.
LIFTED_VALUES = {
    ("process", "overfill_interlock_pct"): float,
    ("process", "dispenser_flow_rate_l_per_min"): float,
    ("fuel_supplier", "reorder_threshold_l"): float,
    ("fuel_supplier", "delivery_volume_l"): float,
    ("fuel_supplier", "lead_time_s"): int,
    ("fuel_supplier", "tanker_pump_rate_l_per_min"): float,
    ("fuel_supplier", "poll_interval_s"): int,
    ("roster", "refuel_volume_min_l"): float,
    ("roster", "refuel_volume_max_l"): float,
}


def test_lifted_values_forced_to_the_types_the_sim_reads():
    """world.yaml authors these as bare integers; each reaches the sim as the
    type it reads, so the volumes, rates and percentages must be float and the
    durations int."""
    w = World.model_validate(load_world())
    for (section, field), kind in LIFTED_VALUES.items():
        value = getattr(getattr(w, section), field)
        assert type(value) is kind, f"{section}.{field} is {type(value).__name__}"


# name -> (mutation, location the rejection must be reported at). Each lifted
# value is positive and finite on its own (deployment-config.md §5, "Range"):
# zero, infinity and absence are each rejected at the value itself. An int
# field cannot hold infinity, so only the floats have an infinite case.
SCENARIO_FIELD_CASES = {}
for (_section, _field), _kind in LIFTED_VALUES.items():
    SCENARIO_FIELD_CASES[f"{_field}_zero"] = (
        _set(_section, _field, 0),
        (_section, _field),
    )
    SCENARIO_FIELD_CASES[f"{_field}_missing"] = (
        _remove(_section, _field),
        (_section, _field),
    )
    if _kind is float:
        SCENARIO_FIELD_CASES[f"{_field}_infinite"] = (
            _set(_section, _field, float("inf")),
            (_section, _field),
        )
SCENARIO_FIELD_CASES.update(
    {
        "fuel_supplier_missing": (
            lambda d: d.pop("fuel_supplier", None),
            ("fuel_supplier",),
        ),
        # The cavitation level is strictly above zero (deployment-config.md §5;
        # its former range admitted 0, which leaves no level at which the FMS
        # refuses a dispense).
        "cavitation_threshold_zero": (
            _set("process", "cavitation_threshold_pct", 0),
            ("process", "cavitation_threshold_pct"),
        ),
        "cavitation_threshold_infinite": (
            _set("process", "cavitation_threshold_pct", float("inf")),
            ("process", "cavitation_threshold_pct"),
        ),
    }
)


@pytest.mark.parametrize("name", sorted(SCENARIO_FIELD_CASES))
def test_schema_rejects_an_invalid_lifted_value(name):
    mutate, loc = SCENARIO_FIELD_CASES[name]
    d = copy.deepcopy(load_world())
    mutate(d)
    _errors_at(d, loc)


# ── The scenario values fit together (deployment-config.md §5) ────────────────
#
# With C the tank's capacity, c the cavitation level and o the overfill
# interlock level:
#   0 < cavitation_threshold_pct < overfill_interlock_pct < 100
#   c < reorder_threshold_l < C
#   reorder_threshold_l + delivery_volume_l <= o
#   refuel_volume_min_l < refuel_volume_max_l <= 6553.5
# Each case breaks one bound by the smallest step from world.yaml's own values,
# so that the case keeps testing its bound when the defaults change. Each names
# the location of the check and the fields its message must name, so that the
# check that fires is the one the case is for.


def _level(d: dict, pct_field: str) -> float:
    return d["process"]["tank_capacity_l"] * d["process"][pct_field] / 100


def _cavitation_level(d: dict) -> float:
    return _level(d, "cavitation_threshold_pct")


def _interlock_level(d: dict) -> float:
    return _level(d, "overfill_interlock_pct")


# The largest preset the dispenser's 16-bit register holds at 0.1 L resolution
# (fms.md §7.4).
LARGEST_PRESET_L = 6553.5

# name -> (mutation, location, fields the error message must name)
CONSISTENCY_CASES = {
    "cavitation_level_at_the_interlock_level": (
        lambda d: d["process"].__setitem__(
            "cavitation_threshold_pct", d["process"]["overfill_interlock_pct"]
        ),
        ("process",),
        ("cavitation_threshold_pct", "overfill_interlock_pct"),
    ),
    "cavitation_level_at_100": (
        _set("process", "cavitation_threshold_pct", 100),
        ("process",),
        ("cavitation_threshold_pct",),
    ),
    "interlock_level_at_100": (
        _set("process", "overfill_interlock_pct", 100),
        ("process",),
        ("overfill_interlock_pct",),
    ),
    "reorder_threshold_at_the_cavitation_level": (
        lambda d: d["fuel_supplier"].__setitem__(
            "reorder_threshold_l", _cavitation_level(d)
        ),
        (),
        ("reorder_threshold_l", "cavitation_threshold_pct"),
    ),
    "reorder_threshold_at_capacity": (
        lambda d: d["fuel_supplier"].__setitem__(
            "reorder_threshold_l", d["process"]["tank_capacity_l"]
        ),
        (),
        ("reorder_threshold_l", "tank_capacity_l"),
    ),
    "delivery_past_the_interlock_level": (
        lambda d: d["fuel_supplier"].__setitem__(
            "delivery_volume_l",
            _interlock_level(d) - d["fuel_supplier"]["reorder_threshold_l"] + 1,
        ),
        (),
        ("delivery_volume_l", "overfill_interlock_pct"),
    ),
    "refuel_range_empty": (
        lambda d: d["roster"].__setitem__(
            "refuel_volume_min_l", d["roster"]["refuel_volume_max_l"]
        ),
        ("roster",),
        ("refuel_volume_min_l", "refuel_volume_max_l"),
    ),
    "refuel_maximum_above_the_largest_preset": (
        _set("roster", "refuel_volume_max_l", LARGEST_PRESET_L + 0.1),
        ("roster",),
        ("refuel_volume_max_l",),
    ),
}


@pytest.mark.parametrize("name", sorted(CONSISTENCY_CASES))
def test_schema_rejects_values_that_do_not_fit_together(name):
    mutate, loc, fields = CONSISTENCY_CASES[name]
    d = copy.deepcopy(load_world())
    mutate(d)
    messages = " ".join(e["msg"] for e in _errors_at(d, loc))
    missing = [f for f in fields if f not in messages]
    assert not missing, f"the error at {loc} does not name {missing}: {messages}"


def test_values_at_their_consistency_bounds_are_accepted():
    """The two bounds that admit equality: a delivery arriving at the reorder
    threshold may fill the tank exactly to the interlock level, and a tug may
    request the largest preset the dispenser holds."""
    d = copy.deepcopy(load_world())
    d["fuel_supplier"]["delivery_volume_l"] = (
        _interlock_level(d) - d["fuel_supplier"]["reorder_threshold_l"]
    )
    d["roster"]["refuel_volume_max_l"] = LARGEST_PRESET_L
    World.model_validate(d)
