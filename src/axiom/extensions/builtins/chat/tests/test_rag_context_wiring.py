# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""T0-1 integration test: ChatAgent._rag_context uses the new retriever.

The body of ``_rag_context`` was swapped to call
``axiom.rag.retriever.retrieve`` + ``build_rag_context_block`` so the
model sees stable ``[C<n>]`` markers. These tests exercise the wiring
with a mock store — no DB required.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest

from axiom.extensions.builtins.chat.agent import ChatAgent
from axiom.rag.store import SearchResult


def _result(path: str, idx: int = 0) -> SearchResult:
    return SearchResult(
        source_path=path,
        source_title=path,
        chunk_text=f"body of {path}",
        chunk_index=idx,
        similarity=0.7,
        combined_score=0.7,
        corpus="rag-internal",
    )


@pytest.fixture
def agent_with_store():
    """Construct a ChatAgent and inject a mock RAG store directly."""
    # Bypass __init__'s heavier setup by making a bare instance.
    agent = ChatAgent.__new__(ChatAgent)
    agent._rag_init_attempted = True
    agent._last_retrieved = []
    store = MagicMock()
    store.search.side_effect = lambda query_embedding=None, query_text="", **_: (
        [] if query_embedding is not None else [_result("a.md"), _result("b.md"), _result("c.md")]
    )
    agent._rag_store = store
    return agent


class TestRagContextFormat:
    def test_block_has_citation_markers(self, agent_with_store):
        out = agent_with_store._rag_context("quantum", limit=3)
        assert "[C1]" in out
        assert "[C2]" in out
        assert "[C3]" in out

    def test_block_includes_cite_guidance(self, agent_with_store):
        out = agent_with_store._rag_context("quantum", limit=3)
        assert "cite" in out.lower() or "citation" in out.lower()

    def test_stashes_retrieved_for_postprocessing(self, agent_with_store):
        agent_with_store._rag_context("quantum", limit=3)
        assert len(agent_with_store._last_retrieved) == 3
        assert agent_with_store._last_retrieved[0].citation_key == "C1"


class TestEmptyCases:
    def test_no_store_returns_empty(self):
        agent = ChatAgent.__new__(ChatAgent)
        agent._rag_init_attempted = True
        agent._rag_store = None
        agent._last_retrieved = ["stale"]  # must be cleared
        assert agent._rag_context("q") == ""
        assert agent._last_retrieved == []

    def test_empty_query_returns_empty(self, agent_with_store):
        assert agent_with_store._rag_context("   ") == ""

    def test_no_results_returns_empty(self):
        agent = ChatAgent.__new__(ChatAgent)
        agent._rag_init_attempted = True
        agent._last_retrieved = []
        store = MagicMock()
        store.search.return_value = []
        agent._rag_store = store
        assert agent._rag_context("quantum") == ""


class TestLowConfidenceHint:
    def test_low_similarity_appends_hint(self):
        agent = ChatAgent.__new__(ChatAgent)
        agent._rag_init_attempted = True
        agent._last_retrieved = []
        store = MagicMock()
        weak = SearchResult(
            source_path="weak.md",
            source_title="weak.md",
            chunk_text="hardly relevant",
            chunk_index=0,
            similarity=0.05,
            combined_score=0.05,
            corpus="rag-internal",
        )
        store.search.side_effect = lambda query_embedding=None, query_text="", **_: (
            [] if query_embedding is not None else [weak]
        )
        agent._rag_store = store
        out = agent._rag_context("quantum")
        assert "[C1]" in out
        assert "Low RAG confidence" in out


# ---------------------------------------------------------------------------
# Store resolution rides the store factory (P2a).
#
# A node joined to a site points ``rag.database_url`` at the site's retrieval
# endpoint (``http(s)://``). Chat must resolve that through
# ``axiom.rag.store_factory.create_store`` so it gets the served retrieval
# store, exactly like the ``axiom_rag__retrieve`` primitive does. No
# client-side retrieval logic anywhere.
# ---------------------------------------------------------------------------


def _bare_agent() -> ChatAgent:
    agent = ChatAgent.__new__(ChatAgent)
    agent._rag_init_attempted = False
    agent._rag_store = None
    agent._last_retrieved = []
    return agent


def _settings_with(url: str):
    """A stand-in ``SettingsStore`` class that answers ``rag.database_url``."""

    class _Settings:
        def get(self, key, default=None):
            return url if key == "rag.database_url" else default

    return _Settings


class TestStoreResolution:
    def test_http_url_resolves_to_served_retrieval_store(self, monkeypatch):
        from axiom.extensions.builtins.settings import store as settings_mod
        from axiom.rag.remote_store import RemoteRetrievalStore

        monkeypatch.setattr(settings_mod, "SettingsStore", _settings_with("http://site.example/"))
        store = _bare_agent()._get_rag_store()
        assert isinstance(store, RemoteRetrievalStore)
        assert store.does_own_hybrid is True

    def test_sqlite_url_resolves_to_local_store(self, monkeypatch, tmp_path):
        from axiom.extensions.builtins.settings import store as settings_mod
        from axiom.rag.sqlite_store import SQLiteRAGStore

        monkeypatch.setattr(
            settings_mod, "SettingsStore", _settings_with(f"sqlite:///{tmp_path}/rag.db")
        )
        store = _bare_agent()._get_rag_store()
        try:
            assert isinstance(store, SQLiteRAGStore)
            assert not getattr(store, "does_own_hybrid", False)
        finally:
            store.close()

    def test_unparseable_url_degrades_to_no_context_and_logs(self, monkeypatch, caplog):
        from axiom.extensions.builtins.settings import store as settings_mod

        monkeypatch.setattr(settings_mod, "SettingsStore", _settings_with("not-a-store-url"))
        agent = _bare_agent()
        with caplog.at_level(logging.WARNING, logger="axiom.extensions.builtins.chat.agent"):
            assert agent._get_rag_store() is None
            assert agent._rag_context("quantum") == ""
        assert agent._last_retrieved == []
        assert any("rag" in rec.getMessage().lower() for rec in caplog.records), (
            "a bad rag.database_url must be logged, not swallowed silently"
        )


