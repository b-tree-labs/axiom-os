# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for per-student learning harvest (§5.9 / spec-classroom.md).

Each student at course completion receives their `.axiompack` —
a portable bundle of their own learning record (chats, notes, quiz
scores, classifications). Signed by the course so the student can
import into their personal node and preserve their learning
trajectory beyond the course.

This is distinct from the research bundle (instructor/researcher
facing). Harvest = student facing, owned-by-student, signed-by-course.
"""

from __future__ import annotations

import json
import zipfile


def _trace(student_id, session_id, content="hi"):
    return {
        "trace_id": f"{session_id}-0",
        "student_id": student_id,
        "session_id": session_id,
        "session_type": "chat",
        "turn_index": 0,
        "timestamp": "2026-04-16T10:00:00+00:00",
        "content": content,
        "labels": ["q_and_a"],
        "topics": ["LO-1"],
    }


def _quiz(student_id, score):
    from axiom.extensions.builtins.classroom.quiz_scoring import ScoredResponse

    return ScoredResponse(
        student_id=student_id, assessment_id="pre", question_id="Q1",
        question_type="mcq", final_score=score, needs_review=False,
    )


class TestHarvestPerStudent:
    def test_per_student_bundle_isolation(self, tmp_path):
        from axiom.extensions.builtins.classroom.learning_harvest import (
            build_student_harvest,
        )

        # Two students, only s1's data in s1's bundle
        build_student_harvest(
            out_path=tmp_path / "s1.axiompack",
            student_id="s1",
            classroom_id="cr",
            course_id="course-ne",
            course_version="1.0.0",
            traces=[_trace("s1", "a"), _trace("s2", "b")],
            quiz_responses=[_quiz("s1", 0.8), _quiz("s2", 0.4)],
            notes=[],
            signer_node="example-host.example.org",
        )

        pack = tmp_path / "s1.axiompack"
        assert pack.exists()

        with zipfile.ZipFile(pack) as zf:
            names = zf.namelist()
            assert "manifest.json" in names
            assert "traces.json" in names
            assert "quiz.json" in names
            assert "notes.json" in names

            traces = json.loads(zf.read("traces.json"))
            assert all(t["student_id"] == "s1" for t in traces)
            assert len(traces) == 1  # only s1's trace

            quiz = json.loads(zf.read("quiz.json"))
            assert all(q["student_id"] == "s1" for q in quiz)
            assert len(quiz) == 1


class TestManifestContents:
    def test_manifest_includes_course_context_and_signer(self, tmp_path):
        from axiom.extensions.builtins.classroom.learning_harvest import (
            build_student_harvest,
        )

        build_student_harvest(
            out_path=tmp_path / "s1.axiompack",
            student_id="s1",
            classroom_id="cr",
            course_id="course-ne",
            course_version="1.0.0",
            traces=[],
            quiz_responses=[],
            notes=[],
            signer_node="example-host.example.org",
        )

        with zipfile.ZipFile(tmp_path / "s1.axiompack") as zf:
            manifest = json.loads(zf.read("manifest.json"))

        assert manifest["student_id"] == "s1"
        assert manifest["classroom_id"] == "cr"
        assert manifest["course_id"] == "course-ne"
        assert manifest["course_version"] == "1.0.0"
        assert manifest["signer_node"] == "example-host.example.org"
        assert manifest["format"] == "axiompack/student-harvest/v1"
        assert "built_at" in manifest
        assert "signature" in manifest  # reserved slot


class TestNotesInclusion:
    def test_student_notes_included(self, tmp_path):
        from axiom.extensions.builtins.classroom.learning_harvest import (
            build_student_harvest,
        )

        notes = [
            {"note_id": "n1", "student_id": "s1", "text": "my understanding of fission",
             "created_at": "2026-04-15T10:00:00Z"},
            {"note_id": "n2", "student_id": "s2", "text": "other student's note",
             "created_at": "2026-04-15T10:00:00Z"},
        ]
        build_student_harvest(
            out_path=tmp_path / "s1.axiompack",
            student_id="s1", classroom_id="cr",
            course_id="course-ne", course_version="1.0.0",
            traces=[], quiz_responses=[], notes=notes,
            signer_node="node",
        )

        with zipfile.ZipFile(tmp_path / "s1.axiompack") as zf:
            loaded = json.loads(zf.read("notes.json"))

        assert len(loaded) == 1
        assert loaded[0]["note_id"] == "n1"


class TestCohortHarvest:
    def test_build_all_students_in_one_pass(self, tmp_path):
        from axiom.extensions.builtins.classroom.learning_harvest import (
            build_cohort_harvest,
        )

        result = build_cohort_harvest(
            out_dir=tmp_path / "harvests",
            classroom_id="cr",
            course_id="course-ne",
            course_version="1.0.0",
            student_ids=["s1", "s2", "s3"],
            traces=[_trace("s1", "a"), _trace("s2", "b")],
            quiz_responses=[_quiz("s1", 0.5), _quiz("s3", 0.9)],
            notes=[],
            signer_node="node",
        )
        # Returns the set of paths written
        assert len(result.bundles) == 3
        for path in result.bundles.values():
            assert path.exists()


class TestFederationPromotionHook:
    """Stretch: harvest can propose peer-reviewed promotion of
    student content into the course RAG (§5.9)."""

    def test_flag_promotion_candidates(self, tmp_path):
        from axiom.extensions.builtins.classroom.learning_harvest import (
            propose_promotion_candidates,
        )

        # A student's note marked as high-quality is a promotion candidate
        notes = [
            {"note_id": "n1", "student_id": "s1", "text": "novel explanation",
             "quality_score": 0.9, "created_at": "2026-04-15T10:00:00Z"},
            {"note_id": "n2", "student_id": "s2", "text": "basic note",
             "quality_score": 0.3, "created_at": "2026-04-15T10:00:00Z"},
        ]
        candidates = propose_promotion_candidates(notes, threshold=0.7)
        assert len(candidates) == 1
        assert candidates[0]["note_id"] == "n1"
        assert candidates[0]["promotion_status"] == "proposed"
