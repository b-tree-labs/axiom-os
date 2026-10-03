# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for classroom enrollment — WF-1 + token auth.

The enrollment flow:
1. Instructor provides students.yaml (or Canvas roster sync)
2. System generates per-student auth tokens (TTL'd)
3. System provisions Open WebUI accounts
4. Nationality attestations are signed by instructor
5. Onboarding rails from Course manifest auto-apply to each student
6. Students receive enrollment email/URL with their token

Canvas is the roster source-of-truth (per feedback_canvas_integration_required).
"""

from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# TOKEN GENERATION
# ---------------------------------------------------------------------------


class TestTokenGeneration:
    def test_generates_unique_tokens_per_student(self):
        from axiom.extensions.builtins.classroom.enrollment import generate_student_tokens

        students = [
            {"student_id": "s1", "name": "Alice", "email": "alice@ut.edu"},
            {"student_id": "s2", "name": "Bob", "email": "bob@ut.edu"},
            {"student_id": "s3", "name": "Carol", "email": "carol@prague.cz"},
        ]
        tokens = generate_student_tokens(students, classroom_id="cls-2026-prague", ttl_days=30)

        assert len(tokens) == 3
        assert len(set(t.token for t in tokens)) == 3  # all unique
        assert all(t.classroom_id == "cls-2026-prague" for t in tokens)
        assert all(t.ttl_days == 30 for t in tokens)

    def test_token_includes_student_metadata(self):
        from axiom.extensions.builtins.classroom.enrollment import generate_student_tokens

        tokens = generate_student_tokens(
            [{"student_id": "s1", "name": "Alice", "email": "alice@ut.edu"}],
            classroom_id="cls-test",
            ttl_days=7,
        )
        t = tokens[0]
        assert t.student_id == "s1"
        assert t.name == "Alice"
        assert t.email == "alice@ut.edu"
        assert t.expires_at is not None  # ISO 8601 string

    def test_token_is_url_safe(self):
        from axiom.extensions.builtins.classroom.enrollment import generate_student_tokens

        tokens = generate_student_tokens(
            [{"student_id": "s1", "name": "A", "email": "a@b.com"}],
            classroom_id="c",
            ttl_days=1,
        )
        # Token should be safe for use in URLs (no +, /, =)
        assert all(c.isalnum() or c in "-_" for c in tokens[0].token)


# ---------------------------------------------------------------------------
# NATIONALITY ATTESTATION
# ---------------------------------------------------------------------------


class TestNationalityAttestation:
    def test_attest_nationality_creates_signed_record(self):
        from axiom.extensions.builtins.classroom.enrollment import (
            attest_nationality,
        )

        attestation = attest_nationality(
            student_id="s3",
            nationality="CZ",
            attested_by="instructor@ut.edu",
            classroom_id="cls-2026-prague",
        )
        assert attestation.student_id == "s3"
        assert attestation.nationality == "CZ"
        assert attestation.attested_by == "instructor@ut.edu"
        assert attestation.signed_at is not None

    def test_nationality_none_when_not_attested(self):
        """Students without an attestation have nationality=None."""
        from axiom.extensions.builtins.classroom.enrollment import (
            attest_nationality,
        )

        # Passing nationality=None explicitly means "not yet attested"
        attestation = attest_nationality(
            student_id="s1",
            nationality=None,
            attested_by="instructor@ut.edu",
            classroom_id="cls-test",
        )
        assert attestation.nationality is None


# ---------------------------------------------------------------------------
# ONBOARDING RAIL AUTO-APPLICATION
# ---------------------------------------------------------------------------


class TestOnboardingRails:
    def test_rails_from_course_manifest(self):
        from axiom.extensions.builtins.classroom.enrollment import (
            load_onboarding_rails,
        )

        manifest = {
            "onboarding_rails": [
                {
                    "id": "begin-of-course-interview",
                    "source": "course-template",
                    "required": True,
                    "questions": [
                        {
                            "id": "Q1",
                            "text": "How familiar are you with AI tools?",
                            "type": "free_text",
                        },
                    ],
                },
                {
                    "id": "data-consent",
                    "source": "axiom-core",
                    "required": True,
                    "questions": [
                        {
                            "id": "C1",
                            "text": "Do you consent to data collection?",
                            "type": "yes_no",
                        },
                    ],
                },
            ]
        }

        rails = load_onboarding_rails(manifest)
        assert len(rails) == 2
        assert rails[0].id == "begin-of-course-interview"
        assert rails[0].required is True
        assert len(rails[0].questions) == 1
        assert rails[1].id == "data-consent"

    def test_empty_manifest_returns_empty_rails(self):
        from axiom.extensions.builtins.classroom.enrollment import (
            load_onboarding_rails,
        )

        rails = load_onboarding_rails({})
        assert rails == []

    def test_rails_auto_apply_to_student(self):
        from axiom.extensions.builtins.classroom.enrollment import (
            apply_rails_to_student,
            load_onboarding_rails,
        )

        manifest = {
            "onboarding_rails": [
                {
                    "id": "interview",
                    "source": "custom",
                    "required": True,
                    "questions": [{"id": "Q1", "text": "Hello?", "type": "free_text"}],
                },
            ]
        }
        rails = load_onboarding_rails(manifest)
        checklist = apply_rails_to_student("s1", rails)

        assert len(checklist) == 1
        assert checklist[0].rail_id == "interview"
        assert checklist[0].student_id == "s1"
        assert checklist[0].status == "pending"


# ---------------------------------------------------------------------------
# FULL ENROLLMENT FLOW (Canvas → tokens → rails)
# ---------------------------------------------------------------------------


class TestFullEnrollmentFlow:
    def test_enroll_from_canvas_roster(self, canvas_mock):
        from axiom.extensions.builtins.classroom.enrollment import enroll_classroom
        from axiom.extensions.builtins.classroom.lms.canvas import CanvasLMSProvider

        provider = CanvasLMSProvider(
            {
                "api_url": canvas_mock.url,
                "api_token": "test",
                "_mock_server": canvas_mock,
            }
        )

        result = enroll_classroom(
            lms_provider=provider,
            canvas_course_id="101",
            classroom_id="cls-prague-2026",
            ttl_days=30,
            instructor_email="ben@ut.edu",
            nationality_map={"s3": "CZ"},  # Carol is Czech
            course_manifest={
                "onboarding_rails": [
                    {
                        "id": "interview",
                        "source": "custom",
                        "required": True,
                        "questions": [{"id": "Q1", "text": "Hi?", "type": "free_text"}],
                    },
                ]
            },
        )

        # 3 students from Canvas mock
        assert len(result.students) == 3
        assert len(result.tokens) == 3
        assert all(t.classroom_id == "cls-prague-2026" for t in result.tokens)

        # Nationality attestations
        attested = {a.student_id: a.nationality for a in result.attestations}
        assert attested["s3"] == "CZ"
        assert attested["s1"] is None  # not in nationality_map
        assert attested["s2"] is None

        # Onboarding rails applied to each student
        assert len(result.checklists) == 3  # one checklist per student
        assert all(len(c) == 1 for c in result.checklists)  # one rail each

    def test_enroll_empty_roster_returns_empty(self, canvas_mock):
        from axiom.extensions.builtins.classroom.enrollment import enroll_classroom
        from axiom.extensions.builtins.classroom.lms.canvas import CanvasLMSProvider

        provider = CanvasLMSProvider(
            {
                "api_url": canvas_mock.url,
                "api_token": "test",
                "_mock_server": canvas_mock,
            }
        )

        result = enroll_classroom(
            lms_provider=provider,
            canvas_course_id="999",  # empty course
            classroom_id="cls-empty",
            ttl_days=7,
            instructor_email="ben@ut.edu",
        )
        assert len(result.students) == 0
        assert len(result.tokens) == 0


# ---------------------------------------------------------------------------
# FIXTURES (reuse Canvas mock from test_lms_provider)
# ---------------------------------------------------------------------------


@pytest.fixture
def canvas_mock():
    from axiom.extensions.builtins.classroom.lms.canvas_mock import CanvasMockServer

    mock = CanvasMockServer()
    mock.add_course("101", "STEM Course 2026")
    mock.add_enrollment(
        "101",
        {"user_id": "s1", "name": "Alice", "email": "alice@ut.edu", "type": "StudentEnrollment"},
    )
    mock.add_enrollment(
        "101", {"user_id": "s2", "name": "Bob", "email": "bob@ut.edu", "type": "StudentEnrollment"}
    )
    mock.add_enrollment(
        "101",
        {"user_id": "s3", "name": "Carol", "email": "carol@prague.cz", "type": "StudentEnrollment"},
    )
    yield mock
