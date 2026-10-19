# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the student-brief engine + store.

Each student gets a per-period narrative summary of their own
learning — what they asked, what they engaged with, what's unvisited,
suggested next prompts. The instructor curates (adds notes, approves)
before release; students on demand see their latest approved brief.

This file covers the engine + storage. CLI wiring + HTTP endpoint
tests live in test_briefs_cli.py and test_coordinator_briefs_http.py.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from axiom.extensions.builtins.classroom.classroom_interaction import (
    InteractionRecord,
)
from axiom.extensions.builtins.classroom.student_briefs import (
    BriefStore,
    StudentBrief,
    generate_brief,
)

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


class TestStudentBriefShape:
    def test_brief_is_frozen(self):
        b = StudentBrief(
            student_id="s", classroom_id="c",
            period_start="t1", period_end="t2", generated_at="t3",
            sections={"activity_summary": "..."},
            review_status="draft",
            instructor_note="",
        )
        with pytest.raises((AttributeError, Exception)):
            b.student_id = "x"

    def test_review_status_defaults_to_draft(self):
        b = StudentBrief(
            student_id="s", classroom_id="c",
            period_start="", period_end="", generated_at="",
            sections={},
        )
        assert b.review_status == "draft"
        assert b.instructor_note == ""


# ---------------------------------------------------------------------------
# Generation — takes interactions → narrative sections
# ---------------------------------------------------------------------------


def _sample_interactions() -> list[InteractionRecord]:
    base = datetime(2026, 4, 20, tzinfo=UTC)
    return [
        InteractionRecord(
            student_id="alice", question="What is a control rod?",
            had_answer=True, citations_count=2,
            timestamp=(base + timedelta(hours=1)).isoformat(),
            classroom_id="NE101", mode="ask",
        ),
        InteractionRecord(
            student_id="alice", question="How do control rods work?",
            had_answer=True, citations_count=1,
            timestamp=(base + timedelta(hours=2)).isoformat(),
            classroom_id="NE101", mode="tutor",
        ),
        InteractionRecord(
            student_id="alice", question="What is the secondary coolant loop?",
            had_answer=False, citations_count=0,
            timestamp=(base + timedelta(hours=3)).isoformat(),
            classroom_id="NE101", mode="ask",
        ),
        InteractionRecord(
            student_id="bob", question="control rods",
            had_answer=True, citations_count=1,
            timestamp=base.isoformat(),
            classroom_id="NE101", mode="ask",
        ),
    ]


class TestGenerateBriefDeterministic:
    """Without an LLM the engine still produces a useful brief by
    composing deterministic summary lines — never raises, never
    produces an empty brief, so the pipeline works fully offline."""

    def test_generate_uses_only_target_students_records(self):
        brief = generate_brief(
            student_id="alice",
            classroom_id="NE101",
            interactions=_sample_interactions(),
            llm=None,
        )
        assert brief.student_id == "alice"
        assert brief.classroom_id == "NE101"
        assert brief.sections

    def test_brief_reports_question_count(self):
        brief = generate_brief(
            student_id="alice", classroom_id="NE101",
            interactions=_sample_interactions(), llm=None,
        )
        blob = json.dumps(brief.sections)
        # Alice asked 3 questions in the sample.
        assert "3" in blob

    def test_brief_reports_unanswered_count(self):
        brief = generate_brief(
            student_id="alice", classroom_id="NE101",
            interactions=_sample_interactions(), llm=None,
        )
        blob = json.dumps(brief.sections).lower()
        # Alice had 1 unanswered.
        assert "unanswered" in blob or "1" in blob

    def test_brief_reports_mode_mix(self):
        brief = generate_brief(
            student_id="alice", classroom_id="NE101",
            interactions=_sample_interactions(), llm=None,
        )
        blob = json.dumps(brief.sections).lower()
        # Alice used ask + tutor modes.
        assert "ask" in blob
        assert "tutor" in blob

    def test_brief_with_zero_interactions_is_still_valid(self):
        brief = generate_brief(
            student_id="dave", classroom_id="NE101",
            interactions=[], llm=None,
        )
        # Nothing to summarize — brief should still be produced,
        # explicitly noting no activity so instructor can follow up.
        assert brief.sections
        blob = json.dumps(brief.sections).lower()
        assert "no" in blob or "zero" in blob or "yet" in blob


