# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for research bundle — IRB-aware dataset bundle.

Builds on trace_export.py to produce a complete research bundle:
- Traces CSV (per-turn + labels + topics)
- Quiz CSV (per-question scores)
- Interviews CSV (questionnaire responses — begin/mid/end interviews)
- manifest.json (IRB protocol, retention policy, consent ledger,
  pseudonym mapping hash)

Standalone; no network. Federation stretch: bundle can be uploaded
to a cross-node research sink via signed manifest.
"""

from __future__ import annotations

import csv
import json


def _trace(student_id, session_id):
    return {
        "trace_id": f"{session_id}-0",
        "student_id": student_id,
        "session_id": session_id,
        "session_type": "chat",
        "turn_index": 0,
        "timestamp": "2026-04-16T10:00:00+00:00",
        "tokens": {"prompt": 100, "completion": 50},
        "rag_results_count": 3,
        "labels": ["q_and_a"],
        "topics": ["LO-1"],
    }


def _quiz(student_id, score):
    from axiom.extensions.builtins.classroom.quiz_scoring import ScoredResponse

    return ScoredResponse(
        student_id=student_id, assessment_id="pre", question_id="Q1",
        question_type="mcq", final_score=score, needs_review=False,
    )


def _interview(student_id, session_id, question_id, answer):
    return {
        "session_id": session_id,
        "student_id": student_id,
        "instrument_id": "begin-interview",
        "question_id": question_id,
        "response": answer,
        "timestamp": "2026-04-16T10:00:00+00:00",
    }


class TestBundleContents:
    def test_bundle_includes_all_artifacts(self, tmp_path):
        from axiom.extensions.builtins.classroom.research_bundle import (
            build_research_bundle,
        )

        build_research_bundle(
            out_dir=tmp_path / "bundle",
            classroom_id="cr",
            traces=[_trace("s1", "a")],
            quiz_responses=[_quiz("s1", 0.75)],
            interview_responses=[_interview("s1", "i-1", "Q1", "physics bg")],
            irb_protocol="IRB-2026-001",
            retention_years=5,
            consent_version="gdpr-v1",
            consented_student_ids={"s1"},
            anonymize=True,
        )

        base = tmp_path / "bundle"
        assert (base / "traces.csv").exists()
        assert (base / "quiz.csv").exists()
        assert (base / "interviews.csv").exists()
        assert (base / "manifest.json").exists()

    def test_manifest_carries_irb_metadata(self, tmp_path):
        from axiom.extensions.builtins.classroom.research_bundle import (
            build_research_bundle,
        )

        build_research_bundle(
            out_dir=tmp_path / "bundle",
            classroom_id="cr",
            traces=[_trace("s1", "a")],
            quiz_responses=[],
            interview_responses=[],
            irb_protocol="IRB-2026-001",
            retention_years=5,
            consent_version="gdpr-v1",
            consented_student_ids={"s1"},
            anonymize=True,
        )

        manifest = json.loads(
            (tmp_path / "bundle" / "manifest.json").read_text()
        )
        assert manifest["irb_protocol"] == "IRB-2026-001"
        assert manifest["retention_years"] == 5
        assert manifest["consent_version"] == "gdpr-v1"
        assert manifest["anonymized"] is True
        assert manifest["classroom_id"] == "cr"
        assert "built_at" in manifest
        assert "consented_student_count" in manifest
        assert manifest["consented_student_count"] == 1


class TestConsentEnforced:
    def test_non_consenting_students_excluded(self, tmp_path):
        from axiom.extensions.builtins.classroom.research_bundle import (
            build_research_bundle,
        )

        build_research_bundle(
            out_dir=tmp_path / "b",
            classroom_id="cr",
            traces=[_trace("s1", "a"), _trace("s2", "b")],
            quiz_responses=[_quiz("s1", 0.5), _quiz("s2", 0.9)],
            interview_responses=[
                _interview("s1", "i1", "Q1", "x"),
                _interview("s2", "i2", "Q1", "y"),
            ],
            irb_protocol="IRB-1",
            retention_years=5,
            consent_version="v1",
            consented_student_ids={"s1"},  # only s1 consented
            anonymize=False,
        )

        with (tmp_path / "b" / "traces.csv").open() as f:
            trace_rows = list(csv.DictReader(f))
        with (tmp_path / "b" / "quiz.csv").open() as f:
            quiz_rows = list(csv.DictReader(f))
        with (tmp_path / "b" / "interviews.csv").open() as f:
            int_rows = list(csv.DictReader(f))

        # Only s1 should appear
        for rows in (trace_rows, quiz_rows, int_rows):
            assert all(r["student_id"] == "s1" for r in rows)


class TestPseudonymConsistencyAcrossArtifacts:
    def test_same_student_same_pseudonym_across_files(self, tmp_path):
        from axiom.extensions.builtins.classroom.research_bundle import (
            build_research_bundle,
        )

        build_research_bundle(
            out_dir=tmp_path / "b",
            classroom_id="cr",
            traces=[_trace("s1", "a")],
            quiz_responses=[_quiz("s1", 0.5)],
            interview_responses=[_interview("s1", "i1", "Q1", "x")],
            irb_protocol="IRB-1", retention_years=5, consent_version="v1",
            consented_student_ids={"s1"}, anonymize=True,
        )

        with (tmp_path / "b" / "traces.csv").open() as f:
            trace_pseudonym = next(csv.DictReader(f))["student_id"]
        with (tmp_path / "b" / "quiz.csv").open() as f:
            quiz_pseudonym = next(csv.DictReader(f))["student_id"]
        with (tmp_path / "b" / "interviews.csv").open() as f:
            interview_pseudonym = next(csv.DictReader(f))["student_id"]

        assert trace_pseudonym == quiz_pseudonym == interview_pseudonym
        assert trace_pseudonym.startswith("anon-")
