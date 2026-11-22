# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for #53 syllabus → manifest extraction.

Instructors shouldn't have to author YAML manifests by hand. They
upload a syllabus (PDF, markdown, or plain text) and this extractor
produces a structured ``SyllabusManifest`` — course title, code,
instructor, learning objectives, assessments, schedule. The manifest
seeds the classroom prep flow directly.

Uses ``structured_output`` under the hood (T0-4) for schema-validated
extraction — no regex parsing over LLM output.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from axiom.extensions.builtins.classroom.syllabus_extraction import (
    SyllabusManifest,
    extract_syllabus_manifest,
    manifest_to_yaml,
)
from axiom.infra.gateway import CompletionResponse, ToolUseBlock


def _gateway_returning(payload: dict) -> MagicMock:
    gw = MagicMock()
    gw.complete_with_tools.return_value = CompletionResponse(
        tool_use=[ToolUseBlock(
            tool_id="t1", name="emit_syllabus_manifest", input=payload,
        )],
        success=True,
    )
    return gw


_SAMPLE_PAYLOAD = {
    "course_title": "Introduction to Reactor Physics",
    "course_code": "NE-301",
    "instructor": "Dr. Jane Researcher",
    "instructor_email": "jane@example.edu",
    "learning_objectives": [
        {"id": "LO-01", "title": "Understand neutron transport",
         "keywords": ["neutron", "transport", "diffusion"]},
        {"id": "LO-02", "title": "Compute critical mass",
         "keywords": ["critical", "k-eff", "fission"]},
    ],
    "assessments": [
        {"type": "quiz", "title": "Quiz 1: Neutron Basics",
         "weight": 0.1, "due_date": "2026-02-10"},
        {"type": "exam", "title": "Midterm",
         "weight": 0.3, "due_date": "2026-03-15"},
    ],
    "schedule": [
        {"week": 1, "topic": "Atomic structure", "readings": ["Ch 1"]},
        {"week": 2, "topic": "Neutron interactions", "readings": ["Ch 2-3"]},
    ],
    "grading_policy": "Letter grade based on weighted average.",
}


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestExtraction:
    def test_returns_manifest_dataclass(self):
        gw = _gateway_returning(_SAMPLE_PAYLOAD)
        manifest = extract_syllabus_manifest(
            text="NE-301 Syllabus\nInstructor: Jane...", gateway=gw,
        )
        assert isinstance(manifest, SyllabusManifest)
        assert manifest.course_title == "Introduction to Reactor Physics"
        assert manifest.course_code == "NE-301"
        assert manifest.instructor == "Dr. Jane Researcher"

    def test_learning_objectives_parsed(self):
        gw = _gateway_returning(_SAMPLE_PAYLOAD)
        m = extract_syllabus_manifest(text="x", gateway=gw)
        assert len(m.learning_objectives) == 2
        assert m.learning_objectives[0]["id"] == "LO-01"
        assert "neutron" in m.learning_objectives[0]["keywords"]

    def test_assessments_parsed(self):
        gw = _gateway_returning(_SAMPLE_PAYLOAD)
        m = extract_syllabus_manifest(text="x", gateway=gw)
        assert len(m.assessments) == 2
        assert m.assessments[0]["type"] == "quiz"
        assert m.assessments[1]["weight"] == 0.3

    def test_schedule_parsed(self):
        gw = _gateway_returning(_SAMPLE_PAYLOAD)
        m = extract_syllabus_manifest(text="x", gateway=gw)
        assert len(m.schedule) == 2
        assert m.schedule[0]["week"] == 1


class TestUsesStructuredOutput:
    def test_sends_syllabus_text_to_gateway(self):
        gw = _gateway_returning(_SAMPLE_PAYLOAD)
        extract_syllabus_manifest(
            text="UNIQUE_SYLLABUS_TOKEN blah blah blah", gateway=gw,
        )
        prompt = gw.complete_with_tools.call_args.kwargs["messages"][0]["content"]
        assert "UNIQUE_SYLLABUS_TOKEN" in prompt

    def test_uses_emit_manifest_tool(self):
        gw = _gateway_returning(_SAMPLE_PAYLOAD)
        extract_syllabus_manifest(text="x", gateway=gw)
        tools = gw.complete_with_tools.call_args.kwargs["tools"]
        assert tools[0]["name"] == "emit_syllabus_manifest"


# ---------------------------------------------------------------------------
# Partial / empty inputs
# ---------------------------------------------------------------------------


class TestPartialInputs:
    def test_minimal_payload_accepted(self):
        """Only course_title is strictly required."""
        gw = _gateway_returning({
            "course_title": "Survey Course",
            "course_code": "",
            "instructor": "",
            "instructor_email": "",
            "learning_objectives": [],
            "assessments": [],
            "schedule": [],
        })
        m = extract_syllabus_manifest(text="x", gateway=gw)
        assert m.course_title == "Survey Course"
        assert m.learning_objectives == []

    def test_empty_text_raises(self):
        gw = MagicMock()
        with pytest.raises(ValueError, match="empty"):
            extract_syllabus_manifest(text="", gateway=gw)

    def test_provider_failure_raises(self):
        gw = MagicMock()
        gw.complete_with_tools.return_value = CompletionResponse(
            tool_use=[], success=False, error="no provider",
        )
        with pytest.raises(Exception):  # noqa: B017
            extract_syllabus_manifest(text="some syllabus", gateway=gw)


# ---------------------------------------------------------------------------
# Manifest → YAML for classroom prep
# ---------------------------------------------------------------------------


class TestManifestToYaml:
    def test_renders_yaml(self):
        m = SyllabusManifest(
            course_title="Test Course",
            course_code="TC-101",
            instructor="Jane",
            instructor_email="",
            learning_objectives=[{"id": "LO-1", "title": "x", "keywords": ["x"]}],
            assessments=[],
            schedule=[],
        )
        yml = manifest_to_yaml(m)
        assert "course_title:" in yml
        assert "Test Course" in yml
        assert "LO-1" in yml

    def test_seedable_by_classroom_prep(self):
        """The YAML shape matches what classroom_prep expects as seed input."""
        m = SyllabusManifest(
            course_title="T",
            course_code="T-1",
            instructor="J",
            instructor_email="j@e.edu",
            learning_objectives=[],
            assessments=[],
            schedule=[],
        )
        yml = manifest_to_yaml(m)
        # Top-level keys that classroom prep consumes:
        for key in ("course_title", "course_code", "instructor",
                    "learning_objectives", "assessments"):
            assert f"{key}:" in yml
