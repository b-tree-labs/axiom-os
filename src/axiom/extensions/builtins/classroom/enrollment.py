# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Classroom enrollment — WF-1 token auth + onboarding rails.

Orchestrates: Canvas roster → student tokens → nationality attestations
→ onboarding rail application → enrollment result.

This module is the implementation of prd-classroom §5.2 (auth tier 0)
and §5.11.1 (federation ephemeral lifecycle mapping for enrollment).
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from .lms.base import LMSProvider, LMSStudent

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class StudentToken:
    """Per-student auth token for classroom access."""

    student_id: str
    name: str
    email: str
    token: str  # URL-safe, unique
    classroom_id: str
    ttl_days: int
    issued_at: str  # ISO 8601
    expires_at: str  # ISO 8601


@dataclass
class NationalityAttestation:
    """Instructor-signed nationality attestation for export-control gating."""

    student_id: str
    nationality: str | None  # ISO 3166-1 alpha-2, or None if not attested
    attested_by: str  # instructor email
    classroom_id: str
    signed_at: str  # ISO 8601


@dataclass
class OnboardingQuestion:
    """Single question in an onboarding rail."""

    id: str
    text: str
    type: str  # "free_text", "likert", "yes_no"
    condition: str | None = None  # e.g. "Q3 == yes"
    scale: list | None = None  # for likert
    anchors: list[str] | None = None


@dataclass
class OnboardingRail:
    """A named sequence of questions that auto-applies to new students."""

    id: str
    source: str  # "axiom-core", "course-template", "example-question-bank", "custom"
    required: bool
    questions: list[OnboardingQuestion]


@dataclass
class StudentRailChecklist:
    """Tracks a student's progress on one onboarding rail."""

    rail_id: str
    student_id: str
    status: str = "pending"  # "pending", "in_progress", "completed", "skipped"
    responses: dict[str, Any] = field(default_factory=dict)


@dataclass
class EnrollmentResult:
    """Result of enrolling a classroom from a Canvas roster."""

    students: list[LMSStudent]
    tokens: list[StudentToken]
    attestations: list[NationalityAttestation]
    checklists: list[list[StudentRailChecklist]]  # one list per student


# ---------------------------------------------------------------------------
# Token generation
# ---------------------------------------------------------------------------


def generate_student_tokens(
    students: list[dict[str, str]],
    classroom_id: str,
    ttl_days: int = 30,
) -> list[StudentToken]:
    """Generate unique, URL-safe auth tokens for each student."""
    now = datetime.now(UTC)
    expires = now + timedelta(days=ttl_days)

    tokens = []
    for s in students:
        token = secrets.token_urlsafe(32)  # 43 chars, URL-safe
        tokens.append(
            StudentToken(
                student_id=s["student_id"],
                name=s.get("name", ""),
                email=s.get("email", ""),
                token=token,
                classroom_id=classroom_id,
                ttl_days=ttl_days,
                issued_at=now.isoformat(),
                expires_at=expires.isoformat(),
            )
        )
    return tokens


# ---------------------------------------------------------------------------
# Nationality attestation
# ---------------------------------------------------------------------------


def attest_nationality(
    student_id: str,
    nationality: str | None,
    attested_by: str,
    classroom_id: str,
) -> NationalityAttestation:
    """Create an instructor-signed nationality attestation."""
    return NationalityAttestation(
        student_id=student_id,
        nationality=nationality,
        attested_by=attested_by,
        classroom_id=classroom_id,
        signed_at=datetime.now(UTC).isoformat(),
    )


# ---------------------------------------------------------------------------
# Onboarding rails
# ---------------------------------------------------------------------------


def load_onboarding_rails(course_manifest: dict) -> list[OnboardingRail]:
    """Load onboarding rails from a Course manifest dict."""
    raw_rails = course_manifest.get("onboarding_rails", [])
    rails = []
    for r in raw_rails:
        questions = [
            OnboardingQuestion(
                id=q["id"],
                text=q["text"],
                type=q["type"],
                condition=q.get("condition"),
                scale=q.get("scale"),
                anchors=q.get("anchors"),
            )
            for q in r.get("questions", [])
        ]
        rails.append(
            OnboardingRail(
                id=r["id"],
                source=r.get("source", "custom"),
                required=r.get("required", False),
                questions=questions,
            )
        )
    return rails


def apply_rails_to_student(
    student_id: str,
    rails: list[OnboardingRail],
) -> list[StudentRailChecklist]:
    """Create a checklist for a student from the onboarding rails."""
    return [StudentRailChecklist(rail_id=rail.id, student_id=student_id) for rail in rails]


# ---------------------------------------------------------------------------
# Full enrollment orchestration
# ---------------------------------------------------------------------------


def enroll_classroom(
    lms_provider: LMSProvider,
    canvas_course_id: str,
    classroom_id: str,
    ttl_days: int = 30,
    instructor_email: str = "",
    nationality_map: dict[str, str] | None = None,
    course_manifest: dict | None = None,
) -> EnrollmentResult:
    """Full WF-1 enrollment: Canvas roster → tokens → attestations → rails.

    Args:
        lms_provider: Canvas (or other LMS) provider instance.
        canvas_course_id: Course ID in the LMS.
        classroom_id: Axiom classroom federation ID.
        ttl_days: Token lifetime in days.
        instructor_email: Who signs nationality attestations.
        nationality_map: {student_id: ISO-3166-alpha-2} for known nationalities.
        course_manifest: Course manifest dict with onboarding_rails.

    Returns:
        EnrollmentResult with students, tokens, attestations, and checklists.
    """
    nationality_map = nationality_map or {}
    course_manifest = course_manifest or {}

    # 1. Fetch roster from Canvas
    students = lms_provider.get_roster(canvas_course_id)

    # 2. Generate tokens
    student_dicts = [
        {"student_id": s.student_id, "name": s.name, "email": s.email} for s in students
    ]
    tokens = generate_student_tokens(student_dicts, classroom_id, ttl_days)

    # 3. Nationality attestations
    attestations = [
        attest_nationality(
            student_id=s.student_id,
            nationality=nationality_map.get(s.student_id),
            attested_by=instructor_email,
            classroom_id=classroom_id,
        )
        for s in students
    ]

    # 4. Onboarding rails
    rails = load_onboarding_rails(course_manifest)
    checklists = [apply_rails_to_student(s.student_id, rails) for s in students]

    return EnrollmentResult(
        students=students,
        tokens=tokens,
        attestations=attestations,
        checklists=checklists,
    )
