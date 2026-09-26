# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for instructor course conclusion workflow."""

from __future__ import annotations


class TestConclusionChecklist:
    def test_create_checklist(self):
        from axiom.extensions.builtins.classroom.course_conclusion import (
            create_conclusion_checklist,
        )

        cl = create_conclusion_checklist("ben@ut.edu", "ne-prague-2026")
        assert len(cl.steps) == 8
        assert all(s.status == "pending" for s in cl.steps)

    def test_step_names(self):
        from axiom.extensions.builtins.classroom.course_conclusion import (
            create_conclusion_checklist,
        )

        cl = create_conclusion_checklist("i", "c")
        names = [s.name for s in cl.steps]
        assert "final_grades_synced" in names
        assert "harvest_bundles_distributed" in names
        assert "classroom_archived" in names
        assert "alumni_transition" in names


class TestGradesStep:
    def test_all_grades_synced(self):
        from axiom.extensions.builtins.classroom.course_conclusion import (
            create_conclusion_checklist,
            validate_grades_step,
        )

        cl = create_conclusion_checklist("i", "c")
        cl = validate_grades_step(cl, grades_synced=True, student_count=12, grades_pushed=12)
        assert cl.steps[1].status == "completed"

    def test_partial_grades_warns(self):
        from axiom.extensions.builtins.classroom.course_conclusion import (
            create_conclusion_checklist,
            validate_grades_step,
        )

        cl = create_conclusion_checklist("i", "c")
        cl = validate_grades_step(cl, grades_synced=True, student_count=12, grades_pushed=10)
        assert cl.steps[1].status == "warning"

    def test_sync_failed(self):
        from axiom.extensions.builtins.classroom.course_conclusion import (
            create_conclusion_checklist,
            validate_grades_step,
        )

        cl = create_conclusion_checklist("i", "c")
        cl = validate_grades_step(cl, grades_synced=False, student_count=12, grades_pushed=0)
        assert cl.steps[1].status == "failed"


class TestHarvestStep:
    def test_all_bundles_generated(self):
        from axiom.extensions.builtins.classroom.course_conclusion import (
            create_conclusion_checklist,
            validate_harvest_step,
        )

        cl = create_conclusion_checklist("i", "c")
        cl = validate_harvest_step(cl, bundles_generated=13, student_count=12)
        assert cl.steps[5].status == "completed"

    def test_no_bundles_fails(self):
        from axiom.extensions.builtins.classroom.course_conclusion import (
            create_conclusion_checklist,
            validate_harvest_step,
        )

        cl = create_conclusion_checklist("i", "c")
        cl = validate_harvest_step(cl, bundles_generated=0, student_count=12)
        assert cl.steps[5].status == "failed"


class TestArchiveStep:
    def test_archived_completes(self):
        from axiom.extensions.builtins.classroom.course_conclusion import (
            create_conclusion_checklist,
            validate_archive_step,
        )

        cl = create_conclusion_checklist("i", "c")
        cl = validate_archive_step(cl, archived=True)
        assert cl.steps[6].status == "completed"


class TestCompletenessGate:
    def test_complete_when_critical_done(self):
        from axiom.extensions.builtins.classroom.course_conclusion import (
            check_conclusion_completeness,
            create_conclusion_checklist,
        )

        cl = create_conclusion_checklist("i", "c")
        for step in cl.steps:
            if step.critical:
                step.status = "completed"

        done, blockers = check_conclusion_completeness(cl)
        assert done is True
        assert len(blockers) == 0

    def test_incomplete_critical_blocks(self):
        from axiom.extensions.builtins.classroom.course_conclusion import (
            check_conclusion_completeness,
            create_conclusion_checklist,
        )

        cl = create_conclusion_checklist("i", "c")
        # Only archive done — grades and harvest still pending
        cl.steps[6].status = "completed"

        done, blockers = check_conclusion_completeness(cl)
        assert done is False
        assert len(blockers) >= 2
