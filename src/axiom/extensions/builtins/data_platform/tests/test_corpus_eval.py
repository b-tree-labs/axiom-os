# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for corpus_eval — cluster derivation + coverage scoring (no DB)."""

from __future__ import annotations

from axiom.extensions.builtins.data_platform.corpus_eval import (
    cluster_of,
    score_retrieval,
)


def test_cluster_of_derives_from_path():
    p = "/_____Literature_____/MSR/MSRE and MSBR/openmsr/ocr/ORNL-1567.txt"
    assert cluster_of(p, depth=2) == "_____Literature_____/MSR"
    assert cluster_of(p, depth=1) == "_____Literature_____"
    assert cluster_of("") == ""


def test_coverage_passes_when_expected_substring_in_hits():
    qs = [{"id": "q1", "question": "msbr salt?", "expected_citations": ["/MSR/"], "tags": ["subfolder:MSR"]}]
    rep = score_retrieval(qs, lambda q: [("/_____Literature_____/MSR/x.pdf", 0.8)])
    assert rep.cited_passed == 1 and rep.cited_total == 1
    assert rep.coverage == 1.0
    assert rep.by_cluster["MSR"] == (1, 1)


def test_coverage_fails_when_expected_not_retrieved():
    qs = [{"id": "q1", "question": "htgr?", "expected_citations": ["/HTGR/"], "tags": ["subfolder:HTGR"]}]
    rep = score_retrieval(qs, lambda q: [("/_____Literature_____/Fusion/y.pdf", 0.7)])
    assert rep.cited_passed == 0
    assert rep.by_cluster["HTGR"] == (0, 1)


def test_no_expected_citations_not_counted_in_coverage():
    qs = [{"id": "q1", "question": "general", "tags": []}]
    rep = score_retrieval(qs, lambda q: [("/_____Literature_____/MSR/z.pdf", 0.9)])
    assert rep.cited_total == 0
    assert rep.coverage == 1.0  # nothing to fail
    assert rep.results[0].cited is None


def test_per_cluster_breakdown_detects_uneven_coverage():
    qs = [
        {"id": "a", "question": "msr", "expected_citations": ["/MSR/"], "tags": ["subfolder:MSR"]},
        {"id": "b", "question": "htgr", "expected_citations": ["/HTGR/"], "tags": ["subfolder:HTGR"]},
    ]

    def retrieve(q):
        # MSR indexed, HTGR not (the partial-ingest failure mode)
        return [("/_____Literature_____/MSR/x.pdf", 0.8)] if "msr" in q else [("/_____Literature_____/MSR/x.pdf", 0.6)]

    rep = score_retrieval(qs, retrieve)
    assert rep.by_cluster["MSR"] == (1, 1)
    assert rep.by_cluster["HTGR"] == (0, 1)  # surfaced as a missing cluster
    assert rep.coverage == 0.5


def test_mean_top_sim():
    qs = [{"id": "a", "question": "x", "tags": []}, {"id": "b", "question": "y", "tags": []}]
    rep = score_retrieval(qs, lambda q: [("/p", 0.8 if q == "x" else 0.6)])
    assert abs(rep.mean_top_sim - 0.7) < 1e-9
