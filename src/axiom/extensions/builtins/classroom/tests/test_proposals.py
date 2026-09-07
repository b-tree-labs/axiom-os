# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for the LMS proposal queue (Phase 0.2).

A proposal is a queued change to an LMS resource (page edit, new
announcement, assignment description refinement, etc.) with explicit
provenance and an instructor-approval gate. Per
`feedback_lms_agnostic_design`, the model is LMS-neutral; Canvas is
the first push target but Moodle / Blackboard / etc. plug into the
same shape.

Storage: file-per-proposal under
``~/.axi/coordinator/classrooms/<cid>/proposals/``.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def store(tmp_path):
    from axiom.extensions.builtins.classroom.proposals import ProposalStore

    return ProposalStore(tmp_path / "proposals")


class TestProposalLifecycle:
    def test_create_proposal_starts_in_draft(self, store):
        from axiom.extensions.builtins.classroom.proposals import LMSProposal

        p = store.create(
            classroom_id="ne101",
            target="page",
            target_id="",
            action="create",
            title="Welcome",
            body="<h1>Welcome</h1>",
            created_by="instructor:ondrej",
        )
        assert isinstance(p, LMSProposal)
        assert p.proposal_id  # uuid generated
        assert p.status == "draft"
        assert p.target == "page"
        assert p.action == "create"

    def test_approve_transitions_draft_to_approved(self, store):
        p = store.create(
            classroom_id="ne101", target="page", target_id="", action="create",
            title="t", body="b", created_by="i",
        )
        store.approve(p.proposal_id, approver="instructor:ondrej")

        loaded = store.get(p.proposal_id)
        assert loaded.status == "approved"
        assert loaded.approved_by == "instructor:ondrej"
        assert loaded.approved_at  # timestamp set

    def test_reject_transitions_draft_to_rejected_with_reason(self, store):
        p = store.create(
            classroom_id="ne101", target="page", target_id="", action="create",
            title="t", body="b", created_by="i",
        )
        store.reject(p.proposal_id, reason="off-topic", rejecter="instructor:ondrej")

        loaded = store.get(p.proposal_id)
        assert loaded.status == "rejected"
        assert loaded.rejected_reason == "off-topic"

    def test_cannot_approve_a_rejected_proposal(self, store):
        p = store.create(
            classroom_id="ne101", target="page", target_id="", action="create",
            title="t", body="b", created_by="i",
        )
        store.reject(p.proposal_id, reason="x", rejecter="i")
        with pytest.raises(ValueError):
            store.approve(p.proposal_id, approver="i")

    def test_cannot_push_a_draft(self, store):
        p = store.create(
            classroom_id="ne101", target="page", target_id="", action="create",
            title="t", body="b", created_by="i",
        )
        with pytest.raises(ValueError):
            store.mark_pushed(p.proposal_id, lms_id="page-1")

    def test_mark_pushed_records_lms_id(self, store):
        p = store.create(
            classroom_id="ne101", target="page", target_id="", action="create",
            title="t", body="b", created_by="i",
        )
        store.approve(p.proposal_id, approver="i")
        store.mark_pushed(p.proposal_id, lms_id="page-42")

        loaded = store.get(p.proposal_id)
        assert loaded.status == "pushed"
        assert loaded.pushed_lms_id == "page-42"
        assert loaded.pushed_at


class TestQueueListing:
    def test_list_filters_by_classroom(self, store):
        store.create(
            classroom_id="ne101", target="page", target_id="", action="create",
            title="x", body="b", created_by="i",
        )
        store.create(
            classroom_id="cs100", target="page", target_id="", action="create",
            title="y", body="b", created_by="i",
        )

        ne = store.list(classroom_id="ne101")
        cs = store.list(classroom_id="cs100")
        assert len(ne) == 1
        assert len(cs) == 1
        assert ne[0].title == "x"

    def test_list_filters_by_status(self, store):
        a = store.create(
            classroom_id="ne101", target="page", target_id="", action="create",
            title="A", body="b", created_by="i",
        )
        store.create(
            classroom_id="ne101", target="page", target_id="", action="create",
            title="B", body="b", created_by="i",
        )
        store.approve(a.proposal_id, approver="i")

        drafts = store.list(classroom_id="ne101", status="draft")
        approved = store.list(classroom_id="ne101", status="approved")
        assert {p.title for p in drafts} == {"B"}
        assert {p.title for p in approved} == {"A"}


class TestProvenance:
    def test_provenance_round_trips(self, store):
        p = store.create(
            classroom_id="ne101", target="page", target_id="", action="create",
            title="t", body="b", created_by="chalke",
            provenance={
                "chat_session_id": "sess-1",
                "rag_citations": ["doc:syllabus#h1", "doc:lect-1#para-3"],
                "drafted_with_model": "qwen2.5:7b",
            },
        )
        loaded = store.get(p.proposal_id)
        assert loaded.provenance["chat_session_id"] == "sess-1"
        assert "doc:syllabus#h1" in loaded.provenance["rag_citations"]


class TestPersistence:
    def test_proposals_survive_store_reload(self, tmp_path):
        from axiom.extensions.builtins.classroom.proposals import ProposalStore

        s1 = ProposalStore(tmp_path / "proposals")
        p = s1.create(
            classroom_id="ne101", target="page", target_id="", action="create",
            title="A", body="b", created_by="i",
        )
        s1.approve(p.proposal_id, approver="i")

        s2 = ProposalStore(tmp_path / "proposals")
        loaded = s2.get(p.proposal_id)
        assert loaded.title == "A"
        assert loaded.status == "approved"
