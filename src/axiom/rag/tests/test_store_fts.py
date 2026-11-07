# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""FTS query construction + bounded ranking in RAGStore.search.

Two defects this locks down:
  1. websearch_to_tsquery ANDs every term, so a multi-term query matched zero
     chunks and hybrid search silently collapsed to dense-only. Fix: OR-of-terms.
  2. OR recall can match a large fraction of the corpus, so ORDER BY ts_rank must
     be bounded (pure-text branch) and the combined branch must score only the
     vector candidates, or ranking scans the whole corpus and hangs.

No DB: the query builder is pure, and the SQL search() emits is captured via a
mock cursor (same MagicMock convention as test_retriever.py).
"""

import contextlib
from unittest.mock import MagicMock

from axiom.rag import store as store_mod
from axiom.rag.store import RAGStore, _fts_entity_terms, _fts_entity_tsquery, _fts_tsquery

# -- pure: OR-of-terms tsquery builder ---------------------------------------


def test_fts_tsquery_is_or_joined_dropping_stopwords():
    q = _fts_tsquery("what is the annual revenue of the region")
    parts = set(q.split(" | "))
    assert " | " in q
    assert {"annual", "revenue", "region"} <= parts
    assert "the" not in parts and "what" not in parts and "of" not in parts


def test_fts_tsquery_preserves_short_alphanumeric_codes():
    # Codes like A3 / B12 are discriminative but <3 chars or mixed; keyword
    # filters drop them, tanking precision.
    q = _fts_tsquery("status of unit A3 and revision B12")
    parts = set(q.split(" | "))
    assert "a3" in parts and "b12" in parts


def test_fts_tsquery_is_lexeme_safe_and_empty_on_no_terms():
    q = _fts_tsquery("revenue?? (region)!!")
    assert all(c.isalnum() or c in " |" for c in q)  # no tsquery-breaking punctuation
    assert _fts_tsquery("the of a an") == ""  # all stopwords -> empty


# -- search() SQL construction (mock cursor, no DB) --------------------------


def _mock_store(capture):
    store = RAGStore.__new__(RAGStore)  # bypass __init__/DB
    cur = MagicMock()
    cur.fetchall.return_value = []

    def _execute(sql, params=None):
        capture["sql"] = sql
        capture["params"] = params

    cur.execute.side_effect = _execute

    @contextlib.contextmanager
    def _cur():
        yield cur

    store._cur = _cur
    return store


def test_search_uses_or_tsquery_not_websearch():
    cap = {}
    _mock_store(cap).search(query_embedding=None, query_text="annual revenue region")
    assert "to_tsquery('english'" in cap["sql"]
    assert "websearch_to_tsquery" not in cap["sql"]


def test_pure_text_search_is_scan_bounded():
    cap = {}
    _mock_store(cap).search(query_embedding=None, query_text="annual revenue")
    # bounded: inner cap LIMIT + outer LIMIT -> two "LIMIT %s"
    assert cap["sql"].count("LIMIT %s") == 2


def test_combined_text_search_scoped_to_vector_candidates():
    cap = {}
    _mock_store(cap).search(query_embedding=[0.1] * 4, query_text="annual revenue")
    # text_search must score only the vector candidates, never the whole corpus.
    assert "id IN (SELECT id FROM vector_search)" in cap["sql"]
    assert "to_tsquery('english'" in cap["sql"]
    assert "websearch_to_tsquery" not in cap["sql"]


def test_empty_text_terms_returns_empty_not_error():
    cap = {}
    res = _mock_store(cap).search(query_embedding=None, query_text="the of a an")
    assert res == []  # no usable terms -> no query, empty result (no tsquery syntax error)


def test_schema_ships_gin_fts_index():
    # Without a GIN expression index matching the FTS query, @@ seq-scans the table.
    assert "USING gin (to_tsvector('english', chunk_text))" in store_mod._SCHEMA_SQL


# -- entity-required precise recall (rare codes buried by a broad OR) ---------
# A query naming a discriminative code (an acronym / model number) matches few
# chunks; a broad OR floods the pool with generic prose whose dense similarity
# buries the code's chunk. entity_search recalls it independently and a small
# boost floats it. Only queries that name a code take this path.


def test_fts_entity_terms_are_uppercase_or_digit_tokens_only():
    ents = _fts_entity_terms("status of unit A3 and model B12")
    assert "a3" in ents and "b12" in ents
    assert _fts_entity_terms("annual revenue of the region") == []  # no code -> none


def test_fts_entity_tsquery_requires_code_anded_with_or_of_rest():
    q = _fts_entity_tsquery("annual revenue for unit A3")
    assert q.startswith("(a3) & (")  # the named code is required
    assert "revenue" in q and " | " in q
    assert _fts_entity_tsquery("annual revenue of the region") == ""  # no code -> empty


def test_hybrid_with_code_adds_independent_entity_search_and_boost():
    cap = {}
    _mock_store(cap).search(query_embedding=[0.1] * 4, query_text="annual revenue unit A3")
    sql = cap["sql"]
    assert "entity_search AS" in sql  # independent recall CTE
    assert "UNION" in sql  # union'd into the candidate pool
    # matched chunks get the float boost
    assert "CASE WHEN p.id IN (SELECT id FROM entity_search)" in sql


def test_hybrid_without_code_stays_plain_dense_scoped():
    cap = {}
    _mock_store(cap).search(query_embedding=[0.1] * 4, query_text="annual revenue region")
    sql = cap["sql"]
    assert "entity_search" not in sql  # no code named -> unchanged behavior
    assert "id IN (SELECT id FROM vector_search)" in sql
