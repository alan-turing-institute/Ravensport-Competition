"""
Declared schema for world.yaml.

`World` is a pydantic model declaring the structure, types, and required fields
of world.yaml, with range and format checks expressed as field constraints and
model validators. Parsing world.yaml through ``World.model_validate`` performs
the validation and fails before any projection is written.

Two properties matter here:

  * Strictness — the ``_Strict`` base sets ``extra="forbid"``, so a mistyped or
    stray key in world.yaml fails loudly rather than being silently dropped.
  * Forced types — the declared ``float``/``int`` field types coerce each
    world.yaml value to a single canonical form that every consumer projection
    inherits (the FMS ``env_f64`` constants stay float; the intervals stay int),
    so a value cannot diverge in type between the consumers that share it.

Imported by render.py; it does not itself read or write any file.

Alongside the schema this module declares ``PROJECTION_MAP`` — the table mapping
each projected numeric world fact to its destination key in every consumer
artefact. It is
declared here once and read by render.py's mapping-integrity check
(``check_projection_map``), so the source-to-destination mapping and the value's
canonical type cannot drift between the projections that share a fact. The
per-node least-knowledge allow-list that also lives in this module on the 162e
branch remains deferred: it belongs with the least-knowledge check, which is not
wired on this branch yet.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    field_validator,
    model_validator,
)

# Fuel grades defined in narrative.md (§1 and the acronym list). The grade
# referential-integrity check validates against this declared set; extend it
# only when narrative.md adds a grade.
NARRATIVE_FUEL_GRADES: frozenset[str] = frozenset({"MGO"})

# 16-byte value expressed as 32 hexadecimal characters.
_SALT_SEED_RE = re.compile(r"\A[0-9a-fA-F]{32}\Z")

# The bunker delivery note declares the fuel's sulphur content against the
# MARPOL Annex VI regulation 14.1 limit (sim.md §8.2); a fuel above it could not
# truthfully carry that declaration.
REGULATION_14_1_SULPHUR_LIMIT_PCT_MM = 0.50

# ISO 8217 minimum flashpoint for distillate marine fuels.
ISO_8217_MIN_FLASHPOINT_C = 60.0

# The largest preset the dispenser's 16-bit register holds at 0.1 L resolution
# (fms.md §7.4); the FMS refuses a larger preset.
DISPENSER_LARGEST_PRESET_L = 6553.5


class _Strict(BaseModel):
    """Base model forbidding unknown keys, so a mistyped field fails loudly."""

    # This guards world.yaml *input* keys only. It does not catch a renamed
    # *output* key in a rendered artefact (e.g. FMS_ADMIN_PASSWORD_HASH in
    # fms.env) — the schema never sees output keys. That class of error is
    # covered by the env-contract test (tests/unit/test_fms_env_contract.py),
    # which derives the required key set from the consumer's own loader.
    model_config = ConfigDict(extra="forbid")


class Admin(_Strict):
    username: str = Field(min_length=1)
    password: str = Field(min_length=1)

    @field_validator("password")
    @classmethod
    def _password_is_typable(cls, value: str) -> str:
        """The value is also typed at admin-01's local SSH login."""
        untypable = sorted({c for c in value if not " " <= c <= "~"})
        if untypable:
            raise ValueError(
                f"admin.password contains {untypable}, outside printable ASCII: "
                "the systems administrator must be able to type the reused "
                "password at admin-01 (deployment-config.md §5)"
            )
        return value


class LdapAdmin(_Strict):
    # The directory's (corp-it) slapd rootDN and its password. The rootDN binds
    # with write over the whole directory tree; render.py projects the password
    # into the slapd install (debconf) and the LDIF-seeding bind in
    # corp-it.sh.j2. The dn's suffix is ldap.base_dn by directory convention, so
    # it is stored whole as the authoritative manager distinguished name rather
    # than reconstructed by each consumer.
    dn: str = Field(min_length=1)
    password: str = Field(min_length=1)


