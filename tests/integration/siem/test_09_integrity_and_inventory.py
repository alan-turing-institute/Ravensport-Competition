"""
File integrity monitoring and inventory tests (siem-tests.md §9; siem.md §2.1,
§6).

Neither capability writes to alerts.json in normal running: the manager keeps
each agent's file integrity baseline and inventory in a per-agent database,
/var/ossec/queue/db/<agent id>.db on siem-01, which these read directly. A
changed monitored file is the exception, and raises an alert.
"""

from __future__ import annotations

import time

import pytest

from tests.integration.siem.conftest import Node, OutputReader
from tests.integration.siem.hosts import MONITORED_HOSTS

# The four inventory categories siem.md §2.1 names, by the table the manager
# keeps each in, and the categories the agents do not report.
_INVENTORY_TABLES = ("sys_programs", "sys_ports", "sys_processes", "sys_netiface")
_UNREPORTED_TABLES = (
    "sys_hwinfo",
    "sys_osinfo",
    "sys_users",
    "sys_groups",
    "sys_services",
    "sys_browser_extensions",
)

# Each agent scans files and inventory at start; the file scan's results reach
# the manager by periodic synchronisation (every 5 minutes, _host-monitoring.xml.j2).
# On a newly started deployment the databases therefore fill over several
# minutes; on an established one they are already full and nothing waits.
_DATABASE_WAIT_S = 600

_FIM_ALERT_TIMEOUT_S = 120


def _agent_ids(siem: Node) -> dict[str, str]:
    """Agent name to its three-digit identifier, from the manager's own key
    file (siem.md §8.5)."""
    ids = {}
    for line in siem.stdout("cat", "/var/ossec/etc/client.keys").splitlines():
        fields = line.split()
        if len(fields) >= 2:
            ids[fields[1]] = fields[0]
    return ids


def _count(siem: Node, agent_id: str, table: str) -> int | None:
    """Rows in `table` of the agent's database, or None if the database does
    not exist yet. Opened read-only, beside the manager's own writer."""
    path = f"/var/ossec/queue/db/{agent_id}.db"
    if not siem.path_exists(path):
        return None
    result = siem.run(
        "sqlite3", "-readonly", path, f"SELECT count(*) FROM {table};", check=True
    )
    return int(result.stdout.strip() or 0)


@pytest.fixture(scope="module")
def agent_ids(siem: Node) -> dict[str, str]:
    ids = _agent_ids(siem)
    missing = [host for host in MONITORED_HOSTS if host not in ids]
    assert not missing, f"no agent key for {missing}"
    return ids


@pytest.fixture(scope="module")
def populated_databases(siem: Node, agent_ids: dict[str, str]) -> None:
    """Wait, only as long as needed, until every monitored host's database
    holds file integrity entries and all four inventory categories."""
    tables = ("fim_entry", *_INVENTORY_TABLES)
    deadline = time.monotonic() + _DATABASE_WAIT_S
    pending = list(MONITORED_HOSTS)
    while pending and time.monotonic() < deadline:
        pending = [
            host
            for host in pending
            if not all((_count(siem, agent_ids[host], t) or 0) > 0 for t in tables)
        ]
        if pending:
            time.sleep(15.0)


@pytest.mark.parametrize("host", MONITORED_HOSTS)
def test_every_agent_reports_file_integrity_and_inventory(
    host: str, siem: Node, agent_ids: dict[str, str], populated_databases: None
) -> None:
    """siem.md §2.1: the manager holds a database for the host's agent, with
    a file integrity baseline and entries in each of the four inventory
    categories."""
    agent_id = agent_ids[host]
    assert siem.path_exists(f"/var/ossec/queue/db/{agent_id}.db"), (
        f"no database {agent_id}.db for {host}: its agent has sent no file "
        "integrity or inventory message"
    )
    for table in ("fim_entry", *_INVENTORY_TABLES):
        assert (_count(siem, agent_id, table) or 0) > 0, f"{host}: {table} is empty"


@pytest.mark.parametrize("host", MONITORED_HOSTS)
def test_no_agent_reports_another_inventory_category(
    host: str, siem: Node, agent_ids: dict[str, str], populated_databases: None
) -> None:
    """siem.md §2.1: inventory is the four named categories only; hardware,
    operating system, users, groups, services and browser extensions are not
    reported."""
    for table in _UNREPORTED_TABLES:
        assert _count(siem, agent_ids[host], table) == 0, f"{host}: {table} has rows"


def test_inventory_records_a_known_listener(
    siem: Node, agent_ids: dict[str, str], populated_databases: None
) -> None:
    """The inventory reflects the host as deployed: corp-it's directory
    listens on 636 (corp-it.md §3), so its listening ports include 636 and
    the process behind it is slapd."""
    result = siem.run(
        "sqlite3",
        "-readonly",
        f"/var/ossec/queue/db/{agent_ids['corp-it']}.db",
        "SELECT process FROM sys_ports WHERE local_port = 636 AND state = 'listening';",
        check=True,
    )
    processes = set(result.stdout.split())
    assert "slapd" in processes, f"corp-it's 636 listener not inventoried: {processes}"


def test_a_new_file_under_etc_raises_an_integrity_alert(
    siem: Node, agent_ids: dict[str, str], outputs: OutputReader
) -> None:
    """A file created under /etc on a monitored host raises the manager's
    file-added alert (rule 554), attributed to that host and naming the
    file. The scheduled scan runs every 12 hours, so the manager triggers an
    immediate scan of that agent instead of waiting for it."""
    host = "corp-it"
    path = f"/etc/ravensport-integrity-check-{int(time.time())}"
    node = Node(host)
    since = outputs.snapshot()[0]
    node.run("sh", "-c", f"echo integrity check > {path}", check=True)
    try:
        siem.run(
            "/var/ossec/bin/agent_control", "-r", "-u", agent_ids[host], check=True
        )
        alerted = outputs.wait_for_alert(
            lambda r: (
                r.get("agent", {}).get("name") == host
                and r.get("rule", {}).get("id") == "554"
                and r.get("syscheck", {}).get("path") == path
            ),
            since=since,
            timeout=_FIM_ALERT_TIMEOUT_S,
        )
        assert alerted is not None, f"no file-added alert for {path} on {host}"
    finally:
        node.run("rm", "-f", path, check=False)


def test_no_rootcheck_or_configuration_assessment_alerts(
    outputs: OutputReader,
) -> None:
    """siem.md §2.1: neither rootcheck nor configuration assessment runs on
    any monitored host or on siem-01 itself, so neither raises an alert."""
    raised = [
        r
        for r in outputs.alerts()
        if r.get("location") in ("rootcheck", "sca")
        or any(g in ("rootcheck", "sca") for g in r.get("rule", {}).get("groups", []))
    ]
    assert not raised, f"rootcheck or assessment alerts: {raised[:3]}"
