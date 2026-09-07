# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for reindex_bronze — pure orchestration, no OCR/DB."""

from __future__ import annotations

from axiom.extensions.builtins.data_platform.orchestration.reindex_bronze import (
    reindex_bronze,
)


def _rec(sp, sha="abc123", disp="allow"):
    return {"source_path": sp, "content_sha256": sha, "disposition": disp}


def test_indexes_new_records_and_counts_chunks():
    recs = [_rec("/a.pdf"), _rec("/b.pdf")]
    rep = reindex_bronze(recs, already_indexed=set(), embed_one=lambda r: 5)
    assert rep.indexed == 2
    assert rep.chunks == 10
    assert rep.failed == 0


def test_skips_already_indexed_idempotent():
    recs = [_rec("/a.pdf"), _rec("/b.pdf")]
    rep = reindex_bronze(recs, already_indexed={"/a.pdf"}, embed_one=lambda r: 3)
    assert rep.indexed == 1
    assert rep.skip_reasons.get("already-indexed") == 1


def test_skips_non_allow_and_missing_id():
    recs = [_rec("/a.pdf", disp="quarantine"), _rec("/b.pdf", sha="")]
    rep = reindex_bronze(recs, already_indexed=set(), embed_one=lambda r: 1)
    assert rep.indexed == 0
    assert rep.skip_reasons.get("not-allow") == 1
    assert rep.skip_reasons.get("no-id") == 1


def test_no_text_is_skipped_not_indexed():
    rep = reindex_bronze([_rec("/img.png")], already_indexed=set(), embed_one=lambda r: 0)
    assert rep.indexed == 0
    assert rep.skip_reasons.get("no-text") == 1


def test_one_failure_does_not_abort_the_pass():
    def embed(r):
        if r["source_path"] == "/bad.pdf":
            raise ValueError("corrupt pdf")
        return 4

    recs = [_rec("/good1.pdf"), _rec("/bad.pdf"), _rec("/good2.pdf")]
    rep = reindex_bronze(recs, already_indexed=set(), embed_one=embed)
    assert rep.indexed == 2  # both good ones still processed
    assert rep.failed == 1
    assert rep.failures[0][0] == "/bad.pdf"


def test_on_record_streams_progress():
    seen = []
    reindex_bronze(
        [_rec("/a.pdf"), _rec("/b.png")],
        already_indexed=set(),
        embed_one=lambda r: 2 if r["source_path"].endswith(".pdf") else 0,
        on_record=lambda status, sp, n: seen.append((status, sp, n)),
    )
    assert ("ok", "/a.pdf", 2) in seen
    assert ("skip", "/b.png", 0) in seen