class Operator(_Strict):
    username: str = Field(min_length=1)
    password: str = Field(min_length=1)
    rfid_card: int = Field(gt=0)


class HistAdmin(_Strict):
    username: str = Field(min_length=1)
    password: str = Field(min_length=1)


class SiemAdmin(_Strict):
    username: str = Field(min_length=1)
    password: str = Field(min_length=1)


class ReconciliationUser(_Strict):
    # Two credentials, held by one person against two systems with two user
    # stores (recon.md §7.1): `password` authenticates to hist-dmz-01's Grafana,
    # `workstation_password` signs in to the workstation — validated against the
    # directory on corp-it, not against recon-01, which holds no verifier.
    username: str = Field(min_length=1)
    password: str = Field(min_length=1)
    workstation_password: str = Field(min_length=1)

    @model_validator(mode="after")
    def _credentials_are_distinct(self) -> ReconciliationUser:
        """Reject one value serving both systems.

        recon.md §7.1 requires that compromising the workstation does not yield
        the log server, and vice versa. Reuse is checked here rather than left
        to review because it is undetectable in a deployed environment: both
        logins go on working, and the two hashes differ anyway, since each is
        salted for its own purpose.
        """
        if self.password == self.workstation_password:
            raise ValueError(
                "reconciliation_user.workstation_password must differ from "
                "reconciliation_user.password: the directory login on corp-it "
                "and the hist-dmz-01 Grafana login are separate systems "
                "(recon.md §7.1)"
            )
        return self

    @model_validator(mode="after")
    def _credentials_are_typable(self) -> ReconciliationUser:
        """Reject a credential the reconciliation user cannot type
        (deployment-config.md §5).

        The username and Grafana password are typed into a terminal as
        `username:password` inside the single-quoted `-u` argument of the audit
        command, through a scancode table covering printable ASCII. A single
        quote ends the argument, and a colon in the username moves the boundary
        Basic authentication reads between the two. The workstation password is
        sent in the remote-desktop logon instead, and is held to printable ASCII
        only.
        """
        for field, refused in (
            ("username", "':"),
            ("password", "'"),
            ("workstation_password", ""),
        ):
            value = getattr(self, field)
            untypable = sorted({c for c in value if not " " <= c <= "~"})
            if untypable:
                raise ValueError(
                    f"reconciliation_user.{field} contains {untypable}, outside "
                    "printable ASCII: the reconciliation user's scancode table has "
                    "no key for them (deployment-config.md §5)"
                )
            present = sorted(set(value) & set(refused))
            if present:
                raise ValueError(
                    f"reconciliation_user.{field} contains {present}: it is typed "
                    "inside the single-quoted `-u` argument of the audit command as "
                    "`username:password` (deployment-config.md §5)"
                )
        return self


class SystemsAdministrator(_Strict):
    username: str = Field(min_length=1)


class Identities(_Strict):
    admin: Admin
    ldap_admin: LdapAdmin
    hist_admin: HistAdmin
    siem_admin: SiemAdmin
    reconciliation_user: ReconciliationUser
    systems_administrator: SystemsAdministrator
    operators: list[Operator] = Field(min_length=1)


class Vessel(_Strict):
    imo_number: int = Field(ge=1_000_000, le=9_999_999)
    name: str = Field(min_length=1)
    rfid_card: int = Field(gt=0)
    # The tug's mean time between leaving the dock and its next arrival, in
    # whole seconds; the generator draws the tug's times away from it (sim.md
    # §6). A mean of zero would bring the tug back the instant it left.
    mean_time_away_s: int = Field(gt=0)

    @field_validator("imo_number")
    @classmethod
    def _imo_check_digit(cls, v: int) -> int:
        # IMO Resolution A.1117(30): the seventh digit is the last digit of the
        # sum of the first six digits weighted 7, 6, 5, 4, 3, 2.
        digits = [int(d) for d in str(v)]
        weighted = sum(d * w for d, w in zip(digits[:6], range(7, 1, -1)))
        if weighted % 10 != digits[6]:
            raise ValueError(f"IMO number {v} fails its check digit")
        return v


