"""
Provisioned-dashboard unit tests (hist.md §7.1)

render.py emits two further artefacts for each log server that carries a
dashboard:

  <node>-dashboards.yaml    the file provider Grafana reads at start
  <node>-dashboard.json     the dashboard itself

A dashboard fails more quietly than the other Grafana artefacts. A panel whose
datasource uid does not resolve renders empty rather than erroring; a LogsQL
query naming a field that does not exist returns no rows rather than a message.
Neither appears in a build log, in journalctl, or in an HTTP status code — only
in a person noticing that a panel which should hold data does not. The
reconciliation user meets this dashboard once a month (narrative.md:161), which
is also how long such a fault would take to surface. Every assertion here exists
because the corresponding mistake would otherwise reach that audit undetected.

Run with: pytest tests/unit -q
"""

from __future__ import annotations

import functools
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]  # deploy/
sys.path.insert(0, str(ROOT / "scripts"))
import render  # noqa: E402

# The hosts that carry a provisioned dashboard (hist.md §7.1).
_DASHBOARD_NODES = ["hist-dmz-01", "hist-01"]


# ── The hist.md §4 vocabulary ─────────────────────────────────────────────────
#
# Transcribed by hand from hist.md §4.1 and §4.2. The specification lives outside
# this repository (Specs/host_specs/hist.md), so no test can read it: this
# transcription is the repository's copy of that vocabulary and updating it is
# part of any §4 edit. That is the guard rather than a weakness of it — a field
# renamed in §4 forces a change here, and a dashboard still naming the old field
# fails below instead of silently rendering an empty panel on the deployed
# instance.

_SECTION_4_1_METRICS = {
    "atg_tank_volume_litres",
    "atg_tank_temperature_celsius",
    "atg_alarm_active",
}

_SECTION_4_1_LABELS = {"tank_id", "alarm"}

# §4.2's common envelope, carried by every JSON-line record.
_SECTION_4_2_ENVELOPE = {
    "_time",
    "_msg",
    "event_type",
    "job_id",
    "type",
    "operator",
    "counterparty",
    "grade",
}

# §4.2's type-specific fields, across all four event types.
_SECTION_4_2_TYPE_SPECIFIC = {
    "atg_volume_at_open_l",
    "atg_volume_at_close_l",
    "created_at",
    "opened_at",
    "closed_at",
    "authorisation_id",
    "preset_volume_l",
    "transaction_id",
    "references_transaction_id",
    "start_totaliser_pulses",
    "end_totaliser_pulses",
    "volume_dispensed_l",
    "delivered_volume_l",
    "ticket_number",
    "supplier",
    "tanker_registration",
    "bdn_number",
}

_SECTION_4_2_FIELDS = _SECTION_4_2_ENVELOPE | _SECTION_4_2_TYPE_SPECIFIC

# The five values of §4.2's `event_type`.
_SECTION_4_2_EVENT_TYPES = {
    "job_opened",
    "transaction_open",
    "transaction_closed",
    "bdn_linked",
    "job_cancelled",
}

# Volumes in litres. §4.2's records are JSON-line, but VictoriaLogs stores every
# field as a string, so these arrive as "701.01" rather than 701.01 — confirmed
# against a real dispense on a live deployment of VictoriaLogs 1.52.0.
_LITRE_FIELDS = {
    "preset_volume_l",
    "volume_dispensed_l",
    "delivered_volume_l",
    "atg_volume_at_open_l",
    "atg_volume_at_close_l",
}

# ── The self-monitoring vocabulary (hist.md §6.1) ─────────────────────────────
#
# hist-01's operations dashboard draws on the log server's own telemetry rather
# than on §4's FMS-derived data model, so it needs a second vocabulary with a
# different provenance. These names are not specified by hist.md: they are what
# the four pinned products emit, so the authority is the products themselves.
#
# Read off all four /metrics endpoints of a live deployment running the versions
# §2.1 pins — VictoriaMetrics and vmagent 1.148.0, VictoriaLogs and vlagent
# 1.52.0 — and transcribed here.
#
# This guard compares the transcription against the dashboard's queries, so both
# sides are files in this repository. It catches a panel naming a series absent
# from the transcription. It cannot catch the transcription being wrong: were a
# product to rename a series at a later version, the panel and the transcription
# would be corrected together, agree with each other, and leave the panel
# rendering empty on the deployed host. Re-read the endpoints whenever a §2.1
# version moves; nothing automated will notice if that is skipped.
# tests/integration/hist/test_08_dashboard_queries.py makes the check that
# requires a running product.

