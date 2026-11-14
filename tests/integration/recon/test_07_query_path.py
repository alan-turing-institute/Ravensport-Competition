"""
Certificate trust and the audit's query path (`recon-tests.md` §5), targeting
`recon.md` §6.1 and §6.4.

The retrieval is run as the reconciliation user's command, less its final
`| xclip -selection clipboard`: `incus exec` has no desktop session, so there
is no selection to write to, and the tests read the text the pipe would carry.
The command is restated here from `sim/sim-impl.md` ("Retrieval"), which the
actor's exact-line test (`the_retrieval_command_is_the_exact_line_typed`) pins
on the sim's side; a change to one is a change to both. The clipboard leg is
the reconciliation user's run.

Records are seeded from inside `hist-01`, to its JSON-line ingestion listener,
so that they reach the replica by replication as the FMS's do. Inserting into
the replica directly would leave records the primary does not hold. They are
dated further back than any audit reaches (`recon-tests.md` §5), so a running
sim's next audit does not retrieve them.

Run on the VM: python -m pytest tests/integration/recon/test_07_query_path.py
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timedelta, timezone

from tests.integration.recon.conftest import Node

QUERY_URL = (
    "https://hist-dmz-01/api/datasources/proxy/uid/monthly-audit-logs"
    "/select/logsql/query"
)

# Replication passes through vlagent's buffer on hist-01 before the replica
# stores the record; bounded generously, as the log-server suite's polling is.
REPLICATION_TIMEOUT_S = 45.0
POLL_INTERVAL_S = 1.0

# Further back than any audit of a running sim reaches: the window audited next
# is at most the previous calendar month, widened by the boundary margin and the
# session lifetime (`recon-tests.md` §5).
SEEDED_DAYS_AGO = 35


def _bound(instant: datetime) -> str:
    """RFC 3339 in UTC with a `Z` designator, as the actor writes a span's
    bounds."""
    return instant.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _retrieval(username: str, password: str, start: datetime, end: datetime) -> str:
    """The actor's retrieval command for the span, less its clipboard pipe."""
    return (
        f"{{ curl -sS --fail-with-body -u '{username}:{password}' -G '{QUERY_URL}' "
        f"--data-urlencode 'query=_time:[{_bound(start)}, {_bound(end)})' 2>&1; "
        f'echo "retrieval-end $?"; }}'
    )


def _retrieve(recon: Node, command: str) -> str:
    return recon.run("bash", "-c", command).stdout


def _credentials(world: dict) -> tuple[str, str]:
    user = world["identities"]["reconciliation_user"]
    return user["username"], user["password"]


def _seed(hist: Node, hist_host: str, records: list[dict]) -> None:
    """Post the records to `hist-01`'s JSON-line ingestion listener from inside
    `hist-01`, verifying its certificate against the system store as the FMS
    does. The name `hist-01` is what the certificate carries; `--resolve`
    directs it to the zone address the listener answers on.

    The content type is stated: `--data-binary` otherwise sends a form type,
    whose body the server parses as form values before the JSON-line handler
    reads it, so nothing is ingested and the post still returns 200."""
    body = "".join(json.dumps(record) + "\n" for record in records)
    result = hist.run(
        "curl",
        "-sS",
        "--fail-with-body",
        "--resolve",
        f"hist-01:9429:{hist_host}",
        "-H",
        "Content-Type: application/stream+json",
        "--data-binary",
        body,
        "https://hist-01:9429/insert/jsonline",
    )
    assert result.returncode == 0, (
        f"seeding hist-01 failed ({result.returncode}): "
        f"{(result.stderr or result.stdout).strip()}"
    )


def test_audit_client_reaches_grafana_without_a_verification_failure(
    recon: Node,
) -> None:
    """From `recon-01` itself, since a tunnelled client dials a name the
    certificate does not carry. Verification is never disabled: a test that used
    `--insecure` would assert nothing (`recon.md` §6.1)."""
    result = recon.run("curl", "-sS", "-o", "/dev/null", "https://hist-dmz-01/")
    assert result.returncode == 0, (
        f"the audit client on recon-01 could not reach Grafana on hist-dmz-01 "
        f"({result.returncode}): {result.stderr.strip()}"
    )