class Roster(_Strict):
    # The bounds of each tug's requested volume, the same for every tug; the
    # generator draws the volume uniformly between them (sim.md §6).
    refuel_volume_min_l: float = Field(gt=0, allow_inf_nan=False)
    refuel_volume_max_l: float = Field(gt=0, allow_inf_nan=False)
    vessels: list[Vessel] = Field(min_length=1)

    @model_validator(mode="after")
    def _refuel_range(self) -> Roster:
        """The uniform draw needs a non-empty range, and a request above the
        largest preset the dispenser holds would be refused by the FMS
        (deployment-config.md §5)."""
        if not self.refuel_volume_min_l < self.refuel_volume_max_l:
            raise ValueError(
                f"roster.refuel_volume_min_l {self.refuel_volume_min_l} is not "
                f"below roster.refuel_volume_max_l {self.refuel_volume_max_l}: each "
                "tug's requested volume is drawn uniformly between them (sim.md §6)"
            )
        if self.refuel_volume_max_l > DISPENSER_LARGEST_PRESET_L:
            raise ValueError(
                f"roster.refuel_volume_max_l {self.refuel_volume_max_l} exceeds "
                f"{DISPENSER_LARGEST_PRESET_L} L, the largest preset the "
                "dispenser's register holds; the FMS would refuse a larger "
                "request (fms.md §7.4)"
            )
        return self


class Ldap(_Strict):
    base_dn: str = Field(min_length=1)
    people_ou: str = Field(min_length=1)


class FmsDatabase(_Strict):
    name: str = Field(min_length=1)
    role: str = Field(min_length=1)


class Database(_Strict):
    fms: FmsDatabase


class Process(_Strict):
    tank_capacity_l: float = Field(gt=0)
    # The FMS refuses a dispense at or below this level (fms.md §5.1). The
    # upper bounds of the two levels are checked together, in order, by
    # _levels_in_order.
    cavitation_threshold_pct: float = Field(gt=0, allow_inf_nan=False)
    # The level at which the hardwired overfill interlock opens (sim.md §1.3).
    overfill_interlock_pct: float = Field(gt=0, allow_inf_nan=False)
    # The tank's outflow while a dispense runs (sim.md §1.1).
    dispenser_flow_rate_l_per_min: float = Field(gt=0, allow_inf_nan=False)
    k_factor: float = Field(gt=0)
    grade: str
    # The fuel's properties, authored beside the grade so that a change of fuel
    # is a change to one file (deployment-config.md §3). Density is shared by
    # the sim and the FMS; sulphur and flashpoint are written only onto the
    # bunker delivery note.
    density_kg_per_l: float = Field(gt=0)
    sulphur_pct_mm: float = Field(gt=0, le=REGULATION_14_1_SULPHUR_LIMIT_PCT_MM)
    flashpoint_c: float = Field(ge=ISO_8217_MIN_FLASHPOINT_C)

    @field_validator("grade")
    @classmethod
    def _grade_in_narrative(cls, value: str) -> str:
        if value not in NARRATIVE_FUEL_GRADES:
            raise ValueError(
                f"grade {value!r} is not a fuel grade defined in narrative.md "
                f"(known: {sorted(NARRATIVE_FUEL_GRADES)})"
            )
        return value

    @model_validator(mode="after")
    def _levels_in_order(self) -> Process:
        """0 < cavitation_threshold_pct < overfill_interlock_pct < 100
        (deployment-config.md §5); the lower bound is each field's own."""
        if not self.cavitation_threshold_pct < self.overfill_interlock_pct:
            raise ValueError(
                f"process.cavitation_threshold_pct {self.cavitation_threshold_pct} "
                f"is not below process.overfill_interlock_pct "
                f"{self.overfill_interlock_pct}: the tank's working range lies "
                "between the two levels"
            )
        if not self.overfill_interlock_pct < 100:
            raise ValueError(
                f"process.overfill_interlock_pct {self.overfill_interlock_pct} is "
                "not below 100: the interlock must open before the tank is full"
            )
        return self


