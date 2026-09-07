# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for `pull_course_to_materials` — the bridge between
Canvas read-only fetchers and the classroom materials store.

Phase 0.1: pulls modules (as a synthesized outline doc), pages,
announcements, and files into the existing materials store so the
chunker + RAG pipeline downstream of `axi classroom prep corpus`
ingests Canvas content with no duplicate upload.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def populated_canvas(tmp_path):
    from axiom.extensions.builtins.classroom.classroom_materials import (
        ClassroomMaterialsStore,
    )
    from axiom.extensions.builtins.classroom.lms.canvas import CanvasLMSProvider
    from axiom.extensions.builtins.classroom.lms.canvas_mock import CanvasMockServer

    mock = CanvasMockServer()
    mock.add_course("c1", "NE 101")

    # Two modules with one item each
    mock.add_module("c1", module_id="m1", name="Week 1: Reactor Basics", position=1)
    mock.add_module("c1", module_id="m2", name="Week 2: Kinetics", position=2)
    mock.add_module_item(
        "c1", "m1", item_id="i1", type="Page", title="Welcome",
        content_id="welcome", position=1,
    )
    mock.add_module_item(
        "c1", "m2", item_id="i2", type="File", title="Slides 2",
        content_id="f1", position=1,
    )

    # Pages
    mock.add_page(
        "c1", page_url="welcome", title="Welcome",
        body="<h1>Welcome</h1><p>Welcome to NE 101.</p>",
    )
    mock.add_page(
        "c1", page_url="syllabus", title="Syllabus",
        body="<h1>Syllabus</h1><p>Schedule below.</p>",
    )

    # Announcements
    mock.add_announcement(
        "c1", announcement_id="a1", title="Reading reminder",
        message="<p>Don't forget the reading.</p>",
        posted_at="2026-01-15T10:00:00Z", author="Ondrej",
    )

    # Files
    mock.add_file(
        "c1", file_id="f1", display_name="slides-2.pdf",
        content_type="application/pdf", size=1024,
        body=b"%PDF-1.4 stub slides body",
    )

    provider = CanvasLMSProvider({
        "api_url": "mock://canvas",
        "api_token": "test-token",
        "_mock_server": mock,
    })
    store = ClassroomMaterialsStore(tmp_path / "classroom-c1")
    return mock, provider, store


class TestPullCourseToMaterials:
    def test_pulls_pages_announcements_files_and_outline(self, populated_canvas):
        from axiom.extensions.builtins.classroom.canvas_pull import (
            pull_course_to_materials,
        )

        _, provider, store = populated_canvas
        summary = pull_course_to_materials(provider, "c1", store)

        assert summary["pages"] == 2
        assert summary["announcements"] == 1
        assert summary["files"] == 1
        assert summary["outline"] == 1
        assert summary["total"] == 5

        filenames = {e.filename for e in store.list_entries()}
        assert "welcome.html" in filenames
        assert "syllabus.html" in filenames
        assert "announcement-a1.html" in filenames
        assert "slides-2.pdf" in filenames
        assert "course-outline.md" in filenames

    def test_outline_reflects_module_structure(self, populated_canvas):
        from axiom.extensions.builtins.classroom.canvas_pull import (
            pull_course_to_materials,
        )

        _, provider, store = populated_canvas
        pull_course_to_materials(provider, "c1", store)

        outline_entry = next(
            e for e in store.list_entries() if e.filename == "course-outline.md"
        )
        body = store.get_path(outline_entry.file_id).read_text()
        assert "Week 1: Reactor Basics" in body
        assert "Week 2: Kinetics" in body
        # Items appear under their module
        assert "Welcome" in body
        assert "Slides 2" in body

    def test_idempotent_on_repeat_pull(self, populated_canvas):
        """Pulling twice must not duplicate stored entries (content-hash dedup)."""
        from axiom.extensions.builtins.classroom.canvas_pull import (
            pull_course_to_materials,
        )

        _, provider, store = populated_canvas
        pull_course_to_materials(provider, "c1", store)
        first_count = len(store.list_entries())

        pull_course_to_materials(provider, "c1", store)
        second_count = len(store.list_entries())

        assert first_count == second_count


class TestPullEmptyCourseGracefullyHandled:
    def test_empty_course_returns_zero_summary(self, tmp_path):
        from axiom.extensions.builtins.classroom.canvas_pull import (
            pull_course_to_materials,
        )
        from axiom.extensions.builtins.classroom.classroom_materials import (
            ClassroomMaterialsStore,
        )
        from axiom.extensions.builtins.classroom.lms.canvas import CanvasLMSProvider
        from axiom.extensions.builtins.classroom.lms.canvas_mock import CanvasMockServer

        mock = CanvasMockServer()
        mock.add_course("empty", "Empty Course")
        provider = CanvasLMSProvider({
            "api_url": "mock://",
            "api_token": "x",
            "_mock_server": mock,
        })
        store = ClassroomMaterialsStore(tmp_path / "empty")

        summary = pull_course_to_materials(provider, "empty", store)
        # Empty course: no pages/announcements/files, AND no outline
        # (no modules → nothing to outline).
        assert summary["pages"] == 0
        assert summary["announcements"] == 0
        assert summary["files"] == 0
        assert summary["outline"] == 0
        assert summary["total"] == 0
