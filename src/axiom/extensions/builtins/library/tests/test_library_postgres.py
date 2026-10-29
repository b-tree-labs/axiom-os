# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The catalog on real Postgres, through the migration a node runs.

SQLite cannot show the failure this exists for: ``session_for`` sets the
schema's ``search_path`` on the connection a session starts with, and a second
transaction on the same session can be handed a pooled connection without it
(``relation "library_items" does not exist``). The upload path committed twice
in one session and failed exactly that way on the first Postgres run. Each
test gets its own schema, provisioned by ``provision_extension`` and dropped
after, so no live schema is touched; without a reachable server it skips.
"""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

DOC = b"# Seals\n\nThe zebracorn seal kit is replaced every quarter.\n"


def _pg_available() -> bool:
    try:
        import psycopg2

        from axiom.infra.db import libpq_url, platform_db_url

        psycopg2.connect(libpq_url(platform_db_url()), connect_timeout=2).close()
        return True
    except Exception:  # noqa: BLE001 - any failure means no database to test against
        return False


@pytest.fixture
def pg_library(tmp_path, monkeypatch):
    if not _pg_available():
        pytest.skip("Postgres not reachable; this test needs the real catalog")
    from sqlalchemy import text

    from axiom.extensions.builtins.library import store
    from axiom.infra.db import engine_for, normalize_extension_name, provision_extension
    from axiom.rag import store_factory

    monkeypatch.setenv("AXIOM_TEST_SCHEMA_SUFFIX", f"_t{uuid.uuid4().hex[:10]}")
    schema = normalize_extension_name("library")
    result = provision_extension("library")
    assert result.ok, result.error

    store_factory.allow_sqlite_for_tests(True)
    rag_url = f"sqlite:///{tmp_path / 'rag.db'}"

    def open_rag():
        s = store_factory.create_store(rag_url)
        s.connect()
        return s

    from axiom.extensions.builtins.data_platform.rag_embed import embedder

    monkeypatch.setattr(embedder, "embed_texts", lambda texts: None)
    monkeypatch.setattr("axiom.rag.embeddings.embed_texts", lambda texts: None)
    monkeypatch.setenv("AXIOM_LIBRARY_BRONZE_ROOT", str(tmp_path / "bronze"))
    monkeypatch.setenv("AXIOM_LIBRARY_TAXONOMY", "generic")
    monkeypatch.setattr("axiom.extensions.builtins.library.forward.bound_node", lambda: None)
    store.reset_provider()
    store.set_rag_store(open_rag)
    try:
        yield schema
    finally:
        store.set_rag_store(None)
        store_factory.allow_sqlite_for_tests(False)
        engine, _ = engine_for("library")
        with engine.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))


def test_the_whole_lifecycle_runs_on_the_migrated_schema(pg_library):
    from axiom.extensions.builtins.library import service
    from axiom.extensions.builtins.library.scope import make_caller

    alice = make_caller("@alice:alpha", "alpha")
    item = service.upload(alice, "seals.md", DOC, tags="seals").item
    assert item["index_status"] == "indexed"
    assert [i["id"] for i in service.list_items(alice)] == [item["id"]]
    assert service.update_item(alice, item["id"], collection="Plant")["collection"] == "Plant"
    assert [h["id"] for h in service.search(alice, "zebracorn")] == [item["id"]]
    assert service.delete_item(alice, item["id"])["deleted_at"]
    assert service.restore_item(alice, item["id"])["index_status"] == "indexed"
    assert service.upload(alice, "again.md", DOC).duplicate


def test_concurrent_uploads_each_land_whole(pg_library):
    """Several uploads at once draw several pooled connections, which is where a
    session that commits twice reaches one without the schema on its path."""
    from axiom.extensions.builtins.library import service
    from axiom.extensions.builtins.library.scope import make_caller

    caller = make_caller("@bob:alpha", "alpha")
    docs = [(f"n{i}.md", DOC + f"\nnote {i}\n".encode()) for i in range(16)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda d: service.upload(caller, d[0], d[1]), docs))
    assert {r.item["index_status"] for r in results} == {"indexed"}
    assert len(service.list_items(caller)) == 16