class FuelSupplier(_Strict):
    # The supplier's ordering (narrative.md §2.1; sim.md §5.3) and the road
    # tanker's pump rate, which is the tank's inflow rate (sim.md §1.1). The
    # tanker and its pump belong to the supplier.
    reorder_threshold_l: float = Field(gt=0, allow_inf_nan=False)
    delivery_volume_l: float = Field(gt=0, allow_inf_nan=False)
    lead_time_s: int = Field(gt=0)
    tanker_pump_rate_l_per_min: float = Field(gt=0, allow_inf_nan=False)
    poll_interval_s: int = Field(gt=0)


class Intervals(_Strict):
    # Every interval here is a value in operation, which a compressed scenario
    # divides by the compression factor (deployment-config.md §4).
    atg_poll_s: int = Field(gt=0)
    modbus_poll_s: int = Field(gt=0)
    session_timeout_s: int = Field(gt=0)
    challenge_timeout_s: int = Field(gt=0)
    admin_check_interval_h: int = Field(gt=0)
    dock_operator_poll_s: int = Field(gt=0)
    file_integrity_interval_h: int = Field(gt=0)
    inventory_interval_h: int = Field(gt=0)
    listening_port_interval_s: int = Field(gt=0)


class Audit(_Strict):
    # The reconciliation audit's period, set by the port (narrative.md §3), how
    # far into the following period each audit runs, and how far beyond each
    # end of the period the audit also retrieves transaction records
    # (deployment-config.md §5, "Audit cadence"). The margin is the delay's
    # lower bound; the sim's `spawn` also refuses a delay below it.
    period: Literal["month", "week", "day"]
    settling_delay_s: int = Field(gt=0)
    boundary_margin_s: int = Field(gt=0)

    @model_validator(mode="after")
    def _settling_delay_shorter_than_its_period(self) -> Audit:
        """A delay of one period or more would start each audit after the
        following window had ended, and the audits would fall behind; the
        bound is the shortest period of its kind."""
        shortest_days = {"month": 28, "week": 7, "day": 1}[self.period]
        if self.settling_delay_s >= shortest_days * 86_400:
            raise ValueError(
                f"audit.settling_delay_s {self.settling_delay_s} is not shorter than "
                f"the shortest {self.period} ({shortest_days} days)"
            )
        return self

    @model_validator(mode="after")
    def _settling_delay_at_least_the_margin(self) -> Audit:
        """A transfer commencing just before a window ends must have completed,
        and its paper been posted, before the audit drains, so the delay is at
        least the margin, which the longest credible transfer bounds."""
        if self.settling_delay_s < self.boundary_margin_s:
            raise ValueError(
                f"audit.settling_delay_s {self.settling_delay_s} is shorter than "
                f"audit.boundary_margin_s {self.boundary_margin_s}"
            )
        return self


class Crypto(_Strict):
    password_salt_seed: str
    # TLS/SSL start date parameter `notBefore` for every certificate the
    # internal CA signs (digital.md §2.7, §8 decision 7). Required because full
    # reproducibility of the CA (deployment.md §9.1) depends on a pinned date,
    # which must be updated at least every year as leaf certificates are valid
    # for 365 days. The ca_provisioning.py script rejects a value stale enough
    # that the resulting leaf certificates would already be expired.
    ca_not_before: date

    @field_validator("password_salt_seed")
    @classmethod
    def _valid_hex_seed(cls, value: str) -> str:
        if not _SALT_SEED_RE.match(value):
            raise ValueError(
                "password_salt_seed must be 32 hexadecimal characters (16 bytes)"
            )
        return value


