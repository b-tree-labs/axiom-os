# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Every module a shipped migration imports comes with a plain install.

A node migrates every builtin extension's schema, whichever extras were
installed. The signals migration imports ``pgvector`` while only the
``signal`` extra declared it, so on a plain ``pip install`` of 0.70.33 the
migration died with ``No module named 'pgvector'`` (#1843). Development
environments had it by accident, so nothing here noticed.
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILTINS = ROOT / "src" / "axiom" / "extensions" / "builtins"

#: import name -> the distribution that provides it, where the two differ
DISTRIBUTION = {"psycopg2": "psycopg2-binary", "yaml": "pyyaml", "dateutil": "python-dateutil"}

_IMPORT = re.compile(r"^\s*(?:import|from)\s+([A-Za-z_][A-Za-z0-9_]*)", re.MULTILINE)


def _imported() -> set[str]:
    found: set[str] = set()
    for path in BUILTINS.glob("*/migrations/**/*.py"):
        found.update(_IMPORT.findall(path.read_text(encoding="utf-8", errors="ignore")))
    return {m for m in found if m not in sys.stdlib_module_names and m != "axiom"}


def _declared() -> set[str]:
    deps = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["dependencies"]
    return {re.split(r"[\[<>=!~; ]", d, maxsplit=1)[0].lower() for d in deps}


def test_each_module_a_migration_imports_is_a_core_dependency():
    missing = {DISTRIBUTION.get(m, m) for m in _imported()} - _declared()
    assert not missing, f"imported by a migration but not a core dependency: {sorted(missing)}"


def test_the_scan_can_fail():
    """Negative control: the scan sees the import that was missing."""
    assert "pgvector" in _imported()
