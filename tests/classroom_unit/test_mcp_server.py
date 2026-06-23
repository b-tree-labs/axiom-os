# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for classroom MCP server tool functions.

Pure-python tool functions are tested directly (the stdio wiring is
smoke-tested indirectly). No running MCP transport.
"""

from __future__ import annotations


def _seed_course_and_classroom(
    *, classroom_id, classroom_data, course_id, course_data,
):
    """Helper: register a classroom + course via the operational store."""
    from axiom.extensions.builtins.classroom.operational_store import _reg

    reg = _reg()
    reg.register(kind="classroom", name=classroom_id, data=classroom_data)
    reg.register(kind="course", name=course_id, data=course_data)


def test_list_sessions_returns_persisted_state(runtime_dir, tmp_path, monkeypatch):
    monkeypatch.setenv("AXIOM_RUNTIME_ROOT", str(runtime_dir))
    from axiom.extensions.builtins.classroom import mcp_server

    _seed_course_and_classroom(
        classroom_id="cr-1",
        classroom_data={
            "id": "cr-1", "slug": "ne-prague", "title": "NE Prague",
            "instructor_id": "ben@ut.edu", "course_id": "co-1",
        },
        course_id="co-1",
        course_data={
            "id": "co-1", "slug": "ne-prague", "title": "NE Prague",
        },
    )

    result = mcp_server.list_sessions()
    assert len(result["classrooms"]) == 1
    assert result["classrooms"][0]["slug"] == "ne-prague"
    assert len(result["courses"]) == 1


def test_prep_status_combines_course_and_classroom(runtime_dir, monkeypatch):
    monkeypatch.setenv("AXIOM_RUNTIME_ROOT", str(runtime_dir))
    from axiom.extensions.builtins.classroom import mcp_server

    _seed_course_and_classroom(
        classroom_id="cr-1",
        classroom_data={
            "id": "cr-1", "slug": "x", "course_id": "co-1",
            "course_slug": "c-slug", "course_version": "1.0.0",
            "steps": [{"name": "course_selected", "status": "completed"}],
        },
        course_id="co-1",
        course_data={
            "id": "co-1", "slug": "c-slug",
            "steps": [{"name": "manifest_loaded", "status": "completed"}],
        },
    )

    result = mcp_server.prep_status("cr-1")
    assert result["course_version"] == "1.0.0"
    assert len(result["course_steps"]) == 1
    assert len(result["classroom_steps"]) == 1


def test_prep_status_missing_classroom_returns_error(runtime_dir, monkeypatch):
    monkeypatch.setenv("AXIOM_RUNTIME_ROOT", str(runtime_dir))
    from axiom.extensions.builtins.classroom import mcp_server

    result = mcp_server.prep_status("does-not-exist")
    assert "error" in result


def test_list_signals_returns_empty_when_no_file(runtime_dir, monkeypatch):
    monkeypatch.setenv("AXIOM_RUNTIME_ROOT", str(runtime_dir))
    from axiom.extensions.builtins.classroom import mcp_server

    assert mcp_server.list_signals("cr-x") == {"signals": []}


def test_build_server_returns_mcp_server_instance():
    from axiom.extensions.builtins.classroom import mcp_server

    srv = mcp_server.build_server()
    assert srv is not None
    assert srv.name == "axiom-classroom"


def test_tools_catalog_shape():
    """Tools have name + description + JSON schema inputSchema."""
    from axiom.extensions.builtins.classroom.mcp_server import _TOOLS

    assert len(_TOOLS) >= 4
    for t in _TOOLS:
        assert t.name.startswith("axiom_classroom_")
        assert t.description
        assert isinstance(t.inputSchema, dict)
        assert t.inputSchema.get("type") == "object"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

import pytest  # noqa: E402  # placed below section header for organization


@pytest.fixture
def runtime_dir(tmp_path):
    return tmp_path / "runtime"