_SELF_MONITORING_METRICS = {
    # Ingestion. `type` names the arrival path on all three.
    "vm_rows_inserted_total",  # victoria-metrics
    "vmagent_rows_inserted_total",  # vmagent
    "vl_rows_ingested_total",  # victoria-logs and vlagent
    # Replication, per remote-write target.
    "vmagent_remotewrite_pending_data_bytes",
    "vlagent_remotewrite_pending_data_bytes",
    "vmagent_remotewrite_blocks_sent_total",
    "vlagent_remotewrite_blocks_sent_total",
    # Store health. vm_app_uptime_seconds is exposed by all four components, so
    # the `job` label of §6.1's scrape configuration is what separates them.
    "vm_app_uptime_seconds",
    "vm_data_size_bytes",
    "vm_free_disk_space_bytes",
    "vl_data_size_bytes",
    "vl_free_disk_space_bytes",
}

_SELF_MONITORING_LABELS = {
    "job",  # set by the scrape configuration, one value per component
    "type",  # the ingestion path, on the rows-inserted/ingested counters
    "url",  # the remote-write target, on the replication counters
}

# vmagent 1.148.0 and vlagent 1.52.0 both mask the remote-write URL:
# `-remoteWrite.showURL` is false by default, confirmed against a live deployment,
# so the `url` label carries an ordinal rather than an address. The ordinal is the
# position of the `-remoteWrite.url` flag on the command line, which is what
# test_the_remote_write_ordinals_match_the_flag_order below pins against the
# build script.
_REMOTE_WRITE_ORDINALS = {1: "local store", 2: "hist-dmz-01 replica"}

# The metric vocabulary each dashboard is permitted to name.
_METRIC_VOCABULARY = {
    "hist-dmz-01": _SECTION_4_1_METRICS,
    "hist-01": _SELF_MONITORING_METRICS,
}

_LABEL_VOCABULARY = {
    "hist-dmz-01": _SECTION_4_1_LABELS,
    "hist-01": _SELF_MONITORING_LABELS,
}

_LOGS_PLUGIN_TYPE = "victoriametrics-logs-datasource"
_METRICS_PLUGIN_TYPE = "prometheus"

# The plugin's query-type values (src/types.ts, v0.30.1): instant, stats,
# statsRange, hits. "Raw Logs" in the query editor is `instant`, and it is the
# one that returns individual records rather than an aggregate — the shape a
# table of events needs.
_RAW_LOGS_QUERY_TYPE = "instant"


# ── Rendering ─────────────────────────────────────────────────────────────────


