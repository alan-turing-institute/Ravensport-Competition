"""The deployed systems-administration session (sim-tests.md)."""

from __future__ import annotations

import time

from tests.integration.siem.conftest import (
    Node,
    OutputReader,
    await_agent_past_first_reload,
)

ADMINISTRATOR = "systems-admin"
MANAGED_HOSTS = ("fms-01", "hist-01", "hmi-01")


def _await_journal_forwarding(host: str, outputs: OutputReader) -> None:
    """Prove the freshly reloaded agent is forwarding new journal records.

    Wazuh can refresh its journald cursor on the first post-reload event and
    omit that event. Retry a harmless marker until one reaches the archive so
    the SSH records below cannot become the cursor probe.
    """
    node = Node(host)
    deadline = time.monotonic() + 90.0
    attempt = 0
    while time.monotonic() < deadline:
        attempt += 1
        marker = f"ravensport-admin-ready-{host}-{attempt}"
        archive_offset = len(outputs.archives())
        node.run("logger", "-t", "ravensport-admin-test", marker, check=True)
        record = outputs.wait_for_archive(
            lambda item, expected=marker: (
                item.get("agent", {}).get("name") == host
                and expected in str(item.get("full_log", ""))
            ),
            since=archive_offset,
            timeout=15.0,
        )
        if record is not None:
            return
    raise AssertionError(f"{host}: Wazuh did not forward a journal readiness marker")


def _authentication(host: str, method: str):
    def matches(record: dict) -> bool:
        full_log = str(record.get("full_log", ""))
        return (
            record.get("agent", {}).get("name") == host
            and "sshd" in full_log
            and f"Accepted {method} for {ADMINISTRATOR}" in full_log
        )

    return matches


def _journal_check(host: str):
    def matches(record: dict) -> bool:
        full_log = str(record.get("full_log", ""))
        return (
            record.get("agent", {}).get("name") == host
            and "sudo" in full_log
            and ADMINISTRATOR in full_log
            and "COMMAND=/usr/bin/journalctl --priority=err --boot" in full_log
        )

    return matches


def _missing_authentication_message(host: str, method: str) -> str:
    """Explain whether SSH or Wazuh lost the expected record.

    Local evidence is diagnostic only: the test still fails because its
    requirement is delivery to the SIEM archive.
    """
    needle = f"Accepted {method} for {ADMINISTRATOR}"
    journal = (
        Node(host)
        .run(
            "journalctl",
            "-u",
            "ssh",
            "--since=-10min",
            "--no-pager",
            "-o",
            "short-iso",
        )
        .stdout
    )
    wazuh_log = Node(host).run("tail", "-n", "200", "/var/ossec/logs/ossec.log").stdout
    collector_lines = [
        line
        for line in wazuh_log.splitlines()
        if "journal" in line.lower()
        and any(word in line.lower() for word in ("cursor", "rotation", "refreshed"))
    ]
    collector_diagnostic = (
        "\nWazuh journald diagnostics:\n" + "\n".join(collector_lines[-10:])
        if collector_lines
        else "\nWazuh reported no journald cursor/rotation event in its last 200 lines."
    )
    if needle in journal:
        return (
            f"SSH authentication occurred locally on {host}, but Wazuh did not "
            "forward it to the SIEM archive."
            f"{collector_diagnostic}\nLocal SSH journal:\n{journal[-4000:]}"
        )
    return (
        f"No SSH authentication was found locally on {host} or in the SIEM archive."
        f"{collector_diagnostic}\nLocal SSH journal:\n{journal[-4000:]}"
    )


def test_session_reaches_each_managed_host() -> None:
    """The real actor signs in to the workstation, traverses the bastion and
    completes the elevated read-only check on every managed host. Wazuh must
    receive the resulting authentications and sudo records."""
    monitored_hosts = ("admin-01", "dmz-bastion-01", *MANAGED_HOSTS)
    outputs = OutputReader(Node("siem-01"))
    for host in monitored_hosts:
        await_agent_past_first_reload(Node(host))
        _await_journal_forwarding(host, outputs)
        split_mode = Node(host).read("/etc/systemd/journald.conf.d/90-ravensport.conf")
        assert "SplitMode=none" in split_mode, (
            f"{host}: system journal is not configured to prevent per-user rotation"
        )

    alert_offset, archive_offset = outputs.snapshot()

    result = Node("sim-host").run(
        "timeout",
        "150",
        "su",
        "sim",
        "-s",
        "/bin/sh",
        "-c",
        "RUST_LOG=warn,sim::actors::systems_administrator=debug "
        "/usr/local/bin/sim --admin-check-once",
    )
    assert result.returncode == 0, (
        f"administration session failed ({result.returncode})\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )

    expected_authentications = {
        "admin-01": "password",
        "dmz-bastion-01": "publickey",
        **{host: "publickey" for host in MANAGED_HOSTS},
    }
    for host, method in expected_authentications.items():
        record = outputs.wait_for_archive(
            _authentication(host, method),
            since=archive_offset,
            timeout=90.0,
        )
        if record is None:
            raise AssertionError(_missing_authentication_message(host, method))

    for host in MANAGED_HOSTS:
        record = outputs.wait_for_archive(
            _journal_check(host),
            since=archive_offset,
            timeout=90.0,
        )
        assert record is not None, f"no journal-check sudo record from {host}"

    non_ssh_alerts = [
        record
        for record in outputs.alerts()[alert_offset:]
        if str(record.get("data", {}).get("alert", {}).get("signature_id")) == "1000008"
        and str(record.get("data", {}).get("dest_port")) == "22"
    ]
    assert not non_ssh_alerts, (
        f"the conforming administration session raised non-SSH alerts: {non_ssh_alerts}"
    )
