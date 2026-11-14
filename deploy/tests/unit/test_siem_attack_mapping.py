"""
ATT&CK mapping tests for security monitoring (siem-tests.md §10), targeting
siem.md §2.2, §5 and §8.4.

The manager resolves a rule's technique only if it loaded the whole ATT&CK
matrix at its last start, and one imported tactic without a reference entry
stops that load. These assert the build inputs that prevent it: the import
writes tactic references, the build fails on a failed load, and each
RavensPort boundary rule maps the technique the siem.md §5 table names — agreeing
with the technique the Suricata signature itself declares — and the RavensPort
PostgreSQL failure rules map T1110.001 for one failure and T1110 for repeated
failures.

Run with: pytest tests/unit -q
"""

from __future__ import annotations

import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # deploy/

# The siem.md §5 table: the technique each RavensPort Suricata signature maps to.
_EXPECTED_TECHNIQUES = {
    "1000001": "T1692.001",  # set command to the tank gauge console
    "1000002": "T1692.001",  # disallowed function code
    "1000003": "T1571",  # non-Tank-Level-System traffic on 10001
    "1000007": "T1571",  # data outside a command on 10001
    "1000004": "T1571",  # non-TLS traffic on 443, 636, 8428, 9428
    "1000005": "T1571",  # non-NTP traffic on 123
    "1000008": "T1571",  # non-SSH traffic on 22
}


def _siem_build_script() -> str:
    out = subprocess.run(
        [sys.executable, "scripts/render.py", "--profile", "ci-minimal"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode == 0, out.stderr
    return (ROOT / "build" / "node-build" / "siem-01.sh").read_text()


def _boundary_rules(script: str) -> dict[str, list[str]]:
    """Map each Suricata signature identifier the RavensPort Wazuh rules
    select to the techniques the selecting rule carries."""
    block = re.search(r"<<'RULEEOF'\n(.*?)\nRULEEOF", script, re.DOTALL)
    assert block, "siem-01's build script has no RavensPort rule block"
    root = ET.fromstring(f"<root>{block.group(1)}</root>")
    mapped: dict[str, list[str]] = {}
    for rule in root.iter("rule"):
        field = rule.find("field[@name='alert.signature_id']")
        assert field is not None, f"rule {rule.get('id')} does not select on a sid"
        techniques = [t.text for t in rule.findall("mitre/id")]
        for sid in _EXPECTED_TECHNIQUES:
            if re.fullmatch(field.text, sid):
                mapped.setdefault(sid, []).extend(techniques)
    return mapped


def test_import_writes_a_reference_for_every_tactic():
    """siem.md §8.4: each imported tactic carries a reference entry, without
    which the manager's matrix load stops at that tactic."""
    script = _siem_build_script()
    assert "VALUES (?, 'mitre-attack', ?, ?, NULL, 'tactic')" in script, (
        "the ATT&CK for ICS import writes no tactic reference entries"
    )


def test_build_fails_if_the_matrix_does_not_load():
    """siem.md §8.4: the build checks the manager's first start for a failed
    matrix load rather than letting it pass unnoticed."""
    script = _siem_build_script()
    assert "Mitre matrix information could not be loaded" in script, (
        "siem-01's build script does not check for a failed ATT&CK matrix load"
    )


def test_each_boundary_signature_maps_its_technique():
    """siem.md §2.2, §5: each RavensPort boundary signature is selected by
    exactly one Wazuh rule, carrying exactly the technique named for it."""
    mapped = _boundary_rules(_siem_build_script())
    for sid, technique in _EXPECTED_TECHNIQUES.items():
        assert mapped.get(sid) == [technique], (
            f"sid {sid} maps to {mapped.get(sid)}, expected [{technique!r}]"
        )


def test_suricata_signatures_declare_the_same_technique():
    """The technique each Suricata signature declares in its own metadata
    agrees with the Wazuh rule that maps it, so an analyst reading either
    source sees the same classification. No signature declares T0855, which
    the pinned ICS matrix revokes in favour of T1692.001."""
    rules = (ROOT / "build-scripts" / "ravensport-boundary.rules").read_text()
    assert "T0855" not in rules
    for sid, technique in _EXPECTED_TECHNIQUES.items():
        line = next((ln for ln in rules.splitlines() if f"sid:{sid};" in ln), None)
        assert line is not None, f"no Suricata signature with sid {sid}"
        assert f"mitre_technique_id {technique}" in line, (
            f"sid {sid} does not declare {technique}"
        )


def test_postgresql_password_failure_maps_password_guessing():
    """siem.md §2.2, §3: a PostgreSQL password failure on fms-01 raises a
    RavensPort rule carrying T1110.001. The rule refines the packaged generic
    failure rule 2501, which is evaluated first, and selects only lines the
    RavensPort PostgreSQL decoder decoded."""
    script = _siem_build_script()
    block = re.search(r"<<'PGRULEEOF'\n(.*?)\nPGRULEEOF", script, re.DOTALL)
    assert block, "siem-01's build script has no RavensPort PostgreSQL rule block"
    root = ET.fromstring(f"<root>{block.group(1)}</root>")
    rules = {rule.get("id"): rule for rule in root.iter("rule")}
    single = rules.get("100110")
    assert single is not None, "no rule for a single PostgreSQL password failure"
    assert single.findtext("if_sid") == "2501"
    assert single.findtext("decoded_as") == "ravensport-postgresql"
    assert "password authentication failed" in single.findtext("match", "")
    assert [t.text for t in single.findall("mitre/id")] == ["T1110.001"]


def test_repeated_postgresql_password_failures_map_brute_force():
    """siem.md §2.2, §3: eight PostgreSQL password failures for one database
    user within 120 seconds raise a rule carrying T1110, with the threshold,
    window and quiet period of the packaged SSH brute-force rule 5763."""
    script = _siem_build_script()
    block = re.search(r"<<'PGRULEEOF'\n(.*?)\nPGRULEEOF", script, re.DOTALL)
    assert block, "siem-01's build script has no RavensPort PostgreSQL rule block"
    root = ET.fromstring(f"<root>{block.group(1)}</root>")
    rules = {rule.get("id"): rule for rule in root.iter("rule")}
    repeated = rules.get("100111")
    assert repeated is not None, "no rule for repeated PostgreSQL password failures"
    assert (
        repeated.get("frequency"),
        repeated.get("timeframe"),
        repeated.get("ignore"),
    ) == ("8", "120", "60")
    assert repeated.findtext("if_matched_sid") == "100110"
    assert repeated.find("same_srcuser") is not None, "not grouped by database user"
    assert [t.text for t in repeated.findall("mitre/id")] == ["T1110"]
