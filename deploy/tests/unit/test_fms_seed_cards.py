"""
FMS seed-card projection tests.

world.yaml is the single source for the RFID card numbers. The FMS validates
two card classes against its card_table — the operator card at login and the
vessel card at the job RFID gate — and on a fresh deploy that table is empty, so
render.py projects the cards into FMS_SEED_CARDS and the FMS enrols them at
startup. These tests guard that projection: the value must carry exactly the
operator and vessel cards from world.yaml, labelled by operator username / vessel
name, and a label that would collide with the entry delimiter must be rejected.

Run with: pytest tests/unit -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]  # deploy/
sys.path.insert(0, str(ROOT / "scripts"))
from render import fms_seed_cards  # noqa: E402
from world_schema import World  # noqa: E402


def world_model() -> World:
    return World.model_validate(yaml.safe_load((ROOT / "world.yaml").read_text()))


def test_seed_cards_cover_every_operator_and_vessel_card():
    """The projection must carry exactly the operator + vessel cards from
    world.yaml, each as number:label with the operator username / vessel IMO
    number."""
    world = world_model()
    expected = [f"{op.rfid_card}:{op.username}" for op in world.identities.operators]
    expected += [f"{v.rfid_card}:{v.imo_number}" for v in world.roster.vessels]
    assert fms_seed_cards(world) == ",".join(expected)


def test_seed_cards_reject_comma_in_label():
    """A comma in a label would be mis-split as an entry boundary, so it must
    abort rendering rather than emit a value the FMS parses into wrong cards."""
    world = world_model()
    bad_operator = world.identities.operators[0].model_copy(
        update={"username": "foo,bar"}
    )
    bad_identities = world.identities.model_copy(update={"operators": [bad_operator]})
    with pytest.raises(ValueError, match="comma"):
        fms_seed_cards(world.model_copy(update={"identities": bad_identities}))
