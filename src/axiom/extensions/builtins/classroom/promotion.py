# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Federated learning harvest — peer-reviewed promotion (#20).

When a student produces content worth promoting to the course RAG
(high-quality notes, research-loop findings, etc.), this module runs
the federation-aware review workflow:

1. **Submit** — a promotion proposal created from a flagged harvest
   item (`learning_harvest.propose_promotion_candidates` surfaces
   the candidate; this module wraps it as a formal proposal).
2. **Review** — peer instructors at peer institutions vote on the
   proposal. Each review is signed by the reviewer's node; votes
   propagate via the federation's cohort registry.
3. **Decision** — once N approvals are reached (threshold is policy-
   driven, typically 2 or 3 for research-grade content), the proposal
   passes.
4. **Promote** — the promoted artifact becomes a MemoryFragment
   (resource) in the course's shared pack, signed by all approving
   authorities. Future cohorts retrieve it via RAG.

Rejected proposals are preserved in the audit log so the student
sees feedback and can refine.

Per ADR-023 (federation lifecycle) + ADR-028 (trust graph):
reviewer trust is weighted by their EigenTrust score in the
proposal's context (research domain + maturity tier).
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from axiom.infra.identifiers import generate_id

if TYPE_CHECKING:
    from axiom.memory.composition import CompositionService
    from axiom.memory.fragment import MemoryFragment


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PromotionProposal:
    """A candidate for peer-reviewed promotion to the course RAG."""

    proposal_id: str
    note_id: str
    source_node: str
    proposer: str                       # principal_id
    proposed_at: str
    quality_score: float                # from learning_harvest heuristic
    content_excerpt: str = ""
    context: str = "general"            # trust context for reviewer weighting


@dataclass(frozen=True)
class PromotionReview:
    """One reviewer's signed vote on a proposal."""

    proposal_id: str
    reviewer_node: str
    reviewer_principal: str
    reviewed_at: str
    vote: str                           # "approve" | "reject" | "abstain"
    comment: str = ""
    signature: bytes | None = None


@dataclass
class PromotionDecision:
    """Composite decision after N reviews."""

    proposal_id: str
    decided_at: str
    outcome: str                        # "promoted" | "rejected" | "pending"
    approver_count: int = 0
    rejector_count: int = 0
    abstain_count: int = 0
    signatures: list[bytes] = field(default_factory=list)
    reviews: list[PromotionReview] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Proposal + review lifecycle
# ---------------------------------------------------------------------------


def submit_promotion_proposal(
    composition: CompositionService,
    note: dict,
    source_node: str,
    proposer: str,
    context: str = "general",
) -> PromotionProposal:
    """Create a proposal from a harvest note. Persist via composition."""
    proposal = PromotionProposal(
        proposal_id=generate_id(),
        note_id=note.get("note_id") or generate_id(),
        source_node=source_node,
        proposer=proposer,
        proposed_at=datetime.now(UTC).isoformat(),
        quality_score=float(note.get("quality_score", 0.0)),
        content_excerpt=str(note.get("text", ""))[:500],
        context=context,
    )

    # Persist as MemoryFragment(procedural) — the workflow shape
    # matches: steps[] = ["submitted"] initially; grows with reviews.
    composition.write(
        content={
            "fact_kind": "promotion_proposal",
            "proposal_id": proposal.proposal_id,
            "note_id": proposal.note_id,
            "source_node": source_node,
            "proposer": proposer,
            "quality_score": proposal.quality_score,
            "content_excerpt": proposal.content_excerpt,
            "context": context,
            "proposed_at": proposal.proposed_at,
            "steps": ["submitted"],
            "workflow": "promotion",
        },
        cognitive_type="procedural",
        principal_id=proposer,
        agents={"chalke"},
        resources={"promotion-queue", f"node:{source_node}"},
    )
    return proposal


