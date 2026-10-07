# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for #23: tmux status-line + completers for ``axi classroom``.

The ``--tmux`` status mode emits a single-line string suitable for
embedding in a tmux ``status-right`` via ``#(axi classroom status --tmux)``.

The completers read from ``runtime/classrooms/`` and ``runtime/courses/``
so shell completion surfaces real IDs rather than making users retype
what they already know.
"""

from __future__ import annotations

import json
from pathlib import Path

from axiom.extensions.builtins.classroom.status_line import (
    classroom_id_completer,
    course_id_completer,
    tmux_status_line,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_classroom(runtime_root: Path, cid: str, **extras) -> Path:
    d = runtime_root / "classrooms" / cid
    d.mkdir(parents=True, exist_ok=True)
    data = {"classroom_id": cid, **extras}
    (d / "classroom.json").write_text(json.dumps(data), encoding="utf-8")
    return d


def _seed_course(runtime_root: Path, course_id: str, **extras) -> Path:
    d = runtime_root / "courses" / course_id
    d.mkdir(parents=True, exist_ok=True)
    data = {"course_id": course_id, **extras}
    (d / "course.json").write_text(json.dumps(data), encoding="utf-8")
    return d


# ---------------------------------------------------------------------------
# Completers
# ---------------------------------------------------------------------------


class TestClassroomIdCompleter:
    def test_returns_all_seeded_ids(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AXIOM_RUNTIME_ROOT", str(tmp_path))
        _seed_classroom(tmp_path, "cr-alpha")
        _seed_classroom(tmp_path, "cr-beta")
        ids = classroom_id_completer(prefix="")
        assert set(ids) == {"cr-alpha", "cr-beta"}

    def test_filters_by_prefix(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AXIOM_RUNTIME_ROOT", str(tmp_path))
        _seed_classroom(tmp_path, "cr-alpha")
        _seed_classroom(tmp_path, "cr-beta")
        _seed_classroom(tmp_path, "test-1")
        assert set(classroom_id_completer(prefix="cr-")) == {"cr-alpha", "cr-beta"}

    def test_empty_runtime_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AXIOM_RUNTIME_ROOT", str(tmp_path))
        assert classroom_id_completer(prefix="") == []

    def test_completer_swallows_errors(self, tmp_path, monkeypatch):
        """A broken runtime dir shouldn't blow up tab completion."""
        monkeypatch.setenv("AXIOM_RUNTIME_ROOT", "/nonexistent/path")
        # Must not raise.
        assert classroom_id_completer(prefix="") == []


class TestCourseIdCompleter:
    def test_returns_seeded_course_ids(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AXIOM_RUNTIME_ROOT", str(tmp_path))
        _seed_course(tmp_path, "course-ne-101")
        _seed_course(tmp_path, "course-ne-201")
        ids = course_id_completer(prefix="")
        assert set(ids) == {"course-ne-101", "course-ne-201"}

    def test_prefix_filter(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AXIOM_RUNTIME_ROOT", str(tmp_path))
        _seed_course(tmp_path, "course-alpha")
        _seed_course(tmp_path, "course-beta")
        assert course_id_completer(prefix="course-a") == ["course-alpha"]


# ---------------------------------------------------------------------------
# tmux status-line
# ---------------------------------------------------------------------------


class TestTmuxStatusLine:
    def test_no_classrooms_shows_idle(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AXIOM_RUNTIME_ROOT", str(tmp_path))
        out = tmux_status_line()
        assert "axi" in out.lower()
        assert "idle" in out.lower() or "0" in out

    def test_single_classroom_shows_id(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AXIOM_RUNTIME_ROOT", str(tmp_path))
        _seed_classroom(tmp_path, "cr-prague")
        out = tmux_status_line()
        assert "cr-prague" in out

    def test_multi_classroom_shows_count(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AXIOM_RUNTIME_ROOT", str(tmp_path))
        _seed_classroom(tmp_path, "a")
        _seed_classroom(tmp_path, "b")
        _seed_classroom(tmp_path, "c")
        out = tmux_status_line()
        assert "3" in out

    def test_one_line_no_newline(self, tmp_path, monkeypatch):
        """tmux reads a single line; the output must not contain newlines."""
        monkeypatch.setenv("AXIOM_RUNTIME_ROOT", str(tmp_path))
        _seed_classroom(tmp_path, "x")
        out = tmux_status_line()
        assert "\n" not in out

    def test_explicit_classroom_id_shows_that_one(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AXIOM_RUNTIME_ROOT", str(tmp_path))
        _seed_classroom(tmp_path, "a")
        _seed_classroom(tmp_path, "b")
        out = tmux_status_line(classroom_id="b")
        assert "b" in out
        assert "a" not in out.split(" ")[1:]  # a only appears if in template