# The compression factor of a compressed scenario (episodes.md §5.2, the
# compression factor) divides every interval and duration of the port's
# operation and multiplies every rate of fuel flow. A factor of 1 runs every
# value in operation. COMPRESSION_FACTOR is the factor of a render without an
# additional override file, or with one that sets none, so that such a render
# projects every value as world.yaml gives it (deployment.md §7.3, additional
# override file). The competition's additional override file defines the
# competition's factor. The factor is no fact about the fictional asset, so
# world.yaml does not hold it (episodes.md §17, parameters).
COMPRESSION_FACTOR = 1


class World(_Strict):
    """Root model for world.yaml. ``model_validate`` performs the checks. A
    validated world is at COMPRESSION_FACTOR, and ``at_compression_factor``
    gives it at another compression factor."""

    identities: Identities
    roster: Roster
    ldap: Ldap
    database: Database
    process: Process
    fuel_supplier: FuelSupplier
    intervals: Intervals
    audit: Audit
    crypto: Crypto

    # Set only through at_compression_factor: world.yaml cannot set a private
    # attribute.
    _compression_factor: int = PrivateAttr(default=COMPRESSION_FACTOR)

    @property
    def compression_factor(self) -> int:
        """The compression factor at which render.py projects this world."""
        return self._compression_factor

    def at_compression_factor(self, factor: int) -> World:
        """This world at the compression factor ``factor``. The fuel management
        system (FMS) and the human-machine interface (HMI) read their
        intervals, and the agents their schedules, as whole seconds of at least
        1 (episodes.md §5.2, the compression factor). The factor must therefore
        be at least 1 and must divide each exactly."""
        if factor < 1:
            raise ValueError(f"the compression factor {factor} is below 1")
        whole = {
            "intervals.atg_poll_s": self.intervals.atg_poll_s,
            "intervals.modbus_poll_s": self.intervals.modbus_poll_s,
            "intervals.session_timeout_s": self.intervals.session_timeout_s,
            "intervals.challenge_timeout_s": self.intervals.challenge_timeout_s,
            "intervals.file_integrity_interval_h": self.intervals.file_integrity_interval_h
            * 3600,
            "intervals.inventory_interval_h": self.intervals.inventory_interval_h
            * 3600,
            "intervals.listening_port_interval_s": self.intervals.listening_port_interval_s,
        }
        broken = [name for name, seconds in whole.items() if seconds % factor]
        if broken:
            raise ValueError(
                f"the compression factor {factor} does not divide "
                f"{', '.join(broken)} into whole seconds"
            )
        world = self.model_copy()
        world._compression_factor = factor
        return world

    @model_validator(mode="after")
    def _unique_cards_and_usernames(self) -> World:
        cards: list[int] = [op.rfid_card for op in self.identities.operators]
        cards += [v.rfid_card for v in self.roster.vessels]
        dup_cards = _duplicates(cards)
        if dup_cards:
            raise ValueError(f"duplicate RFID card numbers: {sorted(dup_cards)}")

        # The IMO number is the vessel card's label in the FMS card table, so
        # two vessels sharing one would make each other's tags interchangeable.
        dup_imo = _duplicates([v.imo_number for v in self.roster.vessels])
        if dup_imo:
            raise ValueError(f"duplicate vessel IMO numbers: {sorted(dup_imo)}")

        usernames = [self.identities.admin.username]
        usernames += [
            self.identities.hist_admin.username,
            self.identities.siem_admin.username,
            self.identities.reconciliation_user.username,
            self.identities.systems_administrator.username,
        ]
        usernames += [op.username for op in self.identities.operators]
        dup_names = _duplicates(usernames)
        if dup_names:
            raise ValueError(f"duplicate usernames: {sorted(dup_names)}")
        return self

    @model_validator(mode="after")
    def _supply_fits_the_tank(self) -> World:
        """c < reorder_threshold_l < C, and reorder_threshold_l +
        delivery_volume_l <= o, with C the tank's capacity and c and o the
        cavitation and overfill interlock levels (deployment-config.md §5). The
        process levels' own order is checked by Process."""
        capacity = self.process.tank_capacity_l
        cavitation_level = capacity * self.process.cavitation_threshold_pct / 100
        interlock_level = capacity * self.process.overfill_interlock_pct / 100
        reorder = self.fuel_supplier.reorder_threshold_l
        delivery = self.fuel_supplier.delivery_volume_l
        if not reorder > cavitation_level:
            raise ValueError(
                f"fuel_supplier.reorder_threshold_l {reorder} is not above the "
                f"cavitation level {cavitation_level} (process.tank_capacity_l × "
                "process.cavitation_threshold_pct / 100): the supplier must order "
                "before the tank reaches it"
            )
        if not reorder < capacity:
            raise ValueError(
                f"fuel_supplier.reorder_threshold_l {reorder} is not below "
                f"process.tank_capacity_l {capacity}"
            )
        if reorder + delivery > interlock_level:
            raise ValueError(
                f"fuel_supplier.reorder_threshold_l {reorder} + "
                f"fuel_supplier.delivery_volume_l {delivery} exceeds the overfill "
                f"interlock level {interlock_level} (process.tank_capacity_l × "
                "process.overfill_interlock_pct / 100): a delivery arriving with "
                "the tank at the reorder threshold would be stopped part-way by "
                "the interlock"
            )
        return self


