# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Routing build plan, Phase 0 acceptance (docs/working/routing-build-plan-2026-11-12.md, section 3).

P0.2, embeddings pass the guard (RS-E2, review finding F2):

- P0.2-A1: with a controlled label, ``embed_texts`` does not call a hosted provider even when its
  key is set. Chat's ``_rag_context`` in a session holding controlled content is the same leak on
  the query side.
- P0.2-A2: with the node's agent policy set to ``none`` or ``assist``, no embedding leaves the
  machine. ``local`` is the permissive default every general install has, so it is not this case.
- P0.2-A3: retrieval falls back to lexical, with a visible note, when no embedder is admitted.

The network is never reached: ``requests.post`` records the URL it was asked for, and the local
model runtime is a recorder.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import axiom.rag.embeddings as emb

_HOSTED = "https://api.openai.com/v1/embeddings"
_REMOTE = "https://embed.example.internal:42000"


class _Resp:
    status_code = 200
    text = ""

    def __init__(self, n: int):
        self._n = n

    def json(self):
        return {"data": [{"index": i, "embedding": [0.5] * 768} for i in range(self._n)]}


@pytest.fixture
def egress(monkeypatch, tmp_path: Path):
    """Record every embedding request; nothing leaves the test.

    Returns ``(posted_urls, local_calls)``. A fresh config directory holds no providers file
    unless a test writes one, and the node file declares nothing unless a test writes one.
    """
    import requests

    posted: list[str] = []
    local: list[list[str]] = []

    def _post(url, *a, json=None, **k):  # noqa: A002 - mirrors requests.post
        posted.append(url)
        return _Resp(len((json or {}).get("input") or [1]))

    monkeypatch.setattr(requests, "post", _post)

    def _ollama(texts):
        local.append(list(texts))
        return None  # the local runtime is not running

    monkeypatch.setattr(emb, "_embed_ollama", _ollama)
    monkeypatch.setattr(emb, "_ollama_reachable", lambda: False)
    monkeypatch.setattr(emb, "_openai_quota_blocked", False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
    monkeypatch.delenv("AXIOM_RAG_EMBED_URL", raising=False)
    monkeypatch.delenv("NEUT_RAG_EMBED_URL", raising=False)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "llm-providers.toml").write_text("[gateway]\n", encoding="utf-8")
    monkeypatch.setenv("AXIOM_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("AXIOM_NODE_CONFIG", str(tmp_path / "node.toml"))
    return posted, local


def _providers(tmp_path: Path, body: str) -> None:
    (tmp_path / "config" / "llm-providers.toml").write_text("[gateway]\n" + body, encoding="utf-8")


def _hosted_calls(posted: list[str]) -> list[str]:
    return [u for u in posted if u.startswith(_HOSTED)]


# ---------------------------------------------------------------------------
# P0.2-A1
# ---------------------------------------------------------------------------


def test_p0_2_a1_a_controlled_label_never_reaches_a_hosted_embedder(egress):
    """P0.2-A1: ``embed_texts`` with a controlled label, OPENAI_API_KEY set: nothing is posted to OpenAI."""
    posted, _local = egress

    out = emb.embed_texts(["HEU inventory at the facility"], routing_tier="export_controlled")

    assert _hosted_calls(posted) == [], f"controlled text was sent to a hosted embedder: {posted}"
    assert out is None  # no admitted embedder here: the caller searches by keyword


def test_p0_2_a1_a_controlled_chat_session_does_not_embed_its_question_on_a_hosted_provider(egress, monkeypatch):
    """P0.2-A1, query side: chat's ``_rag_context`` in a session that holds controlled content."""
    import axiom.rag.retriever as ret
    from axiom.extensions.builtins.chat.agent import ChatAgent
    from axiom.extensions.builtins.chat.scope import ChatScope

    posted, _local = egress
    monkeypatch.setattr(ret, "retrieve", lambda *a, **k: [])

    class _Agent:
        _render = None

        def _get_rag_store(self):
            return object()

    scope = ChatScope()
    scope.session.context["content_tier"] = "export_controlled"

    ChatAgent._rag_context(_Agent(), "what does the logbook say about the core loading", scope=scope)

    assert _hosted_calls(posted) == [], f"a controlled session's question went to a hosted embedder: {posted}"


def test_p0_2_a1_an_endpoint_the_providers_file_clears_is_used_for_controlled_text(egress, monkeypatch, tmp_path):
    """P0.2-A1: clearance is read from the gateway's ``routing_tier`` in llm-providers.toml, not a new variable."""
    posted, _local = egress
    monkeypatch.setenv("AXIOM_RAG_EMBED_URL", _REMOTE)
    _providers(
        tmp_path,
        '[[gateway.providers]]\nname = "site-embed"\nendpoint = "https://embed.example.internal:42000/v1"\n'
        'model = "nomic-embed-text"\nrouting_tier = "export_controlled"\n',
    )

    out = emb.embed_texts(["HEU inventory"], routing_tier="export_controlled")

    assert posted == [f"{_REMOTE}/v1/embeddings"]
    assert out is not None and len(out) == 1


def test_p0_2_a1_an_endpoint_declared_public_is_refused_for_controlled_text(egress, monkeypatch, tmp_path):
    """P0.2-A1: a remote endpoint the providers file declares public never sees controlled text."""
    posted, _local = egress
    monkeypatch.setenv("AXIOM_RAG_EMBED_URL", _REMOTE)
    _providers(
        tmp_path,
        '[[gateway.providers]]\nname = "site-embed"\nendpoint = "https://embed.example.internal:42000/v1"\n'
        'model = "nomic-embed-text"\nrouting_tier = "public"\n',
    )

    assert emb.embed_texts(["HEU inventory"], routing_tier="export_controlled") is None
    assert posted == []


def test_p0_2_a1_public_text_still_embeds_as_before(egress):
    """P0.2-A1, no regression: public text with a key set still embeds on the hosted provider."""
    posted, _local = egress

    out = emb.embed_texts(["the reactor's public fact sheet"])

    assert _hosted_calls(posted) == [_HOSTED]
    assert out is not None


def test_p0_2_a1_a_query_naming_a_controlled_term_is_held_back_with_no_session(egress):
    """P0.2-A1: a surface with no session (MCP, the CLI) is held to the query's own label."""
    posted, _local = egress

    q = emb.embed_query("HEU inventory at the facility")

    assert _hosted_calls(posted) == []
    assert q.vector is None


# ---------------------------------------------------------------------------
# P0.2-A2
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("policy", ["none", "assist"])
def test_p0_2_a2_a_node_that_forbids_model_calls_sends_no_embedding_off_the_machine(egress, monkeypatch, tmp_path, policy):
    """P0.2-A2: the node's agent policy forbids model calls here, so no embedding leaves."""
    posted, local = egress
    monkeypatch.setenv("AXIOM_RAG_EMBED_URL", _REMOTE)
    (tmp_path / "node.toml").write_text(f'[node]\nagents = "{policy}"\n', encoding="utf-8")

    emb.embed_texts(["the reactor's public fact sheet"])
    assert posted == [], f"an embedding left a node whose agent policy is {policy!r}: {posted}"
    emb.embed_query("the reactor's public fact sheet")
    assert posted == [], f"a query embedding left a node whose agent policy is {policy!r}: {posted}"
    assert local, "the machine's own embedder is still asked"


# ---------------------------------------------------------------------------
# P0.2-A3
# ---------------------------------------------------------------------------


def test_p0_2_a3_no_admitted_embedder_means_keyword_retrieval_with_a_note(egress):
    """P0.2-A3: ``embed_query`` says why retrieval is lexical."""
    q = emb.embed_query("core loading", routing_tier="export_controlled")

    assert q.vector is None
    assert q.note, "the fallback to keyword search is silent"
    assert "keyword" in q.note.lower()


def test_p0_2_a3_with_nothing_configured_there_is_no_note(egress, monkeypatch):
    """P0.2-A3, no noise: an install with no embedder at all searches by keyword as it always has."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    q = emb.embed_query("core loading")

    assert q.vector is None
    assert q.note == ""


def test_p0_2_a3_chat_retrieves_by_keyword_and_shows_the_note(egress, monkeypatch):
    """P0.2-A3: chat still retrieves, by keyword, and the person sees why."""
    import axiom.rag.retriever as ret
    from axiom.extensions.builtins.chat.agent import ChatAgent
    from axiom.extensions.builtins.chat.scope import ChatScope

    seen: dict = {}

    def _retrieve(store, query_text, query_embedding=None, **_k):
        seen["embedding"] = query_embedding
        seen["query_text"] = query_text
        return []

    monkeypatch.setattr(ret, "retrieve", _retrieve)
    shown: list[tuple[str, str]] = []

    class _Render:
        def render_message(self, role, content):
            shown.append((role, content))

    class _Agent:
        _render = _Render()

        def _get_rag_store(self):
            return object()

    scope = ChatScope()
    scope.session.context["content_tier"] = "export_controlled"

    ChatAgent._rag_context(_Agent(), "core loading", scope=scope)

    assert seen == {"embedding": None, "query_text": "core loading"}
    assert any("keyword" in text.lower() for _role, text in shown), shown


def test_p0_2_a3_mcp_retrieve_returns_keyword_results_with_the_note(egress, monkeypatch):
    """P0.2-A3: ``axiom_rag__retrieve`` keeps answering and carries the note."""
    import asyncio

    import axiom.rag.retriever as ret
    from axiom.extensions.builtins.mcp import platform_primitives as pp

    class _Store:
        def access_tier_for(self, _path):
            return "public"

    monkeypatch.setattr(pp, "_resolve_rag_store", lambda: _Store())
    seen: dict = {}

    def _retrieve(store, query_text, query_embedding=None, **_k):
        seen["embedding"] = query_embedding
        return []

    monkeypatch.setattr(ret, "retrieve", _retrieve)

    out = asyncio.run(pp._rag_retrieve({"query": "HEU inventory"}))

    assert seen["embedding"] is None
    assert "keyword" in (out.get("note") or "").lower(), out


# ---------------------------------------------------------------------------
# One function: every retrieval path embeds its query through ``embed_query``
# ---------------------------------------------------------------------------

_SRC = Path(__file__).resolve().parents[2] / "src" / "axiom"

#: Every place that embeds a retrieval query. A new one goes here and calls ``embed_query``.
QUERY_PATHS = (
    "extensions/builtins/chat/agent.py",  # chat's _rag_context
    "extensions/builtins/mcp/platform_primitives.py",  # axiom_rag__retrieve
    "extensions/builtins/chat/tools_ext/search_docs.py",  # search_docs
    "rag/cli.py",  # axi rag search, axi rag eval
    "memory/recall.py",  # memory recall
    "extensions/builtins/library/service.py",  # library search
    "extensions/builtins/http/federation_endpoint.py",  # a federated search
    "extensions/builtins/rag/serving.py",  # the /rag mount
)


@pytest.mark.parametrize("rel", QUERY_PATHS)
def test_p0_2_every_retrieval_path_embeds_its_query_through_one_function(rel):
    """P0.2: no query path calls ``embed_texts`` itself; each goes through ``embed_query``."""
    text = (_SRC / rel).read_text(encoding="utf-8")
    assert "embed_query" in text, f"{rel} does not embed its query through embed_query"
    assert not re.search(r"\bembed_texts\b", text), f"{rel} calls embed_texts directly, past the query guard"


# ---------------------------------------------------------------------------
# P0.2-D4 (owner's decision 4): text-only controlled chunks catch up later
# ---------------------------------------------------------------------------


@pytest.fixture
def pg_store(real_postgres):
    import uuid

    from axiom.rag.store import RAGStore

    store = RAGStore(real_postgres.replace("postgresql+psycopg2://", "postgresql://", 1))
    store.connect()
    corpus = f"test-await-{uuid.uuid4().hex[:8]}"
    yield store, corpus
    store.delete_corpus(corpus)
    store.close()


def _controlled_text_only(store, corpus: str, path: str = "controlled.md") -> None:
    from axiom.rag.chunker import Chunk

    chunks = [
        Chunk(text=f"controlled passage {i}", source_path=path, source_title="C", chunk_index=i, start_line=1)
        for i in range(3)
    ]
    store.upsert_chunks(chunks, None, corpus=corpus, access_tier="export_controlled")


def test_p0_2_d4_chunks_without_a_vector_are_counted_per_corpus(pg_store):
    """D4: the store counts the chunks still waiting for a vector, per corpus, through its own API."""
    store, corpus = pg_store
    _controlled_text_only(store, corpus)

    assert store.vectorless_chunk_counts([corpus]) == {corpus: 3}


def test_p0_2_d4_doctor_lists_the_chunks_awaiting_an_admitted_embedder(pg_store):
    """D4: doctor shows "N chunks awaiting an admitted embedder" for the corpus."""
    from axiom.rag.ingest import embedding_doctor_rows

    store, corpus = pg_store
    _controlled_text_only(store, corpus)

    rows = [r for r in embedding_doctor_rows(store) if corpus in r["name"]]

    assert rows and "3 chunks awaiting an admitted embedder" in rows[0]["status"], rows


def test_p0_2_d4_catch_up_is_a_no_op_while_no_embedder_is_admitted(pg_store, egress):
    """D4: with only a hosted embedder configured, controlled chunks stay text-only and nothing is sent."""
    from axiom.rag.ingest import embed_awaiting

    store, corpus = pg_store
    posted, _local = egress
    _controlled_text_only(store, corpus)

    out = embed_awaiting(store, corpus)

    assert out == {"embedded": 0, "waiting": 3}
    assert posted == []
    assert store.vectorless_chunk_counts([corpus]) == {corpus: 3}


def test_p0_2_d4_catch_up_embeds_once_a_cleared_embedder_exists(pg_store, egress, monkeypatch, tmp_path):
    """D4: once an embedder cleared for the label exists, the stored chunks are embedded, not re-extracted."""
    from axiom.rag.ingest import embed_awaiting

    store, corpus = pg_store
    posted, _local = egress
    _controlled_text_only(store, corpus)
    monkeypatch.setenv("AXIOM_RAG_EMBED_URL", _REMOTE)
    _providers(
        tmp_path,
        '[[gateway.providers]]\nname = "site-embed"\nendpoint = "https://embed.example.internal:42000/v1"\n'
        'model = "nomic-embed-text"\nrouting_tier = "export_controlled"\n',
    )

    out = embed_awaiting(store, corpus)

    assert out == {"embedded": 3, "waiting": 0}
    assert posted and all(u.startswith(_REMOTE) for u in posted)
    assert store.vectorless_chunk_counts([corpus]) == {}


def test_p0_2_d4_data_reindex_runs_the_catch_up_without_a_source(pg_store, egress, real_postgres):
    """D4: ``axi data reindex --awaiting --corpus C`` is the catch-up, and needs no bronze source."""
    from axiom.extensions.builtins.data_platform.skills import reindex

    store, corpus = pg_store
    _controlled_text_only(store, corpus)

    dsn = real_postgres.replace("postgresql+psycopg2://", "postgresql://", 1)
    res = reindex.run({"awaiting": True, "corpus": corpus, "dsn": dsn}, None)

    assert res.ok, res.errors
    assert res.value == {"embedded": 0, "waiting": 3}
