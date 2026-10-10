# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Every Postgres driver the shipped code imports is a declared dependency.

The data kit, the conform runner and the SQL source import ``psycopg``
(version 3) while only ``psycopg2-binary`` was declared. On a clean
``pip install`` the first `kit-up` a creator ran (2026-10-06) died in a
traceback: ``No module named 'psycopg'``. Development environments had it
by accident, so nothing here noticed.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: import name -> the distribution that provides it
DRIVERS = {"psycopg": "psycopg", "psycopg2": "psycopg2-binary"}


def _imported() -> set[str]:
    pattern = re.compile(r"^\s*(?:import|from)\s+(psycopg2|psycopg)\b", re.MULTILINE)
    found: set[str] = set()
    for path in (ROOT / "src" / "axiom").rglob("*.py"):
        if "/tests/" in path.as_posix():
            continue
        found.update(pattern.findall(path.read_text(encoding="utf-8", errors="ignore")))
    return found


def _declared() -> set[str]:
    deps = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["dependencies"]
    return {re.split(r"[\[<>=!~; ]", d, maxsplit=1)[0].lower() for d in deps}


def test_each_imported_driver_is_a_core_dependency():
    missing = {DRIVERS[m] for m in _imported()} - _declared()
    assert not missing, f"imported but not declared: {sorted(missing)}"


def test_the_scan_can_fail():
    """Negative control: the scan sees the import that was missing."""
    assert "psycopg" in _imported()
