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

import os
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
def pg_library(tmp_path, monkeypatch, fresh_postgres):
    if not _pg_available():
        pytest.skip("Postgres not reachable; this test needs the real catalog")
    from sqlalchemy import text

    from axiom.extensions.builtins.library import store
    from axiom.infra.db import normalize_extension_name, provision_extension, session_for
    from axiom.rag import store_factory

    monkeypatch.setenv("AXIOM_TEST_SCHEMA_SUFFIX", f"_t{uuid.uuid4().hex[:10]}")
    schema = normalize_extension_name("library")
    result = provision_extension("library")
    assert result.ok, result.error
    # The sharing record and the decision point's receipts live in authz (ADR-263 D10).
    # Its migrations name the ``authz`` schema outright, so it is migrated unsuffixed, in this
    # test's own throwaway database (``fresh_postgres``), as the authz tests do.
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    from axiom.extensions.builtins import authz as authz_extension

    suffix = os.environ.pop("AXIOM_TEST_SCHEMA_SUFFIX")
    try:
        config = Config()
        config.set_main_option("script_location", str(Path(authz_extension.__file__).parent / "migrations"))
        command.upgrade(config, "head")
    finally:
        os.environ["AXIOM_TEST_SCHEMA_SUFFIX"] = suffix
    from axiom.governance.access import decision as access_decision
    from axiom.governance.access import store as access_store

    from contextlib import contextmanager

    @contextmanager
    def authz_session():
        os.environ.pop("AXIOM_TEST_SCHEMA_SUFFIX", None)
        try:
            with session_for("authz") as session:
                yield session
        finally:
            os.environ["AXIOM_TEST_SCHEMA_SUFFIX"] = suffix

    access_store.set_session_provider(authz_session)
    access_decision.set_decide_context(access_decision.build_access_context(session_factory=authz_session))
    access_decision.set_resolvers(groups=lambda p, s: [], roles=lambda p, s: [])

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
        access_decision.set_resolvers()
        access_decision.set_decide_context(None)
        access_store.set_session_provider(None)
        with session_for("library") as session:
            session.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            session.commit()


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


def test_the_upload_pipeline_and_the_data_book_run_on_the_migrated_schema(pg_library, monkeypatch):
    """ADR-213's tables (jobs, sessions, extractions, routes, folders, corrections) on real Postgres."""
    import hashlib

    from axiom.extensions.builtins.library import contrib, pipeline, service, uploads
    from axiom.extensions.builtins.library.book import service as book
    from axiom.extensions.builtins.library.scope import make_caller
    from axiom.extensions.builtins.library.store import session_scope

    monkeypatch.setenv("AXIOM_LIBRARY_PIPELINE_INLINE", "1")
    contrib.reset(rediscover=False)
    alice = make_caller("@alice:alpha", "alpha")
    data = b"t,v\n" + b"".join(f"{i},{i * i}\n".encode() for i in range(5000))
    s = uploads.create(alice, file_name="run.csv", size=len(data), sha256=hashlib.sha256(data).hexdigest())
    half = len(data) // 2
    uploads.append(alice, s["upload_id"], 0, data[:half])
    assert uploads.append(alice, s["upload_id"], half, data[half:])["complete"]
    job = pipeline.get_job(alice, s["job_id"])
    assert job["state"] == "done" and [st["status"] for st in job["stages"]][:5] == ["done"] * 5
    detail = service.item_pipeline(alice, job["item_id"])
    # The indexed stage is done exactly when the file is searchable (#1954).
    indexed = job["stages"][5]["status"]
    assert (indexed == "done") == (detail["item"]["index_status"] == "indexed"), (indexed, detail["item"]["index_status"])
    assert detail["item"]["folder"] == "datasets/series"
    assert any(r["tag"] == "dataset" and r["status"] == "done" for r in detail["routes"])
    with session_scope() as session:
        folder = book.create_folder(session, "alpha", alice.principal, "Runs")
        session.commit()
    moved = service.move_item(alice, job["item_id"], folder["path"])
    assert moved["folder"] == folder["path"]
    with session_scope() as session:
        tree = book.tree(session, "alpha")
    assert next(f for f in tree["folders"] if f["path"] == folder["path"])["count"] == 1
    again = pipeline.start_reextract(alice, job["item_id"])
    assert pipeline.get_job(alice, again["id"])["state"] == "done"
    assert service.get_item(alice, job["item_id"])["folder"] == folder["path"]
    contrib.reset()


