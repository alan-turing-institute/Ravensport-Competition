"""
PostgreSQL client for fms-01, accessed via incus exec.

The FMS does not expose Postgres to the network.  The test runner runs
queries inside the fms-01 container via `incus exec ... -- sudo -u postgres
psql ...`, which avoids the need for an SSH tunnel or a dedicated test user.

The postgres superuser is used so that append-only triggers fire under the
same conditions as they would for any other user (triggers fire for all
users, including superusers, in PostgreSQL).
"""

import json
import subprocess
from typing import Any, Generator


class PostgresClient:
    """
    Executes SQL queries on fms-01's PostgreSQL instance via incus exec.
    Used exclusively for schema inspection and trigger verification in the
    append-only tests.  Not for exercising any application code path.
    """

    def __init__(self, node_name: str, db_name: str = "fms") -> None:
        self._node = node_name
        self._db = db_name

    def _run_psql(
        self, sql: str, expect_error: bool = False
    ) -> subprocess.CompletedProcess:
        cmd = [
            "incus",
            "exec",
            self._node,
            "--",
            "sudo",
            "-u",
            "postgres",
            "psql",
            "--dbname",
            self._db,
            "--no-psqlrc",
            "--tuples-only",
            "--no-align",
            "-c",
            sql,
        ]
        return subprocess.run(cmd, capture_output=True, text=True)

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        """
        Execute a SELECT statement and return results as a list of dicts.

        Parameters are interpolated using Python string formatting after
        quoting each value for PostgreSQL (single-quote escaping only;
        sufficient for the UUIDs and text values used in these tests).
        """
        if params:
            quoted = tuple(_pg_quote(p) for p in params)
            sql = sql % quoted

        result = self._run_psql(sql)
        if result.returncode != 0:
            raise RuntimeError(
                f"psql failed (exit {result.returncode}): {result.stderr.strip()}"
            )

        output = result.stdout.strip()
        if not output:
            return []

        # The query may return rows in tuples-only, no-align format.
        # We need column names separately via a JSON approach.
        json_sql = f"SELECT row_to_json(t) FROM ({sql.rstrip(';')}) t"
        result2 = self._run_psql(json_sql)
        if result2.returncode != 0:
            # Fall back to raw output without column names.
            rows = []
            for line in output.splitlines():
                rows.append({"value": line})
            return rows

        rows = []
        for line in result2.stdout.splitlines():
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return rows

    def execute_write(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        """Execute a write statement (INSERT/UPDATE/DELETE); raises on error."""
        if params:
            quoted = tuple(_pg_quote(p) for p in params)
            sql = sql % quoted
        result = self._run_psql(sql)
        if result.returncode != 0:
            raise RuntimeError(
                f"psql failed (exit {result.returncode}): {result.stderr.strip()}"
            )

    def execute_and_expect_error(self, sql: str, params: tuple[Any, ...] = ()) -> str:
        """
        Execute a statement that must raise a database error.
        Returns the error message.  Raises AssertionError if no error occurs.
        """
        if params:
            quoted = tuple(_pg_quote(p) for p in params)
            sql = sql % quoted

        result = self._run_psql(sql)
        if result.returncode == 0:
            raise AssertionError(
                f"Expected a database error but statement succeeded:\n  {sql}"
            )
        return (result.stderr or result.stdout).strip()


def _pg_quote(value: Any) -> str:
    """Quote a value for safe interpolation into a PostgreSQL statement."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int | float):
        return str(value)
    # Strings and UUIDs: escape single quotes and wrap in single quotes.
    escaped = str(value).replace("'", "''")
    return f"'{escaped}'"


def postgres_client_fixture(
    node_name: str,
    db_name: str = "fms",
) -> Generator[PostgresClient, None, None]:
    yield PostgresClient(node_name=node_name, db_name=db_name)