def _duplicates(values: list) -> set:
    seen: set = set()
    dupes: set = set()
    for value in values:
        if value in seen:
            dupes.add(value)
        seen.add(value)
    return dupes


# ---------------------------------------------------------------------------
# Source-to-destination-key map. Keyed by the dotted path into world.yaml; each
# entry names the destination key in every consumer artefact that carries the
# fact. render.check_projection_map reads this table and asserts, per mapping,
# that the source resolves in the validated World and that the consumer artefact
# emits the destination key carrying the source's canonical typed value — so the
# mapping, and the forced float/int type, cannot drift between the projections
# that share a fact.
#
# Consumer artefact identifiers:
#   fms_env — build/env/fms-01.env      (FMS, read via env_f64 / env_u64)
#   hmi_env — build/env/hmi-01.env      (HMI)
#   sim     — sim-constants.toml content (in-memory; not read from a file)
#
# The sim destination names are the sim-constants.toml keys this branch actually
# emits, and are checked against that artefact. This diverges from the 162e
# branch, where the sim k_factor destination was the sim's Rust configuration
# field name (flow_meter_k_factor), checked against the sim source rather than a
# TOML — a check not wired on this branch.
#
# RFID card numbers, the LDAP/identity projections and the database names are
# structural projections (whole entries or rows), not single-key scalar value
# mappings, so they are handled by their own projection code, not this map.
# The roster is a list, emitted to the sim as numbered keys (deployment-config.md
# §4.1), so its fields are not single-key scalars either. The string values in
# the sim's slice — the grade, each vessel's name, and the credentials — are
# emitted quoted, which this map's unquoted comparison does not represent;
# tests/unit/test_sim_constants_agreement.py checks the roster and the strings by
# parsing the rendered TOML.
# ---------------------------------------------------------------------------
# How render.py scales each projected value in a compressed scenario
# (deployment-config.md §4): an interval or duration of the port's operation is
# divided by the compression factor, in whole seconds where its consumer reads
# whole seconds; a rate is multiplied by it; the audit period becomes a number
# of hours. A source absent here is projected unchanged.
PROJECTION_SCALING: dict[str, Literal["whole_seconds", "seconds", "rate", "hours"]] = {
    # The flow rates (sim.md §1.1) and the fuel supplier's poll interval and
    # lead time (sim.md §5.3). Each tug's mean time away is a roster entry, not
    # a single source, and build_sim_constants divides it alike.
    "process.dispenser_flow_rate_l_per_min": "rate",
    "fuel_supplier.tanker_pump_rate_l_per_min": "rate",
    "fuel_supplier.poll_interval_s": "seconds",
    "fuel_supplier.lead_time_s": "seconds",
    "intervals.atg_poll_s": "whole_seconds",
    "intervals.modbus_poll_s": "whole_seconds",
    "intervals.session_timeout_s": "whole_seconds",
    "intervals.challenge_timeout_s": "whole_seconds",
    "audit.period": "hours",
    "audit.settling_delay_s": "seconds",
}

