# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The RAG CLI must reach every store the factory supports.

Sandbox audit 2026-09-18, gap 4: `axi search` handed the configured URL
straight to psycopg2, so a `sqlite:///` URL — the shipped zero-infra
backend — died with `invalid dsn`, and the "no database" error offered
only `postgresql://`.
"""

from __future__ import annotations

import argparse

import pytest

from axiom.rag import cli as rag_cli
from axiom.rag.sqlite_store import SQLiteRAGStore


class TestGetStoreRoutesThroughFactory:
    def test_sqlite_url_returns_sqlite_store(self, tmp_path, monkeypatch):
        db = tmp_path / "rag.db"
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db}")
        store = rag_cli._get_store()
        try:
            assert isinstance(store, SQLiteRAGStore)
        finally:
            store.close()

    def test_search_works_end_to_end_on_sqlite(self, tmp_path, monkeypatch, capsys):
        """`axi search` round-trips against the zero-infra SQLite store."""
        db = tmp_path / "rag.db"
        url = f"sqlite:///{db}"
        monkeypatch.setenv("DATABASE_URL", url)

        from axiom.rag.chunker import Chunk

        seed = SQLiteRAGStore(url)
        seed.connect()
        seed.upsert_chunks(
            [
                Chunk(
                    source_path="notes/hello.md",
                    source_title="Hello",
                    source_type="md",
                    text="the sandbox stranger path now works",
                    chunk_index=0,
                    start_line=1,
                )
            ],
            corpus="rag-internal",
        )
        seed.close()

        args = argparse.Namespace(query="sandbox stranger", limit=5)
        monkeypatch.setattr(
            rag_cli, "_load_env", lambda: None, raising=False
        )
        # No embedding model in a bare install — text-only search.
        import axiom.rag.embeddings as embeddings_mod

        monkeypatch.setattr(
            embeddings_mod,
            "embed_texts",
            lambda texts: (_ for _ in ()).throw(RuntimeError("no embedder")),
        )
        rag_cli.cmd_search(args)
        out = capsys.readouterr().out
        assert "stranger path" in out

    def test_missing_url_error_offers_sqlite(self, monkeypatch, capsys):
        monkeypatch.delenv("DATABASE_URL", raising=False)
        with pytest.raises(SystemExit) as exc:
            rag_cli._get_store()
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "sqlite:///" in err
        assert "postgresql://" in err


class TestSQLitePathExpansion:
    def test_tilde_in_sqlite_url_is_expanded(self, monkeypatch, tmp_path):
        """A `sqlite:///~/...` URL must land in $HOME, not a literal `~` dir."""
        monkeypatch.setenv("HOME", str(tmp_path))
        store = SQLiteRAGStore("sqlite:///~/.axi/rag.db")
        assert "~" not in store._db_path
        assert store._db_path.startswith(str(tmp_path))
