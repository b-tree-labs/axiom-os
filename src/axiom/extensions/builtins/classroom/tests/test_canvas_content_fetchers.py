# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for CanvasLMSProvider's read-only content fetchers.

Phase 0.1 of Canvas integration: read modules, module items, files,
pages, and announcements so Keplo can reflect what's already in a
course without requiring duplicate upload. Foundation for
`axi classroom canvas pull` (ingest into RAG) and the Canvas MCP
server.

Tests run against the in-process CanvasMockServer; live Canvas paths
are exercised by integration tests separately.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def mock_with_course():
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


class TestGetModules:
    def test_empty_course_returns_empty_list(self, mock_with_course):
        mock, provider = mock_with_course
        assert provider.get_modules("c1") == []

    def test_returns_modules_in_position_order(self, mock_with_course):
        mock, provider = mock_with_course
        mock.add_module("c1", module_id="m2", name="Week 2", position=2)
        mock.add_module("c1", module_id="m1", name="Week 1", position=1)
        mock.add_module("c1", module_id="m3", name="Week 3", position=3)

        modules = provider.get_modules("c1")
        names = [m.name for m in modules]
        assert names == ["Week 1", "Week 2", "Week 3"]
        assert all(m.course_id == "c1" for m in modules)

    def test_unknown_course_returns_empty(self, mock_with_course):
        _, provider = mock_with_course
        assert provider.get_modules("does-not-exist") == []


class TestGetModuleItems:
    def test_returns_items_in_position_order(self, mock_with_course):
        mock, provider = mock_with_course
        mock.add_module("c1", module_id="m1", name="Week 1", position=1)
        mock.add_module_item(
            "c1", "m1",
            item_id="i2", type="Page", title="Reading 1.2",
            content_id="p2", position=2,
        )
        mock.add_module_item(
            "c1", "m1",
            item_id="i1", type="File", title="Slides 1",
            content_id="f1", position=1,
        )

        items = provider.get_module_items("c1", "m1")
        assert [i.title for i in items] == ["Slides 1", "Reading 1.2"]
        assert items[0].type == "File"
        assert items[1].type == "Page"


class TestGetFiles:
    def test_returns_all_files(self, mock_with_course):
        mock, provider = mock_with_course
        mock.add_file(
            "c1", file_id="f1", display_name="syllabus.pdf",
            content_type="application/pdf", size=4096, body=b"%PDF-1.4...",
        )
        mock.add_file(
            "c1", file_id="f2", display_name="lecture-1.html",
            content_type="text/html", size=1200,
            body=b"<html><body>Lecture 1</body></html>",
        )

        files = provider.get_files("c1")
        assert {f.display_name for f in files} == {"syllabus.pdf", "lecture-1.html"}
        assert all(f.course_id == "c1" for f in files)

    def test_get_file_content_returns_bytes(self, mock_with_course):
        mock, provider = mock_with_course
        mock.add_file(
            "c1", file_id="f1", display_name="syllabus.pdf",
            content_type="application/pdf", size=4096, body=b"%PDF-1.4 stub",
        )
        body = provider.get_file_content("c1", "f1")
        assert body == b"%PDF-1.4 stub"

    def test_get_file_content_missing_returns_none(self, mock_with_course):
        _, provider = mock_with_course
        assert provider.get_file_content("c1", "ghost") is None


class TestGetPages:
    def test_returns_pages_with_body(self, mock_with_course):
        mock, provider = mock_with_course
        mock.add_page(
            "c1", page_url="welcome", title="Welcome",
            body="<h1>Welcome</h1><p>Course intro.</p>",
        )
        mock.add_page(
            "c1", page_url="syllabus", title="Syllabus",
            body="<h1>Syllabus</h1><p>Schedule.</p>",
        )

        pages = provider.get_pages("c1")
        assert {p.title for p in pages} == {"Welcome", "Syllabus"}
        welcome = next(p for p in pages if p.title == "Welcome")
        assert "Course intro" in welcome.body


class TestGetAnnouncements:
    def test_returns_announcements_newest_first(self, mock_with_course):
        mock, provider = mock_with_course
        mock.add_announcement(
            "c1", announcement_id="a1", title="Week 1 reminder",
            message="<p>Reading by Friday.</p>",
            posted_at="2026-01-15T10:00:00Z",
            author="Ondrej",
        )
        mock.add_announcement(
            "c1", announcement_id="a2", title="Welcome",
            message="<p>Welcome to NE 101.</p>",
            posted_at="2026-01-10T09:00:00Z",
            author="Ondrej",
        )

        anns = provider.get_announcements("c1")
        assert [a.title for a in anns] == ["Week 1 reminder", "Welcome"]
        assert anns[0].author == "Ondrej"


class TestPaginationUnaffectedByMock:
    """Mock returns full lists; real Canvas paginates. The provider
    must not assume pagination on the mock path."""

    def test_no_pagination_failure_with_many_items(self, mock_with_course):
        mock, provider = mock_with_course
        for i in range(50):
            mock.add_file(
                "c1",
                file_id=f"f{i}",
                display_name=f"doc-{i:02d}.pdf",
                content_type="application/pdf",
                size=100,
                body=b"x",
            )
        assert len(provider.get_files("c1")) == 50
