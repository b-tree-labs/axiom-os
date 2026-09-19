# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for classroom instance preparation (4 steps).

Distinct from course_prep (template authoring). Steps:
1. course_selected — pick a publishable course@version
2. rag_policy_selected — course_only / institutional / A/B / ...
3. lms_connected — Canvas roster preview
4. dry_run_passed — instructor-as-test-student (recommended, not critical)
"""

from __future__ import annotations


class TestChecklistCreation:
    def test_creates_with_4_pending_steps(self):
        from axiom.extensions.builtins.classroom.classroom_prep import (
            create_classroom_prep_checklist,
        )

        c = create_classroom_prep_checklist(
            instructor_id="ben@ut.edu", classroom_id="prague-s26"
        )
        assert c.instructor_id == "ben@ut.edu"
        assert c.classroom_id == "prague-s26"
        assert c.course_id is None  # not selected yet
        assert len(c.steps) == 4
        assert all(s.status == "pending" for s in c.steps)

    def test_step_names_match_flow(self):
        from axiom.extensions.builtins.classroom.classroom_prep import (
            create_classroom_prep_checklist,
        )

        c = create_classroom_prep_checklist("i", "cr")
        names = [s.name for s in c.steps]
        assert names == [
            "course_selected",
            "rag_policy_selected",
            "lms_connected",
            "dry_run_passed",
        ]


class TestCourseSelectedStep:
    def test_publishable_course_marks_complete(self):
        from axiom.extensions.builtins.classroom.classroom_prep import (
            create_classroom_prep_checklist,
            validate_course_selected_step,
        )

        c = create_classroom_prep_checklist("i", "cr")
        c = validate_course_selected_step(c, "ne-prague", "1.0.0", publishable=True)
        assert c.steps[0].status == "completed"
        assert c.course_id == "ne-prague"
        assert c.course_version == "1.0.0"

    def test_unpublishable_course_fails(self):
        from axiom.extensions.builtins.classroom.classroom_prep import (
            create_classroom_prep_checklist,
            validate_course_selected_step,
        )

        c = create_classroom_prep_checklist("i", "cr")
        c = validate_course_selected_step(c, "ne-prague", "0.1.0", publishable=False)
        assert c.steps[0].status == "failed"
        assert "publishable" in c.steps[0].message.lower()


class TestRAGPolicyStep:
    def test_selects_policy(self):
        from axiom.extensions.builtins.classroom.classroom_prep import (
            create_classroom_prep_checklist,
            validate_rag_policy_step,
        )

        c = create_classroom_prep_checklist("i", "cr")
        c = validate_rag_policy_step(c, policy_id="p1", policy_name="Course Only")
        assert c.steps[1].status == "completed"


class TestLMSStep:
    def test_connected_with_roster(self):
        from axiom.extensions.builtins.classroom.classroom_prep import (
            create_classroom_prep_checklist,
            validate_lms_step,
        )

        c = create_classroom_prep_checklist("i", "cr")
        c = validate_lms_step(c, lms_connected=True, roster_count=12)
        assert c.steps[2].status == "completed"

    def test_disconnected_fails(self):
        from axiom.extensions.builtins.classroom.classroom_prep import (
            create_classroom_prep_checklist,
            validate_lms_step,
        )

        c = create_classroom_prep_checklist("i", "cr")
        c = validate_lms_step(c, lms_connected=False, roster_count=0)
        assert c.steps[2].status == "failed"

    def test_empty_roster_warns(self):
        from axiom.extensions.builtins.classroom.classroom_prep import (
            create_classroom_prep_checklist,
            validate_lms_step,
        )

        c = create_classroom_prep_checklist("i", "cr")
        c = validate_lms_step(c, lms_connected=True, roster_count=0)
        assert c.steps[2].status == "warning"


class TestEnrollmentReadiness:
    def test_ready_with_3_critical_steps(self):
        from axiom.extensions.builtins.classroom.classroom_prep import (
            check_classroom_ready_for_enrollment,
            create_classroom_prep_checklist,
        )

        c = create_classroom_prep_checklist("i", "cr")
        for i in [0, 1, 2]:
            c.steps[i].status = "completed"
        ready, blockers = check_classroom_ready_for_enrollment(c)
        assert ready is True
        assert blockers == []

    def test_dry_run_not_required(self):
        from axiom.extensions.builtins.classroom.classroom_prep import (
            check_classroom_ready_for_enrollment,
            create_classroom_prep_checklist,
        )

        c = create_classroom_prep_checklist("i", "cr")
        for i in [0, 1, 2]:
            c.steps[i].status = "completed"
        # step 3 (dry-run) still pending
        ready, _ = check_classroom_ready_for_enrollment(c)
        assert ready is True

    def test_missing_lms_blocks(self):
        from axiom.extensions.builtins.classroom.classroom_prep import (
            check_classroom_ready_for_enrollment,
            create_classroom_prep_checklist,
        )

        c = create_classroom_prep_checklist("i", "cr")
        c.steps[0].status = "completed"
        c.steps[1].status = "completed"
        ready, blockers = check_classroom_ready_for_enrollment(c)
        assert ready is False
        assert any("lms" in b.lower() for b in blockers)