@functools.lru_cache(maxsize=None)
def _render(profile: str = "ci-minimal") -> Path:
    out = subprocess.run(
        [sys.executable, "scripts/render.py", "--profile", profile],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert out.returncode == 0, out.stderr
    return ROOT / "build"


def _artefact(node: str, suffix: str) -> str:
    build = _render()
    path = build / "grafana" / f"{node}-{suffix}"
    assert path.exists(), (
        f"{path.relative_to(ROOT)} was not rendered; hist.md §7.1 requires the "
        f"build script to push it into the {node} image"
    )
    return path.read_text()


def _dashboard(node: str) -> dict:
    return json.loads(_artefact(node, "dashboard.json"))


def _provider(node: str) -> dict:
    return yaml.safe_load(_artefact(node, "dashboards.yaml"))


def _targets(dashboard: dict):
    """Every (panel, target) pair in the dashboard."""
    for panel in dashboard["panels"]:
        for target in panel.get("targets", []):
            yield panel, target


# ── Identifier extraction ─────────────────────────────────────────────────────
#
# The drift guard reads the identifiers a query actually names rather than the
# constants the renderer was built from, so a typed-out expression that has
# drifted from the vocabulary is caught. Comparing the renderer's own constants
# against themselves would assert nothing.


def _promql_metric_names(expr: str) -> set[str]:
    """Metric names in a PromQL expression: bare identifiers, excluding function
    names (which are followed by an opening parenthesis) and label matchers."""
    without_selectors = re.sub(r"\{[^}]*\}", "", expr)
    return {
        m.group(1)
        for m in re.finditer(
            r"\b([a-zA-Z_:][a-zA-Z0-9_:]*)\b(?!\s*\()", without_selectors
        )
    }


def _promql_label_names(expr: str) -> set[str]:
    return {m.group(1) for m in re.finditer(r"([a-zA-Z_][a-zA-Z0-9_]*)\s*[=!]~?", expr)}


def _legend_label_names(legend_format: str) -> set[str]:
    return {m.group(1).strip() for m in re.finditer(r"\{\{([^}]+)\}\}", legend_format)}


def _logsql_field_names(expr: str) -> set[str]:
    """Field names a LogsQL expression names: the `field:value` filters, the
    `| fields a, b, c` projection, and the `| sort by (a, b)` keys."""
    names: set[str] = set()

    pipeline = expr.split("|")
    names |= {
        m.group(1) for m in re.finditer(r"([a-zA-Z_][a-zA-Z0-9_.]*)\s*:", pipeline[0])
    }
    for stage in pipeline[1:]:
        stage = stage.strip()
        if stage.startswith("fields "):
            names |= {
                f.strip() for f in stage[len("fields ") :].split(",") if f.strip()
            }
        elif stage.startswith("sort by"):
            inner = re.search(r"sort by\s*\(([^)]*)\)", stage)
            if inner:
                names |= {
                    f.strip().removesuffix(" desc").strip()
                    for f in inner.group(1).split(",")
                    if f.strip()
                }
    return names


def _logsql_event_type_values(expr: str) -> set[str]:
    """Values matched against `event_type`, excluding the any-value filter `*`."""
    return {
        m.group(1)
        for m in re.finditer(r"event_type\s*:\s*([a-zA-Z_][a-zA-Z0-9_]*)", expr)
    }


# ── The provider ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("node", _DASHBOARD_NODES)
def test_provider_is_a_file_provider_reading_the_directory_the_build_fills(node: str):
    """Grafana loads a provisioned dashboard only if a provider names the
    directory holding it. A provider naming a different directory leaves Grafana
    starting cleanly with no dashboard and no error — the state hist.md §7.1
    exists to end."""
    provider = _provider(node)
    assert provider["apiVersion"] == 1
    providers = provider["providers"]
    assert len(providers) == 1, f"{node}: expected one provider, got {len(providers)}"

    entry = providers[0]
    assert entry["type"] == "file"
    assert entry["orgId"] == 1, (
        f"{node}: the seeded account is a member of organisation 1 only "
        "(hist.md §7), so a dashboard provisioned into any other organisation "
        "would not be visible to it"
    )
    assert entry["options"]["path"] == render.GRAFANA_DASHBOARD_DIR


@pytest.mark.parametrize("node", _DASHBOARD_NODES)
def test_provider_leaves_the_dashboard_read_only_but_deletable_only_by_rebuild(
    node: str,
):
    """hist.md §7.1: file provisioning marks the dashboard read-only in the user
    interface, and the file is part of the image's input hash so the deployed
    view cannot drift from the repository. Both properties are this provider's
    settings, not defaults — Grafana's default allows interface updates."""
    entry = _provider(node)["providers"][0]
    assert entry["allowUiUpdates"] is False, (
        f"{node}: allowUiUpdates permits saving an edit over the provisioned "
        "dashboard from the browser, so the deployed view would no longer match "
        "the rendered file that the input hash covers"
    )
    assert entry["disableDeletion"] is True


# ── Datasource binding ────────────────────────────────────────────────────────


@pytest.mark.parametrize("node", _DASHBOARD_NODES)
def test_every_panel_binds_to_a_datasource_uid_this_host_provisions(node: str):
    """A panel naming a uid the host does not provision renders empty rather than
    failing (render.py's build_grafana_datasources sets out why the uids are
    pinned and why they differ per host). This is the assertion that makes the
    second of those guards effective: without it, the two hosts holding distinct
    uids would only mean the mistake is silent in a different way."""
    dashboard = _dashboard(node)
    permitted = set(render.DATASOURCE_UIDS[node].values())
    others = {
        uid
        for host, uids in render.DATASOURCE_UIDS.items()
        if host != node
        for uid in uids.values()
    }

    seen = set()
    for panel, target in _targets(dashboard):
        for source, where in (
            (panel.get("datasource"), "panel"),
            (target.get("datasource"), "target"),
        ):
            assert source, f"{node}: {where} {panel['title']!r} names no datasource"
            uid = source.get("uid")
            whose = " — it belongs to the other log server" if uid in others else ""
            assert uid in permitted, (
                f"{node}: {where} {panel['title']!r} binds to uid {uid!r}, which "
                f"this host does not provision{whose}. "
                f"Provisioned here: {sorted(permitted)}"
            )
            seen.add(uid)

    assert seen, f"{node}: no panel names a datasource at all"


@pytest.mark.parametrize("node", _DASHBOARD_NODES)
def test_panel_datasource_type_matches_the_store_behind_the_uid(node: str):
    """The uid selects the datasource; the type selects the plugin that builds
    the query. A panel naming the logs uid with the Prometheus type sends PromQL
    to VictoriaLogs, which answers with an error the panel shows as no data."""
    uids = render.DATASOURCE_UIDS[node]
    expected_type = {
        uids["VictoriaMetrics"]: _METRICS_PLUGIN_TYPE,
        uids["VictoriaLogs"]: _LOGS_PLUGIN_TYPE,
    }

    for panel, target in _targets(_dashboard(node)):
        for source in (panel.get("datasource"), target.get("datasource")):
            # An unprovisioned uid is the previous test's finding; reporting it
            # again here as a KeyError would bury that one.
            provisioned = expected_type.get(source["uid"])
            if provisioned is None:
                continue
            assert source["type"] == provisioned, (
                f"{node}: panel {panel['title']!r} names uid {source['uid']!r} "
                f"with type {source['type']!r}, but that uid is provisioned as "
                f"{provisioned!r}"
            )


# ── The drift guard ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("node", _DASHBOARD_NODES)
def test_metric_queries_name_only_series_and_labels_the_host_holds(node: str):
    """A PromQL query naming a metric VictoriaMetrics does not hold returns an
    empty result, not an error.

    The permitted vocabulary differs by host because the two dashboards draw on
    different data. `hist-dmz-01` shows §4.1's FMS-derived series; `hist-01` shows
    the log server's own telemetry from §6.1's scrape, whose names come from the
    products rather than from hist.md.
    """
    metrics = _METRIC_VOCABULARY[node]
    labels = _LABEL_VOCABULARY[node]

    for panel, target in _targets(_dashboard(node)):
        if target["datasource"]["type"] != _METRICS_PLUGIN_TYPE:
            continue
        expr = target["expr"]

        unknown = _promql_metric_names(expr) - metrics
        assert not unknown, (
            f"{node}: panel {panel['title']!r} queries {sorted(unknown)}, which "
            f"this host does not hold. Known series: {sorted(metrics)}"
        )

        unknown_labels = (
            _promql_label_names(expr)
            | _legend_label_names(target.get("legendFormat", ""))
        ) - labels
        assert not unknown_labels, (
            f"{node}: panel {panel['title']!r} names labels {sorted(unknown_labels)}, "
            f"which these series do not carry — a legend referring to one renders "
            f"the placeholder text literally"
        )


# ── hist-01: the masked remote-write target ───────────────────────────────────


def _remote_write_urls(node: str) -> list[str]:
    """The `-remoteWrite.url` values vmagent is given, in command-line order, read
    out of the rendered build script."""
    script = (_render() / "node-build" / f"{node}.sh").read_text()
    body = re.search(
        r"/etc/systemd/system/vmagent\.service.*?<<\s*'?([A-Za-z_]+)'?\s*\n(.*?)\n\1\s*\n",
        script,
        re.DOTALL,
    )
    assert body, f"{node}: no vmagent.service unit found in the rendered script"
    return re.findall(r"-remoteWrite\.url=(\S+)", body.group(2))


def test_the_remote_write_ordinals_match_the_flag_order():
    """The one assertion standing between the operations dashboard and a panel
    that is confidently mislabelled.

    Both agents mask the remote-write URL — `-remoteWrite.showURL` defaults to
    false — so `vmagent_remotewrite_pending_data_bytes` carries
    `url="1:secret-url"` rather than an address. The ordinal is the position of the
    flag on the command line, so the dashboard reads "1" as the local store and "2"
    as the replica. That mapping is an assumption about hist.md §6's flag order,
    and swapping those two lines would leave every replication panel reporting the
    local queue as the replica's and the replica's as local — with no empty panel,
    no error, and no visible symptom.

    The masking is left in place rather than lifted with `-remoteWrite.showURL`:
    vmagent's metrics port binds 0.0.0.0 for the FMS to push to, so unmasking
    would publish the replica's address to every host in the operational-technology
    zone. Naming the targets in the dashboard costs this test instead.
    """
    urls = _remote_write_urls("hist-01")
    assert len(urls) == len(_REMOTE_WRITE_ORDINALS), (
        f"hist-01: vmagent has {len(urls)} remote-write targets, but the dashboard "
        f"names {len(_REMOTE_WRITE_ORDINALS)}: {urls}"
    )
    assert "127.0.0.1" in urls[0], (
        f"hist-01: the first -remoteWrite.url is {urls[0]!r}, which the dashboard "
        f"labels {_REMOTE_WRITE_ORDINALS[1]!r}"
    )
    assert "hist-dmz-01" in urls[1], (
        f"hist-01: the second -remoteWrite.url is {urls[1]!r}, which the dashboard "
        f"labels {_REMOTE_WRITE_ORDINALS[2]!r}"
    )


def test_replication_panels_name_both_targets_by_the_role_each_serves():
    """A legend reading `1:secret-url` is what the masked label renders as by
    default, and it does not tell an administrator which queue is backing up. Every
    query selecting a `url` must therefore carry an explicit legend, and both
    targets must appear — a panel showing only the replica's queue would look
    complete."""
    labelled: set[str] = set()
    for panel, target in _targets(_dashboard("hist-01")):
        expr = target.get("expr", "")
        match = re.search(r'url\s*=\s*"(\d+):', expr)
        if not match:
            continue
        expected = _REMOTE_WRITE_ORDINALS[int(match.group(1))]
        assert target.get("legendFormat") == expected, (
            f"hist-01: panel {panel['title']!r} selects url ordinal "
            f"{match.group(1)} but its legend is "
            f"{target.get('legendFormat')!r}, not {expected!r}"
        )
        assert "secret-url" not in target["legendFormat"], (
            f"hist-01: panel {panel['title']!r} shows the masked label to the user"
        )
        labelled.add(expected)

    assert labelled == set(_REMOTE_WRITE_ORDINALS.values()), (
        f"hist-01: the replication panels name {sorted(labelled)}; §7.1 asks for "
        f"each of §6's two targets: {sorted(_REMOTE_WRITE_ORDINALS.values())}"
    )


@pytest.mark.parametrize("node", _DASHBOARD_NODES)
def test_log_queries_name_only_section_4_2_fields_and_event_types(node: str):
    """The failure this guards is total and silent: LogsQL filtering on a field
    that does not exist matches nothing, and a `fields` projection naming one
    produces an empty column. Either way the panel renders, with no rows or with
    a blank column, and nothing says why."""
    for panel, target in _targets(_dashboard(node)):
        if target["datasource"]["type"] != _LOGS_PLUGIN_TYPE:
            continue
        expr = target["expr"]

        unknown = _logsql_field_names(expr) - _SECTION_4_2_FIELDS
        assert not unknown, (
            f"{node}: panel {panel['title']!r} names fields {sorted(unknown)}, "
            f"which hist.md §4.2 does not define"
        )

        unknown_types = _logsql_event_type_values(expr) - _SECTION_4_2_EVENT_TYPES
        assert not unknown_types, (
            f"{node}: panel {panel['title']!r} matches event_type "
            f"{sorted(unknown_types)}, which §4.2 does not emit"
        )


@pytest.mark.parametrize("node", _DASHBOARD_NODES)
def test_the_events_table_excludes_the_syslog_records(node: str):
    """hist.md §7.1: authentication and card-table events (§4.3) reach both hosts
    but are not on this dashboard. Both classes share one VictoriaLogs instance,
    so a query that does not distinguish them returns both. `event_type:*` selects
    records carrying that field, which is exactly §4.2's set — §4.3's syslog
    records carry msg_id instead."""
    for panel, target in _targets(_dashboard(node)):
        if target["datasource"]["type"] != _LOGS_PLUGIN_TYPE:
            continue
        assert "event_type" in target["expr"], (
            f"{node}: panel {panel['title']!r} does not constrain event_type, so "
            f"it would also return the §4.3 syslog records that §7.1 keeps off "
            f"this dashboard"
        )
        for syslog_field in ("msg_id", "fms@99999", "severity"):
            assert syslog_field not in target["expr"], (
                f"{node}: panel {panel['title']!r} names {syslog_field!r}, a §4.3 "
                f"syslog field; §7.1 keeps those off this dashboard"
            )


@pytest.mark.parametrize("node", _DASHBOARD_NODES)
def test_logs_panels_use_the_raw_logs_query_type(node: str):
    """The plugin's stats query types aggregate; only `instant` returns individual
    records. A table of events built on an aggregate would render a single row of
    counts, which is not what it appears to be."""
    for panel, target in _targets(_dashboard(node)):
        if target["datasource"]["type"] != _LOGS_PLUGIN_TYPE:
            continue
        assert target.get("queryType") == _RAW_LOGS_QUERY_TYPE, (
            f"{node}: panel {panel['title']!r} has queryType "
            f"{target.get('queryType')!r}; a record-level table needs "
            f"{_RAW_LOGS_QUERY_TYPE!r}"
        )


def _transformations(panel: dict) -> list[dict]:
    return panel.get("transformations", [])


def _transformation_ids(panel: dict) -> list[str]:
    return [t["id"] for t in _transformations(panel)]


@pytest.mark.parametrize("node", _DASHBOARD_NODES)
def test_logs_table_extracts_the_labels_map_into_columns(node: str):
    """The plugin does not return a projected field as its own frame field.

    Verified against a live deployment of the VictoriaLogs datasource plugin
    0.30.1 on Grafana 12.4.6: a query projecting five fields returned `Time`,
    `Line`, `id` and `labels`, with all five projected names inside `labels` as a
    map. Without extraction a table renders
    those four columns — one of them a JSON blob — rather than the projection,
    and it renders without erroring, which is why no assertion on the rendered
    JSON alone can catch it.
    """
    for panel, target in _targets(_dashboard(node)):
        if target["datasource"]["type"] != _LOGS_PLUGIN_TYPE:
            continue
        extract = [t for t in _transformations(panel) if t["id"] == "extractFields"]
        assert extract, (
            f"{node}: panel {panel['title']!r} projects fields in LogsQL but has "
            f"no extractFields transformation, so every projected column arrives "
            f"inside the `labels` map instead"
        )
        assert extract[0]["options"]["source"] == "labels", (
            f"{node}: panel {panel['title']!r} extracts from "
            f"{extract[0]['options'].get('source')!r}; the plugin puts the "
            f"projected fields in `labels`"
        )


@pytest.mark.parametrize("node", _DASHBOARD_NODES)
def test_field_conversion_runs_after_label_extraction(node: str):
    """Transformations apply in order. The litre fields do not exist as frame
    fields until extractFields has run, so a conversion placed before it targets
    nothing and silently leaves the columns as the strings VictoriaLogs
    returned — the defect the conversion was added to prevent."""
    for panel, target in _targets(_dashboard(node)):
        if target["datasource"]["type"] != _LOGS_PLUGIN_TYPE:
            continue
        ids = _transformation_ids(panel)
        if "convertFieldType" not in ids or "extractFields" not in ids:
            continue
        assert ids.index("extractFields") < ids.index("convertFieldType"), (
            f"{node}: panel {panel['title']!r} converts field types before "
            f"extracting them from `labels`, so the conversion targets fields "
            f"that do not exist yet: {ids}"
        )


@pytest.mark.parametrize("node", _DASHBOARD_NODES)
def test_the_plugins_synthesised_columns_are_hidden(node: str):
    """`Line` (the renamed `_msg`) and `id` are added by the plugin whether or
    not the query asks for them, and `labels` is left behind by extraction. The
    dmz table projects `_time` and not `_msg`, so `Line` arrives empty: three
    columns the reconciliation user did not ask for, one of them blank and one a
    map already expanded beside it."""
    for panel, target in _targets(_dashboard(node)):
        if target["datasource"]["type"] != _LOGS_PLUGIN_TYPE:
            continue
        organise = [t for t in _transformations(panel) if t["id"] == "organize"]
        assert organise, (
            f"{node}: panel {panel['title']!r} has no organize transformation, so "
            f"Line, id and labels appear as columns beside the projection"
        )
        excluded = organise[0]["options"]["excludeByName"]
        for synthesised in ("Line", "id", "labels"):
            assert excluded.get(synthesised) is True, (
                f"{node}: panel {panel['title']!r} does not hide the "
                f"{synthesised!r} column"
            )

        # The plugin renames `_time` to `Time` when building the frame, so the
        # column order must address it by the name the frame carries. An entry
        # for `_time` matches nothing and leaves the order to chance.
        order = organise[0]["options"]["indexByName"]
        assert "Time" in order and "_time" not in order, (
            f"{node}: panel {panel['title']!r} orders columns by "
            f"{sorted(order)[:3]}…; the timestamp column is named 'Time' in the "
            f"frame, not '_time'"
        )


@pytest.mark.parametrize("node", _DASHBOARD_NODES)
def test_litre_columns_are_converted_from_the_strings_victorialogs_returns(
    node: str,
):
    """VictoriaLogs stores every field as a string, so a volume arrives as
    "701.01". Grafana sorts a string column lexicographically, which places
    "9.5" after "10.2" — an auditor sorting a month of dispenses by volume would
    read a wrong order from a panel that shows no sign of being wrong. The
    conversion is a panel transformation rather than arithmetic in LogsQL so that
    a record not carrying the field (a fill has no volume_dispensed_l) leaves an
    empty cell rather than NaN."""
    for panel, target in _targets(_dashboard(node)):
        if target["datasource"]["type"] != _LOGS_PLUGIN_TYPE:
            continue
        projected = _logsql_field_names(target["expr"])
        numeric = projected & _LITRE_FIELDS
        if not numeric:
            continue

        converted = {
            conversion["targetField"]
            for transformation in panel.get("transformations", [])
            if transformation["id"] == "convertFieldType"
            for conversion in transformation["options"]["conversions"]
            if conversion["destinationType"] == "number"
        }
        assert numeric <= converted, (
            f"{node}: panel {panel['title']!r} shows {sorted(numeric - converted)} "
            f"as the strings VictoriaLogs returns, so the column sorts "
            f"lexicographically"
        )


# ── Identity and stability ────────────────────────────────────────────────────


@pytest.mark.parametrize("node", _DASHBOARD_NODES)
def test_dashboard_carries_a_stable_uid_and_the_title_section_7_1_names(node: str):
    """The uid is what makes a rebuild replace the dashboard rather than add a
    second copy: the provisioner matches on it, and a dashboard rendered without
    one is given a fresh uid at every provisioning."""
    dashboard = _dashboard(node)
    assert dashboard.get("uid"), (
        f"{node}: the dashboard has no uid, so each rebuild provisions it as a "
        f"new dashboard and the account accumulates duplicates"
    )
    assert dashboard.get("title") == render.DASHBOARDS[node]["title"]


def test_the_two_hosts_do_not_share_a_dashboard_uid():
    """The same reasoning as the datasource uids: nothing in Grafana's
    provisioning checks which host a dashboard file was meant for. Vacuous while
    only one host carries a dashboard, and the assertion that has to hold when
    work item C adds the second."""
    uids = [meta["uid"] for meta in render.DASHBOARDS.values()]
    assert len(uids) == len(set(uids)), f"dashboard uids are not distinct: {uids}"


# ── Coupling to the build ─────────────────────────────────────────────────────
#
# Rendering a dashboard correctly and installing it correctly are separate
# failures, and the second is the quieter of the two: every assertion above holds
# on a file the image never receives.


def _script_commands(node: str) -> list[str]:
    """The build script's lines, with backslash continuations joined, so a push
    written across two lines reads as the one command it is."""
    text = (_render() / "node-build" / f"{node}.sh").read_text()
    return text.replace("\\\n", " ").splitlines()


def _push_destination(node: str, artefact: str) -> str:
    """Where the build script pushes a rendered artefact inside the image, read
    from the push itself so this test does not dictate the path."""
    source = f"build/grafana/{node}-{artefact}"
    for line in _script_commands(node):
        if "incus file push" in line and source in line:
            match = re.search(r'\$BUILD_CTR(/[^"\s]+)', line)
            assert match, f"{node}: cannot read a destination from: {line.strip()}"
            return match.group(1)
    raise AssertionError(
        f"{node}.sh does not push {source} into the image, so the rendered "
        f"dashboard never reaches the deployed instance"
    )


@pytest.mark.parametrize("node", _DASHBOARD_NODES)
def test_both_dashboard_artefacts_are_hashed(node: str):
    """hist.md §7.1: the files are part of the image's input hash, so editing a
    dashboard rebuilds the image and the deployed view cannot drift from the
    repository. An input that is consumed but not hashed is the selective-rebuild
    defect behind the FMS_ADMIN_PASSWORD_HASH regression."""
    script = (_render() / "node-build" / f"{node}.sh").read_text()
    hash_block = script.split("current_hash=$(sha256sum", 1)[1].split("| sha256sum", 1)[
        0
    ]

    for artefact in ("dashboards.yaml", "dashboard.json"):
        entry = f"build/grafana/{node}-{artefact}"
        assert entry in hash_block, (
            f"{entry} is not in {node}.sh's current_hash list; editing the "
            f"dashboard would leave the published image unchanged"
        )


@pytest.mark.parametrize("node", _DASHBOARD_NODES)
def test_the_dashboard_is_pushed_into_the_directory_its_provider_reads(node: str):
    """The coupling that decides whether a dashboard appears at all. The provider
    names a directory and the build fills one; if the two disagree, Grafana starts
    cleanly, provisions nothing, and reports the mismatch nowhere — the file is
    present on disk and absent from the interface."""
    destination = _push_destination(node, "dashboard.json")
    provider_path = _provider(node)["providers"][0]["options"]["path"]

    assert destination.rsplit("/", 1)[0] == provider_path, (
        f"{node}: the build pushes the dashboard to {destination!r}, but its "
        f"provider reads {provider_path!r}"
    )
    assert destination.endswith(".json"), (
        f"{node}: the provider loads *.json from its directory; {destination!r} "
        f"would be ignored"
    )


@pytest.mark.parametrize("node", _DASHBOARD_NODES)
def test_the_provider_is_pushed_into_grafanas_provisioning_tree(node: str):
    """A provider outside /etc/grafana/provisioning/dashboards/ is never read, and
    a provider is the only thing that makes Grafana look at the directory above."""
    destination = _push_destination(node, "dashboards.yaml")
    assert destination.startswith("/etc/grafana/provisioning/dashboards/"), (
        f"{node}: the provider is pushed to {destination!r}, where Grafana does "
        f"not look for one"
    )


@pytest.mark.parametrize("node", _DASHBOARD_NODES)
def test_the_build_confirms_the_dashboard_can_be_retrieved(node: str):
    """Provisioning is applied at start and failures are logged rather than
    raised, so a build that only pushes the files cannot tell a provisioned
    dashboard from a rejected one.

    Asked of the HTTP API rather than the database. Grafana 12.4 serves
    dashboards from unified storage, so which table a provisioned dashboard
    lands in belongs to the storage layer and can change with a version; whether
    the dashboard can be retrieved is the question being asked, and it is the
    path the reconciliation user's browser takes.
    """
    commands = "\n".join(_script_commands(node))
    uid = render.DASHBOARDS[node]["uid"]

    assert f"/api/dashboards/uid/{uid}" in commands, (
        f"{node}.sh does not verify that {uid!r} can be retrieved; a malformed "
        f"dashboard, an unreadable file or a misdirected provider would all "
        f"publish an image that looks healthy and carries no dashboard"
    )


@pytest.mark.parametrize("node", _DASHBOARD_NODES)
def test_panels_do_not_overlap_in_the_grid(node: str):
    """Grafana silently reflows overlapping panels, so a layout error is invisible
    in the rendered file and visible only in the browser."""
    occupied: dict[tuple[int, int], str] = {}
    for panel in _dashboard(node)["panels"]:
        pos = panel["gridPos"]
        for x in range(pos["x"], pos["x"] + pos["w"]):
            for y in range(pos["y"], pos["y"] + pos["h"]):
                clash = occupied.get((x, y))
                assert clash is None, (
                    f"{node}: panels {clash!r} and {panel['title']!r} overlap at "
                    f"grid cell ({x}, {y})"
                )
                occupied[(x, y)] = panel["title"]
        assert pos["x"] + pos["w"] <= 24, (
            f"{node}: panel {panel['title']!r} extends past the 24-column grid"
        )
