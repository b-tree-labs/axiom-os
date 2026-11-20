# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Corpus size was discovered as an outage, because nothing measured it.

A 5.3M-chunk corpus reached 73 GB — 13.8 KB per chunk for text that is a small
fraction of that — and the first anyone knew was a node whose cluster would no
longer schedule pods. There was no number to watch, so nobody watched it.

This is the number. Every later milestone in the storage plan is a
before-and-after, and there is no before without this.

The figure that matters most is **bytes per chunk**. A total says the corpus is
big, which may simply mean it holds a lot. Bytes per chunk says whether each
chunk is costing what it should, and that is what reveals an encoding problem
rather than an abundance of content.
"""

from __future__ import annotations

from axiom.rag.storage_stat import (
    IndexFact,
    analyze,
    duplicate_expressions,
    never_scanned,
)


def _idx(name, size, defn, scans=10):
    return IndexFact(name=name, size_bytes=size, definition=defn, scans=scans)


def test_bytes_per_chunk_is_reported():
    """The headline. A total alone cannot distinguish a large corpus from an
    expensive one."""
    report = analyze(rows=1000, heap_bytes=1_000_000, toast_bytes=3_000_000,
                     indexes=[_idx("i_vec", 6_000_000, "USING ivfflat (embedding ...)")])
    assert report.total_bytes == 10_000_000
    assert report.bytes_per_chunk == 10_000


def test_zero_rows_does_not_divide_by_zero_and_says_so():
    """An empty corpus is not a corpus costing nothing per chunk; it is a
    corpus with nothing to divide by. Reporting 0 would read as excellent."""
    report = analyze(rows=0, heap_bytes=8192, toast_bytes=0, indexes=[])
    assert report.bytes_per_chunk is None
    assert "no chunks" in report.note.lower()


def test_the_vector_index_is_split_out_from_other_indexes():
    """The embedding is usually the dominant cost, and lumping it into a single
    index total hides the one line that explains the bill."""
    report = analyze(
        rows=100, heap_bytes=1000, toast_bytes=1000,
        indexes=[
            _idx("i_vec", 90_000, "USING ivfflat (embedding vector_cosine_ops)"),
            _idx("i_fts", 5_000, "USING gin (to_tsvector('english', chunk_text))"),
            _idx("i_pk", 1_000, "USING btree (id)"),
        ],
    )
    assert report.vector_index_bytes == 90_000
    assert report.other_index_bytes == 6_000


def test_hnsw_counts_as_a_vector_index_too():
    """Recognising only one index type would silently under-report on any
    install that chose the other."""
    report = analyze(rows=10, heap_bytes=0, toast_bytes=0,
                     indexes=[_idx("i", 500, "USING hnsw (embedding vector_l2_ops)")])
    assert report.vector_index_bytes == 500


def test_duplicate_expressions_are_found():
    """Two GIN indexes over the identical expression were live in production.
    The planner uses either; the second is pure cost."""
    dupes = duplicate_expressions([
        _idx("idx_a", 1, "CREATE INDEX idx_a ON public.chunks USING gin (to_tsvector('english'::regconfig, chunk_text))"),
        _idx("idx_b", 1, "CREATE INDEX idx_b ON public.chunks USING gin (to_tsvector('english'::regconfig, chunk_text))"),
        _idx("idx_c", 1, "CREATE INDEX idx_c ON public.chunks USING btree (id)"),
    ])
    assert len(dupes) == 1
    assert {"idx_a", "idx_b"} == set(dupes[0])


def test_indexes_differing_only_by_name_are_still_duplicates():
    """The name is the one part guaranteed to differ. Comparing whole
    definitions verbatim would find nothing, ever."""
    dupes = duplicate_expressions([
        _idx("x", 1, "CREATE INDEX x ON t USING btree (a, b)"),
        _idx("y", 1, "CREATE INDEX y ON t USING btree (a, b)"),
    ])
    assert len(dupes) == 1


def test_genuinely_different_indexes_are_not_flagged():
    """The negative control. A detector that flags everything gets muted, and
    then the real duplicate rides along with the noise."""
    assert duplicate_expressions([
        _idx("x", 1, "CREATE INDEX x ON t USING btree (a)"),
        _idx("y", 1, "CREATE INDEX y ON t USING btree (b)"),
    ]) == []


def test_never_scanned_indexes_are_reported_not_removed():
    """An index with no scans may serve a feature that has not shipped. The
    finding names it; a human decides. Automatic removal would make this tool
    something nobody dares run."""
    unused = never_scanned([
        _idx("cold", 76_000_000, "USING btree (fragment_ref)", scans=0),
        _idx("warm", 1_000, "USING btree (id)", scans=340_001),
    ])
    assert [i.name for i in unused] == ["cold"]


def test_a_report_carries_the_raw_numbers():
    """A measurement that cannot be cross-checked against the database is not a
    measurement. Every figure here must be reproducible by hand."""
    report = analyze(rows=2, heap_bytes=10, toast_bytes=20,
                     indexes=[_idx("i", 30, "USING btree (id)")])
    d = report.as_dict()
    for key in ("rows", "heap_bytes", "toast_bytes", "total_bytes",
                "vector_index_bytes", "other_index_bytes", "bytes_per_chunk"):
        assert key in d, key
    assert d["heap_bytes"] + d["toast_bytes"] + d["vector_index_bytes"] \
        + d["other_index_bytes"] == d["total_bytes"]


def test_declared_encoding_is_reported_when_known():
    """Precision and dimensionality are meant to be deliberate choices. The
    report states what is actually stored, so a default can be recognised as a
    default rather than mistaken for a decision."""
    report = analyze(rows=1, heap_bytes=1, toast_bytes=1, indexes=[],
                     embedding_type="vector", embedding_dim=768)
    assert report.embedding_type == "vector"
    assert report.embedding_dim == 768
    assert report.bytes_per_vector == 768 * 4


def test_halfvec_is_costed_at_two_bytes():
    report = analyze(rows=1, heap_bytes=1, toast_bytes=1, indexes=[],
                     embedding_type="halfvec", embedding_dim=768)
    assert report.bytes_per_vector == 768 * 2


def test_unknown_embedding_type_reports_none_not_a_guess():
    """Costing an unrecognised type at a guessed width would put a wrong number
    beside correct ones, which is worse than a gap."""
    report = analyze(rows=1, heap_bytes=1, toast_bytes=1, indexes=[],
                     embedding_type="somethingelse", embedding_dim=4)
    assert report.bytes_per_vector is None


# --- the query layer, without a database --------------------------------------


class _Cursor:
    """A cursor that answers the three queries in order."""

    def __init__(self, relation, indexes, embedding):
        self._answers = {"relation": relation, "indexes": indexes, "embedding": embedding}
        self._last = None

    def execute(self, sql, params=None):
        self._last = ("relation" if "reltuples" in sql
                      else "indexes" if "pg_stat_user_indexes" in sql
                      else "embedding")

    def fetchone(self):
        v = self._answers[self._last]
        return v[0] if self._last == "indexes" else v

    def fetchall(self):
        return self._answers["indexes"]


def test_collect_assembles_a_report_from_the_three_queries():
    from axiom.rag.storage_stat import collect

    cur = _Cursor(
        relation=(5_304_997, 8_262_057_984, 26_843_545_600),
        indexes=[
            ("idx_chunks_embedding", 38_654_705_664,
             "CREATE INDEX idx_chunks_embedding ON public.chunks USING ivfflat (embedding vector_cosine_ops)", 2761),
            ("idx_chunks_fts", 1_876_951_040,
             "CREATE INDEX idx_chunks_fts ON public.chunks USING gin (to_tsvector('english'::regconfig, chunk_text))", 112),
            ("idx_chunks_tsv", 1_875_902_464,
             "CREATE INDEX idx_chunks_tsv ON public.chunks USING gin (to_tsvector('english'::regconfig, chunk_text))", 1904),
        ],
        embedding=("vector", "vector(768)"),
    )
    report = collect(cur)
    assert report.rows == 5_304_997
    assert report.vector_index_bytes == 38_654_705_664
    assert report.embedding_dim == 768
    assert report.bytes_per_vector == 3072
    assert len(duplicate_expressions(report.indexes)) == 1


def test_render_leads_with_bytes_per_chunk_and_names_the_duplicate():
    from axiom.rag.storage_stat import render

    report = analyze(
        rows=1000, heap_bytes=1_000_000, toast_bytes=1_000_000,
        indexes=[
            _idx("a", 10, "CREATE INDEX a ON t USING gin (expr)"),
            _idx("b", 10, "CREATE INDEX b ON t USING gin (expr)"),
            _idx("cold", 999, "CREATE INDEX cold ON t USING btree (x)", scans=0),
        ],
        embedding_type="vector", embedding_dim=768,
    )
    out = "\n".join(render(report))
    assert "per chunk" in out
    assert "DUPLICATE" in out and "a, b" in out
    assert "never scanned" in out and "not removed" in out
    assert "3072 B/vector" in out


def test_render_does_not_claim_a_per_chunk_figure_for_an_empty_corpus():
    from axiom.rag.storage_stat import render

    out = "\n".join(render(analyze(rows=0, heap_bytes=0, toast_bytes=0, indexes=[])))
    assert "no chunks" in out


def test_an_estimated_row_count_is_labelled_as_one():
    """Found by running this against the live corpus: `reltuples` read 5.9M on a
    5.3M-row table, an 11% overstatement that moved bytes-per-chunk — the
    headline figure of the whole milestone — from 14.8 KB down to 13.0 KB.

    A number that looks precise and is not is worse than one that admits it."""
    from axiom.rag.storage_stat import render

    r = analyze(rows=100, heap_bytes=1, toast_bytes=1, indexes=[], rows_estimated=True)
    assert r.as_dict()["rows_estimated"] is True
    assert "planner estimate" in "\n".join(render(r))


def test_an_exact_count_is_not_labelled():
    from axiom.rag.storage_stat import render

    r = analyze(rows=100, heap_bytes=1, toast_bytes=1, indexes=[])
    assert r.rows_estimated is False
    assert "planner estimate" not in "\n".join(render(r))


def test_collect_counts_exactly_and_says_so():
    from axiom.rag.storage_stat import collect

    class _C(_Cursor):
        def execute(self, sql, params=None):
            if sql.strip().upper().startswith("SELECT COUNT(*)"):
                self._last = "count"
            else:
                super().execute(sql, params)

        def fetchone(self):
            if self._last == "count":
                return (5_304_997,)
            return super().fetchone()

    cur = _C(relation=(5_906_727, 10, 20), indexes=[], embedding=("vector", "vector(768)"))
    report = collect(cur)
    assert report.rows == 5_304_997, "the estimate was used instead of the count"
    assert report.rows_estimated is False


def test_collect_falls_back_to_the_estimate_and_labels_it():
    """A corpus whose count is refused still reports a figure — flagged."""
    from axiom.rag.storage_stat import collect

    class _C(_Cursor):
        def execute(self, sql, params=None):
            if sql.strip().upper().startswith("SELECT COUNT(*)"):
                raise RuntimeError("statement timeout")
            super().execute(sql, params)

    cur = _C(relation=(5_906_727, 10, 20), indexes=[], embedding=("vector", "vector(768)"))
    report = collect(cur)
    assert report.rows == 5_906_727
    assert report.rows_estimated is True


# --- before/after: how every remaining milestone proves itself ----------------


def test_a_reduction_is_reported_per_component():
    """A total that fell says something worked. WHICH component fell says what.
    Dropping an index and re-embedding move different numbers, and a single
    delta cannot tell them apart."""
    from axiom.rag.storage_stat import compare

    before = analyze(rows=1000, heap_bytes=100, toast_bytes=200,
                     indexes=[_idx("v", 900, "USING ivfflat (embedding)"),
                              _idx("d", 100, "USING gin (e)")])
    after = analyze(rows=1000, heap_bytes=100, toast_bytes=200,
                    indexes=[_idx("v", 900, "USING ivfflat (embedding)")])
    d = compare(before, after)
    assert d["other_index_bytes"]["delta"] == -100
    assert d["vector_index_bytes"]["delta"] == 0
    assert d["total_bytes"]["delta"] == -100


def test_a_changed_row_count_makes_the_comparison_confounded():
    """The trap. If the corpus grew while an index was dropped, a flat total is
    not 'no change' — it is two changes cancelling. Saying so beats reporting a
    number that reads like a result."""
    from axiom.rag.storage_stat import compare

    before = analyze(rows=1000, heap_bytes=1000, toast_bytes=0, indexes=[])
    after = analyze(rows=1200, heap_bytes=1000, toast_bytes=0, indexes=[])
    d = compare(before, after)
    assert d["confounded"] is True
    assert "row count" in d["note"].lower()


def test_an_unchanged_corpus_is_not_confounded():
    """Negative control. If every comparison were flagged confounded the flag
    would be ignored, and the one that mattered would go with it."""
    from axiom.rag.storage_stat import compare

    r = analyze(rows=10, heap_bytes=1, toast_bytes=1, indexes=[])
    assert compare(r, r)["confounded"] is False


def test_bytes_per_chunk_is_the_comparable_figure_when_rows_move():
    """When the corpus legitimately grew, per-chunk cost is what still compares.
    It is the figure that survives a changing denominator."""
    from axiom.rag.storage_stat import compare

    before = analyze(rows=1000, heap_bytes=10_000, toast_bytes=0, indexes=[])
    after = analyze(rows=2000, heap_bytes=10_000, toast_bytes=0, indexes=[])
    d = compare(before, after)
    assert d["bytes_per_chunk"]["before"] == 10
    assert d["bytes_per_chunk"]["after"] == 5
    assert d["bytes_per_chunk"]["delta"] == -5


def test_an_estimated_side_makes_the_comparison_unreliable():
    """An 11% estimate error on either side swamps the win being claimed."""
    from axiom.rag.storage_stat import compare

    before = analyze(rows=1000, heap_bytes=1, toast_bytes=1, indexes=[], rows_estimated=True)
    after = analyze(rows=1000, heap_bytes=1, toast_bytes=1, indexes=[])
    d = compare(before, after)
    assert d["confounded"] is True
    assert "estimate" in d["note"].lower()


def test_a_regression_is_reported_as_one():
    """A harness that can only report improvement is an advocacy tool."""
    from axiom.rag.storage_stat import compare

    before = analyze(rows=100, heap_bytes=100, toast_bytes=0, indexes=[])
    after = analyze(rows=100, heap_bytes=500, toast_bytes=0, indexes=[])
    d = compare(before, after)
    assert d["total_bytes"]["delta"] == 400
    assert d["improved"] is False


def test_a_snapshot_round_trips():
    """A before taken today has to be readable by an after taken next week,
    across a process boundary."""
    import json

    from axiom.rag.storage_stat import from_dict

    original = analyze(rows=5, heap_bytes=7, toast_bytes=11,
                       indexes=[_idx("v", 13, "USING hnsw (embedding)", 3)],
                       embedding_type="halfvec", embedding_dim=256)
    restored = from_dict(json.loads(json.dumps(original.as_dict())))
    assert restored.total_bytes == original.total_bytes
    assert restored.bytes_per_chunk == original.bytes_per_chunk
    assert restored.vector_index_bytes == original.vector_index_bytes
