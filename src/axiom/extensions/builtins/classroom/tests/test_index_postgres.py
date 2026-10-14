# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The classroom search index on Postgres (ADR-174): schema placement, isolation, cleanup."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text

from axiom.extensions.builtins.classroom.classroom_local_index import ClassroomLocalIndex
from axiom.infra.db import normalize_extension_name

pytestmark = pytest.mark.usefixtures("real_postgres")


def _index(tmp_path, name):
    idx = ClassroomLocalIndex(base_dir=tmp_path / name)
    idx.open()
    return idx


def test_the_migration_builds_its_tables_in_the_classroom_schema_never_public(
    real_postgres, tmp_path
):
    _index(tmp_path, "a")
    names = ("index_chunk", "index_entity", "index_edge")
    with create_engine(real_postgres).connect() as c:
        rows = c.execute(
            text(
                "select table_schema, table_name from information_schema.tables where table_name = any(:n)"
            ),
            {"n": list(names)},
        ).fetchall()
        gin = c.execute(
            text(
                "select indexdef from pg_indexes where schemaname = :s and indexname = 'ix_index_chunk_fts'"
            ),
            {"s": normalize_extension_name("classroom")},
        ).scalar()
    schema = normalize_extension_name("classroom")  # carries the per-worker test suffix, if any
    found = {(s, n) for s, n in rows}
    assert {(schema, n) for n in names} <= found
    assert not [r for r in found if r[0] == "public"], found
    assert gin and "gin" in gin.lower() and "to_tsvector" in gin


def test_one_classrooms_index_never_answers_for_another(tmp_path):
    a, b = _index(tmp_path, "class-a"), _index(tmp_path, "class-b")
    a.ingest(file_id="f", title="A", content="Control rods absorb neutrons.")
    assert a.chunk_count() >= 1 and a.search("control rods")
    assert b.chunk_count() == 0 and b.search("control rods") == []
    assert not b.has_content() and a.has_content()


def test_reingest_replaces_that_file_only(tmp_path):
    idx = _index(tmp_path, "c")
    idx.ingest(file_id="one", title="One", content="Alpha beta gamma.")
    idx.ingest(file_id="two", title="Two", content="Delta epsilon zeta.")
    idx.ingest(file_id="one", title="One", content="Theta iota kappa.")
    assert idx.search("alpha") == []
    assert idx.search("theta") and idx.search("delta")


def test_drop_removes_everything_for_that_classroom_and_only_that(tmp_path):
    a, b = _index(tmp_path, "keep"), _index(tmp_path, "gone")
    a.ingest(file_id="f", title="A", content="Neutrons moderate in water.")
    b.ingest(file_id="f", title="B", content="Neutrons moderate in graphite. Ana Ramirez, Author")
    b.drop()
    assert not b.has_content() and b.entities() == [] and b.edges() == []
    assert a.has_content() and a.search("water")


def test_a_hostile_query_is_only_ever_search_terms(tmp_path):
    idx = _index(tmp_path, "h")
    idx.ingest(file_id="f", title="T", content="Control rods absorb neutrons.")
    # Operators and quotes are stripped to alphanumerics before they reach the tsquery.
    assert idx.search("rods'); DROP TABLE classroom.index_chunk; --")
    assert idx.search("!!! ??? &&& |||") == []
    assert idx.chunk_count() >= 1


def test_the_transient_evaluation_index_leaves_no_rows_behind(tmp_path):
    """The instructor self-eval seeds a throwaway index; cleanup must remove its rows, not only a
    directory. (A missing import once made the cleanup a silent no-op that no test noticed.)"""
    from axiom.extensions.builtins.classroom.cli import _cleanup_transient

    transient = tmp_path / "axi-evals-x"
    idx = _index(tmp_path, "axi-evals-x")
    idx.ingest(file_id="f", title="T", content="Control rods absorb neutrons.")
    assert idx.has_content() and transient.exists()
    _cleanup_transient(transient)
    assert not ClassroomLocalIndex(base_dir=transient).has_content()
    assert not transient.exists()


def test_a_failing_cleanup_is_reported_not_swallowed(tmp_path, caplog, monkeypatch):
    from axiom.extensions.builtins.classroom import classroom_local_index as mod
    from axiom.extensions.builtins.classroom.cli import _cleanup_transient

    def boom(self):
        raise RuntimeError("database went away")

    monkeypatch.setattr(mod.ClassroomLocalIndex, "drop", boom)
    d = tmp_path / "t"
    d.mkdir()
    with caplog.at_level("WARNING"):
        _cleanup_transient(d)
    assert "database went away" in caplog.text
    assert not d.exists()  # the directory is still removed
