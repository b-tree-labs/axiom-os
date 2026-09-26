# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for AXI prep tools — FW-1 P2.

Chat tools that wrap the prep workflow so the instructor can drive
course-prep from a chat conversation. The tools export TOOLS + an
execute(name, params) dispatcher — the same contract every extension's
chat-tool module follows (declared via the ``[chat_tools]`` manifest section
and discovered by ``axiom.extensions.discovery.load_chat_tools``).
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.classroom.chat_tools import prep_tools
from axiom.extensions.builtins.classroom.demo import (
    DEMO_CLASSROOM_ID,
    DEMO_COURSE_ID,
)
from axiom.extensions.builtins.classroom.operational_store import load_course_data


@pytest.fixture(autouse=True)
def _isolated_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("AXIOM_RUNTIME_ROOT", str(tmp_path))
    import axiom.extensions.builtins.classroom.operational_store as store

    store._registry = None
    yield
    store._registry = None


# ---------------------------------------------------------------------------
# Tool manifest
# ---------------------------------------------------------------------------


class TestToolManifest:
    def test_exports_expected_tools(self):
        names = {t.name for t in prep_tools.TOOLS}
        assert names == {
            "classroom_prep_status",
            "classroom_list_courses",
            "classroom_demo_seed",
            "classroom_clone_from_demo",
            "classroom_prep_extract_syllabus",
            "classroom_prep_tune_prompt",
            "classroom_prep_configure_checkpoints",
            "classroom_prep_configure_rails",
            "classroom_prep_lms_setup",
            "classroom_prep_dry_run_enhanced",
            "classroom_publish",
            "classroom_archive",
            "classroom_wrap_analytics",
            "classroom_wrap_harvest",
            "classroom_wrap_grades",
            "classroom_wrap_template",
            "classroom_prep_edit_rail",
            "classroom_enroll",
        }

    def test_tools_have_descriptions(self):
        for t in prep_tools.TOOLS:
            assert t.description, f"tool {t.name!r} has no description"

    def test_tools_have_parameters_schema(self):
        for t in prep_tools.TOOLS:
            assert isinstance(t.parameters, dict)
            assert t.parameters.get("type") == "object"

    def test_read_vs_write_categorization(self):
        """Read tools are auto-approved; write tools need HITL (per RACI memory)."""
        by_name = {t.name: t for t in prep_tools.TOOLS}
        from axiom.infra.orchestrator.actions import ActionCategory

        assert by_name["classroom_prep_status"].category == ActionCategory.READ
        assert by_name["classroom_list_courses"].category == ActionCategory.READ
        assert by_name["classroom_prep_extract_syllabus"].category == ActionCategory.READ

        assert by_name["classroom_demo_seed"].category == ActionCategory.WRITE
        assert by_name["classroom_clone_from_demo"].category == ActionCategory.WRITE
        assert by_name["classroom_prep_tune_prompt"].category == ActionCategory.WRITE

    def test_unknown_tool_dispatch_returns_error(self):
        result = prep_tools.execute("classroom_nonexistent_tool", {})
        assert "error" in result


# ---------------------------------------------------------------------------
# classroom_prep_status
# ---------------------------------------------------------------------------


class TestPrepStatus:
    def test_status_on_demo_classroom(self):
        from axiom.extensions.builtins.classroom.demo import seed_demo

        seed_demo()
        result = prep_tools.execute(
            "classroom_prep_status", {"classroom_id": DEMO_CLASSROOM_ID}
        )
        assert result.get("classroom_id") == DEMO_CLASSROOM_ID
        assert result.get("course_id") == DEMO_COURSE_ID
        assert result.get("course_ready") is True
        assert result.get("classroom_ready") is True
        assert "checklist" in result
        assert len(result["checklist"]) >= 4

    def test_status_on_missing_classroom(self):
        result = prep_tools.execute(
            "classroom_prep_status", {"classroom_id": "nope"}
        )
        assert "error" in result
        assert "nope" in result["error"]

    def test_status_requires_classroom_id(self):
        result = prep_tools.execute("classroom_prep_status", {})
        assert "error" in result


# ---------------------------------------------------------------------------
# classroom_list_courses
# ---------------------------------------------------------------------------


class TestListCourses:
    def test_empty_list_when_no_courses(self):
        result = prep_tools.execute("classroom_list_courses", {})
        assert result["courses"] == []
        assert result["count"] == 0

    def test_lists_demo_after_seed(self):
        from axiom.extensions.builtins.classroom.demo import seed_demo

        seed_demo()
        result = prep_tools.execute("classroom_list_courses", {})
        ids = {c["id"] for c in result["courses"]}
        assert DEMO_COURSE_ID in ids
        assert result["count"] >= 1

    def test_includes_title_and_instructor(self):
        from axiom.extensions.builtins.classroom.demo import seed_demo

        seed_demo()
        result = prep_tools.execute("classroom_list_courses", {})
        for c in result["courses"]:
            assert "title" in c
            assert "instructor_id" in c


