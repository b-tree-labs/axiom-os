# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for LMSProvider write methods (Phase 0.2).

Per `feedback_lms_agnostic_design`, write methods live on the abstract
`LMSProvider` interface. Canvas is the first concrete implementation;
Moodle / Blackboard / Brightspace / Google Classroom plug into the
same shape. Tests run against `CanvasLMSProvider` + `CanvasMockServer`
since Canvas is the only adapter today.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def provider_with_course():
    from axiom.extensions.builtins.classroom.lms.canvas import CanvasLMSProvider
    from axiom.extensions.builtins.classroom.lms.canvas_mock import CanvasMockServer

    mock = CanvasMockServer()
    mock.add_course("c1", "NE 101")
    provider = CanvasLMSProvider({
        "api_url": "mock://canvas",
        "api_token": "test-token",
        "_mock_server": mock,
    })
    return mock, provider


class TestCreatePage:
    def test_creates_page_visible_via_get_pages(self, provider_with_course):
        mock, provider = provider_with_course
        result = provider.create_page(
            course_id="c1",
            title="Welcome",
            body="<h1>Welcome</h1><p>Hello.</p>",
        )
        assert result.success is True
        assert result.url_slug  # Canvas slugifies title
        assert result.lms_id  # the page's id

        pages = provider.get_pages("c1")
        titles = [p.title for p in pages]
        assert "Welcome" in titles


class TestUpdatePage:
    def test_updates_existing_page_body(self, provider_with_course):
        mock, provider = provider_with_course
        # Pre-existing page seeded via mock
        mock.add_page(
            "c1", page_url="syllabus", title="Syllabus",
            body="<p>Old body.</p>",
        )

        result = provider.update_page(
            course_id="c1",
            url_slug="syllabus",
            title="Syllabus",
            body="<p>New body.</p>",
        )
        assert result.success is True

        pages = provider.get_pages("c1")
        syl = next(p for p in pages if p.url_slug == "syllabus")
        assert "New body" in syl.body

    def test_update_unknown_page_fails(self, provider_with_course):
        _, provider = provider_with_course
        result = provider.update_page(
            course_id="c1",
            url_slug="does-not-exist",
            title="x",
            body="x",
        )
        assert result.success is False
        assert result.message  # non-empty diagnostic


class TestPostAnnouncement:
    def test_posts_announcement_visible_via_get_announcements(
        self, provider_with_course,
    ):
        _, provider = provider_with_course
        result = provider.post_announcement(
            course_id="c1",
            title="Reading reminder",
            message="<p>Don't forget the reading.</p>",
        )
        assert result.success is True
        assert result.lms_id

        anns = provider.get_announcements("c1")
        titles = [a.title for a in anns]
        assert "Reading reminder" in titles


class TestUpdateAssignmentDescription:
    def test_updates_assignment_description(self, provider_with_course):
        mock, provider = provider_with_course
        # Create an assignment first
        created = provider.create_assignment(
            course_id="c1",
            name="Problem set 1",
            description="Old description.",
        )
        assert created.success
        aid = created.canvas_assignment_id

        result = provider.update_assignment_description(
            course_id="c1",
            assignment_id=aid,
            description="Refined description with citations.",
        )
        assert result.success is True

    def test_update_unknown_assignment_fails(self, provider_with_course):
        _, provider = provider_with_course
        result = provider.update_assignment_description(
            course_id="c1",
            assignment_id="does-not-exist",
            description="x",
        )
        assert result.success is False
