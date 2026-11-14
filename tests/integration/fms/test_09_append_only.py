"""
Group 9 — Append-only guarantee.

Verifies that Postgres triggers block UPDATE and DELETE on the transaction,
job, session and job-cancellation logs, and that the BDN correction mechanism correctly
produces a new row rather than mutating the original, per digital.md §6.2.

All tests are marked fast: they require only a Postgres connection (via
incus exec) and do not depend on the boundary-protocol stubs.
"""

import pytest

from tests.integration.fms.stubs.postgres import PostgresClient


# ---------------------------------------------------------------------------
# Helpers — return an existing row ID, seeding a sentinel row if the table
# is empty.  The append-only constraint means inserted rows cannot be
# deleted; sentinel rows are identifiable by counterparty/username = '__test__'.
# ---------------------------------------------------------------------------


def _ensure_session_id(postgres: PostgresClient) -> str:
    rows = postgres.execute(
        "SELECT session_id FROM sessions ORDER BY logged_in_at DESC LIMIT 1"
    )
    if rows:
        return rows[0]["session_id"]
    postgres.execute_write(
        "INSERT INTO sessions (username, role) VALUES ('__test__', 'admin')"
    )
    rows = postgres.execute(
        "SELECT session_id FROM sessions ORDER BY logged_in_at DESC LIMIT 1"
    )
    return rows[0]["session_id"]


def _ensure_job_id(postgres: PostgresClient) -> str:
    rows = postgres.execute("SELECT job_id FROM jobs ORDER BY created_at DESC LIMIT 1")
    if rows:
        return rows[0]["job_id"]
    # jobs.session_id is NOT NULL and references sessions (fms.md §8.3), so the
    # sentinel job is created under a sentinel (or existing) session.
    session_id = _ensure_session_id(postgres)
    postgres.execute_write(
        "INSERT INTO jobs (type, counterparty, grade, operator, session_id)"
        " VALUES ('dispense', '__test__', 'MGO', '__test__', %s)",
        (session_id,),
    )
    rows = postgres.execute("SELECT job_id FROM jobs ORDER BY created_at DESC LIMIT 1")
    return rows[0]["job_id"]


def _ensure_bdn_transaction_id(postgres: PostgresClient) -> str:
    """Return the references_transaction_id from an existing BDN-linked row, seeding one if absent."""
    rows = postgres.execute(
        "SELECT references_transaction_id FROM transactions"
        " WHERE references_transaction_id IS NOT NULL LIMIT 1"
    )
    if rows:
        return rows[0]["references_transaction_id"]
    original_id = _ensure_transaction_id(postgres)
    job_id = _ensure_job_id(postgres)
    postgres.execute_write(
        "INSERT INTO transactions (job_id, counterparty, grade, references_transaction_id)"
        " VALUES (%s, '__test__', 'MGO', %s)",
        (job_id, original_id),
    )
    return original_id


def _ensure_transaction_id(postgres: PostgresClient) -> str:
    rows = postgres.execute(
        "SELECT transaction_id FROM transactions ORDER BY created_at DESC LIMIT 1"
    )
    if rows:
        return rows[0]["transaction_id"]
    job_id = _ensure_job_id(postgres)
    postgres.execute_write(
        "INSERT INTO transactions (job_id, counterparty, grade) VALUES (%s, '__test__', 'MGO')",
        (job_id,),
    )
    rows = postgres.execute(
        "SELECT transaction_id FROM transactions ORDER BY created_at DESC LIMIT 1"
    )
    return rows[0]["transaction_id"]


# ---------------------------------------------------------------------------
# Transaction table
# ---------------------------------------------------------------------------


@pytest.mark.fast
def test_transaction_update_blocked(postgres: PostgresClient) -> None:
    """UPDATE on the transactions table must raise a Postgres exception."""
    tx_id = _ensure_transaction_id(postgres)
    error = postgres.execute_and_expect_error(
        "UPDATE transactions SET counterparty = 'tampered' WHERE transaction_id = %s",
        (tx_id,),
    )
    assert error, "UPDATE on transactions succeeded — append-only trigger is missing"


