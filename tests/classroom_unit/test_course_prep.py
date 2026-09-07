# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for course template preparation (5 steps).

A Course is the reusable template. Classroom (instance) preparation
is tested separately in test_classroom_prep.py.

Steps covered here:
1. Course manifest loaded + validated
2. Corpus uploaded + RAG indexed + retrieval preview tested
3. System prompt set + test query validated
4. Assessments defined (schedule, rubrics)
5. Onboarding rails configured (question bank selection)
"""

from __future__ import annotations


class TestCoursePrepWorkflow:
    def test_create_prep_checklist(self):
        from axiom.extensions.builtins.classroom.course_prep import (
            create_prep_checklist,
        )

        checklist = create_prep_checklist(instructor_id="ben@ut.edu", course_id="ne-prague-2026")

        assert checklist.instructor_id == "ben@ut.edu"
        assert checklist.course_id == "ne-prague-2026"
        assert len(checklist.steps) == 5
        assert all(s.status == "pending" for s in checklist.steps)

    def test_step_names_match_flow(self):
        from axiom.extensions.builtins.classroom.course_prep import create_prep_checklist

        checklist = create_prep_checklist("i", "c")
        names = [s.name for s in checklist.steps]

        assert names == [
            "manifest_loaded",
            "corpus_indexed",
            "system_prompt_set",
            "assessments_defined",
            "onboarding_rails_configured",
        ]


class TestManifestStep:
    def test_validate_manifest_marks_complete(self):
        from axiom.extensions.builtins.classroom.course_prep import (
            create_prep_checklist,
            validate_manifest_step,
        )

        manifest = {
            "id": "test",
            "title": "Test",
            "version": "1.0.0",
            "system_prompt": "You are a tutor.",
        }
        checklist = create_prep_checklist("i", "c")
        checklist = validate_manifest_step(checklist, manifest)

        assert checklist.steps[0].status == "completed"

    def test_invalid_manifest_fails_step(self):
        from axiom.extensions.builtins.classroom.course_prep import (
            create_prep_checklist,
            validate_manifest_step,
        )

        checklist = create_prep_checklist("i", "c")
        checklist = validate_manifest_step(checklist, {"title": "Missing ID"})

        assert checklist.steps[0].status == "failed"
        assert "id" in checklist.steps[0].message.lower()


class TestCorpusStep:
    def test_corpus_preview_marks_complete(self):
        from axiom.extensions.builtins.classroom.course_prep import (
            create_prep_checklist,
            validate_corpus_step,
        )

        checklist = create_prep_checklist("i", "c")
        checklist = validate_corpus_step(
            checklist,
            corpus_doc_count=3,
            test_query="What is fission?",
            test_results=[{"text": "Fission splits atoms.", "source": "ch3"}],
        )

        assert checklist.steps[1].status == "completed"
        assert "3 documents" in checklist.steps[1].message

    def test_empty_corpus_warns(self):
        from axiom.extensions.builtins.classroom.course_prep import (
            create_prep_checklist,
            validate_corpus_step,
        )

        checklist = create_prep_checklist("i", "c")
        checklist = validate_corpus_step(
            checklist, corpus_doc_count=0, test_query="", test_results=[]
        )

        assert checklist.steps[1].status == "warning"


class TestSystemPromptStep:
    def test_prompt_test_passes(self):
        from axiom.extensions.builtins.classroom.course_prep import (
            create_prep_checklist,
            validate_prompt_step,
        )

        checklist = create_prep_checklist("i", "c")
        checklist = validate_prompt_step(
            checklist,
            system_prompt="You are a tutor for nuclear engineering.",
            test_response="Fission is the process of splitting heavy atomic nuclei.",
            instructor_approved=True,
        )

        assert checklist.steps[2].status == "completed"

    def test_prompt_not_approved_stays_pending(self):
        from axiom.extensions.builtins.classroom.course_prep import (
            create_prep_checklist,
            validate_prompt_step,
        )

        checklist = create_prep_checklist("i", "c")
        checklist = validate_prompt_step(
            checklist,
            system_prompt="You are a tutor.",
            test_response="Some response.",
            instructor_approved=False,
        )

        assert checklist.steps[2].status == "pending"


class TestPublishReadiness:
    def test_ready_when_critical_steps_complete(self):
        from axiom.extensions.builtins.classroom.course_prep import (
            check_course_ready_to_publish,
            create_prep_checklist,
        )

        checklist = create_prep_checklist("i", "c")
        # Complete the 3 critical steps (0-manifest, 1-corpus, 2-prompt)
        for i in [0, 1, 2]:
            checklist.steps[i].status = "completed"

        ready, blockers = check_course_ready_to_publish(checklist)
        assert ready is True
        assert len(blockers) == 0

    def test_not_ready_when_manifest_incomplete(self):
        from axiom.extensions.builtins.classroom.course_prep import (
            check_course_ready_to_publish,
            create_prep_checklist,
        )

        checklist = create_prep_checklist("i", "c")
        # Only corpus done — manifest still pending
        checklist.steps[1].status = "completed"

        ready, blockers = check_course_ready_to_publish(checklist)
        assert ready is False
        assert any("manifest" in b.lower() for b in blockers)

    def test_non_critical_steps_dont_block(self):
        from axiom.extensions.builtins.classroom.course_prep import (
            check_course_ready_to_publish,
            create_prep_checklist,
        )

        checklist = create_prep_checklist("i", "c")
        # Complete critical steps, leave assessments + rails pending
        for i in [0, 1, 2]:
            checklist.steps[i].status = "completed"

        ready, _ = check_course_ready_to_publish(checklist)
        assert ready is True
