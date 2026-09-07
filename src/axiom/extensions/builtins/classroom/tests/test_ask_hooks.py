# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for ClassroomAskHooks + ClassroomRetriever.

The classroom CLI's ``axi classroom ask`` is layered on top of the
generic ``axiom.memory.ask.AskPipeline``. The classroom-specific bits
(learning modes, classroom corpus retrieval, mode-aware system prompt
overlays, tutor-mode short-circuit) live as hooks + a retriever
adapter so the pipeline itself stays generic.

These tests pin the contract those hooks expose.
"""

from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def seeded_class_dir(tmp_path):
    """A classroom directory with a ClassroomLocalIndex pre-populated
    with two short documents, so retrieval has something to find."""
    from axiom.extensions.builtins.classroom.classroom_local_index import (
        ClassroomLocalIndex,
    )

    class_dir = tmp_path / "classrooms" / "NE101"
    class_dir.mkdir(parents=True)
    index = ClassroomLocalIndex(base_dir=class_dir)
    index.open()
    try:
        index.ingest(
            file_id="f1",
            title="Chapter 1 — Control rods",
            content=(
                "Control rods absorb neutrons to slow fission reactions. "
                "They are typically made of boron or cadmium."
            ),
            embed=None,
        )
        index.ingest(
            file_id="f2",
            title="Chapter 2 — Fuel assemblies",
            content=(
                "Fuel assemblies hold the uranium pellets. "
                "They sit in a lattice cooled by water."
            ),
            embed=None,
        )
    finally:
        index.close()
    return class_dir


# ---------------------------------------------------------------------------
# ClassroomRetriever — adapter from ClassroomLocalIndex to Retriever protocol
# ---------------------------------------------------------------------------


class TestClassroomRetriever:
    def test_retriever_returns_citations_from_local_index(self, seeded_class_dir):
        from axiom.extensions.builtins.classroom.ask_hooks import (
            ClassroomRetriever,
        )

        retriever = ClassroomRetriever(classroom_dir=seeded_class_dir)
        citations = retriever.retrieve("control rod", k=3)
        assert len(citations) >= 1
        # Citation shape conforms to the generic ask protocol.
        first = citations[0]
        assert hasattr(first, "title")
        assert hasattr(first, "text")
        assert hasattr(first, "source_id")
        # Pulled from the seeded chapter.
        assert "Chapter 1" in first.title

    def test_retriever_source_id_is_file_id(self, seeded_class_dir):
        """Source IDs in the generic ``Citation`` should be the
        classroom file_id so provenance round-trips cleanly."""
        from axiom.extensions.builtins.classroom.ask_hooks import (
            ClassroomRetriever,
        )

        retriever = ClassroomRetriever(classroom_dir=seeded_class_dir)
        citations = retriever.retrieve("control rod", k=3)
        # Either f1 or f2; control-rod content sits in f1.
        assert any(c.source_id == "f1" for c in citations)

    def test_retriever_returns_empty_on_no_match(self, seeded_class_dir):
        from axiom.extensions.builtins.classroom.ask_hooks import (
            ClassroomRetriever,
        )

        retriever = ClassroomRetriever(classroom_dir=seeded_class_dir)
        citations = retriever.retrieve(
            "turbine blade aerodynamics", k=3,
        )
        assert citations == []

    def test_retriever_conforms_to_protocol(self, seeded_class_dir):
        """Statically — the adapter exposes ``.retrieve(query, *, k)``
        with the right return shape."""
        from axiom.extensions.builtins.classroom.ask_hooks import (
            ClassroomRetriever,
        )
        from axiom.memory.ask import Citation

        retriever = ClassroomRetriever(classroom_dir=seeded_class_dir)
        out = retriever.retrieve("control", k=2)
        for c in out:
            assert isinstance(c, Citation)


# ---------------------------------------------------------------------------
# ClassroomAskHooks — mode-specific specialization
# ---------------------------------------------------------------------------


class _FakeComposer:
    """Lightweight PromptComposer-shaped recorder for testing what the
    hooks contribute, without spinning up a full composer."""

    def __init__(self):
        self.contributions = []

    def add(self, layer, *, name, content, source, required=True):
        self.contributions.append({
            "layer": layer,
            "name": name,
            "content": content,
            "source": source,
            "required": required,
        })


class TestClassroomAskHooksContributeLayers:
    def test_tutor_mode_adds_socratic_overlay(self):
        from axiom.extensions.builtins.classroom.ask_hooks import (
            ClassroomAskHooks,
        )
        from axiom.memory.ask import AskRequest

        hooks = ClassroomAskHooks(classroom_id="NE101")
        composer = _FakeComposer()
        request = AskRequest(
            question="why does this happen?",
            principal_id="alice",
            scope_id="NE101",
            mode="tutor",
        )

        hooks.contribute_layers(request, composer)

        # The hook contributes a domain_context block.
        domain = [
            c for c in composer.contributions
            if c["layer"] == "domain_context"
        ]
        assert domain, "tutor mode must contribute a domain_context block"
        joined = " ".join(c["content"] for c in domain)
        # Socratic policy comes through.
        assert "socratic" in joined.lower()
        assert "do not answer" in joined.lower() or "never give" in joined.lower()

    def test_review_mode_adds_summary_overlay(self):
        from axiom.extensions.builtins.classroom.ask_hooks import (
            ClassroomAskHooks,
        )
        from axiom.memory.ask import AskRequest

        hooks = ClassroomAskHooks(classroom_id="NE101")
        composer = _FakeComposer()
        request = AskRequest(
            question="control rods",
            principal_id="alice",
            scope_id="NE101",
            mode="review",
        )

        hooks.contribute_layers(request, composer)

        domain = [
            c for c in composer.contributions
            if c["layer"] == "domain_context"
        ]
        assert domain
        joined = " ".join(c["content"] for c in domain).lower()
        assert "summarize" in joined or "summary" in joined

    def test_ask_mode_does_not_add_overlay(self):
        """Default 'ask' mode has no system_prompt_overlay, so the
        hook should not pollute the composer with an empty domain_context
        contribution."""
        from axiom.extensions.builtins.classroom.ask_hooks import (
            ClassroomAskHooks,
        )
        from axiom.memory.ask import AskRequest

        hooks = ClassroomAskHooks(classroom_id="NE101")
        composer = _FakeComposer()
        request = AskRequest(
            question="control rods",
            principal_id="alice",
            scope_id="NE101",
            mode="ask",
        )

        hooks.contribute_layers(request, composer)

        # No mode overlay → no domain_context contribution from this hook.
        domain = [
            c for c in composer.contributions
            if c["layer"] == "domain_context" and "mode" in c["name"]
        ]
        assert domain == []

    def test_unknown_mode_falls_back_safely(self):
        """An unknown mode shouldn't crash the hook — it should treat
        the request like default 'ask'."""
        from axiom.extensions.builtins.classroom.ask_hooks import (
            ClassroomAskHooks,
        )
        from axiom.memory.ask import AskRequest

        hooks = ClassroomAskHooks(classroom_id="NE101")
        composer = _FakeComposer()
        request = AskRequest(
            question="control rods",
            principal_id="alice",
            scope_id="NE101",
            mode="not-a-real-mode",
        )

        # Must not raise.
        hooks.contribute_layers(request, composer)


# ---------------------------------------------------------------------------
# pre_llm short-circuit — closed-book modes refuse synthesis
# ---------------------------------------------------------------------------


class TestClassroomAskHooksShortCircuit:
    def test_quiz_mode_short_circuits_with_empty_answer(self):
        """Quiz mode has llm_constraint='none' — the LLM must NOT be
        called. The hook returns an empty AskResult so the pipeline
        skips synthesis."""
        from axiom.extensions.builtins.classroom.ask_hooks import (
            ClassroomAskHooks,
        )
        from axiom.memory.ask import AskRequest, AskResult, Citation

        hooks = ClassroomAskHooks(classroom_id="NE101")
        request = AskRequest(
            question="control rods",
            principal_id="alice",
            scope_id="NE101",
            mode="quiz",
        )

        result = hooks.pre_llm(
            request, _FakeComposer(),
            [Citation(title="t", text="x", source_id="f1")],
        )
        assert isinstance(result, AskResult)
        assert result.answer == ""
        assert result.mode_used == "quiz"

    def test_reflect_mode_short_circuits_with_empty_answer(self):
        from axiom.extensions.builtins.classroom.ask_hooks import (
            ClassroomAskHooks,
        )
        from axiom.memory.ask import AskRequest, AskResult

        hooks = ClassroomAskHooks(classroom_id="NE101")
        request = AskRequest(
            question="what clicked?",
            principal_id="alice",
            scope_id="NE101",
            mode="reflect",
        )

        result = hooks.pre_llm(request, _FakeComposer(), [])
        assert isinstance(result, AskResult)
        assert result.answer == ""

    def test_ask_mode_does_not_short_circuit(self):
        """Default 'ask' mode hands off to the LLM normally."""
        from axiom.extensions.builtins.classroom.ask_hooks import (
            ClassroomAskHooks,
        )
        from axiom.memory.ask import AskRequest, Citation

        hooks = ClassroomAskHooks(classroom_id="NE101")
        request = AskRequest(
            question="what is a control rod?",
            principal_id="alice",
            scope_id="NE101",
            mode="ask",
        )

        result = hooks.pre_llm(
            request, _FakeComposer(),
            [Citation(title="t", text="x", source_id="f1")],
        )
        assert result is None

    def test_tutor_mode_does_not_short_circuit_when_llm_available(self):
        """Tutor mode wants the LLM to run with the Socratic overlay
        so it can produce a guiding question. It should NOT short-
        circuit just because mode=tutor — the overlay does the work."""
        from axiom.extensions.builtins.classroom.ask_hooks import (
            ClassroomAskHooks,
        )
        from axiom.memory.ask import AskRequest, Citation

        hooks = ClassroomAskHooks(classroom_id="NE101")
        request = AskRequest(
            question="why does this happen?",
            principal_id="alice",
            scope_id="NE101",
            mode="tutor",
        )

        result = hooks.pre_llm(
            request, _FakeComposer(),
            [Citation(title="t", text="x", source_id="f1")],
        )
        assert result is None

    def test_quiz_mode_filters_citations_to_empty(self):
        """Quiz mode is closed-book; even if the retriever fired,
        citations must be hidden from the result. Hook handles this
        via filter_citations."""
        from axiom.extensions.builtins.classroom.ask_hooks import (
            ClassroomAskHooks,
        )
        from axiom.memory.ask import AskRequest, Citation

        hooks = ClassroomAskHooks(classroom_id="NE101")
        request = AskRequest(
            question="control rods",
            principal_id="alice",
            scope_id="NE101",
            mode="quiz",
        )
        cites = [
            Citation(title="t", text="x", source_id="f1"),
            Citation(title="t2", text="y", source_id="f2"),
        ]
        out = hooks.filter_citations(request, cites)
        assert out == []


# ---------------------------------------------------------------------------
# End-to-end: classroom CLI _cmd_ask continues to work after the swap
# ---------------------------------------------------------------------------


def _seed_student_home(tmp_path, monkeypatch, classroom_id="NE101"):
    home = tmp_path / "student-home"
    monkeypatch.setenv("HOME", str(home))
    class_dir = home / ".axi" / "classrooms" / classroom_id
    class_dir.mkdir(parents=True)

    from axiom.extensions.builtins.classroom.classroom_local_index import (
        ClassroomLocalIndex,
    )

    index = ClassroomLocalIndex(base_dir=class_dir)
    index.open()
    try:
        index.ingest(
            file_id="f1",
            title="Chapter 1 — Control rods",
            content=(
                "Control rods absorb neutrons to slow fission reactions. "
                "They are typically made of boron or cadmium."
            ),
            embed=None,
        )
    finally:
        index.close()
    return home, class_dir


class TestCmdAskUsesAskPipeline:
    def test_ask_returns_citations_via_pipeline(
        self, tmp_path, monkeypatch, capsys,
    ):
        """The CLI ``axi classroom ask`` still surfaces citations
        from the local index after the swap to AskPipeline."""
        from axiom.extensions.builtins.classroom.cli import main

        _seed_student_home(tmp_path, monkeypatch)

        rc = main(["ask", "NE101", "what is a control rod?"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Chapter 1" in out

    def test_ask_pipeline_writes_episodic_fragment(
        self, tmp_path, monkeypatch, capsys,
    ):
        """The pipeline's L1 logging fires for classroom asks. We can
        observe the episodic fragment in the per-classroom memory
        scope's artifact registry."""
        from axiom.extensions.builtins.classroom.cli import main
        from axiom.memory.bootstrap import build_memory_stack

        _seed_student_home(tmp_path, monkeypatch)

        runtime_root = tmp_path / "runtime"
        monkeypatch.setenv("AXIOM_RUNTIME_ROOT", str(runtime_root))

        rc = main(["ask", "NE101", "what is a control rod?"])
        assert rc == 0

        # Re-build the same scope and inspect what landed.
        stack = build_memory_stack(scope_id="classroom:NE101")
        artifacts = list(stack.artifact_registry.list(kind="fragment"))
        questions = [
            a.data.get("content", {}).get("question") for a in artifacts
        ]
        assert any(
            q and "control rod" in q.lower() for q in questions
        ), f"expected an episodic fragment with the question; got {questions}"

    def test_quiz_mode_skips_retrieval_and_llm(
        self, tmp_path, monkeypatch, capsys,
    ):
        """Quiz mode is closed-book — the JSON output must show no
        citations and no answer, even though the index has matches."""
        import json as _json

        from axiom.extensions.builtins.classroom.cli import main

        _seed_student_home(tmp_path, monkeypatch)

        rc = main([
            "ask", "NE101", "control rod",
            "--mode", "quiz", "--json",
        ])
        assert rc == 0
        payload = _json.loads(capsys.readouterr().out)
        assert payload["mode"] == "quiz"
        assert payload["citations"] == []
        assert payload["answer"] == ""
