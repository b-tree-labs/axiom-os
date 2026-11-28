# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Every retrieval surface ranks documents through ``retrieve()``, and they agree (#1984).

A ranking fix used to land on one surface only. ``retrieve()`` carries the ranking (fusion,
the rerank, the site's ranking profile, named documents, neighbours), the generation each
corpus serves, the access filter and the embedding-model check; a surface that calls
``store.search`` itself gets none of them and silently disagrees with the others.

Acceptance rows:

- ONE-1: ``search_docs`` retrieves through ``retrieve()``, with the conversation's principal,
  corpora and label, and keeps its output format.
- ONE-2 (static): no retrieval surface ranks documents without ``retrieve()``.
- ONE-3 (behaviour): for the same question, principal and store, every surface returns the same
  ranked list; and none returns a document the principal may not read.

The behavioural rows run on a real Postgres store (the production store) holding the public
sample (``axiom.rag.eval_sample``).
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import math
import pathlib
import re
import uuid

import pytest
import yaml

SRC = pathlib.Path(__file__).resolve().parents[2]

# ---------------------------------------------------------------------------
# ONE-2: the static guard
# ---------------------------------------------------------------------------

#: Each retrieval surface, by the module that implements it.
SURFACES = {
    "chat _rag_context (the web app and terminal chat)": "extensions/builtins/chat/agent.py",
    "search_docs (the chat tool)": "extensions/builtins/chat/tools_ext/search_docs.py",
    "axiom_rag__retrieve (MCP)": "extensions/builtins/mcp/platform_primitives.py",
    "axi rag search and axi rag eval": "rag/cli.py",
}

#: Calls that rank documents. Only ``retrieve()`` may make them.
RANKING_CALLS = {"search_with_fallback", "reciprocal_rank_fusion", "rerank_score", "name_ranking",
                 "best_chunks_in", "chunks_at", "hybrid_search"}

#: Calls that are the one path.
ONE_PATH = {"retrieve", "retrieve_for"}


def _calls(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
            receiver = ast.unparse(f.value) if isinstance(f, ast.Attribute) else ""
            yield node, name, receiver


def _store_searches(tree: ast.AST) -> list[str]:
    """``<something store>.search(...)``: a document store searched directly."""
    return [f"{recv}.search (line {node.lineno})" for node, name, recv in _calls(tree)
            if name == "search" and re.search(r"store", recv, re.IGNORECASE)]


@pytest.mark.parametrize("surface", sorted(SURFACES))
def test_one_2_no_surface_ranks_documents_without_retrieve(surface):
    """ONE-2: a surface reaches ``retrieve()`` and makes no ranking call of its own."""
    tree = ast.parse((SRC / SURFACES[surface]).read_text(encoding="utf-8"))
    names = [name for _, name, _ in _calls(tree)]
    assert ONE_PATH & set(names), f"{surface} never calls retrieve()"
    bypass = sorted(set(names) & RANKING_CALLS) + _store_searches(tree)
    assert not bypass, f"{surface} ranks documents without retrieve(): {bypass}"


def test_one_2_the_v1_chat_agent_retrieves_through_the_chat_agent():
    """ONE-2: the ``/v1`` surface builds a ChatAgent (whose ``_rag_context`` is guarded above)
    with a serving scope, and searches no store itself."""
    tree = ast.parse((SRC / "extensions/builtins/http/chat_server.py").read_text(encoding="utf-8"))
    names = {name for _, name, _ in _calls(tree)}
    assert {"ChatAgent", "serving_scope"} <= names
    assert not (names & RANKING_CALLS) and not _store_searches(tree)


def test_one_2_memory_recall_reads_only_the_principals_memory_corpus():
    """ONE-2: memory recall ranks memory fragments, not documents. It is exempt from
    ``retrieve()`` only while every search it makes is confined to the principal's own
    memory corpus (``recall_corpus_for``); a search of any other corpus would be a document
    retrieval past the one path."""
    text = (SRC / "memory/recall.py").read_text(encoding="utf-8")
    tree = ast.parse(text)
    searches = [node for node, name, recv in _calls(tree) if name == "search" and "store" in recv]
    assert searches, "memory recall no longer searches a store; drop this exemption"
    for node in searches:
        corpora = next((kw.value for kw in node.keywords if kw.arg == "corpora"), None)
        assert corpora is not None and ast.unparse(corpora) == "[corpus]", (
            f"memory recall searches beyond its memory corpus (line {node.lineno})")
    assert "corpus = recall_corpus_for(principal)" in text


# ---------------------------------------------------------------------------
# ONE-3: the surfaces agree on a real store
# ---------------------------------------------------------------------------

PRINCIPAL = "@reader:sample"
K = 5
DIM = 768


def _vector(text: str) -> list[float]:
    """A deterministic bag-of-words vector: the same text always embeds the same."""
    v = [0.0] * DIM
    for word in re.findall(r"[a-z0-9]+", text.lower()):
        v[int(hashlib.sha1(word.encode()).hexdigest(), 16) % DIM] += 1.0
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def _embed(texts, *_a, routing_tier="any", **_k):
    return [_vector(t) for t in texts], "sample-hash"


class _Shared:
    """The one store, handed to every surface; a surface's ``close()`` leaves it open."""

    def __init__(self, store):
        self._store = store

    def __getattr__(self, name):
        return getattr(self._store, name)

    def close(self):
        pass


@pytest.fixture(scope="module")
def sample_store(_scratch_postgres_url):
    import psycopg2

    from axiom.rag import eval_sample
    from axiom.rag.store import RAGStore

    dsn = _scratch_postgres_url.replace("postgresql+psycopg2://", "postgresql://", 1)
    db = "rag_onepath_" + uuid.uuid4().hex[:8]
    admin = psycopg2.connect(dsn)
    admin.autocommit = True
    admin.cursor().execute(f"CREATE DATABASE {db}")
    url = dsn.rsplit("/", 1)[0] + "/" + db
    with psycopg2.connect(url) as cn, cn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
    store = RAGStore(url)
    store.connect()
    eval_sample.load_corpus(store, embed=lambda texts: _embed(texts)[0], embedding_model="sample-hash")
    try:
        yield _Shared(store)
    finally:
        store.close()
        admin.cursor().execute(f"DROP DATABASE {db} WITH (FORCE)")
        admin.close()


@pytest.fixture
def one_node(monkeypatch, sample_store):
    """Every surface sees the same store, principal and embedder, and no site profile."""
    import axiom.rag.embeddings as emb

    monkeypatch.setattr(emb, "embed_texts_with_model", _embed)
    monkeypatch.delenv("AXIOM_RAG_RANKING", raising=False)
    monkeypatch.delenv("AXIOM_RAG_SERVED_CORPORA", raising=False)
    monkeypatch.setattr("axiom.infra.principal.local_handle", lambda: PRINCIPAL)
    # **_kw: the CLI asks for a reader or a writer (write=, #2100); the sample store is both.
    monkeypatch.setattr("axiom.rag.cli._get_store", lambda **_kw: sample_store)
    monkeypatch.setattr(
        "axiom.extensions.builtins.mcp.platform_primitives._resolve_rag_store", lambda: sample_store)
    # Patch the module object, not a dotted string: under xdist another test can leave
    # the chat package without its tools_ext attribute, and string resolution then fails.
    import importlib

    search_docs = importlib.import_module("axiom.extensions.builtins.chat.tools_ext.search_docs")
    monkeypatch.setattr(search_docs, "_open_store", lambda: sample_store)
    return sample_store


def _texts() -> dict[tuple[str, int], str]:
    from axiom.rag import eval_sample

    return {(d["path"], i): c["text"] for d in eval_sample.documents() for i, c in enumerate(d["chunks"])}


def _key(path: str, text: str) -> tuple[str, str]:
    return (path, text.strip()[:120])


class _Agent:
    def __init__(self, store):
        self._store = store
        self._render = None

    def _get_rag_store(self):
        return self._store


def _scope(serving: bool):
    from axiom.extensions.builtins.chat.scope import ChatScope, serving_scope
    from axiom.infra.orchestrator.session import Session

    session = Session(principal_id=PRINCIPAL)
    return serving_scope(session=session) if serving else ChatScope(session=session)


def _chat(store, q, *, serving):
    from axiom.extensions.builtins.chat.agent import ChatAgent

    scope = _scope(serving)
    ChatAgent._rag_context(_Agent(store), q, limit=K, scope=scope)
    return [_key(c.source_path, c.chunk_text) for c in scope.last_retrieved]


def _search_docs(_store, q):
    from axiom.extensions.builtins.chat import view_drive
    from axiom.extensions.builtins.chat.tools_ext import search_docs

    token = view_drive.ACTIVE_SCOPE.set(_scope(serving=True))
    try:
        out = search_docs.execute("search_docs", {"query": q, "max_results": K})
    finally:
        view_drive.ACTIVE_SCOPE.reset(token)
    assert out["source"] == "pgvector", out
    return [_key(r["path"], r["excerpt"]) for r in out["results"]]


def _mcp(_store, q):
    from axiom.extensions.builtins.mcp import platform_primitives as pp

    out = asyncio.run(pp._rag_retrieve({"query": q, "k": K, "principal": PRINCIPAL}))
    assert out["ok"], out
    return [_key(r["source_path"], r["text"]) for r in out["results"]]


def _cli_search(_store, q, capsys):
    from axiom.rag import cli as rag_cli

    capsys.readouterr()
    rag_cli.main(["search", q, "--limit", str(K)])
    printed = capsys.readouterr().out
    texts = _texts()
    rows = re.findall(r"Source: (.+) \(chunk (\d+)\)", printed)
    return [_key(path, texts[(path, int(i))]) for path, i in rows]


def _cli_eval(store, q):
    from axiom.rag import cli as rag_cli

    return [_key(c.source_path, c.chunk_text) for c in rag_cli._chat_retriever(store, k=K, rerank=None)(q)]


def _questions() -> list[str]:
    from axiom.rag import eval_sample

    return [q["question"] for q in yaml.safe_load(eval_sample.QUESTIONS.read_text(encoding="utf-8"))]


@pytest.mark.parametrize("question", _questions())
def test_one_3_every_surface_returns_the_same_ranked_list(one_node, capsys, question):
    """ONE-3: the same question, principal and store give the same ranked list on every surface."""
    got = {
        "chat (terminal scope)": _chat(one_node, question, serving=False),
        "chat (serving scope: the web app and /v1)": _chat(one_node, question, serving=True),
        "search_docs": _search_docs(one_node, question),
        "axiom_rag__retrieve": _mcp(one_node, question),
        "axi rag search": _cli_search(one_node, question, capsys),
        "axi rag eval": _cli_eval(one_node, question),
    }
    reference = got["chat (serving scope: the web app and /v1)"]
    assert reference, f"the sample store returned nothing for {question!r}"
    differ = {name: ranked for name, ranked in got.items() if ranked != reference}
    assert not differ, f"surfaces disagree with chat on {question!r}:\nchat: {reference}\n{differ}"


def test_one_3_no_surface_returns_what_the_principal_may_not_read(one_node, capsys):
    """ONE-3: the sample's staff-only document is refused on every surface alike."""
    q = "How is the desk float counted and how often does the safe code change?"
    for ranked in (_chat(one_node, q, serving=True), _search_docs(one_node, q), _mcp(one_node, q),
                   _cli_search(one_node, q, capsys), _cli_eval(one_node, q)):
        assert all("Cash Handling" not in path for path, _ in ranked), ranked


# ---------------------------------------------------------------------------
# ONE-1: search_docs keeps its output format, and its scope
# ---------------------------------------------------------------------------


def test_one_1_search_docs_keeps_its_output_format(one_node):
    """ONE-1: the same keys as before: query, source, total_indexed_chunks, and per result
    path, title, excerpt, line, score."""
    from axiom.extensions.builtins.chat.tools_ext import search_docs

    out = search_docs.execute("search_docs", {"query": "How long can I borrow a book for?"})
    assert set(out) == {"query", "source", "total_indexed_chunks", "results"}
    assert out["source"] == "pgvector" and 0 < len(out["results"]) <= 5  # its default, unchanged
    first = out["results"][0]
    assert set(first) == {"path", "title", "excerpt", "line", "score"}
    assert first["path"] == "/policies/Lending Policy.pdf" and first["title"] == "Lending Policy"


def test_one_1_search_docs_retrieves_with_the_conversations_principal_and_corpora(monkeypatch):
    """ONE-1: inside a conversation, search_docs asks ``retrieve()`` as that conversation's
    principal, over its corpora, embedded at its label."""
    import axiom.rag.embeddings as emb
    import axiom.rag.retriever as ret
    from axiom.extensions.builtins.chat import view_drive
    from axiom.extensions.builtins.chat.tools_ext import search_docs

    seen: dict = {}
    tiers: list[str] = []

    def spy(store, query_text, query_embedding, **kw):
        seen.update(kw, query=query_text, embedding=query_embedding)
        return []

    def embed(texts, *_a, routing_tier="any", **_k):
        tiers.append(routing_tier)
        return [[0.5] * 4], "m"

    monkeypatch.setattr(ret, "retrieve", spy)
    monkeypatch.setattr(emb, "embed_texts_with_model", embed)
    monkeypatch.setattr(search_docs, "_open_store", lambda: _Shared(object()))
    scope = _scope(serving=True)
    scope.session_mode = "export_controlled"
    token = view_drive.ACTIVE_SCOPE.set(scope)
    try:
        search_docs.execute("search_docs", {"query": "loan periods", "max_results": 3})
    finally:
        view_drive.ACTIVE_SCOPE.reset(token)

    assert seen["query"] == "loan periods" and seen["embedding"] == [0.5] * 4
    assert seen["limit"] == 3
    assert seen["access_context"].principal == PRINCIPAL
    assert seen["corpora"] == list(scope.retrieval_corpora)  # never the operator's own corpus
    assert tiers == ["export_controlled"]
    assert "rerank" not in seen  # the retriever's default governs, as in chat
