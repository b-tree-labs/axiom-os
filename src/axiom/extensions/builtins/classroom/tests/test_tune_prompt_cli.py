# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for ``axi classroom prep tune-prompt`` — G3 CLI parity.

Thin CLI around the existing ``classroom_prep_tune_prompt`` chat tool:
one-shot set + test + persist in a single command so CLI users get
the same surface that AXI does.
"""

from __future__ import annotations

import json

import pytest

from axiom.extensions.builtins.classroom.cli import main
from axiom.extensions.builtins.classroom.demo import seed_demo
from axiom.extensions.builtins.classroom.operational_store import load_course_data


@pytest.fixture(autouse=True)
def _isolated_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("AXIOM_RUNTIME_ROOT", str(tmp_path))
    import axiom.extensions.builtins.classroom.operational_store as store

    store._registry = None
    yield
    store._registry = None


@pytest.fixture
def cloned_course():
    from axiom.extensions.builtins.classroom.demo import clone_demo

    seed_demo()
    clone_demo(new_course_id="my-course", instructor_id="@ben:ut")
    return "my-course"


class TestTunePromptCLI:
    def test_happy_path_persists_prompt(self, cloned_course, capsys):
        rc = main(
            [
                "prep", "tune-prompt", cloned_course,
                "--system-prompt", "You are a patient physics TA.",
                "--test-query", "What is Newton's second law?",
                "--json",
            ]
        )
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data.get("course_id") == cloned_course
        assert data.get("system_prompt") == "You are a patient physics TA."
        assert data.get("test_response")

        # Persisted on the course artifact
        persisted = load_course_data(cloned_course)
        assert persisted["system_prompt"] == "You are a patient physics TA."

    def test_rejects_unknown_course(self, capsys):
        rc = main(
            [
                "prep", "tune-prompt", "nonexistent",
                "--system-prompt", "x",
                "--test-query", "y",
                "--json",
            ]
        )
        assert rc == 1
        data = json.loads(capsys.readouterr().out)
        assert "error" in data

    def test_human_output_shows_response(self, cloned_course, capsys):
        rc = main(
            [
                "prep", "tune-prompt", cloned_course,
                "--system-prompt", "You are a patient physics TA.",
                "--test-query", "What is Newton's second law?",
            ]
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "Tuned prompt" in out
        assert cloned_course in out
        assert "response:" in out

    def test_required_flags_enforced(self, capsys):
        # Missing --system-prompt / --test-query should fail fast
        with pytest.raises(SystemExit):
            main(["prep", "tune-prompt", "my-course"])

    def test_matches_chat_tool_behavior(self, cloned_course):
        """CLI and chat tool should produce equivalent persistence.
        Run both against the same course; the final state is what the
        second call wrote."""
        from axiom.extensions.builtins.classroom.chat_tools.prep_tools import execute

        # Chat tool first
        r1 = execute(
            "classroom_prep_tune_prompt",
            {
                "course_id": cloned_course,
                "system_prompt": "Prompt A",
                "test_query": "Question?",
            },
        )
        assert "test_response" in r1
        assert load_course_data(cloned_course)["system_prompt"] == "Prompt A"

        # CLI second with a different prompt
        rc = main(
            [
                "prep", "tune-prompt", cloned_course,
                "--system-prompt", "Prompt B",
                "--test-query", "Another?",
                "--json",
            ]
        )
        assert rc == 0
        assert load_course_data(cloned_course)["system_prompt"] == "Prompt B"
