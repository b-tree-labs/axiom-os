# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""CLI tests for ``axi classroom demo`` + ``prep from-demo`` (FW-1 P1)."""

from __future__ import annotations

import json

import pytest

from axiom.extensions.builtins.classroom.cli import main
from axiom.extensions.builtins.classroom.demo import (
    DEMO_CLASSROOM_ID,
    DEMO_COURSE_ID,
)
from axiom.extensions.builtins.classroom.operational_store import (
    load_classroom_data,
    load_course_data,
)


@pytest.fixture(autouse=True)
def _isolated_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("AXIOM_RUNTIME_ROOT", str(tmp_path))
    import axiom.extensions.builtins.classroom.operational_store as store

    store._registry = None
    yield
    store._registry = None


class TestDemoCLI:
    def test_demo_command_seeds_classroom(self, capsys):
        rc = main(["demo"])
        assert rc == 0
        out = capsys.readouterr().out
        assert DEMO_COURSE_ID in out
        assert DEMO_CLASSROOM_ID in out
        assert load_course_data(DEMO_COURSE_ID) is not None
        assert load_classroom_data(DEMO_CLASSROOM_ID) is not None

    def test_demo_command_json_output(self, capsys):
        rc = main(["demo", "--json"])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["action"] == "seeded"
        assert data["course_id"] == DEMO_COURSE_ID
        assert data["classroom_id"] == DEMO_CLASSROOM_ID
        assert "next_steps" in data
        assert any("prep status" in step for step in data["next_steps"])

    def test_demo_reset_reports_reset_action(self, capsys):
        main(["demo"])
        capsys.readouterr()
        rc = main(["demo", "--reset", "--json"])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["action"] == "reset"

    def test_demo_next_steps_include_from_demo_hint(self, capsys):
        rc = main(["demo", "--json"])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert any("from-demo" in step for step in data["next_steps"])


class TestPrepFromDemoCLI:
    def test_clones_to_new_course_id_and_classroom_id(self, capsys):
        main(["demo"])
        capsys.readouterr()
        rc = main(
            [
                "prep",
                "from-demo",
                "my-course",
                "--instructor",
                "@ben:ut",
                "--json",
            ]
        )
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["cloned_course_id"] == "my-course"
        assert data["cloned_classroom_id"] == "my-course"
        assert data["instructor_id"] == "@ben:ut"
        assert load_course_data("my-course") is not None
        assert load_classroom_data("my-course") is not None

    def test_explicit_classroom_id_flag(self, capsys):
        main(["demo"])
        capsys.readouterr()
        rc = main(
            [
                "prep", "from-demo", "my-course",
                "--instructor", "@ben:ut",
                "--classroom-id", "my-course-spring-2026",
                "--json",
            ]
        )
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["cloned_course_id"] == "my-course"
        assert data["cloned_classroom_id"] == "my-course-spring-2026"
        assert load_classroom_data("my-course-spring-2026") is not None

    def test_human_output_lists_editable_next_steps(self, capsys):
        main(["demo"])
        capsys.readouterr()
        rc = main(
            ["prep", "from-demo", "my-course", "--instructor", "@ben:ut"]
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "my-course" in out
        assert "prep corpus" in out  # tells instructor what to edit next
        # New-in-G1: next steps include `publish` against the cloned classroom
        assert "publish" in out

    def test_rejects_existing_course_id(self, capsys):
        main(["demo"])
        capsys.readouterr()
        main(["prep", "from-demo", "my-course", "--instructor", "@ben:ut"])
        capsys.readouterr()
        rc = main(
            [
                "prep",
                "from-demo",
                "my-course",
                "--instructor",
                "@ben:ut",
                "--json",
            ]
        )
        assert rc == 1
        data = json.loads(capsys.readouterr().out)
        assert "already exists" in data["error"]

    def test_rejects_demo_course_id(self, capsys):
        main(["demo"])
        capsys.readouterr()
        rc = main(
            [
                "prep",
                "from-demo",
                DEMO_COURSE_ID,
                "--instructor",
                "@ben:ut",
                "--json",
            ]
        )
        assert rc == 1
        data = json.loads(capsys.readouterr().out)
        assert "demo" in data["error"]

    def test_clone_seeds_demo_if_missing(self, capsys):
        """If the user runs `prep from-demo` before `demo`, we seed the
        demo lazily so the clone call still succeeds. Enter-through-end:
        every entry point should work."""
        rc = main(
            [
                "prep",
                "from-demo",
                "my-course",
                "--instructor",
                "@ben:ut",
                "--json",
            ]
        )
        assert rc == 0
        # Both the demo and the clone exist now
        assert load_course_data(DEMO_COURSE_ID) is not None
        assert load_course_data("my-course") is not None
