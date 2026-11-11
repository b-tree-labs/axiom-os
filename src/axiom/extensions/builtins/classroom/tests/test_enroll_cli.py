# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for ``axi classroom enroll`` — Keplo P6a.

Wraps the existing ``enrollment.enroll_classroom`` Python API as a
CLI + chat tool so instructors don't have to open a REPL to run the
WF-1 flow. Uses the populated CanvasMockServer for --fake mode.
"""

from __future__ import annotations

import json

import pytest

from axiom.extensions.builtins.classroom.demo import DEMO_CLASSROOM_ID, seed_demo
from axiom.extensions.builtins.classroom.publish import publish_classroom


@pytest.fixture(autouse=True)
def _isolated_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("AXIOM_RUNTIME_ROOT", str(tmp_path))
    import axiom.extensions.builtins.classroom.operational_store as store

    store._registry = None
    yield
    store._registry = None


@pytest.fixture
def published_demo():
    seed_demo()
    publish_classroom(classroom_id=DEMO_CLASSROOM_ID, approver="@ben:ut")
    return DEMO_CLASSROOM_ID


# ---------------------------------------------------------------------------
# CLI — axi classroom enroll
# ---------------------------------------------------------------------------


class TestEnrollCLI:
    def test_fake_enrollment_happy_path(self, published_demo, capsys):
        from axiom.extensions.builtins.classroom.cli import main

        rc = main(
            [
                "enroll", published_demo,
                "--instructor", "@ben:ut",
                "--fake",
                "--json",
            ]
        )
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["enrolled"] is True
        assert data["student_count"] == 5  # fake mock ships 5 synthetic students
        assert len(data["tokens"]) == 5
        for t in data["tokens"]:
            assert t["token"]
            assert t["student_id"]
            assert t["expires_at"]

    def test_refuses_unpublished_classroom(self, capsys):
        from axiom.extensions.builtins.classroom.cli import main

        seed_demo()  # seeded but not published
        rc = main(
            [
                "enroll", DEMO_CLASSROOM_ID,
                "--instructor", "@ben:ut",
                "--fake",
                "--json",
            ]
        )
        assert rc == 1
        data = json.loads(capsys.readouterr().out)
        assert data["enrolled"] is False
        assert "published" in data["error"].lower()

    def test_unknown_classroom(self, capsys):
        from axiom.extensions.builtins.classroom.cli import main

        rc = main(
            [
                "enroll", "nope",
                "--instructor", "@ben:ut",
                "--fake",
                "--json",
            ]
        )
        assert rc == 1
        data = json.loads(capsys.readouterr().out)
        assert data["enrolled"] is False

    def test_ttl_days_overrides_default(self, published_demo, capsys):
        from axiom.extensions.builtins.classroom.cli import main

        rc = main(
            [
                "enroll", published_demo,
                "--instructor", "@ben:ut",
                "--fake",
                "--ttl-days", "7",
                "--json",
            ]
        )
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        # All tokens should carry the custom TTL
        for t in data["tokens"]:
            assert t["ttl_days"] == 7

    def test_human_output_shows_roster_summary(self, published_demo, capsys):
        from axiom.extensions.builtins.classroom.cli import main

        rc = main(
            [
                "enroll", published_demo,
                "--instructor", "@ben:ut",
                "--fake",
            ]
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "5 student" in out or "5 enrolled" in out.lower()


# ---------------------------------------------------------------------------
# Chat tool — classroom_enroll
# ---------------------------------------------------------------------------


class TestEnrollChatTool:
    def test_tool_registered(self):
        from axiom.extensions.builtins.classroom.chat_tools import prep_tools

        names = {t.name for t in prep_tools.TOOLS}
        assert "classroom_enroll" in names

    def test_tool_dispatches_happy_path(self, published_demo):
        from axiom.extensions.builtins.classroom.chat_tools import prep_tools

        result = prep_tools.execute(
            "classroom_enroll",
            {
                "classroom_id": published_demo,
                "instructor": "@ben:ut",
                "fake": True,
            },
        )
        assert result["enrolled"] is True
        assert result["student_count"] == 5

    def test_tool_refuses_unpublished(self):
        from axiom.extensions.builtins.classroom.chat_tools import prep_tools

        seed_demo()
        result = prep_tools.execute(
            "classroom_enroll",
            {
                "classroom_id": DEMO_CLASSROOM_ID,
                "instructor": "@ben:ut",
                "fake": True,
            },
        )
        assert result["enrolled"] is False

    def test_tool_missing_params_error(self):
        from axiom.extensions.builtins.classroom.chat_tools import prep_tools

        result = prep_tools.execute("classroom_enroll", {})
        assert result["enrolled"] is False
