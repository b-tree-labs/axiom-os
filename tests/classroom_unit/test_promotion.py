# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for federated peer-reviewed promotion (#20)."""

from __future__ import annotations

import pytest


@pytest.fixture
def composition(tmp_path, monkeypatch):
    monkeypatch.setenv("AXIOM_RUNTIME_ROOT", str(tmp_path))
    from axiom.extensions.builtins.classroom.composition_boot import (
        build_classroom_composition,
    )

    return build_classroom_composition(classroom_id="cr-promo")


class TestSubmitProposal:
    def test_submit_creates_proposal(self, composition):
        from axiom.extensions.builtins.classroom.promotion import (
            submit_promotion_proposal,
        )

        note = {
            "note_id": "n1",
            "text": "novel explanation of the core concept",
            "quality_score": 0.9,
        }
        proposal = submit_promotion_proposal(
            composition=composition,
            note=note,
            source_node="example-host.example-org",
            proposer="@alice:example-org",
        )
        assert proposal.proposal_id
        assert proposal.note_id == "n1"
        assert proposal.quality_score == 0.9
        assert proposal.source_node == "example-host.example-org"

    def test_proposal_persisted_as_fragment(self, composition):
        from axiom.extensions.builtins.classroom.promotion import (
            submit_promotion_proposal,
        )

        note = {"text": "x", "quality_score": 0.8}
        submit_promotion_proposal(
            composition=composition, note=note,
            source_node="example-host.example-org", proposer="@alice:example-org",
        )
        fragments = [
            a for a in composition.artifact_registry.list(kind="fragment")
            if a.data["content"].get("fact_kind") == "promotion_proposal"
        ]
        assert len(fragments) == 1


class TestReview:
    def _proposal(self, composition):
        from axiom.extensions.builtins.classroom.promotion import (
            submit_promotion_proposal,
        )

        return submit_promotion_proposal(
            composition=composition,
            note={"text": "x", "quality_score": 0.8},
            source_node="example-host.example-org",
            proposer="@alice:example-org",
        )

    def test_approve_review(self, composition):
        from axiom.extensions.builtins.classroom.promotion import review_proposal

        proposal = self._proposal(composition)
        review = review_proposal(
            composition=composition, proposal=proposal,
            reviewer_node="osu.edu", reviewer_principal="@bob:osu",
            vote="approve", comment="great insight",
        )
        assert review.vote == "approve"
        assert review.reviewer_node == "osu.edu"

    def test_invalid_vote_raises(self, composition):
        from axiom.extensions.builtins.classroom.promotion import review_proposal

        proposal = self._proposal(composition)
        with pytest.raises(ValueError, match="approve"):
            review_proposal(
                composition=composition, proposal=proposal,
                reviewer_node="osu.edu", reviewer_principal="@bob:osu",
                vote="supercalifragilistic",
            )

    def test_review_can_be_signed(self, composition):
        from axiom.extensions.builtins.classroom.promotion import review_proposal
        from axiom.vega.identity.keypair import generate_keypair

        kp = generate_keypair()
        proposal = self._proposal(composition)
        review = review_proposal(
            composition=composition, proposal=proposal,
            reviewer_node="osu.edu", reviewer_principal="@bob:osu",
            vote="approve", signing_keypair=kp,
        )
        assert review.signature is not None


class TestDecision:
    def _proposal(self, composition):
        from axiom.extensions.builtins.classroom.promotion import (
            submit_promotion_proposal,
        )

        return submit_promotion_proposal(
            composition=composition,
            note={"text": "x", "quality_score": 0.9},
            source_node="example-host.example-org",
            proposer="@alice:example-org",
        )

    def test_two_approvals_promoted(self, composition):
        from axiom.extensions.builtins.classroom.promotion import (
            compute_decision,
            review_proposal,
        )

        proposal = self._proposal(composition)
        reviews = [
            review_proposal(composition=composition, proposal=proposal,
                            reviewer_node="osu.edu",
                            reviewer_principal="@bob:osu", vote="approve"),
            review_proposal(composition=composition, proposal=proposal,
                            reviewer_node="inl.gov",
                            reviewer_principal="@chen:inl", vote="approve"),
        ]
        decision = compute_decision(proposal, reviews, approval_threshold=2)
        assert decision.outcome == "promoted"
        assert decision.approver_count == 2

    def test_two_rejections_rejected(self, composition):
        from axiom.extensions.builtins.classroom.promotion import (
            compute_decision,
            review_proposal,
        )

        proposal = self._proposal(composition)
        reviews = [
            review_proposal(composition=composition, proposal=proposal,
                            reviewer_node="osu.edu",
                            reviewer_principal="@bob:osu", vote="reject"),
            review_proposal(composition=composition, proposal=proposal,
                            reviewer_node="inl.gov",
                            reviewer_principal="@chen:inl", vote="reject"),
        ]
        decision = compute_decision(proposal, reviews, rejection_threshold=2)
        assert decision.outcome == "rejected"

    def test_mixed_pending(self, composition):
        from axiom.extensions.builtins.classroom.promotion import (
            compute_decision,
            review_proposal,
        )

        proposal = self._proposal(composition)
        reviews = [
            review_proposal(composition=composition, proposal=proposal,
                            reviewer_node="osu.edu",
                            reviewer_principal="@bob:osu", vote="approve"),
            review_proposal(composition=composition, proposal=proposal,
                            reviewer_node="inl.gov",
                            reviewer_principal="@chen:inl", vote="reject"),
        ]
        decision = compute_decision(proposal, reviews,
                                    approval_threshold=2, rejection_threshold=2)
        assert decision.outcome == "pending"


class TestPromote:
    def test_promoted_outcome_emits_resource_fragment(self, composition):
        from axiom.extensions.builtins.classroom.promotion import (
            compute_decision,
            promote_to_course_rag,
            review_proposal,
            submit_promotion_proposal,
        )

        proposal = submit_promotion_proposal(
            composition=composition,
            note={"text": "x", "quality_score": 0.9},
            source_node="example-host.example-org", proposer="@alice:example-org",
        )
        reviews = [
            review_proposal(composition=composition, proposal=proposal,
                            reviewer_node="osu.edu",
                            reviewer_principal="@bob:osu", vote="approve"),
            review_proposal(composition=composition, proposal=proposal,
                            reviewer_node="inl.gov",
                            reviewer_principal="@chen:inl", vote="approve"),
        ]
        decision = compute_decision(proposal, reviews)

        fragment = promote_to_course_rag(
            composition=composition, proposal=proposal, decision=decision,
            course_id="course-101",
        )
        assert fragment.cognitive_type.value == "resource"
        assert fragment.content["fact_kind"] == "promoted_artifact"
        assert fragment.content["approver_count"] == 2
        # Student retains mastership
        assert fragment.ownership.master == "@alice:example-org"

    def test_cannot_promote_pending(self, composition):
        from axiom.extensions.builtins.classroom.promotion import (
            PromotionDecision,
            promote_to_course_rag,
            submit_promotion_proposal,
        )

        proposal = submit_promotion_proposal(
            composition=composition,
            note={"text": "x", "quality_score": 0.5},
            source_node="example-host.example-org", proposer="@alice:example-org",
        )
        decision = PromotionDecision(
            proposal_id=proposal.proposal_id,
            decided_at="2026-04-17T10:00:00Z", outcome="pending",
        )
        with pytest.raises(ValueError, match="pending"):
            promote_to_course_rag(
                composition=composition, proposal=proposal, decision=decision,
                course_id="course-101",
            )
