# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Nothing outside the database provider opens a raw ``engine_for()`` connection (#1778).

``search_path`` is a property of a pooled connection, and the pool is FIFO. A
connection taken straight from ``engine_for(...)`` carries whatever path the
last ``session_for`` on the same process set, so an unqualified query reads
another extension's schema: ``relation "attest_records" does not exist``,
passing alone and failing in whatever order the xdist worker happened to run.
``session_for`` (and an extension's own ``session_scope()`` over it) sets the
path at the start of every transaction, which is the seam ADR-052 names.

Allowed by design: the provider itself, its own tests, and Alembic migration
environments (``engine_for`` exists for them). Everything else is either clean
or on the named debt list below, which only shrinks: a listed file that no
longer calls ``engine_for`` fails the guard until its line is deleted.
"""

from __future__ import annotations

import ast
import functools
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
ROOTS = ("src", "tests", "packages", "conftest.py")

# The seam's own home, its tests, and the migration environments it exists for.
ALLOWED = {
    "src/axiom/infra/db.py",
    "tests/infra/test_db.py",
}

# Shipped modules that still take a raw connection. Each queries schema-qualified
# tables or bootstraps its own; move it to ``session_for`` and delete its line.
DEBT = {
    "src/axiom/extensions/builtins/data_platform/ingest_sink/summary.py",
    "src/axiom/extensions/builtins/data_platform/skills/ingest_freshness.py",
    "src/axiom/extensions/builtins/data_platform/skills/site_rename.py",
    "src/axiom/extensions/builtins/notifications/inbox_db.py",
    "src/axiom/extensions/builtins/notifications/preferences.py",
}


def calls_engine_for(source: str) -> list[int]:
    """Line numbers where ``engine_for(...)`` (bare or as an attribute) is called."""
    lines = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            f = node.func
            name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None
            if name == "engine_for":
                lines.append(node.lineno)
    return lines


def _python_files():
    for root in ROOTS:
        path = REPO / root
        files = [path] if path.is_file() else path.rglob("*.py")
        for f in files:
            rel = f.relative_to(REPO).as_posix()
            if "/node_modules/" in rel or "/migrations/" in rel or rel in ALLOWED:
                continue
            yield rel, f


@functools.cache
def _offenders() -> dict[str, list[int]]:
    found = {}
    for rel, f in _python_files():
        text = f.read_text(encoding="utf-8", errors="replace")
        if "engine_for" not in text:
            continue
        lines = calls_engine_for(text)
        if lines:
            found[rel] = lines
    return found


def test_no_new_module_opens_a_raw_extension_connection():
    new = {rel: lines for rel, lines in _offenders().items() if rel not in DEBT}
    assert not new, (
        "these call engine_for() outside the database provider; use the extension's "
        "session_scope() or axiom.infra.db.session_for(<ext>), which sets search_path "
        f"per transaction (#1778): {new}"
    )


def test_the_debt_list_only_shrinks():
    clean = sorted(DEBT - set(_offenders()))
    assert not clean, f"no longer call engine_for(); delete their lines from DEBT: {clean}"


def test_the_guard_finds_a_raw_connection_in_a_test():
    """Negative control: the scan sees the shape that flaked."""
    fixture = (
        "from axiom.infra.db import engine_for\n"
        "def test_reads(attest_db):\n"
        "    engine, _ = engine_for('attest')\n"
        "    with engine.connect() as c:\n"
        "        c.execute('SELECT 1 FROM attest_records')\n"
    )
    assert calls_engine_for(fixture) == [3]
    assert calls_engine_for("from axiom.infra import db\ne = db.engine_for('x')\n") == [2]
    # Patching the name, or a different helper that happens to end in it, is not a call.
    assert calls_engine_for("monkeypatch.setattr(db, 'engine_for', f)\nself._engine_for(a)\n") == []