def test_datasource_proxy_returns_records_for_the_seeded_period(
    recon: Node, hist: Node, hist_host: str, world: dict
) -> None:
    """The span includes its start and excludes its end; with no filter, a
    record without an `event_type` returns with the bunkering events; and a
    successful retrieval ends with its completion line."""
    run = uuid.uuid4().hex
    t = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(
        days=SEEDED_DAYS_AGO
    )
    opened = {
        "_time": _bound(t),
        "_msg": "dispense job opened",
        "event_type": "job_opened",
        "job_id": f"recon-query-{run}-start",
        "type": "dispense",
        "session_id": f"recon-query-{run}",
    }
    login = {
        "_time": _bound(t + timedelta(milliseconds=500)),
        "_msg": "login",
        "msg_id": "LOGIN",
        "app_name": "fms",
        "hostname": "fms-01",
        "fms@99999.username": "recon-query",
        "fms@99999.session_id": f"recon-query-{run}",
    }
    at_end = {
        **opened,
        "_time": _bound(t + timedelta(seconds=1)),
        "job_id": f"recon-query-{run}-end",
    }
    _seed(hist, hist_host, [opened, login, at_end])

    username, password = _credentials(world)
    command = _retrieval(username, password, t, t + timedelta(seconds=1))
    deadline = time.monotonic() + REPLICATION_TIMEOUT_S
    while True:
        output = _retrieve(recon, command)
        ours = [line for line in output.splitlines() if run in line]
        if len(ours) >= 2 or time.monotonic() > deadline:
            break
        time.sleep(POLL_INTERVAL_S)

    lines = output.splitlines()
    assert lines and lines[-1] == "retrieval-end 0", (
        f"the retrieval did not end with its completion line: {output!r}"
    )
    returned = []
    for line in lines[:-1]:
        record = json.loads(line)
        assert isinstance(record, dict), f"a record line is not a JSON object: {line}"
        returned.append(record)
    seeded = [record for record in returned if run in json.dumps(record)]
    identities = {
        record.get("job_id") or record.get("fms@99999.session_id") for record in seeded
    }
    assert identities == {f"recon-query-{run}-start", f"recon-query-{run}"}, (
        "expected the record at the span's start and the login-shaped record "
        f"within it, and not the record at its end; returned {sorted(identities)}"
    )


def test_a_failed_retrieval_is_distinguishable_from_an_empty_period(
    recon: Node, world: dict
) -> None:
    """A refused credential arrives as a diagnostic, never as a record; an empty
    span arrives as the completion line alone. Neither can be mistaken for the
    other, nor for records (`recon.md` §6.1)."""
    username, password = _credentials(world)
    now = datetime.now(timezone.utc).replace(microsecond=0)

    refused = _retrieve(
        recon,
        _retrieval(username, password + "-wrong", now, now + timedelta(seconds=1)),
    )
    # The diagnostic, written to standard error at once, precedes the error
    # body, which `curl` writes to standard output when it finishes; the order
    # is the client's, so either is accepted.
    lines = refused.splitlines()
    assert lines and lines[-1] == "retrieval-end 22", (
        f"a refused credential did not end with status 22: {refused!r}"
    )
    assert "curl: (22) The requested URL returned error: 401" in lines[:-1], (
        f"a refused credential carried no 401 diagnostic: {refused!r}"
    )
    assert any(line.startswith("{") for line in lines[:-1]), (
        f"expected Grafana's error body with curl's diagnostic: {refused!r}"
    )

    later = now + timedelta(hours=1)
    empty = _retrieve(
        recon, _retrieval(username, password, later, later + timedelta(seconds=1))
    )
    assert empty == "retrieval-end 0\n", (
        f"an empty span did not return the completion line alone: {empty!r}"
    )