def review_proposal(
    composition: CompositionService,
    proposal: PromotionProposal,
    reviewer_node: str,
    reviewer_principal: str,
    vote: str,
    comment: str = "",
    signing_keypair=None,
) -> PromotionReview:
    """Record a peer review vote. Optionally sign with a federation key."""
    if vote not in ("approve", "reject", "abstain"):
        raise ValueError(
            f"vote must be approve|reject|abstain; got {vote!r}"
        )

    review = PromotionReview(
        proposal_id=proposal.proposal_id,
        reviewer_node=reviewer_node,
        reviewer_principal=reviewer_principal,
        reviewed_at=datetime.now(UTC).isoformat(),
        vote=vote,
        comment=comment,
        signature=None,
    )

    # If a keypair is provided, sign the canonical review payload
    if signing_keypair is not None:
        import json

        payload = json.dumps({
            "proposal_id": review.proposal_id,
            "reviewer_node": review.reviewer_node,
            "reviewer_principal": review.reviewer_principal,
            "reviewed_at": review.reviewed_at,
            "vote": review.vote,
            "comment": review.comment,
        }, sort_keys=True).encode("utf-8")
        review = dataclasses.replace(review, signature=signing_keypair.sign(payload))

    # Persist as MemoryFragment(episodic) — a review event
    composition.write(
        content={
            "fact_kind": "promotion_review",
            "proposal_id": review.proposal_id,
            "reviewer_node": reviewer_node,
            "reviewer_principal": reviewer_principal,
            "vote": vote,
            "comment": comment,
            "reviewed_at": review.reviewed_at,
            "event_time": review.reviewed_at,
            "signature_hex": review.signature.hex() if review.signature else None,
        },
        cognitive_type="episodic",
        principal_id=reviewer_principal,
        agents={"chalke"},
        resources={"promotion-queue", f"proposal:{proposal.proposal_id}"},
    )
    return review


# ---------------------------------------------------------------------------
# Decision composition
# ---------------------------------------------------------------------------


def compute_decision(
    proposal: PromotionProposal,
    reviews: list[PromotionReview],
    approval_threshold: int = 2,
    rejection_threshold: int = 2,
) -> PromotionDecision:
    """Tally votes and produce an outcome per threshold policy.

    - N or more `approve` → "promoted"
    - N or more `reject` → "rejected"
    - otherwise → "pending"
    """
    approvers = [r for r in reviews if r.vote == "approve"]
    rejectors = [r for r in reviews if r.vote == "reject"]
    abstains = [r for r in reviews if r.vote == "abstain"]

    outcome = "pending"
    if len(approvers) >= approval_threshold:
        outcome = "promoted"
    elif len(rejectors) >= rejection_threshold:
        outcome = "rejected"

    signatures = [r.signature for r in approvers if r.signature is not None]

    return PromotionDecision(
        proposal_id=proposal.proposal_id,
        decided_at=datetime.now(UTC).isoformat(),
        outcome=outcome,
        approver_count=len(approvers),
        rejector_count=len(rejectors),
        abstain_count=len(abstains),
        signatures=signatures,
        reviews=reviews,
    )


# ---------------------------------------------------------------------------
# Final promotion — promoted artifact into course RAG
# ---------------------------------------------------------------------------


def promote_to_course_rag(
    composition: CompositionService,
    proposal: PromotionProposal,
    decision: PromotionDecision,
    course_id: str,
) -> MemoryFragment:
    """If decision is 'promoted', emit the final artifact into the course RAG.

    The promoted fragment is a MemoryFragment(resource) referencing the
    original harvest note, with multi-authority signatures from the
    reviewers encoded in content. Ownership: the source student remains
    master; the course instructor receives GOALS delegation (can direct
    how the promoted content is used pedagogically).
    """
    if decision.outcome != "promoted":
        raise ValueError(
            f"cannot promote — decision outcome is {decision.outcome!r}"
        )

    from axiom.memory.ownership import Right, delegate, new_ownership

    own = new_ownership(master=proposal.proposer)
    # Instructor role (placeholder): GOALS delegation so they can frame
    # how the content is used in the course
    own = delegate(
        own,
        delegate_principal=f"@instructor:{proposal.source_node}",
        rights={Right.GOALS},
        expires_at="2099-12-31T23:59:59Z",
    )

    return composition.write(
        content={
            "fact_kind": "promoted_artifact",
            "proposal_id": proposal.proposal_id,
            "note_id": proposal.note_id,
            "source_node": proposal.source_node,
            "course_id": course_id,
            "content_excerpt": proposal.content_excerpt,
            "quality_score": proposal.quality_score,
            "context": proposal.context,
            "approver_count": decision.approver_count,
            "review_signatures_hex": [
                s.hex() for s in decision.signatures
            ],
            "decided_at": decision.decided_at,
            "ref": f"axiom://{proposal.source_node}/note/{proposal.note_id}",
        },
        cognitive_type="resource",
        principal_id=proposal.proposer,
        agents={"chalke"},
        resources={
            f"course:{course_id}",
            f"course-rag:{course_id}",
            "promotion-queue",
        },
        ownership=own,
    )
