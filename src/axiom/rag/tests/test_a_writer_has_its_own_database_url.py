# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A RAG writer has its own database URL; a reader never needs to change the schema (#2100).

#2010 gave every CLI name one settings store, so a node that kept a read-only serving role in one
store and the owner role in another now has one ``rag.database_url``. Serving stays read-only, as it
should, but every CLI command that touches the store failed with ``permission denied for schema
public``: the readers because they ran schema DDL on connect, and the writers because they had
nowhere to find the owner role.

- 2100-A1: ``rag.write_database_url`` is the writers' URL; ``rag.database_url`` stays the read path,
  and ``DATABASE_URL`` stays the highest-precedence override for both.
- 2100-A2: ingest, reindex, relabel and schema upgrades open the writer's URL.
- 2100-A3: the read-only CLI commands (``rag search``, ``rag generations``, ``rag status``) open the
  store with ``ensure_schema=False``, so a SELECT-only role can run them.
- 2100-A4: refused schema DDL is said in one sentence naming the setting, not as the raw psycopg error.
- 2100-A5: ``doctor`` reports a store schema that is behind the installed code.
"""

from __future__ import annotations

import argparse
import uuid

import pytest

from axiom.rag import store_factory

OWNER = "postgresql://owner@db.example.org/rag"
READER = "postgresql://reader@db.example.org/rag"
OVERRIDE = "postgresql://override@db.example.org/rag"


class _Settings:
    def __init__(self, values: dict) -> None:
        self._values = values

    def get(self, key, default=None):
        return self._values.get(key, default)


_DB_ENV = ("DATABASE_URL", "DP1_RAG_DSN", "AXIOM_DB_URL")


@pytest.fixture(autouse=True, scope="module")
def _no_test_here_leaves_a_database_url_behind():
    """``cli._load_env`` writes ``os.environ`` itself. A URL left there reaches every later test in
    the process: the archive status tests then asked db.example.org for their compression policy.
    Module scope, so the check runs after every test's monkeypatch has been undone."""
    import os

    before = {name: os.environ.get(name) for name in _DB_ENV}
    yield
    assert {name: os.environ.get(name) for name in _DB_ENV} == before


@pytest.fixture
def settings(monkeypatch):
    values: dict = {"rag.database_url": READER}
    monkeypatch.setattr(
        "axiom.extensions.builtins.settings.store.SettingsStore", lambda: _Settings(values)
    )
    for name in _DB_ENV:
        monkeypatch.delenv(name, raising=False)
    # cli._load_env assigns os.environ and a module global directly, which monkeypatch does not see
    # (delenv does not track a variable that was unset). Give it a copy to write to.
    import os

    from axiom.rag import cli

    monkeypatch.setattr(os, "environ", dict(os.environ))
    monkeypatch.setattr(cli, "_operator_database_url", cli._operator_database_url)
    monkeypatch.setattr("axiom.infra.serving_mode.resolve_mode", lambda: None)
    return values


# ---------------------------------------------------------------------------
# 2100-A1
# ---------------------------------------------------------------------------


def test_2100_a1_writers_use_the_write_setting_and_readers_the_read_setting(settings):
    """2100-A1: two settings, two paths."""
    settings["rag.write_database_url"] = OWNER

    assert store_factory.resolve_write_url().url == OWNER
    assert store_factory.resolve_write_url().origin == "rag.write_database_url setting"
    assert store_factory.resolve_store_url().url == READER


def test_2100_a1_with_no_write_setting_a_writer_uses_the_read_path(settings):
    """2100-A1: a node with one role for both keeps working as before."""
    assert store_factory.resolve_write_url().url == READER


def test_2100_a1_database_url_overrides_both(settings, monkeypatch):
    """2100-A1: ``DATABASE_URL`` stays the highest-precedence override, for writers too."""
    settings["rag.write_database_url"] = OWNER
    monkeypatch.setenv("DATABASE_URL", OVERRIDE)

    assert store_factory.resolve_write_url().url == OVERRIDE
    assert store_factory.resolve_store_url().url == OVERRIDE


# ---------------------------------------------------------------------------
# 2100-A2
# ---------------------------------------------------------------------------


def test_2100_a2_data_platform_writers_resolve_the_write_setting(settings, monkeypatch):
    """2100-A2: reindex, relabel, ingest and embedding-model --apply resolve the writer's DSN."""
    from axiom.extensions.builtins.data_platform._dsn import resolve_write_dsn

    settings["rag.write_database_url"] = OWNER
    monkeypatch.setenv("AXIOM_DB_URL", "postgresql://platform@db.example.org/axiom")

    assert resolve_write_dsn({}) == OWNER, "the platform's own URL beat the write setting"
    assert resolve_write_dsn({"dsn": "postgresql://explicit@h/db"}) == "postgresql://explicit@h/db"
    monkeypatch.setenv("DATABASE_URL", OVERRIDE)
    assert resolve_write_dsn({}) == OVERRIDE, "DATABASE_URL is the operator's override"
    monkeypatch.setenv("DP1_RAG_DSN", READER)
    assert resolve_write_dsn({}) == READER, "a connector's own variable still wins, as for every skill"


