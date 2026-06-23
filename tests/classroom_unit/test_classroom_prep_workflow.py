# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for ClassroomPrepWorkflow (instance orchestrator)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class FakeRetriever:
    results: list[dict] = field(default_factory=list)

    def retrieve(self, query: str, k: int = 5) -> list[dict]:
        return list(self.results)


@dataclass
class FakeLLM:
    reply: str = "stubbed response"

    def __call__(self, messages: list[dict], **kw: Any) -> str:
        return self.reply


@dataclass
class FakeLMS:
    connected: bool = True
    roster: list[dict] = field(default_factory=list)

    def ping(self) -> bool:
        return self.connected

    def list_students(self, course_id: str) -> list[dict]:
        return list(self.roster)


def _fresh_wf():
    from axiom.extensions.builtins.classroom.classroom_prep_workflow import (
        ClassroomPrepWorkflow,
    )

    return ClassroomPrepWorkflow(
        instructor_id="ben@ut.edu",
        classroom_id="prague-s26",
        retriever=FakeRetriever(results=[{"text": "fission", "source": "ch1"}]),
        llm=FakeLLM(),
        lms=FakeLMS(roster=[{"id": "s1"}, {"id": "s2"}]),
    )


class TestSelectCourse:
    def test_publishable_course_marks_step_complete(self):
        wf = _fresh_wf()
        wf.select_course(course_id="ne-prague", course_version="1.0.0",
                         publishable=True, system_prompt="You are a tutor.")
        assert wf.checklist.steps[0].status == "completed"
        assert wf.course_id == "ne-prague"
        assert wf.course_version == "1.0.0"

    def test_unpublishable_course_marks_failed(self):
        wf = _fresh_wf()
        wf.select_course(course_id="x", course_version="0.1.0", publishable=False)
        assert wf.checklist.steps[0].status == "failed"


class TestRAGPolicy:
    def test_select_valid_mode(self):
        wf = _fresh_wf()
        wf.select_rag_policy("course_only")
        assert wf.rag_policy_mode == "course_only"
        assert wf.checklist.steps[1].status == "completed"

    def test_invalid_mode_raises(self):
        import pytest

        wf = _fresh_wf()
        with pytest.raises(ValueError, match="unknown"):
            wf.select_rag_policy("not_a_mode")


class TestLMSConnect:
    def test_connected_with_roster(self):
        wf = _fresh_wf()
        roster = wf.connect_lms(course_id="CANVAS-101")
        assert len(roster) == 2
        assert wf.checklist.steps[2].status == "completed"

    def test_disconnected_fails(self):
        from axiom.extensions.builtins.classroom.classroom_prep_workflow import (
            ClassroomPrepWorkflow,
        )

        wf = ClassroomPrepWorkflow(
            instructor_id="i", classroom_id="cr",
            retriever=FakeRetriever(), llm=FakeLLM(),
            lms=FakeLMS(connected=False),
        )
        wf.connect_lms(course_id="CX")
        assert wf.checklist.steps[2].status == "failed"


class TestDryRun:
    def test_dry_run_marks_complete(self):
        wf = _fresh_wf()
        wf.select_course("ne-prague", "1.0.0", publishable=True,
                         system_prompt="You are a tutor.")
        result = wf.run_dry_run(["What is fission?", "Explain critical mass."])
        assert result.turns == 2
        assert wf.checklist.steps[3].status == "completed"

    def test_dry_run_without_course_raises(self):
        import pytest

        wf = _fresh_wf()
        with pytest.raises(RuntimeError, match="course"):
            wf.run_dry_run(["What is fission?"])


class TestEndToEndReadiness:
    def test_ready_when_3_critical_complete(self):
        wf = _fresh_wf()
        wf.select_course("c", "1.0.0", publishable=True, system_prompt="p")
        wf.select_rag_policy("course_only")
        wf.connect_lms("CANVAS-1")
        ready, blockers = wf.is_ready_for_enrollment()
        assert ready is True
        assert blockers == []

    def test_not_ready_without_course(self):
        wf = _fresh_wf()
        wf.select_rag_policy("course_only")
        wf.connect_lms("CX")
        ready, blockers = wf.is_ready_for_enrollment()
        assert ready is False
        assert any("course" in b.lower() for b in blockers)


class TestSummary:
    def test_summary_exposes_classroom_and_course(self):
        wf = _fresh_wf()
        wf.select_course("ne-prague", "1.0.0", publishable=True, system_prompt="p")
        summary = wf.summary()
        assert summary["classroom_id"] == "prague-s26"
        assert summary["course_id"] == "ne-prague"
        assert summary["course_version"] == "1.0.0"
        assert len(summary["steps"]) == 4