def test_the_common_views_run_on_the_migrated_schema(pg_library):
    """Migration 0003's per-person marks (Starred, Recent) and the read-only views on real Postgres."""
    from axiom.extensions.builtins.library import service, views
    from axiom.extensions.builtins.library.scope import make_caller

    alice = make_caller("@alice:alpha", "alpha")
    bob = make_caller("@bob:alpha", "alpha")
    item = service.upload(alice, "seals.md", DOC).item
    views.set_item_star(alice, item["id"], True)
    views.set_item_star(alice, item["id"], True)
    assert [i["id"] for i in views.starred(alice)["items"]] == [item["id"]]
    assert views.starred(bob)["items"] == []
    views.record_open(bob, item["id"])
    assert [(r["id"], r["recent_reason"]) for r in views.recent(bob)] == [(item["id"], "opened")]
    assert [i["id"] for i in views.shared_with_me(bob)["items"]] == [item["id"]]
    assert views.originals(alice)["items"][0]["file_name"] == "seals.md"


def test_copy_versions_and_delete_forever_run_on_the_migrated_schema(pg_library, monkeypatch, tmp_path):
    """Migration 0004's versions, a copy over shared bronze bytes, and a purge, on real Postgres."""
    from axiom.extensions.builtins.library import operations, service
    from axiom.extensions.builtins.library.scope import make_caller

    monkeypatch.setenv("AXIOM_LIBRARY_PIPELINE_INLINE", "1")
    monkeypatch.setenv("AXIOM_ACTION_LEDGER_BACKEND", "jsonl")
    monkeypatch.setattr("axiom.policy.action_ledger._default_state_dir", lambda _d: tmp_path / "state")
    alice = make_caller("@alice:alpha", "alpha")
    item = service.upload(alice, "seals.md", DOC).item
    copy = operations.copy_item(alice, item["id"])
    assert copy["sha256"] == item["sha256"]
    operations.add_version(alice, item["id"], "seals.md", DOC + b"\nRevised.\n")
    operations.restore_version(alice, item["id"], 1)
    assert [v["version_number"] for v in operations.list_versions(alice, item["id"])] == [3, 2, 1]
    service.delete_item(alice, item["id"])
    out = operations.purge_item(alice, item["id"])
    assert out["purged"] and out["bytes_kept_shared"] == 1  # the copy still uses version 1's bytes


def test_sharing_and_the_folder_delete_run_on_the_migrated_schemas(pg_library):
    """#1952 on Postgres: a colleague's move is refused until shared; a folder delete takes what it holds."""
    from axiom.extensions.builtins.library import access, folders, service
    from axiom.extensions.builtins.library.book import service as book
    from axiom.extensions.builtins.library.db_models import LibraryItem
    from axiom.extensions.builtins.library.scope import make_caller
    from axiom.extensions.builtins.library.store import session_scope
    from axiom.governance.access import decision, sharing

    alice, bob = make_caller("@alice:alpha", "alpha"), make_caller("@bob:alpha", "alpha")
    with session_scope() as s:
        path = book.create_folder(s, "alpha", alice.principal, "Project")["path"]
        s.commit()
    item = service.upload(alice, "seals.md", DOC, folder=path).item
    with pytest.raises(service.Forbidden):
        service.move_item(bob, item["id"], "documents")
    with session_scope() as s:
        obj = access.item_object(s.get(LibraryItem, item["id"]), access.user_folders(s, "alpha"))
    sharing.share(decision.who(alice.principal, "alpha"), obj, bob.principal, "editor")
    assert service.move_item(bob, item["id"], "documents")["folder"] == "documents"
    with pytest.raises(service.NotFound):  # Editor on the file is not a role on Alice's folder
        service.move_item(bob, item["id"], path)
    service.move_item(alice, item["id"], path)

    gone = folders.delete(alice, path)
    assert gone["deleted_items"] == 1
    assert folders.restore(alice, gone["undo"])["restored_items"] == 1
    assert service.get_item(alice, item["id"])["deleted_at"] is None
