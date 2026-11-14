"""Puts the repository's root on the module search path, so that the tests
import the episode runner as competition.episode_runner."""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
