"""
Host log data tests (siem-tests.md §3; siem.md §3, §8.2).
"""

from __future__ import annotations

import uuid

import pytest

from tests.integration.siem.conftest import (
    Node,
    OutputReader,
    carries_line,
    process_output,
    systemd_reports,
    trigger_failed_authentication,
    trigger_service_restart,
)
from tests.integration.siem.hosts import NAMED_LOG_FILE_HOSTS


def _trigger_network_config_change(node: Node) -> None:
    """A network configuration change that is actually visible to the
    journal: reload systemd-networkd, which reapplies the host's network
    configuration and logs doing so.

    A raw `ip addr add`/`del` is invisible here — it is a direct kernel
    netlink operation with no accompanying syslog/journal message from any
    service, so it never reached archives.json regardless of collection
    correctness (confirmed live: no record followed it). Reloading the
    network manager itself is a genuine reconfiguration event with a
    corresponding log line, without touching the interface a same-zone
    caller such as `incus exec` depends on to keep working."""
    node.run("systemctl", "reload-or-restart", "systemd-networkd", check=True)


def test_journal_sources_are_collected_on_every_monitored_host(
    outputs: OutputReader,
) -> None:
    """A unit start/stop, an authentication event, and a network
    configuration change each reach archives.json."""
    node = Node("corp-it")

    since = outputs.snapshot()[1]
    trigger_service_restart(node)
    assert outputs.wait_for_archive(
        systemd_reports("corp-it", "Started cron.service"), since=since, timeout=20.0
    ), "unit restart did not reach archives.json"

    since = outputs.snapshot()[1]
    line = trigger_failed_authentication(node)
    assert outputs.wait_for_archive(
        carries_line("corp-it", line), since=since, timeout=20.0
    ), "authentication event did not reach archives.json"

    since = outputs.snapshot()[1]
    _trigger_network_config_change(node)
    assert outputs.wait_for_archive(
        systemd_reports("corp-it", "Reloaded systemd-networkd.service"),
        since=since,
        timeout=20.0,
    ), "network configuration change did not reach archives.json"


@pytest.mark.parametrize("host", ["fms-01", "hmi-01"])
def test_fms_and_hmi_process_output_is_collected_via_the_journal(
    host: str, outputs: OutputReader
) -> None:
    """The Fuel Management System's and Human-Machine Interface's own process
    output, carried via the journal, is included: the restarted process logs
    under its own program name at start."""
    unit = host.split("-")[0]
    since = outputs.snapshot()[1]
    pid = trigger_service_restart(Node(host), unit)
    assert outputs.wait_for_archive(
        process_output(host, unit, pid), since=since, timeout=20.0
    ), f"{host}'s service output did not reach archives.json"


def _matches_source(record: dict, hint: str) -> bool:
    haystack = (
        " ".join(str(record.get(key, "")) for key in ("location", "full_log"))
        + " "
        + str(record.get("decoder", {}).get("name", ""))
    )
    return hint in haystack


@pytest.mark.parametrize("host,hint", NAMED_LOG_FILE_HOSTS.items())
def test_named_log_files_are_collected_where_specified(
    host: str, hint: str, outputs: OutputReader
) -> None:
    """The file inputs of siem.md §3 — PostgreSQL on fms-01, the directory
    client's domain log on recon-01, and Suricata's event file on the
    firewall nodes — each reach archives.json when written to.

    Neither PostgreSQL's checkpoints nor recon-01's domain log can be relied
    on to have written something recently on their own: PostgreSQL's
    default checkpoint_timeout is 5 minutes, so a suite run against a
    freshly deployed environment can otherwise land before the first one;
    recon-01's domain log writes only on a state change (start, stop, a
    connectivity problem), never during steady healthy operation. Both are
    forced directly rather than waited for, and the wait is for a record
    from the named source itself: every host reports continuously, so a
    record from the host alone can arrive before the forced one."""

    def from_source(record: dict) -> bool:
        return record.get("agent", {}).get("name") == host and _matches_source(
            record, hint
        )

    since = outputs.snapshot()[1]
    if host == "recon-01":
        Node(host).run("systemctl", "restart", "sssd", check=True)
    elif host == "fms-01":
        Node(host).run(
            "sudo", "-u", "postgres", "psql", "-c", "CHECKPOINT;", check=True
        )

    collected = any(from_source(r) for r in outputs.archives()) or (
        outputs.wait_for_archive(from_source, since=since, timeout=20.0) is not None
    )
    assert collected, f"no archived record from {host} matches the source {hint!r}"


def test_no_input_is_collected_twice(outputs: OutputReader) -> None:
    """One event produces exactly one record, not two under different
    decoders."""
    node = Node("corp-it")
    marker = uuid.uuid4().hex
    since = outputs.snapshot()[1]

    node.run("logger", "-t", "siem_test_marker", marker)

    outputs.wait_for_archive(
        lambda r: marker in str(r.get("full_log", "")), since=since, timeout=20.0
    )
    matches = [
        r for r in outputs.archives()[since:] if marker in str(r.get("full_log", ""))
    ]
    assert len(matches) == 1, (
        f"expected exactly one record for the marker, got {len(matches)}"
    )
