# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for the LMS provider interface + Canvas implementation.

Written BEFORE the implementation exists. Run these, watch them fail,
then implement until they pass.

The LMS provider follows the ADR-012 ProviderBase pattern: Canvas is
one implementation; Moodle/Blackboard are future implementations.
"""

from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# 1. LMS PROVIDER INTERFACE (abstract contract)
# ---------------------------------------------------------------------------


class TestLMSProviderInterface:
    """The abstract interface every LMS provider must satisfy."""

    def test_import_base_class(self):
        from axiom.extensions.builtins.classroom.lms.base import LMSProvider

        assert hasattr(LMSProvider, "get_roster")
        assert hasattr(LMSProvider, "push_grade")
        assert hasattr(LMSProvider, "create_assignment")
        assert hasattr(LMSProvider, "get_student")
        assert hasattr(LMSProvider, "sync_enrollment_changes")

    def test_base_is_abstract(self):
        from axiom.extensions.builtins.classroom.lms.base import LMSProvider

        with pytest.raises(TypeError):
            LMSProvider({})  # type: ignore[abstract]


# ---------------------------------------------------------------------------
# 2. CANVAS PROVIDER (concrete implementation)
# ---------------------------------------------------------------------------


class TestCanvasProviderInit:
    def test_requires_api_url_and_token(self):
        from axiom.extensions.builtins.classroom.lms.canvas import CanvasLMSProvider

        with pytest.raises(ValueError, match="api_url"):
            CanvasLMSProvider({"api_token": "fake"})
        with pytest.raises(ValueError, match="api_token"):
            CanvasLMSProvider({"api_url": "https://canvas.example.com"})

    def test_valid_config_initializes(self):
        from axiom.extensions.builtins.classroom.lms.canvas import CanvasLMSProvider

        provider = CanvasLMSProvider(
            {
                "api_url": "https://canvas.example.com",
                "api_token": "fake-token-123",
                "name": "test-canvas",
            }
        )
        assert provider.name == "test-canvas"
        assert provider.available() is False  # no real Canvas at that URL


# ---------------------------------------------------------------------------
# 3. CANVAS ROSTER SYNC (with mock Canvas API)
# ---------------------------------------------------------------------------


class TestCanvasRosterSync:
    """Roster sync: Canvas → Axiom enrollment records."""

    def _make_provider(self, mock_server) :
        from axiom.extensions.builtins.classroom.lms.canvas import CanvasLMSProvider

        return CanvasLMSProvider(
            {
                "api_url": mock_server.url,
                "api_token": "test-token",
                "_mock_server": mock_server,
            }
        )

    def test_get_roster_returns_student_list(self, canvas_mock):
        """Canvas mock serves 3 enrolled students; adapter returns them."""
        provider = self._make_provider(canvas_mock)
        roster = provider.get_roster(course_id="101")

        assert len(roster) == 3
        assert all(hasattr(s, "student_id") for s in roster)
        assert all(hasattr(s, "name") for s in roster)
        assert all(hasattr(s, "email") for s in roster)
        # Nationality is NOT from Canvas — it's an Axiom-side attestation.
        # Canvas students should have nationality=None until attested.
        assert all(s.nationality is None for s in roster)

    def test_get_roster_empty_course(self, canvas_mock):
        provider = self._make_provider(canvas_mock)
        roster = provider.get_roster(course_id="999")
        assert roster == []

    def test_get_roster_filters_to_students_only(self, canvas_mock):
        """Canvas returns teachers + TAs + students; we only want students."""
        provider = self._make_provider(canvas_mock)
        roster = provider.get_roster(course_id="102")
        # Course 102 in mock has 2 students + 1 teacher + 1 TA
        assert len(roster) == 2
        assert all(s.role == "student" for s in roster)


# ---------------------------------------------------------------------------
# 4. CANVAS GRADE PUSH
# ---------------------------------------------------------------------------


class TestCanvasGradePush:
    def _make_provider(self, mock_server) :
        from axiom.extensions.builtins.classroom.lms.canvas import CanvasLMSProvider

        return CanvasLMSProvider(
            {
                "api_url": mock_server.url,
                "api_token": "test-token",
                "_mock_server": mock_server,
            }
        )

    def test_push_grade_succeeds(self, canvas_mock):
        provider = self._make_provider(canvas_mock)
        result = provider.push_grade(
            course_id="101",
            assignment_id="a1",
            student_id="s1",
            score=85.0,
            comment="Good work on the analysis.",
        )
        assert result.success is True
        assert result.canvas_submission_id is not None

    def test_push_grade_invalid_student_fails(self, canvas_mock):
        provider = self._make_provider(canvas_mock)
        result = provider.push_grade(
            course_id="101",
            assignment_id="a1",
            student_id="nonexistent",
            score=50.0,
        )
        assert result.success is False
        assert "not found" in result.message.lower()


# ---------------------------------------------------------------------------
# 5. CANVAS ASSIGNMENT CREATION
# ---------------------------------------------------------------------------


class TestCanvasAssignmentCreation:
    def _make_provider(self, mock_server) :
        from axiom.extensions.builtins.classroom.lms.canvas import CanvasLMSProvider

        return CanvasLMSProvider(
            {
                "api_url": mock_server.url,
                "api_token": "test-token",
                "_mock_server": mock_server,
            }
        )

    def test_create_assignment_from_manifest(self, canvas_mock):
        provider = self._make_provider(canvas_mock)
        result = provider.create_assignment(
            course_id="101",
            name="Mid-Course Quiz",
            description="Assessment of learning objectives 1-5",
            points_possible=100,
            due_at="2026-07-15T23:59:00Z",
        )
        assert result.success is True
        assert result.canvas_assignment_id is not None


# ---------------------------------------------------------------------------
# 6. ENROLLMENT CHANGE DETECTION
# ---------------------------------------------------------------------------


class TestCanvasEnrollmentChanges:
    def _make_provider(self, mock_server) :
        from axiom.extensions.builtins.classroom.lms.canvas import CanvasLMSProvider

        return CanvasLMSProvider(
            {
                "api_url": mock_server.url,
                "api_token": "test-token",
                "_mock_server": mock_server,
            }
        )

    def test_detect_new_enrollment(self, canvas_mock):
        """After initial sync, a new student appears in Canvas."""
        provider = self._make_provider(canvas_mock)
        # First sync
        initial = provider.get_roster(course_id="101")
        # Mock adds a student
        canvas_mock.add_enrollment(
            "101",
            {
                "user_id": "s4",
                "name": "New Student",
                "email": "new@example.com",
                "type": "StudentEnrollment",
            },
        )
        changes = provider.sync_enrollment_changes(
            course_id="101",
            known_student_ids=[s.student_id for s in initial],
        )
        assert len(changes.added) == 1
        assert changes.added[0].student_id == "s4"
        assert len(changes.dropped) == 0

    def test_detect_dropped_student(self, canvas_mock):
        provider = self._make_provider(canvas_mock)
        initial = provider.get_roster(course_id="101")
        canvas_mock.drop_enrollment("101", "s2")
        changes = provider.sync_enrollment_changes(
            course_id="101",
            known_student_ids=[s.student_id for s in initial],
        )
        assert len(changes.dropped) == 1
        assert changes.dropped[0].student_id == "s2"


# ---------------------------------------------------------------------------
# FIXTURES
# ---------------------------------------------------------------------------


@pytest.fixture
def canvas_mock():
    """A lightweight in-process mock Canvas API server.

    Serves roster, grade, assignment, and enrollment endpoints for test
    courses. NOT a real HTTP server — just a request-response simulator
    that the Canvas provider can talk to via a test adapter.
    """
    from axiom.extensions.builtins.classroom.lms.canvas_mock import CanvasMockServer

    mock = CanvasMockServer()
    # Pre-populate test data
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

    mock.add_course("102", "Mixed Roles Course")
    mock.add_enrollment(
        "102",
        {"user_id": "s5", "name": "Dave", "email": "dave@ut.edu", "type": "StudentEnrollment"},
    )
    mock.add_enrollment(
        "102", {"user_id": "s6", "name": "Eve", "email": "eve@ut.edu", "type": "StudentEnrollment"}
    )
    mock.add_enrollment(
        "102",
        {
            "user_id": "t1",
            "name": "Prof Smith",
            "email": "smith@ut.edu",
            "type": "TeacherEnrollment",
        },
    )
    mock.add_enrollment(
        "102",
        {"user_id": "ta1", "name": "TA Jones", "email": "jones@ut.edu", "type": "TaEnrollment"},
    )

    yield mock
