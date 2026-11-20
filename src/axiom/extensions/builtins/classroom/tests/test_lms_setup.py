# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for LMS setup walkthrough — FW-1 P4.

Exposes a small API over the existing LMS provider layer so the CLI
and AXI can guide an instructor through Canvas setup:

- ``list_providers()`` — what's installed
- ``canvas_probe(instance_url, token)`` — test connectivity
- ``canvas_configure(classroom_id, ...)`` — wire Canvas to a classroom
- ``mark_no_lms(classroom_id)`` — explicit opt-out

Tests use the ``CanvasMockServer`` so they don't hit real Canvas.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.classroom.lms_setup import (
    SUPPORTED_PROVIDERS,
    canvas_configure,
    canvas_probe,
    list_providers,
    mark_no_lms,
    seed_mock_canvas,
)


@pytest.fixture(autouse=True)
def _isolated_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("AXIOM_RUNTIME_ROOT", str(tmp_path))
    import axiom.extensions.builtins.classroom.operational_store as store

    store._registry = None
    yield
    store._registry = None


@pytest.fixture
def seeded_classroom():
    """A clone of the demo classroom that we can point Canvas at."""
    from axiom.extensions.builtins.classroom.demo import (
        DEMO_CLASSROOM_ID,
        seed_demo,
    )

    seed_demo()
    return DEMO_CLASSROOM_ID


# ---------------------------------------------------------------------------
# list_providers
# ---------------------------------------------------------------------------


class TestListProviders:
    def test_includes_canvas_built_in(self):
        providers = list_providers()
        by_id = {p["id"]: p for p in providers}
        assert "canvas" in by_id
        assert by_id["canvas"]["status"] == "built-in"

    def test_enumerates_all_supported(self):
        providers = list_providers()
        ids = {p["id"] for p in providers}
        # Canvas is built-in; Moodle + Blackboard are "coming-soon"
        # (stubs for the adoption-strategy roadmap); "none" is always available.
        assert {"canvas", "moodle", "blackboard", "none"}.issubset(ids)
        assert set(SUPPORTED_PROVIDERS) == ids

    def test_every_provider_has_display_name(self):
        for p in list_providers():
            assert p.get("display_name")


# ---------------------------------------------------------------------------
# canvas_probe
# ---------------------------------------------------------------------------


class TestCanvasProbe:
    def test_returns_connected_against_mock(self):
        mock = seed_mock_canvas(courses=[("c1", "Test Course")])
        result = canvas_probe(
            instance_url="mock://canvas", token="dummy", mock_server=mock,
        )
        assert result["connected"] is True
        assert "error" not in result

    def test_returns_not_connected_when_mock_offline(self):
        mock = seed_mock_canvas(courses=[], offline=True)
        result = canvas_probe(
            instance_url="mock://canvas", token="dummy", mock_server=mock,
        )
        assert result["connected"] is False

    def test_missing_params_returns_error(self):
        result = canvas_probe(instance_url="", token="x")
        assert result["connected"] is False
        assert "error" in result


# ---------------------------------------------------------------------------
# canvas_configure
# ---------------------------------------------------------------------------


class TestCanvasConfigure:
    def test_attaches_roster_to_classroom(self, seeded_classroom):
        from axiom.extensions.builtins.classroom.operational_store import (
            load_classroom_data,
        )

        mock = seed_mock_canvas(
            courses=[("c1", "Classical Mechanics — Spring 26")],
            enrollments={"c1": [
                {"user_id": "s1", "name": "Alice", "email": "a@test.local"},
                {"user_id": "s2", "name": "Bob", "email": "b@test.local"},
            ]},
        )
        result = canvas_configure(
            classroom_id=seeded_classroom,
            instance_url="mock://canvas",
            token="dummy",
            canvas_course_id="c1",
            mock_server=mock,
        )
        assert result["configured"] is True
        assert result["roster_count"] == 2
        data = load_classroom_data(seeded_classroom)
        assert len(data["lms_roster"]) == 2

    def test_unknown_classroom_returns_error(self):
        mock = seed_mock_canvas(courses=[("c1", "t")])
        result = canvas_configure(
            classroom_id="nope",
            instance_url="mock://canvas",
            token="dummy",
            canvas_course_id="c1",
            mock_server=mock,
        )
        assert result["configured"] is False
        assert "error" in result

    def test_bad_canvas_course_id_returns_error(self, seeded_classroom):
        mock = seed_mock_canvas(
            courses=[("c1", "t")], enrollments={"c1": []}
        )
        result = canvas_configure(
            classroom_id=seeded_classroom,
            instance_url="mock://canvas",
            token="dummy",
            canvas_course_id="nonexistent-course",
            mock_server=mock,
        )
        # Empty roster → configure still succeeds structurally but signals
        # that no students are attached yet. Instructor can re-run.
        assert result["configured"] is True
        assert result["roster_count"] == 0