PROJECTION_MAP: dict[str, dict[str, str]] = {
    # Deliberate credential reuse: the FMS/LDAP administrator's password is the
    # local systems-administrator password on admin-01 and is presented by the
    # sim actor. The FMS and LDAP projections are hashed and tested separately.
    "identities.admin.password": {"sim": "systems_administrator_password"},
    "process.tank_capacity_l": {
        "fms_env": "FMS_TANK_CAPACITY_L",
        "sim": "tank_capacity_l",
    },
    "process.cavitation_threshold_pct": {"fms_env": "FMS_CAVITATION_THRESHOLD_PCT"},
    "process.k_factor": {"fms_env": "FMS_K_FACTOR", "sim": "k_factor"},
    # The FMS refuses a job for any other grade (fms.md §7.4). The sim's copy
    # is emitted quoted and checked by test_sim_constants_agreement.py.
    "process.grade": {"fms_env": "FMS_GRADE"},
    "process.density_kg_per_l": {
        "fms_env": "FMS_DENSITY_KG_PER_L",
        "sim": "density_kg_per_l",
    },
    # Scenario parameters with a single consumer: written onto the bunker
    # delivery note by the sim only (deployment-config.md §3).
    "process.sulphur_pct_mm": {"sim": "sulphur_pct_mm"},
    "process.flashpoint_c": {"sim": "flashpoint_c"},
    # Scenario parameters lifted from the sim's literals and read only by the
    # sim, under the sim's own names where they differ (deployment-config.md
    # §4.1): the flow rates take sim.md §1.1's names, and the supplier's poll
    # interval a name unique across sim-constants.toml.
    "process.overfill_interlock_pct": {"sim": "overfill_interlock_pct"},
    "process.dispenser_flow_rate_l_per_min": {"sim": "outflow_rate_l_per_min"},
    "fuel_supplier.reorder_threshold_l": {"sim": "reorder_threshold_l"},
    "fuel_supplier.delivery_volume_l": {"sim": "delivery_volume_l"},
    "fuel_supplier.lead_time_s": {"sim": "lead_time_s"},
    "fuel_supplier.tanker_pump_rate_l_per_min": {"sim": "inflow_rate_l_per_min"},
    "fuel_supplier.poll_interval_s": {"sim": "supplier_poll_interval_s"},
    "roster.refuel_volume_min_l": {"sim": "refuel_volume_min_l"},
    "roster.refuel_volume_max_l": {"sim": "refuel_volume_max_l"},
    "intervals.atg_poll_s": {"fms_env": "FMS_ATG_POLL_INTERVAL"},
    "intervals.modbus_poll_s": {"fms_env": "FMS_MODBUS_POLL_INTERVAL"},
    # The FMS expires a session after it; the sim's reconciliation user
    # reaches back by it for the LOGIN that opened a session.
    "intervals.session_timeout_s": {
        "fms_env": "FMS_SESSION_TIMEOUT",
        "sim": "session_timeout_s",
    },
    "intervals.challenge_timeout_s": {"hmi_env": "HMI_CHALLENGE_TIMEOUT_SECS"},
    # The reconciliation audit's cadence: the port's procedure, read only by
    # the sim's reconciliation user.
    "audit.period": {"sim": "audit_period"},
    "audit.settling_delay_s": {"sim": "settling_delay_s"},
}
