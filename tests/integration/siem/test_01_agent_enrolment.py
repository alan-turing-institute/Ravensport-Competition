"""
Agent enrolment and channel tests (siem-tests.md §1; siem.md §2.1, §8.1, §8.5).
"""

from __future__ import annotations

import os
import subprocess
import time

import pytest

from tests.integration.siem.conftest import (
    Config,
    Node,
    OutputReader,
    WazuhAPI,
    carries_line,
    systemd_reports,
    trigger_failed_authentication,
    trigger_service_restart,
)
from tests.integration.siem.hosts import MONITORED_HOSTS


def test_all_twelve_agents_report_connected(wazuh_api: WazuhAPI) -> None:
    """Every monitored host's agent shows as connected, including siem-01's
    own self-monitoring agent.

    Waits rather than checking once: an agent freshly started alongside the
    manager reports `pending` until its first keepalive is processed, so a
    suite run immediately after deployment can otherwise catch it before
    that settles."""
    deadline = time.monotonic() + 60.0
    statuses: dict[str, str | None] = {}
    while True:
        statuses = {agent["name"]: agent["status"] for agent in wazuh_api.agents()}
        if all(
            statuses.get(host) == "active" for host in (*MONITORED_HOSTS, "siem-01")
        ):
            break
        if time.monotonic() >= deadline:
            break
        time.sleep(2.0)
    for host in MONITORED_HOSTS:
        assert statuses.get(host) == "active", f"{host}: {statuses.get(host)!r}"
    assert statuses.get("siem-01") == "active"


@pytest.mark.parametrize("host_name", MONITORED_HOSTS)
def test_deliberate_event_reaches_both_outputs_correctly_attributed(
    host_name: str, outputs: OutputReader
) -> None:
    """A deliberate event triggered on the host appears in archives.json
    within seconds, attributed to that host's agent identity; where it
    matches a rule, it also appears in alerts.json with the same
    attribution.

    Matches the event's own line, not any record from the host: each host
    reports continuously, so a looser match passes on unrelated traffic."""
    node = Node(host_name)
    since = outputs.snapshot()

    line = trigger_failed_authentication(node)
    own_event = carries_line(host_name, line)

    archived = outputs.wait_for_archive(own_event, since=since[1], timeout=20.0)
    assert archived is not None, f"{host_name}: event not archived ({line!r})"

    alerted = outputs.wait_for_alert(own_event, since=since[0], timeout=20.0)
    assert alerted is not None, f"{host_name}: event raised no alert ({line!r})"


def test_agent_channel_uses_the_pre_shared_key_not_a_certificate(
    config: Config,
) -> None:
    """The agent-to-manager connection on TCP 1514 authenticates with the
    per-agent key, not a certificate: a TLS handshake attempted against it is
    rejected outright (no certificate is ever presented on this channel —
    distinct from the management interface, which does present one, §8), and
    a connection carrying obviously invalid protocol bytes — never a valid
    encrypted agent stream — is closed by the manager rather than accepted
    indefinitely.

    Run from corp-it via incus exec, not from the test runner: no firewall
    path admits vnet-ext to siem-01 on any port (only the boundary-inspection
    tests need — and get, via configure-siem — a runner presence on the
    external zone), so a direct socket from the runner would time out
    regardless of what the manager does on this port. corp-it is same-zone
    with siem-01 and needs no route."""
    script = f"""
import socket, ssl
context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
context.check_hostname = False
context.verify_mode = ssl.CERT_NONE
try:
    with socket.create_connection(("{config.siem_host}", {config.agent_port}), timeout=5) as raw:
        with context.wrap_socket(raw):
            pass
    print("NO_TLS_ERROR")
except ssl.SSLError:
    print("TLS_REJECTED")

with socket.create_connection(("{config.siem_host}", {config.agent_port}), timeout=5) as sock:
    sock.sendall(b"not a valid agent protocol stream" * 4)
    sock.settimeout(5)
    try:
        closed = sock.recv(1) == b""
    except TimeoutError:
        closed = False
    print("CLOSED" if closed else "STAYED_OPEN")
"""
    result = Node("corp-it").run("python3", "-c", script, check=True)
    assert "TLS_REJECTED" in result.stdout, (
        "a TLS handshake against the agent channel did not fail — a "
        "certificate may be in use where siem.md §8.6 says none should be"
    )
    assert "CLOSED" in result.stdout, "manager kept an unauthenticated stream open"


@pytest.mark.slow
def test_agents_reconnect_after_a_full_teardown_and_redeploy(
    wazuh_api: WazuhAPI,
) -> None:
    """Following make down and make up, every agent reconnects and reports as
    connected without any manual re-enrolment step, since keys are derived
    deterministically at build time rather than issued at first contact.
    Alert and archive data do not survive: the stores begin empty on a
    rebuilt environment (siem-tests.md §7).

    Destructive: tears down and rebuilds the whole deployed topology. Gated
    behind SIEM_ALLOW_REDEPLOY=1 so it cannot fire as a side effect of an
    ordinary suite run.
    """
    if os.environ.get("SIEM_ALLOW_REDEPLOY") != "1":
        pytest.skip("set SIEM_ALLOW_REDEPLOY=1 to run the teardown/redeploy test")

    deploy_dir = Config.from_env().world_yaml.parent
    subprocess.run(["make", "down"], cwd=deploy_dir, check=True)
    subprocess.run(["make", "up"], cwd=deploy_dir, check=True)

    statuses = {agent["name"]: agent["status"] for agent in wazuh_api.agents()}
    for host in MONITORED_HOSTS:
        assert statuses.get(host) == "active", f"{host}: {statuses.get(host)!r}"


def test_service_restart_is_a_valid_deliberate_event(outputs: OutputReader) -> None:
    """The other deliberate event this suite relies on (§0.4): a restarted
    unit reaches archives.json the same way a failed authentication does."""
    node = Node("corp-it")
    since = outputs.snapshot()

    trigger_service_restart(node)

    archived = outputs.wait_for_archive(
        systemd_reports("corp-it", "Started cron.service"),
        since=since[1],
        timeout=20.0,
    )
    assert archived is not None, "the restart of cron did not reach archives.json"
