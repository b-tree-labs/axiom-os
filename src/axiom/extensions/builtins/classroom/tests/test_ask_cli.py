# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for `axi classroom ask <classroom_id> <question>`.

Phase 6 of the materials-flow tier. First-cut Q&A: surface top-k
chunks from the student's local index, with citations, no LLM
generation yet (honesty beats confabulation until we wire a
grounded-answer path with the existing axiom.rag policy machinery).
"""

from __future__ import annotations

import json

import pytest

from axiom.extensions.builtins.classroom.classroom_local_index import (
    ClassroomLocalIndex,
)
from axiom.extensions.builtins.classroom.cli import main


def _seed_index(classroom_dir, *, files: list[dict]) -> None:
    """Helper: populate a classroom's local index without running the join
    flow. Matches what Phase 5 produces post-join."""
    index = ClassroomLocalIndex(base_dir=classroom_dir)
    index.open()
    try:
        for f in files:
            index.ingest(
                file_id=f["file_id"],
                title=f["title"],
                content=f["content"],
                embed=None,
            )
    finally:
        index.close()


@pytest.fixture
def home_with_class(tmp_path, monkeypatch):
    home = tmp_path / "student-home"
    monkeypatch.setenv("HOME", str(home))
    class_dir = home / ".axi" / "classrooms" / "NE101"
    class_dir.mkdir(parents=True)
    _seed_index(class_dir, files=[
        {
            "file_id": "f1",
            "title": "Chapter 1 — Control rods",
            "content": (
                "Control rods absorb neutrons to slow fission reactions. "
                "They are typically made of boron or cadmium."
            ),
        },
        {
            "file_id": "f2",
            "title": "Chapter 2 — Fuel assemblies",
            "content": (
                "Fuel assemblies hold the uranium pellets. "
                "They sit in a lattice cooled by water."
            ),
        },
    ])
    return home


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestBasicAsk:
    def test_ask_returns_matching_citation(self, home_with_class, capsys):
        rc = main(["ask", "NE101", "what is a control rod?"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Control rods" in out or "control rods" in out.lower()
        # The source title should be cited.
        assert "Chapter 1" in out

    def test_ask_shows_question(self, home_with_class, capsys):
        rc = main(["ask", "NE101", "what is a control rod?"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "control rod" in out.lower()

    def test_ask_returns_zero_on_no_match(self, home_with_class, capsys):
        """An honest "no match in your class materials" is still a
        successful invocation — the CLI worked, the student just got a
        negative result."""
        rc = main(["ask", "NE101", "turbine blade aerodynamics"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "no" in out.lower() or "couldn't find" in out.lower()


# ---------------------------------------------------------------------------
# Not-a-member classroom — friendly error with next step
# ---------------------------------------------------------------------------


class TestUnknownClassroom:
    def test_asking_about_unknown_classroom(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("HOME", str(tmp_path / "h"))
        rc = main(["ask", "NEVER_JOINED", "anything"])
        assert rc == 1
        err = capsys.readouterr().err
        # Point the student at the command they need.
        assert "axi classroom join" in err


# ---------------------------------------------------------------------------
# Jargon discipline on Q&A output
# ---------------------------------------------------------------------------


class TestNoJargon:
    def test_ask_output_has_no_jargon(self, home_with_class, capsys):
        rc = main(["ask", "NE101", "control rod"])
        assert rc == 0
        out = capsys.readouterr().out.lower()
        for forbidden in ("chunk", "embedding", "manifest", "fts5", "sqlite"):
            assert forbidden not in out, f"ask output leaked {forbidden!r}"


# ---------------------------------------------------------------------------
# JSON mode for scripting
# ---------------------------------------------------------------------------


class TestJsonMode:
    def test_ask_json_output_shape(self, home_with_class, capsys):
        rc = main(["ask", "NE101", "control rod", "--json"])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["question"] == "control rod"
        assert payload["classroom_id"] == "NE101"
        assert "citations" in payload
        assert len(payload["citations"]) >= 1
        first = payload["citations"][0]
        assert "title" in first
        assert "text" in first


class TestGroundedAnswer:
    """With an LLM provider available, the ask command synthesizes an
    answer alongside the citations."""

    def test_synthesized_answer_appears_when_gateway_returns_text(
        self, home_with_class, capsys, monkeypatch,
    ):
        # Force the Gateway.complete path to return a canned answer —
        # simulates a configured provider (a remote model, OpenAI, Ollama).
        class _FakeResp:
            success = True
            text = "A control rod absorbs neutrons [Chapter 1]."

        class _FakeGateway:
            def __init__(self, *a, **kw): pass
            def complete(self, *, prompt, system, task):
                return _FakeResp()

        monkeypatch.setattr(
            "axiom.infra.gateway.Gateway", _FakeGateway
        )

        rc = main(["ask", "NE101", "what is a control rod?"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "absorbs neutrons" in out
        # Citation section still shown.
        assert "Chapter 1" in out

    def test_cite_only_skips_llm(
        self, home_with_class, capsys, monkeypatch,
    ):
        """--cite-only must not construct a Gateway at all — cheaper +
        fully offline even when provider config would otherwise fire."""
        called = {"gateway_init": 0}

        class _TrackingGateway:
            def __init__(self, *a, **kw):
                called["gateway_init"] += 1
            def complete(self, **kw):
                raise AssertionError("should not have been called")

        monkeypatch.setattr(
            "axiom.infra.gateway.Gateway", _TrackingGateway
        )

        rc = main(["ask", "NE101", "control rod", "--cite-only"])
        assert rc == 0
        assert called["gateway_init"] == 0

    def test_gateway_exception_falls_back_to_citations(
        self, home_with_class, capsys, monkeypatch,
    ):
        class _BrokenGateway:
            def __init__(self, *a, **kw): pass
            def complete(self, **kw):
                raise RuntimeError("provider exploded mid-request")

        monkeypatch.setattr(
            "axiom.infra.gateway.Gateway", _BrokenGateway
        )

        rc = main(["ask", "NE101", "control rod"])
        # Student still gets useful output — just citations, no synthesis.
        assert rc == 0
        out = capsys.readouterr().out
        assert "Chapter 1" in out