@pytest.mark.parametrize(
    "rel",
    [
        "extensions/builtins/data_platform/skills/reindex.py",
        "extensions/builtins/data_platform/skills/relabel.py",
        "extensions/builtins/data_platform/skills/embedding_model.py",
        "extensions/builtins/data_platform/skills/backfill_urls.py",
        "extensions/builtins/data_platform/agents/plinth/skills/run_ingest.py",
    ],
)
def test_2100_a2_each_rag_writer_skill_opens_the_writer_dsn(rel):
    """2100-A2: no RAG-writing skill resolves its DSN through the read-or-anything resolver."""
    from pathlib import Path

    text = (Path(__file__).resolve().parents[2] / rel).read_text(encoding="utf-8")
    assert "resolve_write_dsn" in text, f"{rel} does not open the writer's DSN"
    assert "import resolve_dsn" not in text, f"{rel} still resolves its DSN with resolve_dsn"


def _rag_cli(monkeypatch, settings, *, user_database_url: str | None = None):
    """The rag CLI with its store factory recorded; nothing connects."""
    from axiom.rag import cli

    opened: list[tuple[str, bool]] = []

    class _Store:
        def connect(self):
            pass

        def close(self):
            pass

        def generation_report(self):
            return []

        def stats(self):
            return {"total_documents": 0, "total_chunks": 0, "documents_by_corpus": {}, "chunks_by_corpus": {}}

    def _create(url, *, ensure_schema=True):
        opened.append((url, ensure_schema))
        return _Store()

    monkeypatch.setattr(store_factory, "create_store", _create)
    monkeypatch.setattr(cli, "_first_reachable_url", lambda: "")
    monkeypatch.setattr(cli, "_print_storage", lambda store: None, raising=False)
    if user_database_url:
        monkeypatch.setenv("DATABASE_URL", user_database_url)
    cli._load_env()
    return cli, opened


def test_2100_a2_rag_cli_writers_open_the_write_url_with_the_schema_ensured(settings, monkeypatch):
    """2100-A2: a writing ``axi rag`` command opens ``rag.write_database_url`` and ensures the schema."""
    settings["rag.write_database_url"] = OWNER
    cli, opened = _rag_cli(monkeypatch, settings)

    cli._get_store()

    assert opened == [(OWNER, True)]


def test_2100_a2_rag_cli_database_url_still_wins(settings, monkeypatch):
    """2100-A2: an operator's ``DATABASE_URL`` is used by writers and readers alike."""
    settings["rag.write_database_url"] = OWNER
    cli, opened = _rag_cli(monkeypatch, settings, user_database_url=OVERRIDE)

    cli._get_store()
    cli._get_store(write=False)

    assert [u for u, _ in opened] == [OVERRIDE, OVERRIDE]


# ---------------------------------------------------------------------------
# 2100-A3
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("command", ["search", "generations", "status"])
def test_2100_a3_read_only_rag_commands_never_ensure_the_schema(settings, monkeypatch, command):
    """2100-A3: the read path, with ``ensure_schema=False``, even when a write URL is set."""
    settings["rag.write_database_url"] = OWNER
    cli, opened = _rag_cli(monkeypatch, settings)
    from axiom.rag.embeddings import QueryEmbedding

    # The real type, so a field retrieval reads later (keyword_only came with #2113) is there.
    monkeypatch.setattr("axiom.rag.embeddings.embed_query", lambda *a, **k: QueryEmbedding(vector=None))
    monkeypatch.setattr("axiom.rag.retriever.search_with_fallback", lambda *a, **k: [])
    # `rag search` ranks through retrieve_for (#1984); this test is about which URL is opened.
    from axiom.rag.retriever import Retrieval

    monkeypatch.setattr("axiom.rag.retriever.retrieve_for", lambda *a, **k: Retrieval(chunks=[], hybrid=False))

    handler = {"search": cli.cmd_search, "generations": cli.cmd_generations, "status": cli.cmd_status}[command]
    handler(argparse.Namespace(query="core loading", limit=3, json=True))

    assert opened == [(READER, False)], opened


# ---------------------------------------------------------------------------
# 2100-A4 and 2100-A5: against a real Postgres, with a SELECT-only role as on the motivating node
# ---------------------------------------------------------------------------


