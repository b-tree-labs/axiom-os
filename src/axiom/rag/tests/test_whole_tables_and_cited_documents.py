# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A whole table, and a follow-up that stays in the documents already cited (#1984, step 1c).

A table or list is split across consecutive chunks, and retrieval returns whichever chunk
ranks. Asked for "all runs including 8-10", a chat held the chunk with runs 1-7 and said the rest
was cut off; runs 8-10 were in the next chunk. And a follow-up such as "get the rest of that
table" often rewrites to something retrieval no longer ties to the document the conversation
was about. A site's own retrieval copy fetched the neighbours of a table-like hit when the
question asks for completeness, and led with the best chunks of the documents the conversation
had already cited. ``retrieve()`` does both when the caller asks (``expand_for``, ``cited``);
neither runs otherwise, and every added chunk passes the same access filter.
"""

from __future__ import annotations

import os
import uuid

import pytest

from axiom.rag import neighbours
from axiom.rag.ranking import RankingProfile
from axiom.rag.retriever import UNFILTERED, AccessContext, retrieve
from axiom.rag.store import SearchResult

ROWS_1_7 = "\n".join(f"{i} Jan 1999" for i in range(1, 8))
ROWS_8_14 = "\n".join(f"{i} Feb 1999" for i in range(8, 15))
CAPTION = "Chapter 3 summarizes the runs. Table 3-2. Dates of Runs\nRun No. Starting Date"


def _hit(path, text, idx=0, corpus="site", tier="public"):
    return SearchResult(source_path=path, source_title=path, chunk_text=text, chunk_index=idx,
                        similarity=0.5, combined_score=0.5, corpus=corpus, access_tier=tier)


class _Store:
    def __init__(self, ranked, chunks=(), cited=(), suffixes=None):
        self.ranked = list(ranked)
        self.chunks = {(c.source_path, c.corpus, c.chunk_index): c for c in chunks}
        self.cited = list(cited)
        self.suffixes = suffixes or {}
        self.asked: list = []

    def search(self, query_embedding=None, query_text="", corpora=None, limit=5, **_):
        return list(self.ranked)

    def chunks_at(self, keys, corpora=None):
        self.asked.append(("chunks_at", list(keys)))
        return [self.chunks[k] for k in keys if k in self.chunks]

    def best_chunks_in(self, paths, query_text, corpora=None, *, limit):
        self.asked.append(("best_chunks_in", sorted(paths)))
        return [c for c in self.cited if c.source_path in paths][:limit]

    def paths_ending_with(self, suffixes, corpora=None):
        return [p for s in suffixes for p in self.suffixes.get(s, [])]


class TestThePureRules:
    def test_a_completeness_cue(self):
        assert neighbours.wants_completeness("list all runs including 8-10")
        assert not neighbours.wants_completeness("when did it first operate")

    def test_fragment_like_and_table_start(self):
        assert neighbours.fragment_like(ROWS_1_7)
        assert not neighbours.fragment_like("A paragraph of ordinary prose about the runs.")
        assert neighbours.table_start_tail(CAPTION)
        assert not neighbours.table_start_tail("As Table 3-2 shows, the runs were long, " + "and the prose goes on " * 6)

    def test_the_following_chunk_is_planned_first(self):
        rows = [("/r", "t", ROWS_1_7, 4, "site", 1.0), ("/s", "t", ROWS_1_7, 9, "site", 0.9)]
        plan = neighbours.plan_neighbours(rows, "all runs", max_extra=2)
        assert plan == [("/r", "site", 5), ("/s", "site", 10)]

    def test_a_second_continuation_follows_the_first(self):
        # The site's copy put chunk 5 after every other hit; a table reads top to bottom.
        rows = [("/r", "t", CAPTION, 3, "site", 1.0), ("/x", "t", "other", 0, "site", 0.5)]
        fetched = [("/r", "t", ROWS_8_14, 5, "site", 0.0), ("/r", "t", ROWS_1_7, 4, "site", 0.0)]
        merged = neighbours.merge_neighbours(rows, fetched)
        assert [(r[0], r[3]) for r in merged] == [("/r", 3), ("/r", 4), ("/r", 5), ("/x", 0)]

    def test_neighbours_land_beside_their_anchor_in_document_order(self):
        rows = [("/r", "t", ROWS_1_7, 4, "site", 1.0), ("/x", "t", "other", 0, "site", 0.5)]
        merged = neighbours.merge_neighbours(rows, [("/r", "t", ROWS_8_14, 5, "site", 0.0)])
        assert [(r[0], r[3]) for r in merged] == [("/r", 4), ("/r", 5), ("/x", 0)]


class TestRetrieve:
    def test_nothing_is_expanded_unless_asked(self):
        store = _Store([_hit("/r", ROWS_1_7, idx=4)], chunks=[_hit("/r", ROWS_8_14, idx=5)])
        out = retrieve(store, "all runs 8-10", [0.1], access_context=UNFILTERED, profile=RankingProfile())
        assert [(c.source_path, c.chunk_index) for c in out] == [("/r", 4)]
        assert store.asked == []

    def test_the_rest_of_the_table_comes_with_it(self):
        store = _Store([_hit("/r", CAPTION, idx=3), _hit("/x", "unrelated prose")],
                       chunks=[_hit("/r", ROWS_1_7, idx=4), _hit("/r", ROWS_8_14, idx=5)])
        out = retrieve(store, "list all runs including 8-10", [0.1], limit=2, access_context=UNFILTERED,
                       profile=RankingProfile(), expand_for="list all runs including 8-10")
        assert [(c.source_path, c.chunk_index) for c in out] == [("/r", 3), ("/r", 4), ("/r", 5), ("/x", 0)]
        assert [c.citation_key for c in out] == ["C1", "C2", "C3", "C4"]

    def test_a_neighbour_the_caller_may_not_read_is_not_added(self):
        store = _Store([_hit("/r", ROWS_1_7, idx=4)], chunks=[_hit("/r", ROWS_8_14, idx=5, tier="institutional")])
        out = retrieve(store, "all runs", [0.1], access_context=AccessContext(), profile=RankingProfile(),
                       expand_for="all runs")
        assert [c.chunk_index for c in out] == [4]

    def test_a_follow_up_leads_with_the_documents_already_cited(self):
        cited = [_hit("/cited.pdf", "rows of the table, continued", idx=7)]
        store = _Store([_hit("/other", "the rest of it")], cited=cited, suffixes={"cited.pdf": ["/cited.pdf"]})
        out = retrieve(store, "the rest of that table", [0.1], access_context=UNFILTERED, profile=RankingProfile(),
                       expand_for="the rest of that table", cited=["/cited.pdf"])
        assert [c.source_path for c in out] == ["/cited.pdf", "/other"]
        out = retrieve(store, "the rest of that table", [0.1], access_context=UNFILTERED, profile=RankingProfile(),
                       expand_for="the rest of that table", cited_suffixes=["cited.pdf"])
        assert [c.source_path for c in out] == ["/cited.pdf", "/other"]

    def test_a_cited_chunk_the_caller_may_not_read_is_not_added(self):
        store = _Store([_hit("/other", "x")], cited=[_hit("/cited.pdf", "secret rows", tier="institutional")])
        out = retrieve(store, "the rest of that table", [0.1], access_context=AccessContext(),
                       profile=RankingProfile(), expand_for="the rest", cited=["/cited.pdf"])
        assert [c.source_path for c in out] == ["/other"]

    def test_a_store_without_the_methods_is_left_alone(self):
        class Old:
            def search(self, **_):
                return [_hit("/r", ROWS_1_7, idx=4)]
        out = retrieve(Old(), "all runs", [0.1], access_context=UNFILTERED, profile=RankingProfile(),
                       expand_for="all runs", cited=["/c"])
        assert [c.chunk_index for c in out] == [4]


DSN = os.environ.get("AXIOM_RAG_TEST_DSN", "")
real = pytest.mark.skipif(not DSN, reason="AXIOM_RAG_TEST_DSN not set (needs a scratch pgvector database)")


@pytest.fixture(scope="module")
def pg_store():
    import psycopg2

    from axiom.rag.chunker import Chunk
    from axiom.rag.store import RAGStore

    db = "rag_nb_" + uuid.uuid4().hex[:8]
    admin = psycopg2.connect(DSN)
    admin.autocommit = True
    admin.cursor().execute(f"CREATE DATABASE {db}")
    url = DSN.rsplit("/", 1)[0] + "/" + db
    with psycopg2.connect(url) as cn, cn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
    store = RAGStore(url)
    store.connect()
    texts = [CAPTION, ROWS_1_7, ROWS_8_14]
    store.upsert_chunks([Chunk(text=t, source_path="/reports/Runs report.pdf", source_title="Runs report",
                               chunk_index=i, start_line=0) for i, t in enumerate(texts)],
                        [[1.0, 0.1 * i] + [0.0] * 766 for i in range(3)], corpus="rag-org")
    store.upsert_chunks([Chunk(text="Unrelated prose about staffing.", source_path="/other.pdf",
                               source_title="Other", chunk_index=0, start_line=0)],
                        [[0.0, 1.0] + [0.0] * 766], corpus="rag-org")
    yield store
    store.close()
    admin.cursor().execute(f"DROP DATABASE {db} WITH (FORCE)")
    admin.close()


@real
class TestARealStore:
    def test_chunks_at(self, pg_store):
        got = pg_store.chunks_at([("/reports/Runs report.pdf", "rag-org", 2), ("/nope", "rag-org", 0)], ["rag-org"])
        assert [(h.source_path, h.chunk_index) for h in got] == [("/reports/Runs report.pdf", 2)]

    def test_best_chunks_in_a_cited_source(self, pg_store):
        got = pg_store.best_chunks_in(["/reports/Runs report.pdf"], "table runs", ["rag-org"], limit=1)
        assert got[0].chunk_index == 0

    def test_paths_ending_with(self, pg_store):
        assert pg_store.paths_ending_with(["Runs report.pdf", "100%_x"], ["rag-org"]) == ["/reports/Runs report.pdf"]

    def test_through_retrieve(self, pg_store):
        out = retrieve(pg_store, "list all runs in Table 3-2", [1.0, 0.0] + [0.0] * 766, corpora=["rag-org"],
                       limit=1, access_context=AccessContext(), profile=RankingProfile(),
                       expand_for="list all runs in Table 3-2")
        assert [c.chunk_index for c in out if c.source_path == "/reports/Runs report.pdf"] == [0, 1, 2]
