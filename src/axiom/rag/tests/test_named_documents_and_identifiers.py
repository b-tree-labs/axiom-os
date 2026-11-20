# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A document named in the question is found, and a rare identifier is not drowned (#1984, step 1b).

Chunk text is all the full-text index covers, so a document whose name is in its title or path
and nowhere in its text (a proposal titled with a project name, a report filed under its report
number) cannot be found by name. And a broad OR-of-terms query buries the one chunk that names a
rare code under the generic literature around it. A site's own retrieval copy solved both:

- two more text rankings, one requiring any named identifier and one requiring all of them;
- a name ranking over the sources' titles and paths, which stands down when the name matches
  many sources (a topic, not a document), and whose best chunks take reserved places at the
  front of the results;
- near-duplicate chunks (one passage chunked twice) collapsed to the better-ranked one.

Each is off until a site's ranking profile turns it on. With the default profile retrieval
issues exactly the two searches it always did.
"""

from __future__ import annotations

import os
import uuid
from unittest.mock import MagicMock

import pytest

from axiom.rag.fts_query import fts_entity_all_tsquery, fts_name_terms
from axiom.rag.ranking import DEVELOPER_PATH_PATTERN, RankingProfile
from axiom.rag.retriever import UNFILTERED, AccessContext, retrieve
from axiom.rag.store import SearchResult


def _hit(path, text="body", *, idx=0, corpus="site", tier="public", title=None) -> SearchResult:
    return SearchResult(
        source_path=path, source_title=title or path.rsplit("/", 1)[-1], chunk_text=text,
        chunk_index=idx, similarity=0.5, combined_score=0.5, corpus=corpus, access_tier=tier,
    )


class _Store:
    """Records every search; ``by_tsquery`` answers a named tsquery, ``names`` the name ranking."""

    def __init__(self, vector=(), text=(), by_tsquery=None, names=None):
        self.vector, self.text = list(vector), list(text)
        self.by_tsquery = by_tsquery or {}
        self.names = names
        self.calls: list[dict] = []
        self.name_calls: list[dict] = []

    def search(self, query_embedding=None, query_text="", corpora=None, limit=5, tsquery=None, **_):
        self.calls.append({"embedding": query_embedding is not None, "text": query_text, "tsquery": tsquery})
        if query_embedding is not None:
            return list(self.vector)
        if tsquery is not None:
            return list(self.by_tsquery.get(tsquery, []))
        return list(self.text)

    def name_ranking(self, name_tsquery, corpora=None, *, max_sources, limit, exclude_path_re=None,
                     query_embedding=None, names_table=None):
        self.name_calls.append({"tsquery": name_tsquery, "max_sources": max_sources,
                                "exclude": exclude_path_re, "limit": limit})
        return list(self.names or [])


def _paths(chunks):
    return [c.source_path for c in chunks]


ON = RankingProfile(entity_rankings=True, name_ranking=True, extra_code_re="n-ms")


class TestTheDefaultIsUnchanged:
    def test_two_searches_and_no_name_ranking(self):
        store = _Store(vector=[_hit("/a")], text=[_hit("/b")], names=[_hit("/named")])
        retrieve(store, "What limit applies to the N-MS loop?", [0.1], access_context=UNFILTERED,
                 profile=RankingProfile())
        assert len(store.calls) == 2
        assert all(c["tsquery"] is None for c in store.calls)
        assert store.name_calls == []

    def test_a_magicmock_store_still_works(self):
        store = MagicMock()
        store.search.side_effect = lambda **_: [_hit("/a")]
        assert _paths(retrieve(store, "anything here", [0.1], access_context=UNFILTERED,
                               profile=RankingProfile())) == ["/a"]


class TestIdentifiers:
    def test_a_named_identifier_gets_its_own_ranking(self):
        rare = _hit("/auth/loop authorization.pdf", "The N-MS loop is limited to $0.50.")
        store = _Store(vector=[_hit(f"/lit/{i}", "salt loop") for i in range(10)],
                       text=[_hit(f"/lit/{i}", "salt loop") for i in range(10)],
                       by_tsquery={"(nms) & (limit | applies | loop)": [rare]})
        out = retrieve(store, "What limit applies to the N-MS loop?", [0.1], limit=5,
                       access_context=UNFILTERED, profile=ON)
        assert "(nms) & (limit | applies | loop)" in [c["tsquery"] for c in store.calls]
        assert "/auth/loop authorization.pdf" in _paths(out)

    def test_every_named_identifier_together(self):
        assert fts_entity_all_tsquery("Publisher Architecture Specification") == (
            "publisher & architecture & specification")
        assert fts_entity_all_tsquery("the Publisher spec") == ""
        store = _Store()
        retrieve(store, "Publisher Architecture Specification", [0.1], access_context=UNFILTERED, profile=ON)
        assert "publisher & architecture & specification" in [c["tsquery"] for c in store.calls]

    def test_a_store_without_the_tsquery_keyword_is_left_alone(self):
        class Old:
            def search(self, query_embedding=None, query_text="", corpora=None, limit=5):
                return [_hit("/a")]
        assert _paths(retrieve(Old(), "the N-MS loop", [0.1], access_context=UNFILTERED, profile=ON)) == ["/a"]


class TestNames:
    @pytest.mark.parametrize("query,terms", [
        ("Summarize LAB-4658", ["lab", "4658"]),
        ("what is in LAB-4658?", ["lab", "4658"]),
        ("What is AtlasBench?", ["atlasbench"]),
        ("tell me about salt chemistry", []),
        ("A B7 C8 D9 E1 F2 G3 H4", []),  # too many names to require all of them
    ])
    def test_the_words_a_name_must_carry(self, query, terms):
        assert fts_name_terms(query) == terms

    def test_the_named_documents_best_chunks_lead(self):
        named = [_hit("/proposals/Bench proposal.pdf", "we propose a benchmark", idx=i) for i in range(3)]
        store = _Store(vector=[_hit(f"/x/{i}", "what is it") for i in range(6)],
                       text=[_hit(f"/x/{i}", "what is it") for i in range(6)], names=named)
        out = retrieve(store, "What is AtlasBench?", [0.1], limit=5, access_context=UNFILTERED, profile=ON)
        assert _paths(out)[:2] == ["/proposals/Bench proposal.pdf"] * 2  # two reserved places, no more
        assert _paths(out).count("/proposals/Bench proposal.pdf") == 2
        assert len(out) == 5
        assert [c.citation_key for c in out] == ["C1", "C2", "C3", "C4", "C5"]
        call = store.name_calls[0]
        assert call["tsquery"] == "atlasbench" and call["max_sources"] == 6

    def test_a_reserved_place_still_passes_the_access_filter(self):
        store = _Store(vector=[_hit("/open")], text=[_hit("/open")],
                       names=[_hit("/secret", tier="institutional"), _hit("/named")])
        out = retrieve(store, "What is AtlasBench?", [0.1], access_context=AccessContext(), profile=ON)
        assert "/secret" not in _paths(out)
        assert _paths(out)[0] == "/named"

    def test_developer_files_are_left_out_of_the_name_ranking(self):
        store = _Store()
        profile = RankingProfile(name_ranking=True, developer_path_re=DEVELOPER_PATH_PATTERN)
        retrieve(store, "What is AtlasBench?", [0.1], access_context=UNFILTERED, profile=profile)
        assert store.name_calls[0]["exclude"] == DEVELOPER_PATH_PATTERN

    def test_a_question_for_the_governing_documents_names_them(self):
        profile = RankingProfile(name_ranking=True, primary_query_re=r"\blicen[sc]ed\b",
                                 primary_name_tsquery="charter | (operating & licence)")
        store = _Store()
        retrieve(store, "what is the licensed power limit", [0.1], access_context=UNFILTERED, profile=profile)
        assert store.name_calls[0]["tsquery"] == "charter | (operating & licence)"
        assert store.name_calls[0]["max_sources"] == 12


class TestNearDuplicates:
    def test_one_passage_chunked_twice_is_returned_once(self):
        text = "The same passage, ingested twice under two chunkings of one document. " * 3
        store = _Store(vector=[_hit("/a", text, idx=0), _hit("/a", text, idx=7), _hit("/b", "other")],
                       text=[])
        out = retrieve(store, "same passage", [0.1], access_context=UNFILTERED,
                       profile=RankingProfile(dedup_prefix_chars=120))
        assert _paths(out) == ["/a", "/b"]


# ---------------------------------------------------------------------------
# Against a real Postgres store (AXIOM_RAG_TEST_DSN names a scratch pgvector database)
# ---------------------------------------------------------------------------

DSN = os.environ.get("AXIOM_RAG_TEST_DSN", "")
real = pytest.mark.skipif(not DSN, reason="AXIOM_RAG_TEST_DSN not set (needs a scratch pgvector database)")


@pytest.fixture(scope="module")
def pg_store():
    import psycopg2

    from axiom.rag.chunker import Chunk
    from axiom.rag.store import RAGStore

    db = "rag_names_" + uuid.uuid4().hex[:8]
    admin = psycopg2.connect(DSN)
    admin.autocommit = True
    admin.cursor().execute(f"CREATE DATABASE {db}")
    url = DSN.rsplit("/", 1)[0] + "/" + db
    with psycopg2.connect(url) as cn, cn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
    store = RAGStore(url)
    store.connect()
    docs = [
        ("/proposals/Bench proposal.pdf", "AtlasBench", "We propose a benchmark of graded abstention."),
        ("/reports/LAB-4658.pdf", "Semiannual Progress Report", "Progress for the period ending August 1970."),
        ("/repo/docs/spec-bench.md", "Technical Specification: bench", "The bench component."),
    ] + [(f"/lit/salt-{i}.pdf", f"Salt chemistry review {i}", "salt chemistry") for i in range(8)]
    for i, (path, title, text) in enumerate(docs):
        store.upsert_chunks([Chunk(text=text, source_path=path, source_title=title, chunk_index=0, start_line=0)],
                            [[1.0, 0.01 * i] + [0.0] * 766], corpus="rag-org")
    yield store
    store.close()
    admin.cursor().execute(f"DROP DATABASE {db} WITH (FORCE)")
    admin.close()


@real
class TestARealStore:
    def test_a_name_only_in_the_title_is_found(self, pg_store):
        hits = pg_store.name_ranking("atlasbench", ["rag-org"], max_sources=6, limit=12)
        assert [h.source_path for h in hits] == ["/proposals/Bench proposal.pdf"]

    def test_a_name_only_in_the_path_is_found(self, pg_store):
        hits = pg_store.name_ranking("lab & 4658", ["rag-org"], max_sources=6, limit=12)
        assert [h.source_path for h in hits] == ["/reports/LAB-4658.pdf"]

    def test_a_topic_stands_the_name_ranking_down(self, pg_store):
        assert pg_store.name_ranking("salt", ["rag-org"], max_sources=6, limit=12) == []

    def test_developer_files_do_not_count(self, pg_store):
        hits = pg_store.name_ranking("bench", ["rag-org"], max_sources=6, limit=12,
                                     exclude_path_re=DEVELOPER_PATH_PATTERN)
        assert [h.source_path for h in hits] == ["/proposals/Bench proposal.pdf"]

    def test_nearest_chunks_first_with_an_embedding(self, pg_store):
        hits = pg_store.name_ranking("atlasbench | lab", ["rag-org"], max_sources=6, limit=12,
                                     query_embedding=[1.0, 0.0] + [0.0] * 766)
        assert hits[0].source_path == "/proposals/Bench proposal.pdf"
        assert hits[0].similarity > 0.99

    def test_a_given_tsquery_replaces_the_or_of_terms(self, pg_store):
        got = pg_store.search(query_text="ignored words", tsquery="abstention & benchmark", corpora=["rag-org"])
        assert [h.source_path for h in got] == ["/proposals/Bench proposal.pdf"]

    def test_through_retrieve(self, pg_store):
        profile = RankingProfile(name_ranking=True, entity_rankings=True, developer_path_re=DEVELOPER_PATH_PATTERN)
        out = retrieve(pg_store, "What is AtlasBench?", [0.0, 1.0] + [0.0] * 766, corpora=["rag-org"],
                       limit=4, access_context=AccessContext(), profile=profile)
        assert out[0].source_path == "/proposals/Bench proposal.pdf"