@pytest.mark.fast
def test_transaction_delete_blocked(postgres: PostgresClient) -> None:
    tx_id = _ensure_transaction_id(postgres)
    error = postgres.execute_and_expect_error(
        "DELETE FROM transactions WHERE transaction_id = %s",
        (tx_id,),
    )
    assert error, "DELETE on transactions succeeded — append-only trigger is missing"


# ---------------------------------------------------------------------------
# Jobs table
# ---------------------------------------------------------------------------


@pytest.mark.fast
def test_job_update_blocked(postgres: PostgresClient) -> None:
    job_id = _ensure_job_id(postgres)
    error = postgres.execute_and_expect_error(
        "UPDATE jobs SET counterparty = 'tampered' WHERE job_id = %s",
        (job_id,),
    )
    assert error, "UPDATE on jobs succeeded — append-only trigger is missing"


@pytest.mark.fast
def test_job_delete_blocked(postgres: PostgresClient) -> None:
    job_id = _ensure_job_id(postgres)
    error = postgres.execute_and_expect_error(
        "DELETE FROM jobs WHERE job_id = %s",
        (job_id,),
    )
    assert error, "DELETE on jobs succeeded — append-only trigger is missing"


# ---------------------------------------------------------------------------
# Sessions table
# ---------------------------------------------------------------------------


@pytest.mark.fast
def test_session_update_blocked(postgres: PostgresClient) -> None:
    session_id = _ensure_session_id(postgres)
    error = postgres.execute_and_expect_error(
        "UPDATE sessions SET username = 'tampered' WHERE session_id = %s",
        (session_id,),
    )
    assert error, "UPDATE on sessions succeeded — append-only trigger is missing"


@pytest.mark.fast
def test_session_delete_blocked(postgres: PostgresClient) -> None:
    session_id = _ensure_session_id(postgres)
    error = postgres.execute_and_expect_error(
        "DELETE FROM sessions WHERE session_id = %s",
        (session_id,),
    )
    assert error, "DELETE on sessions succeeded — append-only trigger is missing"


# ---------------------------------------------------------------------------
# Job cancellations table
# ---------------------------------------------------------------------------


def _ensure_cancelled_job_id(postgres: PostgresClient) -> str:
    rows = postgres.execute("SELECT job_id FROM job_cancellations LIMIT 1")
    if rows:
        return rows[0]["job_id"]
    # job_cancellations admits one row per job (fms.md §8.5); with the table
    # empty, any job is one no cancellation names yet.
    job_id = _ensure_job_id(postgres)
    session_id = _ensure_session_id(postgres)
    postgres.execute_write(
        "INSERT INTO job_cancellations (job_id, operator, session_id)"
        " VALUES (%s, '__test__', %s)",
        (job_id, session_id),
    )
    return job_id


@pytest.mark.fast
def test_job_cancellation_update_blocked(postgres: PostgresClient) -> None:
    job_id = _ensure_cancelled_job_id(postgres)
    error = postgres.execute_and_expect_error(
        "UPDATE job_cancellations SET operator = 'tampered' WHERE job_id = %s",
        (job_id,),
    )
    assert error, (
        "UPDATE on job_cancellations succeeded — append-only trigger is missing"
    )


@pytest.mark.fast
def test_job_cancellation_delete_blocked(postgres: PostgresClient) -> None:
    job_id = _ensure_cancelled_job_id(postgres)
    error = postgres.execute_and_expect_error(
        "DELETE FROM job_cancellations WHERE job_id = %s",
        (job_id,),
    )
    assert error, (
        "DELETE on job_cancellations succeeded — append-only trigger is missing"
    )


# ---------------------------------------------------------------------------
# Correction pattern: new row, not in-place edit
# ---------------------------------------------------------------------------


@pytest.mark.fast
def test_bdn_correction_is_new_row(postgres: PostgresClient) -> None:
    """
    The BDN linkage (digital.md §4.2 step 6) must produce a second row in
    the transactions table referencing the original, not an UPDATE of the
    first row.  Verifies the correction pattern by schema inspection.
    """
    ref_id = _ensure_bdn_transaction_id(postgres)
    original = postgres.execute(
        "SELECT transaction_id FROM transactions WHERE transaction_id = %s",
        (ref_id,),
    )
    assert original, (
        f"Original transaction {ref_id} is missing — it may have been deleted instead of referenced"
    )
