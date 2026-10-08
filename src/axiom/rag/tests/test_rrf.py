# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for Reciprocal Rank Fusion (T0-1).

RRF fuses multiple ranked lists into one, giving the combined ranking used
before a cross-encoder reranker:

    RRF(d) = Σ_{i} 1 / (k + rank_i(d))

where rank_i(d) is d's 1-based position in ranking i (∞ if absent) and
k = 60 is the standard hyperparameter.
"""

from __future__ import annotations

import pytest

from axiom.rag.rrf import reciprocal_rank_fusion


class TestRRFSingleRanking:
    def test_single_ranking_preserves_order(self):
        ranked = reciprocal_rank_fusion([["a", "b", "c", "d"]], k=60)
        assert [r.doc_id for r in ranked] == ["a", "b", "c", "d"]

    def test_single_ranking_first_has_highest_score(self):
        ranked = reciprocal_rank_fusion([["a", "b", "c"]], k=60)
        # rank 1 → 1/(60+1), rank 2 → 1/(60+2)
        assert ranked[0].score == pytest.approx(1 / 61)
        assert ranked[1].score == pytest.approx(1 / 62)


class TestRRFFusion:
    def test_two_rankings_agreement(self):
        """If two rankings agree, ordering is preserved and scores add."""
        ranked = reciprocal_rank_fusion(
            [["a", "b", "c"], ["a", "b", "c"]], k=60
        )
        assert [r.doc_id for r in ranked] == ["a", "b", "c"]
        # Each top doc gets 1/61 from both rankings.
        assert ranked[0].score == pytest.approx(2 / 61)

    def test_two_rankings_disagreement(self):
        """A doc ranked well in both lists beats one ranked #1 in only one."""
        # Vector: a=1, b=2, c=3, d=4
        # Text:   b=1, c=2, d=3, a=4
        # b is #1 + #2 → highest fused score; a is #1 + #4.
        ranked = reciprocal_rank_fusion(
            [["a", "b", "c", "d"], ["b", "c", "d", "a"]], k=60
        )
        assert ranked[0].doc_id == "b"

    def test_symmetric_disagreement_ties_go_to_first_seen(self):
        """Pure swap of rank-1 and rank-3 leaves the first ranking's head winning."""
        # Vector: a=1, b=2, c=3. Text: c=1, b=2, a=3.
        # a and c score identically; tie-break on first-seen (a).
        ranked = reciprocal_rank_fusion(
            [["a", "b", "c"], ["c", "b", "a"]], k=60
        )
        assert ranked[0].doc_id == "a"
        assert ranked[1].doc_id == "c"

    def test_doc_absent_from_one_list_still_ranked(self):
        """A doc in only one ranking still appears, but with lower score."""
        ranked = reciprocal_rank_fusion(
            [["a", "b"], ["a", "c"]], k=60
        )
        doc_ids = [r.doc_id for r in ranked]
        assert "b" in doc_ids
        assert "c" in doc_ids
        # a appears in both, so it ranks highest.
        assert ranked[0].doc_id == "a"


class TestRRFLimit:
    def test_limit_truncates(self):
        ranked = reciprocal_rank_fusion(
            [["a", "b", "c", "d", "e"]], k=60, limit=3
        )
        assert len(ranked) == 3
        assert [r.doc_id for r in ranked] == ["a", "b", "c"]


class TestRRFEdgeCases:
    def test_empty_rankings(self):
        assert reciprocal_rank_fusion([], k=60) == []

    def test_all_empty_lists(self):
        assert reciprocal_rank_fusion([[], []], k=60) == []

    def test_k_must_be_positive(self):
        with pytest.raises(ValueError, match="k"):
            reciprocal_rank_fusion([["a"]], k=0)