@pytest.fixture
def owner_and_reader(fresh_postgres):
    """A store whose tables the test's own role owns, and a role that may only SELECT them.

    The reader is reached with ``options=-c role=...`` on the owner's DSN, so no password is made.
    """
    import psycopg2

    from axiom.rag.store import RAGStore

    owner = fresh_postgres.replace("postgresql+psycopg2://", "postgresql://", 1)
    s = RAGStore(owner)
    s.connect()
    s.close()
    role = f"rag_ro_{uuid.uuid4().hex[:8]}"
    conn = psycopg2.connect(owner)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f"CREATE ROLE {role} NOLOGIN")
        cur.execute(f"GRANT USAGE ON SCHEMA public TO {role}")
        cur.execute(f"GRANT SELECT ON ALL TABLES IN SCHEMA public TO {role}")
        cur.execute(f"REVOKE CREATE ON SCHEMA public FROM PUBLIC, {role}")
    sep = "&" if "?" in owner else "?"
    reader = f"{owner}{sep}options=-c%20role%3D{role}"
    yield owner, reader, conn
    with conn.cursor() as cur:
        cur.execute(f"REASSIGN OWNED BY {role} TO CURRENT_USER")
        cur.execute(f"DROP OWNED BY {role}")
        cur.execute(f"DROP ROLE {role}")
    conn.close()


def test_2100_a4_refused_schema_ddl_names_the_setting_in_one_sentence(owner_and_reader):
    """2100-A4: a SELECT-only role asked to ensure the schema gets one sentence and one fix."""
    from axiom.rag.store import RAGStore, SchemaChangeRefused

    _owner, reader, _conn = owner_and_reader

    with pytest.raises(SchemaChangeRefused) as caught:
        RAGStore(reader).connect()

    message = str(caught.value)
    assert "rag.write_database_url" in message, message
    assert "\n" not in message.strip(), message
    assert "psycopg2" not in message and "Traceback" not in message


def test_2100_a4_the_cli_prints_the_sentence_not_a_traceback(owner_and_reader, settings, monkeypatch, capsys):
    """2100-A4: ``axi rag index`` with a read-only role exits with the sentence."""
    from axiom.rag import cli

    _owner, reader, _conn = owner_and_reader
    monkeypatch.setenv("DATABASE_URL", reader)

    with pytest.raises(SystemExit) as exited:
        cli.main(["reindex", "--corpus", "rag-internal"])

    assert exited.value.code == 1
    err = capsys.readouterr().err
    assert "rag.write_database_url" in err and "Traceback" not in err, err


def test_2100_a3_a_select_only_role_runs_the_read_commands(owner_and_reader, settings, monkeypatch, capsys):
    """2100-A3, for real: ``rag generations`` and ``rag status`` work under a SELECT-only role."""
    from axiom.rag import cli

    _owner, reader, _conn = owner_and_reader
    settings["rag.database_url"] = reader
    monkeypatch.setattr(cli, "_first_reachable_url", lambda: "")
    cli._load_env()

    cli.cmd_generations(argparse.Namespace(json=True))
    cli.cmd_status(argparse.Namespace())

    assert "RAG Index Status" in capsys.readouterr().out


def test_2100_a5_doctor_reports_a_schema_behind_the_installed_code(owner_and_reader):
    """2100-A5: the store lacks ``documents.parser`` (#2071); doctor says so and names the fix."""
    from axiom.rag.ingest import schema_doctor_rows
    from axiom.rag.store import RAGStore

    _owner, reader, conn = owner_and_reader
    with conn.cursor() as cur:
        cur.execute("ALTER TABLE documents DROP COLUMN parser")

    store = RAGStore(reader, ensure_schema=False)
    store.connect()
    try:
        assert store.schema_behind() == {"documents": ["parser"]}
        rows = schema_doctor_rows(store)
    finally:
        store.close()

    [row] = rows
    assert "documents.parser" in row["status"], row
    assert "rag.write_database_url" in row["fix"], row


def test_2100_a5_a_current_schema_reports_nothing(owner_and_reader):
    """2100-A5, no noise: a store at the installed schema has no doctor row."""
    from axiom.rag.ingest import schema_doctor_rows
    from axiom.rag.store import RAGStore

    _owner, reader, _conn = owner_and_reader
    store = RAGStore(reader, ensure_schema=False)
    store.connect()
    try:
        assert store.schema_behind() == {}
        assert schema_doctor_rows(store) == []
    finally:
        store.close()


def test_2100_a4_data_reindex_returns_the_sentence(owner_and_reader):
    """2100-A4: ``axi data reindex`` under a SELECT-only role fails with the sentence, not a traceback."""
    from axiom.extensions.builtins.data_platform.skills import reindex

    _owner, reader, _conn = owner_and_reader

    res = reindex.run({"awaiting": True, "corpus": "rag-internal", "dsn": reader}, None)

    assert not res.ok
    assert any("rag.write_database_url" in e for e in res.errors), res.errors