# ---------------------------------------------------------------------------
# classroom_demo_seed
# ---------------------------------------------------------------------------


class TestDemoSeed:
    def test_seeds_when_empty(self):
        result = prep_tools.execute("classroom_demo_seed", {})
        assert result["action"] == "seeded"
        assert result["course_id"] == DEMO_COURSE_ID
        assert result["classroom_id"] == DEMO_CLASSROOM_ID
        assert load_course_data(DEMO_COURSE_ID) is not None

    def test_reset_flag_calls_reset(self):
        prep_tools.execute("classroom_demo_seed", {})
        result = prep_tools.execute("classroom_demo_seed", {"reset": True})
        assert result["action"] == "reset"


# ---------------------------------------------------------------------------
# classroom_clone_from_demo
# ---------------------------------------------------------------------------


class TestCloneFromDemo:
    def test_clones_to_new_id(self):
        from axiom.extensions.builtins.classroom.demo import seed_demo

        seed_demo()
        result = prep_tools.execute(
            "classroom_clone_from_demo",
            {"new_course_id": "my-course", "instructor_id": "@ben:ut"},
        )
        assert result["cloned_course_id"] == "my-course"
        assert load_course_data("my-course") is not None

    def test_returns_error_on_collision(self):
        from axiom.extensions.builtins.classroom.demo import seed_demo

        seed_demo()
        prep_tools.execute(
            "classroom_clone_from_demo",
            {"new_course_id": "my-course", "instructor_id": "@ben:ut"},
        )
        result = prep_tools.execute(
            "classroom_clone_from_demo",
            {"new_course_id": "my-course", "instructor_id": "@ben:ut"},
        )
        assert "error" in result
        assert "already exists" in result["error"]

    def test_missing_params_return_error(self):
        result = prep_tools.execute("classroom_clone_from_demo", {})
        assert "error" in result


# ---------------------------------------------------------------------------
# classroom_prep_extract_syllabus
# ---------------------------------------------------------------------------


class TestExtractSyllabus:
    def test_returns_proposed_manifest(self, monkeypatch):
        """The tool runs syllabus_extraction and returns the proposed
        manifest — it does NOT save it. Instructor reviews, then calls
        a separate save tool (P3) or uses the CLI path."""
        from axiom.extensions.builtins.classroom import syllabus_extraction

        # Stub the extractor so the test doesn't depend on a real LLM.
        def _fake_extract(text, **kw):
            return syllabus_extraction.SyllabusManifest(
                course_title="Intro to Quantum Mechanics",
                learning_objectives=[
                    {"title": "Understand wave-particle duality.", "keywords": ["wave"]},
                    {"title": "Apply the Schrödinger equation.", "keywords": ["PDE"]},
                ],
                assessments=[],
                schedule=[],
            )

        monkeypatch.setattr(
            syllabus_extraction, "extract_syllabus_manifest", _fake_extract,
        )

        result = prep_tools.execute(
            "classroom_prep_extract_syllabus",
            {"syllabus_text": "Week 1: Wave-particle duality..."},
        )
        assert result.get("proposed_manifest")
        manifest = result["proposed_manifest"]
        assert manifest["course_title"] == "Intro to Quantum Mechanics"
        assert len(manifest["learning_objectives"]) == 2

    def test_missing_text_returns_error(self):
        result = prep_tools.execute("classroom_prep_extract_syllabus", {})
        assert "error" in result


# ---------------------------------------------------------------------------
# classroom_prep_tune_prompt
# ---------------------------------------------------------------------------


class TestTunePrompt:
    def test_stores_prompt_and_returns_test_response(self):
        from axiom.extensions.builtins.classroom.demo import clone_demo_course, seed_demo

        seed_demo()
        clone_demo_course(new_course_id="my-course", instructor_id="@ben:ut")

        result = prep_tools.execute(
            "classroom_prep_tune_prompt",
            {
                "course_id": "my-course",
                "system_prompt": "You are a patient and rigorous physics TA.",
                "test_query": "What is Newton's second law?",
            },
        )
        # Tool produces a test response so the instructor can iterate.
        assert result.get("course_id") == "my-course"
        assert result.get("system_prompt") == "You are a patient and rigorous physics TA."
        assert result.get("test_response")

        # And persists the prompt on the course.
        data = load_course_data("my-course")
        assert data["system_prompt"] == "You are a patient and rigorous physics TA."

    def test_missing_course_returns_error(self):
        result = prep_tools.execute(
            "classroom_prep_tune_prompt",
            {"course_id": "nope", "system_prompt": "x", "test_query": "y"},
        )
        assert "error" in result

    def test_missing_params_return_error(self):
        result = prep_tools.execute("classroom_prep_tune_prompt", {})
        assert "error" in result
