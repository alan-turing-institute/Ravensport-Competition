"""
Polling helper shared by the log server ingestion and replication test modules.

vmagent/vlagent buffer records before remote-writing them, so a freshly pushed
metric or event is not queryable the instant the push returns; and during the §4
outage tests the DMZ replica may be down or booting when queried. `poll_until_count`
retries a query until it returns enough records.

The dominant term in that delay is not the buffering but VictoriaMetrics'
`-search.latencyOffset`, which defaults to 30 seconds and is deliberately left at
its default (hist.md §9): an instant query is evaluated at `now - 30s`, so a
record is invisible to it for that long however promptly it was stored. The
default timeout below must therefore exceed 30 seconds, or every ingestion
assertion fails against a store that holds the record perfectly.

By default a query error (store unreachable, malformed response) propagates
immediately — when the store is meant to be up, a raised error is a real fault
worth surfacing at once rather than hiding behind a timeout. Pass
`tolerate_errors=True` where an outage is expected (the §4 recovery tests): the
error is then treated as "not ready yet" and retried, with the last one reported
if the timeout is reached.
"""

from __future__ import annotations

import time
from typing import Protocol

import pytest


class Queryable(Protocol):
    """A store client exposing `query(str) -> list[dict]` (MetricsClient/LogsClient)."""

    def query(self, query: str) -> list[dict]: ...


# Longer than VictoriaMetrics' 30-second -search.latencyOffset, with margin for
# the agent's own flush. Shortening this below 30 seconds makes every ingestion
# test fail regardless of whether ingestion works.
_DEFAULT_TIMEOUT = 45.0
_POLL_INTERVAL = 0.5


def poll_until_count(
    client: Queryable,
    query: str,
    count: int = 1,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
    interval: float = _POLL_INTERVAL,
    tolerate_errors: bool = False,
) -> list[dict]:
    """Poll `client.query(query)` until it returns at least `count` records.

    Returns the records once `len(records) >= count`. Fails via `pytest.fail`
    if the timeout is reached. With `tolerate_errors=True`, a query error counts
    as zero records and is retried instead of propagating; the last error is
    included in the failure message.
    """
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    records: list[dict] = []
    while time.monotonic() < deadline:
        try:
            records = client.query(query)
        except Exception as error:
            if not tolerate_errors:
                raise
            last_error = error
            records = []
        if len(records) >= count:
            return records
        time.sleep(interval)
    suffix = f" (last error: {last_error})" if tolerate_errors else ""
    pytest.fail(
        f"expected >= {count} records within {timeout}s, got {len(records)} "
        f"for {query!r}{suffix}"
    )
