"""Fixtures of the living port's tests (living_fakes.py holds the fakes)."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest
from living_fakes import FakeHost


@pytest.fixture
def short_tmp():
    """A directory with a short path, since a UNIX socket's path is limited to
    about 100 bytes, which pytest's own temporary directories can exceed."""
    # Resolved, as the manager resolves its root (/tmp is a link on macOS).
    path = Path(tempfile.mkdtemp(prefix="lv-", dir="/tmp")).resolve()
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def host():
    fake = FakeHost()
    yield fake
    fake.close()
