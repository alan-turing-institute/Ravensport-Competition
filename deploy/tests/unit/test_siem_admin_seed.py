"""
Management-interface account seed tests for security monitoring
(siem-tests.md §10), targeting siem.md §8.5.

The image build starts the Wazuh manager once so that it creates its role-based
access control database, rbac.db, with two default superusers (`wazuh`, id 1,
and `wazuh-wui`, id 2), then applies render.py's seed with sqlite3. The seed
must leave the management-interface account as the only user, with no role
assignment for a removed user, or fail the build. These run the rendered seed
against the manager's own schema for the three tables it touches, and check
that the build script waits for the default users and stops on a failing
statement.

Run with: pytest tests/unit -q
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]  # deploy/

# Wazuh 4.14.7's rbac.db, as the manager creates it on first start.
_SCHEMA = """
CREATE TABLE users (
    id INTEGER NOT NULL,
    username VARCHAR(32) NOT NULL,
    password VARCHAR(256) NOT NULL,
    allow_run_as BOOLEAN NOT NULL,
    created_at DATETIME,
    PRIMARY KEY (id),
    CONSTRAINT username_restriction UNIQUE (username)
);
CREATE TABLE roles (
    id INTEGER NOT NULL,
    name VARCHAR(20) NOT NULL,
    created_at DATETIME,
    PRIMARY KEY (id),
    CONSTRAINT name_role UNIQUE (name),
    CHECK (length(name) <= 64)
);
CREATE TABLE user_roles (
    id INTEGER NOT NULL,
    user_id INTEGER,
    role_id INTEGER,
    level INTEGER,
    created_at DATETIME,
    PRIMARY KEY (id),
    CONSTRAINT user_role UNIQUE (user_id, role_id),
    FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE,
    FOREIGN KEY(role_id) REFERENCES roles (id) ON DELETE CASCADE
);
INSERT INTO roles VALUES (1, 'administrator', NULL);
"""

_DEFAULT_USERS = """
INSERT INTO users VALUES (1, 'wazuh', 'x', 0, NULL);
INSERT INTO users VALUES (2, 'wazuh-wui', 'x', 1, NULL);
INSERT INTO user_roles VALUES (1, 1, 1, 0, NULL);
INSERT INTO user_roles VALUES (2, 2, 1, 0, NULL);
"""


@pytest.fixture(scope="module")
def rendered() -> tuple[str, str, str]:
    """The seed, the siem-01 build script, and the account's username."""
    out = subprocess.run(
        [sys.executable, "scripts/render.py", "--profile", "ci-minimal"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode == 0, out.stderr
    world = yaml.safe_load((ROOT / "world.yaml").read_text())
    return (
        (ROOT / "build" / "siem" / "siem-01-admin-seed.sql").read_text(),
        (ROOT / "build" / "node-build" / "siem-01.sh").read_text(),
        world["identities"]["siem_admin"]["username"],
    )


def _apply(seed: str, rows: str) -> sqlite3.Connection:
    db = sqlite3.connect(":memory:", isolation_level=None)
    db.executescript(_SCHEMA + rows)
    db.executescript(seed)
    return db


def test_seed_leaves_only_the_management_account(rendered) -> None:
    """Applied to the manager's default users, the seed leaves the
    management-interface account as the only user, and the removed default
    user's administrator role assignment goes with it."""
    seed, _, username = rendered
    db = _apply(seed, _DEFAULT_USERS)
    assert db.execute("SELECT id, username FROM users").fetchall() == [(1, username)]
    assert db.execute("SELECT user_id, role_id FROM user_roles").fetchall() == [
        (1, 1)
    ], "a role assignment remains for a removed user"


@pytest.mark.parametrize(
    "rows",
    [
        pytest.param("", id="default-users-not-yet-inserted"),
        pytest.param(
            _DEFAULT_USERS + "INSERT INTO users VALUES (100, 'other', 'x', 0, NULL);",
            id="another-user-present",
        ),
    ],
)
def test_seed_fails_unless_its_result_is_the_management_account(
    rendered, rows: str
) -> None:
    """Neither the UPDATE nor the DELETE fails when it matches no row, so the
    seed checks its own result and fails, rolling back, when any user other
    than the management-interface account remains or none does."""
    seed, _, _ = rendered
    db = sqlite3.connect(":memory:", isolation_level=None)
    db.executescript(_SCHEMA + rows)
    before = db.execute("SELECT * FROM users ORDER BY id").fetchall()
    with pytest.raises(sqlite3.IntegrityError, match="only_the_management_account"):
        db.executescript(seed)
    db.execute("ROLLBACK")
    assert db.execute("SELECT * FROM users ORDER BY id").fetchall() == before


def test_build_waits_for_the_default_users_and_stops_on_a_failing_statement(
    rendered,
) -> None:
    """The build waits until the manager has inserted both default users and
    fails if it has not, then applies the seed with -bail, without which
    sqlite3 reports a failing statement and exits 0."""
    _, script, _ = rendered
    assert '= "wazuh,wazuh-wui" ]' in script, (
        "the build does not wait for the default users"
    )
    assert "did not create its default users" in script, (
        "the wait for the default users does not fail the build"
    )
    assert (
        "sqlite3 -bail /var/ossec/api/configuration/security/rbac.db "
        "< /tmp/siem-01-admin-seed.sql"
    ) in script, "the seed is not applied with -bail"
