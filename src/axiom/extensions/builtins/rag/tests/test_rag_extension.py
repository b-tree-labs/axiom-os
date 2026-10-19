# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the neut rag extension and RAG subsystem.

Unit tests that do not require a real PostgreSQL connection.
Integration tests (marked integration) require DATABASE_URL.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Extension manifest
# ---------------------------------------------------------------------------

def test_extension_manifest_exists():
    manifest = (
        Path(__file__).parents[1] / "axiom-extension.toml"
    )
    assert manifest.exists(), "axiom-extension.toml not found"


def test_extension_manifest_valid():
    manifest = Path(__file__).parents[1] / "axiom-extension.toml"
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib  # type: ignore[no-redef]
    with open(manifest, "rb") as f:
        data = tomllib.load(f)
    assert data["extension"]["name"] == "rag"
    # AEOS spec moved `kind` and `noun` to per-provides entries (§4/§5).
    provides = data["extension"]["provides"]
    cmd_blocks = [p for p in provides if p.get("kind") == "cmd"]
    assert cmd_blocks, "expected at least one [[extension.provides]] with kind=cmd"
    assert any(p.get("noun") == "rag" for p in cmd_blocks)


# ---------------------------------------------------------------------------
# store.py — unit tests (no DB required)
# ---------------------------------------------------------------------------

def test_corpus_constants():
    from axiom.rag.store import ALL_CORPORA, CORPUS_COMMUNITY, CORPUS_INTERNAL, CORPUS_ORG

    assert CORPUS_COMMUNITY == "rag-community"
    assert CORPUS_ORG == "rag-org"
    assert CORPUS_INTERNAL == "rag-internal"
    assert set(ALL_CORPORA) == {CORPUS_COMMUNITY, CORPUS_ORG, CORPUS_INTERNAL}


def test_search_result_has_corpus_field():
    from axiom.rag.store import SearchResult

    r = SearchResult(
        source_path="docs/foo.md",
        source_title="Foo",
        chunk_text="hello",
        chunk_index=0,
        similarity=0.9,
        combined_score=0.9,
        corpus="rag-internal",
    )
    assert r.corpus == "rag-internal"


def test_schema_sql_uses_corpus_not_tier():
    from axiom.rag.store import _SCHEMA_SQL

    assert "corpus" in _SCHEMA_SQL
    # Old tier column should not be defined as a column name in chunks table
    # (it may appear in comments)
    assert "tier TEXT" not in _SCHEMA_SQL


# ---------------------------------------------------------------------------
# ingest.py — corpus parameter propagation
# ---------------------------------------------------------------------------

def test_ingest_file_passes_corpus(tmp_path):
    from axiom.rag.store import CORPUS_ORG

    md_file = tmp_path / "test.md"
    md_file.write_text("# Hello\n\nThis is a test document for RAG ingestion.\n")

    store_mock = MagicMock()
    store_mock.get_document.return_value = None  # not cached

    with patch("axiom.rag.embeddings.embed_texts", return_value=None):
        from axiom.rag.ingest import ingest_file
        stats = ingest_file(md_file, store_mock, repo_root=tmp_path, corpus=CORPUS_ORG)

    assert stats.files_indexed == 1
    call_kwargs = store_mock.upsert_chunks.call_args
    assert call_kwargs.kwargs.get("corpus") == CORPUS_ORG or (
        len(call_kwargs.args) > 3 and call_kwargs.args[3] == CORPUS_ORG
    )


def test_ingest_skips_unchanged_file(tmp_path):
    md_file = tmp_path / "cached.md"
    md_file.write_text("# Unchanged\n\nContent.\n")

    import hashlib
    checksum = hashlib.md5(md_file.read_bytes()).hexdigest()

    store_mock = MagicMock()
    store_mock.get_document.return_value = {"checksum": checksum, "chunk_count": 1}

    from axiom.rag.ingest import ingest_file
    stats = ingest_file(md_file, store_mock, repo_root=tmp_path)

    assert stats.files_skipped == 1
    store_mock.upsert_chunks.assert_not_called()


# ---------------------------------------------------------------------------
# cli.py — argument parsing (no DB, no side effects)
# ---------------------------------------------------------------------------

def test_cli_main_no_command_exits():
    from axiom.rag.cli import main

    with pytest.raises(SystemExit) as exc_info:
        main([])
    assert exc_info.value.code != 0


def test_cli_sync_org_prints_instructions(capsys):
    from axiom.rag import cli as rag_cli

    # Build a fake args namespace
    args = MagicMock()
    args.target = "org"
    rag_cli.cmd_sync(args)
    out = capsys.readouterr().out
    assert "rsync" in out
    assert "org-corpus" in out


def test_cli_status_calls_store_stats():
    from axiom.rag import cli as rag_cli

    fake_store = MagicMock()
    fake_store.stats.return_value = {
        "total_documents": 5,
        "total_chunks": 42,
        "chunks_by_corpus": {"rag-internal": 42},
        "documents_by_corpus": {"rag-internal": 5},
    }

    with patch.object(rag_cli, "_get_store", return_value=fake_store):
        args = MagicMock()
        rag_cli.cmd_status(args)

    fake_store.stats.assert_called_once()


# ---------------------------------------------------------------------------
# Settings — rag.database_url default
# ---------------------------------------------------------------------------

def test_settings_rag_database_url_default():
    from axiom.extensions.builtins.settings.store import _DEFAULTS

    assert "rag.database_url" in _DEFAULTS
    assert _DEFAULTS["rag.database_url"] == ""


# ---------------------------------------------------------------------------
# Chat agent — RAG context wiring
# ---------------------------------------------------------------------------

def test_neut_agent_rag_context_no_url(monkeypatch):
    """ChatAgent._rag_context returns '' when no database_url configured."""
    from axiom.extensions.builtins.chat.agent import ChatAgent

    monkeypatch.setenv("DATABASE_URL", "")
    agent = ChatAgent.__new__(ChatAgent)
    agent._rag_store = None
    agent._rag_init_attempted = False

    with patch(
        "axiom.extensions.builtins.settings.store.SettingsStore.get",
        return_value="",
    ):
        result = agent._rag_context("cache invalidation in distributed systems")

    assert result == ""


def test_neut_agent_rag_context_returns_formatted_chunks():
    """ChatAgent._rag_context formats results correctly when store is available."""
    from axiom.extensions.builtins.chat.agent import ChatAgent
    from axiom.rag.store import SearchResult

    agent = ChatAgent.__new__(ChatAgent)
    agent._rag_init_attempted = True  # skip lazy init
    agent._rag_store = MagicMock()
    agent._rag_store.search.return_value = [
        SearchResult(
            source_path="docs/specs/caching.md",
            source_title="Cache Invalidation",
            chunk_text="Cache coherence is critical for distributed systems consistency.",
            chunk_index=0,
            similarity=0.92,
            combined_score=0.92,
            corpus="rag-community",
        )
    ]

    result = agent._rag_context("cache invalidation")

    # T0-1: header is now "## Retrieved Context" + stable [C<n>] markers.
    assert "coherence" in result
    assert "rag-community" in result
    assert "retrieved context" in result.lower()
    assert "[C1]" in result
    # _last_retrieved must carry the citation-key mapping.
    assert agent._last_retrieved[0].citation_key == "C1"
