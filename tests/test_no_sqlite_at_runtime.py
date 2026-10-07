# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""SQLite is for tests; the runtime database is Postgres (ADR-174).

This scans non-test source for the three ways SQLite re-enters the runtime: importing
``sqlite3`` (or ``sqlite_vec``), a ``sqlite:`` URL, and constructing one of the SQLite
store classes. Every file that legitimately does so is named below with the reason, in one
of three kinds. The list can only shrink: a file listed here that no longer uses SQLite
fails the test, so a finished migration has to delete its own entry.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "axiom"

STORE_CLASSES = {"SQLiteBackend", "SQLiteConceptGraph", "SQLiteRAGStore"}

#: Reading another tool's SQLite file is reading, not storing Axiom state.
READERS = {
    "memory/absorb/passage_store.py": "reads another tool's database read-only",
    "memory/absorb/structured_store.py": "reads another tool's database read-only",
    "memory/session_capture.py": "copies another tool's state database to read it",
    "extensions/builtins/commands/openwebui_projector.py": "writes into Open WebUI's own database file",
}

#: SQLite implementations kept so tests can bind a seam without a server. Production
#: code never constructs them (the construction check below enforces that elsewhere).
TEST_SUPPORT = {
    "artifacts/registry.py": "SQLiteBackend, the ledger test seam and reference implementation",
    "memory/graph.py": "SQLiteConceptGraph, the graph test seam and reference implementation",
    "rag/sqlite_store.py": "SQLiteRAGStore, bound by tests through the recall factory",
    "extensions/builtins/schedule/demo_scheduled_event.py": "an in-memory demo, not a store",
}

#: Runtime SQLite still to be migrated. Each entry is work, and the entry goes when it is done.
PENDING = {
    "infra/tasks/store.py": "background task store (migrates to Postgres)",
    "infra/orchestrator/conversation_store.py": "local fallback conversation store",
    "extensions/builtins/classroom/classroom_local_index.py": "per-classroom search index",
    "rag/store_factory.py": "sqlite:// branch of the retrieval store factory",
    "rag/health.py": "reads rag.db / operational.db files",
    "extensions/builtins/chat/planes.py": "labels sqlite stores in the status report",
}

ALLOWED = {**READERS, **TEST_SUPPORT, **PENDING}


def _docstring_ids(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                ids.add(id(body[0].value))
    return ids


def _uses_sqlite(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docs = _docstring_ids(tree)
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [
                f"import {a.name}"
                for a in node.names
                if a.name.split(".")[0] in ("sqlite3", "sqlite_vec")
            ]
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] in ("sqlite3", "sqlite_vec"):
                found.append(f"from {node.module} import ...")
        elif (
            isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs
        ):
            if node.value.lstrip().startswith("sqlite:"):
                found.append(f"url {node.value[:30]!r}")
        elif isinstance(node, ast.Call):
            fn = node.func
            name = (
                fn.attr
                if isinstance(fn, ast.Attribute)
                else fn.id
                if isinstance(fn, ast.Name)
                else ""
            )
            if name in STORE_CLASSES:
                found.append(f"constructs {name}")
    return found


def _source_files():
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        if "/tests/" in f"/{rel}" or path.name.startswith("test_") or path.name == "conftest.py":
            continue
        yield rel, path


def test_runtime_source_uses_sqlite_only_where_it_is_named_and_justified():
    offenders = {}
    for rel, path in _source_files():
        if rel in ALLOWED:
            continue
        found = _uses_sqlite(path)
        if found:
            offenders[rel] = found
    assert not offenders, (
        "SQLite in runtime source (ADR-174: Postgres is the runtime database; SQLite is for tests). "
        "Use the Postgres store, or, for a genuine reader or test seam, name it in this test with the "
        f"reason:\n{offenders}"
    )


def test_the_allowlist_has_no_stale_entries():
    """A finished migration must delete its entry; otherwise the list stops meaning anything."""
    files = dict(_source_files())
    stale = [rel for rel in ALLOWED if rel not in files or not _uses_sqlite(files[rel])]
    assert not stale, f"these no longer use SQLite; remove them from the allowlist: {stale}"


def test_negative_control_the_scanner_does_see_a_violation(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text("import sqlite3\nx = 'sqlite:///a.db'\nSQLiteBackend('p')\n")
    kinds = " ".join(_uses_sqlite(bad))
    assert (
        "import sqlite3" in kinds
        and "sqlite:///a.db" in kinds
        and "constructs SQLiteBackend" in kinds
    )
    ok = tmp_path / "ok.py"
    ok.write_text('"""mentions sqlite://x in a docstring only"""\nimport sqlalchemy\n')
    assert _uses_sqlite(ok) == []
