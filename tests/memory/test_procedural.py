# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for procedural effectiveness score (#44).

Per MIRIX: procedural memories track success + failure counts and
expose effectiveness_score = success / (success + failure). Low-
effectiveness procedures get demotion-flagged for peer review
(federated-learning-harvest, task #20).
"""

from __future__ import annotations

import pytest


def _procedural(success=0, failure=0):
    from axiom.memory.fragment import create_fragment

    content = {
        "workflow_name": "deploy",
        "steps": ["checkout", "test", "push"],
    }
    if success:
        content["success_count"] = success
    if failure:
        content["failure_count"] = failure
    return create_fragment(
        content=content,
        cognitive_type="procedural",
        principal_id="u1", agents={"tidy"}, resources=set(),
    )


class TestEffectivenessScore:
    def test_no_data_returns_none(self):
        from axiom.memory.procedural import effectiveness

        assert effectiveness(_procedural()) is None

    def test_all_successes(self):
        from axiom.memory.procedural import effectiveness

        assert effectiveness(_procedural(success=10)) == 1.0

    def test_all_failures(self):
        from axiom.memory.procedural import effectiveness

        assert effectiveness(_procedural(failure=5)) == 0.0

    def test_mixed(self):
        from axiom.memory.procedural import effectiveness

        assert effectiveness(_procedural(success=7, failure=3)) == 0.7


class TestRecordOutcome:
    def test_success_increments(self):
        from axiom.memory.procedural import effectiveness, record_outcome

        f = _procedural(success=4, failure=1)
        f2 = record_outcome(f, succeeded=True)
        assert f2.content["success_count"] == 5
        assert f2.content["failure_count"] == 1
        assert abs(effectiveness(f2) - 5/6) < 1e-9

    def test_failure_increments(self):
        from axiom.memory.procedural import effectiveness, record_outcome

        f = _procedural()
        f2 = record_outcome(f, succeeded=False)
        assert f2.content["success_count"] == 0
        assert f2.content["failure_count"] == 1
        assert effectiveness(f2) == 0.0

    def test_non_procedural_raises(self):
        from axiom.memory.fragment import create_fragment
        from axiom.memory.procedural import record_outcome

        semantic = create_fragment(
            content={"fact": "x"}, cognitive_type="semantic",
            principal_id="u1", agents=set(), resources=set(),
        )
        with pytest.raises(ValueError, match="procedural"):
            record_outcome(semantic, succeeded=True)

    def test_outcome_clears_signature(self):
        import dataclasses

        from axiom.memory.procedural import record_outcome

        f = _procedural(success=1)
        f = dataclasses.replace(f, signature="old-sig")
        f2 = record_outcome(f, succeeded=True)
        assert f2.signature is None  # caller re-signs


class TestDemotionCandidates:
    def test_below_threshold_flagged(self):
        from axiom.memory.procedural import demotion_candidates

        bad = _procedural(success=2, failure=8)  # 0.2
        ok = _procedural(success=9, failure=1)   # 0.9
        candidates = demotion_candidates([bad, ok], threshold=0.5)
        assert len(candidates) == 1
        assert candidates[0].id == bad.id

    def test_sparse_data_skipped_by_min_runs(self):
        """Require N runs before we trust the effectiveness score."""
        from axiom.memory.procedural import demotion_candidates

        # 1 failure, 0 successes = 0.0 but only 1 run — not enough
        sparse = _procedural(failure=1)
        candidates = demotion_candidates([sparse], threshold=0.5, min_runs=5)
        assert candidates == []

    def test_non_procedural_ignored(self):
        from axiom.memory.fragment import create_fragment
        from axiom.memory.procedural import demotion_candidates

        # Semantic with no effectiveness data
        semantic = create_fragment(
            content={"fact": "x"}, cognitive_type="semantic",
            principal_id="u1", agents=set(), resources=set(),
        )
        # Procedural that would be flagged
        bad_proc = _procedural(success=1, failure=9)
        candidates = demotion_candidates([semantic, bad_proc], threshold=0.5)
        assert len(candidates) == 1
        assert candidates[0].id == bad_proc.id


class TestEffectivenessField:
    def test_with_score_computed_into_field(self):
        """Helper: bake effectiveness_score into the fragment's reserved slot."""

        from axiom.memory.procedural import with_effectiveness_score

        f = _procedural(success=8, failure=2)
        f2 = with_effectiveness_score(f)
        assert f2.effectiveness_score == 0.8
