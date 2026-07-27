# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Syllabus → classroom-manifest extraction (#53).

Instructors shouldn't have to hand-author YAML. They upload a syllabus
(PDF text, markdown, plain text) and this extractor produces a
structured :class:`SyllabusManifest` that seeds the classroom prep
flow directly. Uses :func:`axiom.infra.structured_output.structured_output`
under the hood so the emitted manifest adheres to a fixed schema — no
regex parsing over LLM output.

Downstream: :func:`manifest_to_yaml` renders the manifest in the shape
that ``axi classroom prep init --from <syllabus.yaml>`` consumes.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from axiom.infra.structured_output import structured_output


@dataclass
class SyllabusManifest:
    """Structured extraction of a course syllabus."""

    course_title: str
    course_code: str = ""
    instructor: str = ""
    instructor_email: str = ""
    learning_objectives: list[dict] = field(default_factory=list)
    assessments: list[dict] = field(default_factory=list)
    schedule: list[dict] = field(default_factory=list)
    grading_policy: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_SCHEMA = {
    "type": "object",
    "properties": {
        "course_title": {"type": "string"},
        "course_code": {"type": "string"},
        "instructor": {"type": "string"},
        "instructor_email": {"type": "string"},
        "learning_objectives": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "title": {"type": "string"},
                    "keywords": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["title"],
            },
        },
        "assessments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string"},
                    "title": {"type": "string"},
                    "weight": {"type": "number"},
                    "due_date": {"type": "string"},
                },
                "required": ["type", "title"],
            },
        },
        "schedule": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "week": {"type": "integer"},
                    "topic": {"type": "string"},
                    "readings": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["week", "topic"],
            },
        },
        "grading_policy": {"type": "string"},
    },
    "required": ["course_title"],
}


_SYSTEM = (
    "You extract structured syllabus data from course materials. "
    "Identify the course title (required), course code if present, "
    "instructor name and email, learning objectives, assessments "
    "(quizzes, exams, projects) with their weights and due dates if "
    "stated, and any week-by-week schedule. Do not invent data — leave "
    "a field empty if the syllabus does not state it. Call the "
    "emit_syllabus_manifest tool with the result."
)


def extract_syllabus_manifest(text: str, *, gateway) -> SyllabusManifest:
    """Extract a :class:`SyllabusManifest` from raw syllabus text."""
    if not text or not text.strip():
        raise ValueError("cannot extract from empty syllabus text")

    prompt = (
        "Extract the syllabus below into the emit_syllabus_manifest "
        "tool's schema. Preserve the instructor's exact wording for "
        "titles; generate stable IDs (LO-01, LO-02…) for learning "
        "objectives if none are given.\n\n"
        "SYLLABUS:\n" + text
    )

    result = structured_output(
        gateway=gateway,
        schema=_SCHEMA,
        messages=[{"role": "user", "content": prompt}],
        system=_SYSTEM,
        tool_name="emit_syllabus_manifest",
        task="extraction",
        max_tokens=3072,
    )
    data = result.value
    return SyllabusManifest(
        course_title=data.get("course_title", ""),
        course_code=data.get("course_code", ""),
        instructor=data.get("instructor", ""),
        instructor_email=data.get("instructor_email", ""),
        learning_objectives=list(data.get("learning_objectives", [])),
        assessments=list(data.get("assessments", [])),
        schedule=list(data.get("schedule", [])),
        grading_policy=data.get("grading_policy", ""),
    )


def manifest_to_yaml(manifest: SyllabusManifest) -> str:
    """Render a :class:`SyllabusManifest` as YAML for classroom prep seed."""
    try:
        import yaml  # type: ignore
    except ImportError:  # pragma: no cover — yaml should always be installed
        return _manifest_to_yaml_fallback(manifest)
    return yaml.safe_dump(
        manifest.to_dict(), sort_keys=False, default_flow_style=False,
    )


def _manifest_to_yaml_fallback(manifest: SyllabusManifest) -> str:
    """Minimal YAML serializer for environments without PyYAML."""
    lines = []
    for key, value in manifest.to_dict().items():
        if isinstance(value, list):
            lines.append(f"{key}:")
            for item in value:
                lines.append(f"  - {item!r}")
        else:
            lines.append(f"{key}: {value!r}")
    return "\n".join(lines) + "\n"