class TestServedRetrieval:
    """Against a served store the query goes to the site as text, once."""

    def test_remote_store_gets_one_text_query_and_no_local_embedding(self, monkeypatch):
        import axiom.rag.embeddings as emb
        import axiom.rag.remote_store as remote
        from axiom.extensions.builtins.settings import store as settings_mod

        monkeypatch.setattr(settings_mod, "SettingsStore", _settings_with("http://site.example/"))

        embed_calls: list[list[str]] = []

        def _no_embed(texts):
            embed_calls.append(texts)
            raise AssertionError("chat must not embed locally against a served store")

        monkeypatch.setattr(emb, "embed_texts", _no_embed)

        posts: list[tuple[str, dict]] = []

        def _fake_post(url, payload, headers, timeout):
            posts.append((url, payload))
            return 200, {
                "results": [
                    {
                        "source_path": "a.md",
                        "source_title": "a.md",
                        "chunk_text": "body of a.md",
                        "chunk_index": 0,
                        "similarity": 0.8,
                        "corpus": "rag-community",
                    },
                    {
                        "source_path": "b.md",
                        "source_title": "b.md",
                        "chunk_text": "body of b.md",
                        "chunk_index": 0,
                        "similarity": 0.6,
                        "corpus": "rag-community",
                    },
                ]
            }

        monkeypatch.setattr(remote, "_post_json", _fake_post)

        agent = _bare_agent()
        out = agent._rag_context("quantum", limit=2)

        assert embed_calls == []
        assert len(posts) == 1, "served retrieval is one round-trip, not vector + text"
        url, payload = posts[0]
        assert url == "http://site.example/api/v1/rag/search"
        assert payload["query"] == "quantum"
        assert "query_embedding" not in payload
        assert payload["limit"] >= 2
        assert "[C1]" in out and "[C2]" in out
        assert "body of a.md" in out and "body of b.md" in out
        assert "Low RAG confidence" not in out
        assert [c.citation_key for c in agent._last_retrieved] == ["C1", "C2"]

    def test_remote_failure_degrades_to_no_context(self, monkeypatch):
        import axiom.rag.remote_store as remote
        from axiom.extensions.builtins.settings import store as settings_mod

        monkeypatch.setattr(settings_mod, "SettingsStore", _settings_with("http://site.example/"))
        monkeypatch.setattr(remote, "_post_json", lambda *a, **k: (503, {"error": "down"}))
        agent = _bare_agent()
        assert agent._rag_context("quantum") == ""
        assert agent._last_retrieved == []


class TestSessionIndexStore:
    """The per-turn session indexer resolves its store the same way."""

    @pytest.fixture
    def sync_threads(self, monkeypatch):
        """Run the indexer's daemon thread inline so the test can observe it."""
        from types import SimpleNamespace

        from axiom.extensions.builtins.chat import agent as agent_mod

        class _SyncThread:
            def __init__(self, target, daemon=False):
                self._target = target

            def start(self):
                self._target()

        monkeypatch.setattr(agent_mod, "threading", SimpleNamespace(Thread=_SyncThread))

    @pytest.fixture
    def session_file(self, monkeypatch, tmp_path):
        from types import SimpleNamespace

        from axiom.extensions.builtins.chat import agent as agent_mod

        monkeypatch.setattr(agent_mod, "_REPO_ROOT", tmp_path)
        sessions = tmp_path / "runtime" / "sessions"
        sessions.mkdir(parents=True)
        (sessions / "s1.json").write_text("{}")
        agent = ChatAgent.__new__(ChatAgent)
        agent.session = SimpleNamespace(session_id="s1")
        return agent

    def test_served_store_is_not_indexed_into(self, monkeypatch, sync_threads, session_file):
        import axiom.rag.personal as personal
        from axiom.extensions.builtins.settings import store as settings_mod

        monkeypatch.setattr(settings_mod, "SettingsStore", _settings_with("http://site.example/"))
        calls = []
        monkeypatch.setattr(personal, "ingest_session_file", lambda *a, **k: calls.append(a))
        session_file._schedule_session_index()
        assert calls == [], "a served (read-only) store has no local corpus to index into"

    def test_local_store_is_indexed_through_factory(
        self, monkeypatch, sync_threads, session_file, tmp_path
    ):
        import axiom.rag.personal as personal
        from axiom.extensions.builtins.settings import store as settings_mod
        from axiom.rag.sqlite_store import SQLiteRAGStore

        monkeypatch.setattr(
            settings_mod, "SettingsStore", _settings_with(f"sqlite:///{tmp_path}/rag.db")
        )
        calls = []
        monkeypatch.setattr(
            personal, "ingest_session_file", lambda path, store, **k: calls.append(store)
        )
        session_file._schedule_session_index()
        assert len(calls) == 1
        assert isinstance(calls[0], SQLiteRAGStore)