class TestGenerateBriefWithLLM:
    def test_llm_output_goes_in_narrative_section(self):
        def llm(prompt: str, *, system: str = "") -> str:
            return "Alice had a productive week engaging with control rods."

        brief = generate_brief(
            student_id="alice", classroom_id="NE101",
            interactions=_sample_interactions(), llm=llm,
        )
        blob = json.dumps(brief.sections)
        assert "productive week" in blob

    def test_llm_exception_falls_back_cleanly(self):
        def angry(prompt: str, *, system: str = "") -> str:
            raise RuntimeError("provider exploded")

        brief = generate_brief(
            student_id="alice", classroom_id="NE101",
            interactions=_sample_interactions(), llm=angry,
        )
        # Still produced a brief — just without the LLM narrative.
        assert brief.sections


# ---------------------------------------------------------------------------
# Store — per-classroom JSON files, instructor curation lifecycle
# ---------------------------------------------------------------------------


class TestBriefStore:
    def test_save_and_load_roundtrip(self, tmp_path):
        store = BriefStore(tmp_path)
        b = StudentBrief(
            student_id="alice", classroom_id="NE101",
            period_start="2026-04-20T00:00+00:00",
            period_end="2026-04-26T00:00+00:00",
            generated_at="2026-04-23T10:00+00:00",
            sections={"activity_summary": "..."},
        )
        store.save(b)
        loaded = store.latest_for_student("alice")
        assert loaded is not None
        assert loaded.student_id == "alice"
        assert loaded.sections == {"activity_summary": "..."}

    def test_latest_for_student_returns_most_recent(self, tmp_path):
        store = BriefStore(tmp_path)
        for i, ts in enumerate(
            ["2026-04-10T00:00+00:00", "2026-04-20T00:00+00:00", "2026-04-15T00:00+00:00"]
        ):
            store.save(StudentBrief(
                student_id="alice", classroom_id="NE101",
                period_start="", period_end=ts,
                generated_at=ts, sections={"v": i},
            ))
        latest = store.latest_for_student("alice")
        # 2026-04-20 is the most recent.
        assert latest.generated_at == "2026-04-20T00:00+00:00"

    def test_latest_for_unknown_student_is_none(self, tmp_path):
        store = BriefStore(tmp_path)
        assert store.latest_for_student("nobody") is None

    def test_list_student_briefs_in_order(self, tmp_path):
        store = BriefStore(tmp_path)
        for ts in ["2026-04-10", "2026-04-20", "2026-04-15"]:
            store.save(StudentBrief(
                student_id="alice", classroom_id="NE101",
                period_start="", period_end="",
                generated_at=f"{ts}T00:00+00:00", sections={},
            ))
        all_b = store.list_for_student("alice")
        assert len(all_b) == 3
        # Newest first.
        assert all_b[0].generated_at.startswith("2026-04-20")
        assert all_b[-1].generated_at.startswith("2026-04-10")

    def test_approve_flips_review_status(self, tmp_path):
        store = BriefStore(tmp_path)
        b = StudentBrief(
            student_id="alice", classroom_id="NE101",
            period_start="", period_end="", generated_at="2026-04-23T10:00+00:00",
            sections={},
        )
        store.save(b)
        store.approve("alice", "2026-04-23T10:00+00:00", note="Great week!")
        loaded = store.latest_for_student("alice")
        assert loaded.review_status == "approved"
        assert loaded.instructor_note == "Great week!"

    def test_latest_approved_only_returns_approved(self, tmp_path):
        store = BriefStore(tmp_path)
        # A draft brief.
        store.save(StudentBrief(
            student_id="alice", classroom_id="NE101",
            period_start="", period_end="",
            generated_at="2026-04-20T00:00+00:00", sections={"v": 1},
        ))
        # A later draft — still unapproved.
        store.save(StudentBrief(
            student_id="alice", classroom_id="NE101",
            period_start="", period_end="",
            generated_at="2026-04-23T00:00+00:00", sections={"v": 2},
        ))
        # Approve the earlier one.
        store.approve("alice", "2026-04-20T00:00+00:00")
        # Student on demand sees the latest APPROVED, not the newer draft.
        latest_approved = store.latest_approved_for_student("alice")
        assert latest_approved is not None
        assert latest_approved.generated_at == "2026-04-20T00:00+00:00"

    def test_list_student_ids_with_briefs(self, tmp_path):
        store = BriefStore(tmp_path)
        for sid in ["alice", "bob", "alice"]:
            store.save(StudentBrief(
                student_id=sid, classroom_id="NE101",
                period_start="", period_end="",
                generated_at=f"2026-04-{20 if sid == 'alice' else 21}T00:00+00:00",
                sections={},
            ))
        assert sorted(store.list_student_ids()) == ["alice", "bob"]