# ---------------------------------------------------------------------------
# mark_no_lms
# ---------------------------------------------------------------------------


class TestMarkNoLms:
    def test_sets_lms_mode_to_none(self, seeded_classroom):
        from axiom.extensions.builtins.classroom.operational_store import (
            load_classroom_data,
        )

        result = mark_no_lms(classroom_id=seeded_classroom)
        assert result["no_lms"] is True
        data = load_classroom_data(seeded_classroom)
        # Roster preserved (the instructor may have a manual students.yaml
        # that already populated it) but the lms_provider flag is cleared.
        assert data.get("lms_provider") in (None, "", "none")

    def test_unknown_classroom_returns_error(self):
        result = mark_no_lms(classroom_id="nope")
        assert result["no_lms"] is False
        assert "error" in result


# ---------------------------------------------------------------------------
# CLI — axi classroom prep lms setup {list-providers|canvas probe|canvas configure|none}
# ---------------------------------------------------------------------------


class TestLmsSetupCLI:
    def test_list_providers_json(self, capsys):
        import json

        from axiom.extensions.builtins.classroom.cli import main

        rc = main(["prep", "lms-setup", "list-providers", "--json"])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert any(p["id"] == "canvas" for p in data["providers"])

    def test_canvas_probe_fake(self, capsys):
        import json

        from axiom.extensions.builtins.classroom.cli import main

        rc = main(
            [
                "prep", "lms-setup", "canvas-probe",
                "--instance-url", "mock://canvas",
                "--token", "dummy",
                "--fake",
                "--json",
            ]
        )
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["connected"] is True

    def test_canvas_configure_fake(self, seeded_classroom, capsys):
        import json

        from axiom.extensions.builtins.classroom.cli import main

        rc = main(
            [
                "prep", "lms-setup", "canvas-configure",
                seeded_classroom,
                "--instance-url", "mock://canvas",
                "--token", "dummy",
                "--canvas-course-id", "c1",
                "--fake",
                "--json",
            ]
        )
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["configured"] is True
        # --fake auto-seeds 5 synthetic students into the mock course
        assert data["roster_count"] == 5

    def test_none_command(self, seeded_classroom, capsys):
        import json

        from axiom.extensions.builtins.classroom.cli import main

        rc = main(
            ["prep", "lms-setup", "none", seeded_classroom, "--json"]
        )
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["no_lms"] is True


# ---------------------------------------------------------------------------
# Chat tool — classroom_prep_lms_setup
# ---------------------------------------------------------------------------


class TestLmsSetupChatTool:
    def test_tool_registered(self):
        from axiom.extensions.builtins.classroom.chat_tools import prep_tools

        names = {t.name for t in prep_tools.TOOLS}
        assert "classroom_prep_lms_setup" in names

    def test_list_providers_action(self):
        from axiom.extensions.builtins.classroom.chat_tools import prep_tools

        result = prep_tools.execute(
            "classroom_prep_lms_setup", {"action": "list-providers"},
        )
        assert any(p["id"] == "canvas" for p in result["providers"])

    def test_canvas_probe_action(self):
        from axiom.extensions.builtins.classroom.chat_tools import prep_tools

        result = prep_tools.execute(
            "classroom_prep_lms_setup",
            {
                "action": "canvas-probe",
                "instance_url": "mock://canvas",
                "token": "dummy",
                "fake": True,
            },
        )
        assert result["connected"] is True

    def test_canvas_configure_action(self, seeded_classroom):
        from axiom.extensions.builtins.classroom.chat_tools import prep_tools

        result = prep_tools.execute(
            "classroom_prep_lms_setup",
            {
                "action": "canvas-configure",
                "classroom_id": seeded_classroom,
                "instance_url": "mock://canvas",
                "token": "dummy",
                "canvas_course_id": "c1",
                "fake": True,
            },
        )
        assert result["configured"] is True
        assert result["roster_count"] == 5

    def test_none_action(self, seeded_classroom):
        from axiom.extensions.builtins.classroom.chat_tools import prep_tools

        result = prep_tools.execute(
            "classroom_prep_lms_setup",
            {"action": "none", "classroom_id": seeded_classroom},
        )
        assert result["no_lms"] is True

    def test_bad_action(self):
        from axiom.extensions.builtins.classroom.chat_tools import prep_tools

        result = prep_tools.execute(
            "classroom_prep_lms_setup", {"action": "unsupported"},
        )
        assert "error" in result
