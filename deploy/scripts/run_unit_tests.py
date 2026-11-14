#!/usr/bin/env python3
"""Run tests/unit/test_*.py without pytest. Fallback for hosts that don't have it."""

import importlib.util
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests" / "unit"

failures = 0
total = 0
for path in sorted(TESTS.glob("test_*.py")):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for name in dir(mod):
        if not name.startswith("test_"):
            continue
        fn = getattr(mod, name)
        if not callable(fn):
            continue
        total += 1
        try:
            fn()
            print(f"  ✓ {path.stem}::{name}")
        except Exception as e:
            failures += 1
            print(f"  ✗ {path.stem}::{name}: {e}")
            traceback.print_exc()

print(f"\n{total - failures}/{total} tests passed")
sys.exit(1 if failures else 0)
