# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for CoursePrepWorkflow orchestrator (5 template steps).

Classroom instance prep (RAG policy, LMS, dry-run) is tested
separately in test_classroom_prep_workflow.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class FakeCorpusIndexer:
    indexed: list[dict] = field(default_factory=list)

    def index(self, documents: list[dict]) -> int:
        self.indexed.extend(documents)
        return len(self.indexed)


@dataclass
class FakeCorpusRetriever:
    results: list[dict] = field(default_factory=list)
    empty: bool = False
    calls: list[str] = field(default_factory=list)

    def retrieve(self, query: str, k: int = 5) -> list[dict]:
        self.calls.append(query)
        if self.empty:
            return []
        return list(self.results)


@dataclass
class FakeLLMBackend:
    reply: str = "Fission is the splitting of heavy atomic nuclei."
    calls: list[dict] = field(default_factory=list)

    def __call__(self, messages: list[dict], **kw: Any) -> str:
        self.calls.append({"messages": messages, "kw": kw})
        return self.reply


def _fresh_wf():
    from axiom.extensions.builtins.classroom.course_prep_workflow import (
        CoursePrepWorkflow,
    )

    return CoursePrepWorkflow(
        instructor_id="ben@ut.edu",
        course_id="ne-prague-2026",
        indexer=FakeCorpusIndexer(),
        retriever=FakeCorpusRetriever(results=[{"text": "x", "source": "a"}]),
        llm=FakeLLMBackend(),
    )


class TestWorkflowConstruction:
    def test_creates_workflow_with_injected_deps(self):
        wf = _fresh_wf()
        assert wf.checklist.instructor_id == "ben@ut.edu"
        assert wf.checklist.course_id == "ne-prague-2026"
        assert len(wf.checklist.steps) == 5
        assert all(s.status == "pending" for s in wf.checklist.steps)


class TestManifestExecutor:
    def test_load_manifest_marks_complete(self):
        wf = _fresh_wf()
        manifest = {"id": "ne-prague", "title": "Prague NE", "version": "1.0.0"}
        wf.load_manifest(manifest)
        assert wf.checklist.steps[0].status == "completed"
        assert wf.manifest == manifest

    def test_invalid_manifest_fails(self):
        wf = _fresh_wf()
        wf.load_manifest({"title": "No id"})
        assert wf.checklist.steps[0].status == "failed"


class TestCorpusExecutor:
    def test_upload_and_preview_marks_complete(self):
        from axiom.extensions.builtins.classroom.course_prep_workflow import (
            CoursePrepWorkflow,
        )

        indexer = FakeCorpusIndexer()
        retriever = FakeCorpusRetriever(results=[{"text": "fission", "source": "ch1"}])
        wf = CoursePrepWorkflow("i", "c", indexer, retriever, FakeLLMBackend())

        wf.load_manifest({"id": "x", "title": "x", "version": "1.0.0"})
        wf.upload_corpus([{"text": "fission", "source": "ch1"},
                          {"text": "fusion", "source": "ch2"}])
        results = wf.preview_corpus("What is fission?")

        assert len(indexer.indexed) == 2
        assert results == [{"text": "fission", "source": "ch1"}]
        assert wf.checklist.steps[1].status == "completed"

    def test_preview_with_empty_retrieval_warns(self):
        from axiom.extensions.builtins.classroom.course_prep_workflow import (
            CoursePrepWorkflow,
        )

        wf = CoursePrepWorkflow("i", "c", FakeCorpusIndexer(),
                                FakeCorpusRetriever(empty=True), FakeLLMBackend())
        wf.upload_corpus([{"text": "stuff", "source": "a"}])
        wf.preview_corpus("nothing matches")
        assert wf.checklist.steps[1].status == "warning"


class TestPromptExecutor:
    def test_iterate_then_approve(self):
        wf = _fresh_wf()
        r1 = wf.test_prompt(system_prompt="Be helpful.", test_query="What is fission?")
        assert r1  # got a response
        assert wf.checklist.steps[2].status == "pending"

        wf.test_prompt("You are a tutor for nuclear engineering.", "What is fission?")
        wf.approve_prompt()
        assert wf.checklist.steps[2].status == "completed"
        assert wf.system_prompt == "You are a tutor for nuclear engineering."

    def test_cannot_approve_without_testing(self):
        import pytest

        wf = _fresh_wf()
        with pytest.raises(RuntimeError, match="test"):
            wf.approve_prompt()


class TestAssessmentExecutor:
    def test_define_assessments(self):
        wf = _fresh_wf()
        wf.define_assessment({"id": "pre-quiz", "type": "quiz", "week": 0})
        wf.define_assessment({"id": "post-quiz", "type": "quiz", "week": 4})
        assert len(wf.assessments) == 2
        assert wf.checklist.steps[3].status == "completed"

    def test_no_assessments_warns(self):
        wf = _fresh_wf()
        wf.skip_assessments()
        assert wf.checklist.steps[3].status == "warning"


class TestRailsExecutor:
    def test_configure_rails(self):
        wf = _fresh_wf()
        wf.configure_rails([
            {"id": "begin", "questions": [{"id": "q1", "text": "Background?", "type": "free_text"}]},
        ])
        assert len(wf.rails) == 1
        assert wf.checklist.steps[4].status == "completed"

    def test_use_defaults_warns(self):
        wf = _fresh_wf()
        wf.use_default_rails()
        assert wf.checklist.steps[4].status == "warning"


class TestEndToEndPublishReady:
    def test_full_flow_marks_publishable(self):
        wf = _fresh_wf()
        wf.load_manifest({"id": "c", "title": "C", "version": "1.0.0"})
        wf.upload_corpus([{"text": "x", "source": "a"}])
        wf.preview_corpus("test")
        wf.test_prompt("You are a tutor.", "test")
        wf.approve_prompt()

        ready, blockers = wf.is_ready_to_publish()
        assert ready is True
        assert blockers == []

    def test_missing_critical_blocks(self):
        wf = _fresh_wf()
        ready, blockers = wf.is_ready_to_publish()
        assert ready is False
        assert len(blockers) == 3  # manifest, corpus, prompt


class TestSummarySerialization:
    def test_summary_dict_exposes_status(self):
        wf = _fresh_wf()
        wf.load_manifest({"id": "x", "title": "x", "version": "1.0.0"})
        summary = wf.summary()

        assert summary["instructor_id"] == "ben@ut.edu"
        assert summary["course_id"] == "ne-prague-2026"
        assert summary["ready"] is False
        assert len(summary["steps"]) == 5
        assert summary["steps"][0]["status"] == "completed"
