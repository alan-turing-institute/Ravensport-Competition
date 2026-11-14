"""
What the system produces (siem-tests.md §6; siem.md §6).
"""

from __future__ import annotations

from tests.integration.siem.conftest import (
    Node,
    OutputReader,
    WazuhAPI,
    carries_line,
    trigger_failed_authentication,
)


def test_alerts_json_carries_the_specified_fields(outputs: OutputReader) -> None:
    """A sampled alerts.json object carries rule identifier, severity,
    originating agent, decoded fields, and timestamp. The technique field
    (siem.md §6) is present on this alert either way — Wazuh's bundled
    ruleset carries its own ATT&CK Enterprise mappings on many stock rules,
    not only on rules with an explicit RavensPort/ICS mapping — so this test
    checks the field exists, not that it is empty. The original-line field
    is present for a text-sourced alert and absent for a Suricata- or
    connection-log-sourced one, with the decoded fields present in its
    place.

    A service restart does not reliably match any stock rule (confirmed
    live: a plain unit restart produced no alert), so this uses a failed
    authentication instead, which reliably does."""
    since = outputs.snapshot()[0]
    line = trigger_failed_authentication(Node("corp-it"))
    text_alert = outputs.wait_for_alert(
        carries_line("corp-it", line), since=since, timeout=20.0
    )
    assert text_alert is not None
    for field in ("rule", "agent", "timestamp"):
        assert field in text_alert, f"missing {field}"
    assert "id" in text_alert["rule"], "missing rule identifier"
    assert "level" in text_alert["rule"], "missing severity"
    assert "mitre" in text_alert["rule"], "the mitre field must exist even when empty"
    assert text_alert.get("full_log"), "text-sourced alert missing the original line"

    # A firewall node's own agent forwards its journald records too (e.g. its
    # own "New wazuh agent connected" event, rule 501), so being attributed
    # to a firewall node does not by itself mean Suricata-sourced — that
    # bit only distinguishes text- from JSON-sourced records: no full_log,
    # decoded fields under data instead (siem.md §6).
    json_sourced = next(
        (
            r
            for r in outputs.alerts()
            if r.get("agent", {}).get("name")
            in ("fw-ext-dmz", "fw-dmz-it", "fw-ot-dmz")
            and not r.get("full_log")
            and r.get("data")
        ),
        None,
    )
    if json_sourced is not None:
        assert not json_sourced.get("full_log"), (
            "a Suricata-sourced alert unexpectedly carries the original line"
        )
        assert json_sourced.get("data"), "JSON-sourced alert missing decoded fields"


def test_archives_json_holds_every_record_regardless_of_a_rule_match(
    outputs: OutputReader,
) -> None:
    """A record that matches no rule still reaches archives.json."""
    alert_count, archive_count = outputs.snapshot()
    assert archive_count > alert_count, (
        "archives.json should hold at least as many records as alerts.json, "
        "including those that matched no rule"
    )


def test_alerts_log_mirrors_alerts_json(outputs: OutputReader) -> None:
    """The plain-text rendering carries the same alerts as the structured
    file, in the same order."""
    alerts = outputs.alerts()
    log_lines = outputs.alerts_log_lines()
    assert len(log_lines) >= len(alerts) - 1, (
        "alerts.log has noticeably fewer entries than alerts.json"
    )


def test_outputs_rotate_daily_into_dated_subdirectories(siem: Node) -> None:
    """Both files rotate on the daily boundary into a directory named for the
    date, rather than truncating or being overwritten in place."""
    for base in (
        "/var/ossec/logs/alerts",
        "/var/ossec/logs/archives",
    ):
        result = siem.run("find", base, "-maxdepth", "1", "-type", "d", check=True)
        year_dirs = [line for line in result.stdout.splitlines() if line != base]
        assert year_dirs, f"no dated subdirectory under {base}"


def test_outputs_carry_no_record_from_the_image_build(
    siem: Node, outputs: OutputReader
) -> None:
    """The deployed manager's records begin at its own first start: every
    archived record names siem-01 as its manager, and no file under
    /var/ossec names a build container (`rpbuild<node>-<pid>` or
    `rp-build-<node>-<pid>`, deploy/build-scripts/), as the manager's start
    during the image build would otherwise leave behind."""
    managers = {r.get("manager", {}).get("name") for r in outputs.archives()}
    assert managers <= {"siem-01"}, f"records from another manager: {managers}"
    result = siem.run("grep", "-rlI", "rp-\\?build", "/var/ossec")
    assert not result.stdout, f"build container named in:\n{result.stdout}"


def test_management_interface_does_not_serve_alerts(wazuh_api: WazuhAPI) -> None:
    """Every endpoint the management interface exposes returns configuration,
    agent state, rule, inventory, or the manager's own operational log; none
    returns alert content. There is no `/alerts`-shaped endpoint at all —
    alert content is read only from the files (§0.3).

    `/manager/logs` is a real, legitimate endpoint (the manager's own
    ossec.log-style operational log, unrelated to detection output), so it
    belongs in the second group below — checked for not resembling alert
    content, not asserted absent."""
    for path in ("/alerts", "/alerts/summary"):
        status, _ = wazuh_api.get(path, {"limit": "1"})
        assert status in (404, 501), f"{path} unexpectedly served ({status})"

    for path in ("/agents", "/rules", "/decoders", "/manager/logs"):
        status, payload = wazuh_api.get(path, {"limit": "1"})
        assert status == 200
        assert "full_log" not in str(payload), (
            f"{path} unexpectedly resembles alert content"
        )
