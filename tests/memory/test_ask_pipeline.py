# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for axiom.memory.ask — generic ask pipeline.

The generic pipeline replaces extension-specific ask flows. It composes
the system prompt via PromptComposer, retrieves context (FTS + concept-
graph neighbors), invokes the LLM via the gateway abstraction, logs
the interaction through CompositionService, returns a typed result.

Extensions hook in by:
- Subclassing or composing AskPipeline
- Implementing the Retriever protocol (extension-specific corpus)
- Providing pre/post hooks for mode resolution, prompt overlays,
  response transformation

Tests pin the generic contract so refactors don't drift.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def stack(tmp_path):
    from axiom.memory.bootstrap import build_memory_stack
    return build_memory_stack(scope_id="test-ask-scope", data_root=tmp_path)


# ---------------------------------------------------------------------------
# AskRequest / AskResult shape
# ---------------------------------------------------------------------------


class TestRequestResultDataclasses:
    def test_request_carries_required_fields(self):
        from axiom.memory.ask import AskRequest

        req = AskRequest(
            question="What is X?",
            principal_id="alice",
            scope_id="scope-1",
        )
        assert req.question == "What is X?"
        assert req.principal_id == "alice"
        assert req.scope_id == "scope-1"
        assert req.mode is None
        assert req.cite_only is False

    def test_result_carries_provenance(self):
        from axiom.memory.ask import AskResult, Citation

        result = AskResult(
            answer="X is...",
            citations=[Citation(title="Doc1", text="...", source_id="s1")],
            fragments_used=["frag-abc"],
            mode_used="default",
        )
        # Per spec-memory I16, every result carries the fragments it composed.
        assert "frag-abc" in result.fragments_used


# ---------------------------------------------------------------------------
# Retriever protocol — extensions implement this
# ---------------------------------------------------------------------------


class _StubRetriever:
    """Minimal Retriever for tests — returns a fixed list."""

    def __init__(self, citations):
        self._citations = citations

    def retrieve(self, query, *, k):
        from axiom.memory.ask import Citation
        return [
            Citation(title=c["title"], text=c["text"], source_id=c["source_id"])
            for c in self._citations[:k]
        ]


class _StubLLM:
    """LLM stub that records what it received + returns canned text."""

    def __init__(self, response="canned-answer"):
        self.invocations = []
        self.response = response

    def __call__(self, *, system_blocks, user_message, task):
        self.invocations.append({
            "system_blocks": system_blocks,
            "user_message": user_message,
            "task": task,
        })
        return self.response


# ---------------------------------------------------------------------------
# AskPipeline — basic invocation
# ---------------------------------------------------------------------------


class TestAskPipelineBasics:
    def test_pipeline_returns_answer_and_citations(self, stack):
        from axiom.memory.ask import AskPipeline, AskRequest

        retriever = _StubRetriever([
            {"title": "C1", "text": "criticality is...",
             "source_id": "s1"},
        ])
        llm = _StubLLM(response="answer text")
        pipeline = AskPipeline(
            memory_stack=stack,
            retriever=retriever,
            llm=llm,
        )

        result = pipeline.ask(AskRequest(
            question="What is criticality?",
            principal_id="alice",
            scope_id="test-ask-scope",
        ))

        assert result.answer == "answer text"
        assert len(result.citations) == 1
        assert result.citations[0].title == "C1"

    def test_pipeline_logs_interaction_to_l1(self, stack):
        """Per spec-memory §3 + Stage 1 dual-write: every ask through
        the pipeline writes an episodic fragment to L1."""
        from axiom.memory.ask import AskPipeline, AskRequest

        retriever = _StubRetriever([])
        llm = _StubLLM(response="answer")
        pipeline = AskPipeline(
            memory_stack=stack, retriever=retriever, llm=llm,
        )

        pipeline.ask(AskRequest(
            question="logged question?",
            principal_id="bob",
            scope_id="test-ask-scope",
        ))

        artifacts = list(stack.artifact_registry.list(kind="fragment"))
        # Find the ask fragment by question text.
        questions = [a.data["content"].get("question") for a in artifacts]
        assert "logged question?" in questions

    def test_pipeline_runs_concept_extraction(self, stack):
        """Per Stage 2: every memorable write runs through the
        ExtractorRegistry; concepts populate the L2 graph."""
        from axiom.memory.ask import AskPipeline, AskRequest

        retriever = _StubRetriever([])
        llm = _StubLLM(response="answer")
        pipeline = AskPipeline(
            memory_stack=stack, retriever=retriever, llm=llm,
        )

        pipeline.ask(AskRequest(
            question="What is criticality and reactor design?",
            principal_id="charlie",
            scope_id="test-ask-scope",
        ))

        # Concept graph populated from question text.
        names = sorted(c.canonical_name for c in stack.graph.all_concepts())
        assert "criticality" in names
        assert "reactor" in names


# ---------------------------------------------------------------------------
# PromptComposer integration — system prompt has all 7 layers
# ---------------------------------------------------------------------------


class TestPromptComposerIntegration:
    def test_pipeline_uses_prompt_composer_not_string_concat(self, stack):
        """The pipeline MUST build the system prompt via PromptComposer
        with separate layers — not raw string concat. Test by inspecting
        what reached the LLM."""
        from axiom.memory.ask import AskPipeline, AskRequest

        retriever = _StubRetriever([])
        llm = _StubLLM(response="answer")
        pipeline = AskPipeline(
            memory_stack=stack, retriever=retriever, llm=llm,
        )

        pipeline.ask(AskRequest(
            question="anything",
            principal_id="alice",
            scope_id="test-ask-scope",
        ))

        # The LLM should have been invoked with a list of content blocks
        # (Anthropic format) — proof the composer's render_blocks fired.
        assert len(llm.invocations) == 1
        sb = llm.invocations[0]["system_blocks"]
        assert isinstance(sb, list)
        assert all(b.get("type") == "text" for b in sb)

    def test_recent_activity_lands_in_session_memory_layer(self, stack):
        """When a principal has prior interactions, the pipeline folds
        them into the system prompt as the session_memory layer
        contribution. Verifies via prompt_composer integration."""
        from axiom.memory.ask import AskPipeline, AskRequest

        retriever = _StubRetriever([])
        llm = _StubLLM(response="answer")
        pipeline = AskPipeline(
            memory_stack=stack, retriever=retriever, llm=llm,
        )

        # First ask: nothing prior.
        pipeline.ask(AskRequest(
            question="first question about criticality",
            principal_id="alice",
            scope_id="test-ask-scope",
        ))

        # Second ask: prior interaction should be in context.
        pipeline.ask(AskRequest(
            question="second question",
            principal_id="alice",
            scope_id="test-ask-scope",
        ))

        # The second invocation's system blocks should include the first
        # question's text in some session-memory form.
        second_invocation = llm.invocations[1]
        joined = " ".join(b["text"] for b in second_invocation["system_blocks"])
        assert "criticality" in joined.lower()


# ---------------------------------------------------------------------------
# Concept-graph-aware retrieval — Gap 1 fix
# ---------------------------------------------------------------------------


class TestConceptAwareRetrieval:
    """Stage 2 + AskPipeline: question text gets concept-extracted at
    query time; concepts and their 1-hop neighbors expand the
    retrieval scope. A question about 'control rods' surfaces material
    on 'neutrons' even without exact word match — when 'neutrons' is a
    graph neighbor of 'control rods'."""

    def test_question_concepts_expand_retrieval(self, stack):
        from axiom.memory.ask import AskPipeline, AskRequest
        from axiom.memory.graph import (
            Concept,
            ConceptEdge,
            canonical_concept_id,
        )

        # Seed the graph with a co-occurrence: control + rods + neutrons.
        # In real flow these come from extraction over materials; here
        # we plant them directly to isolate the retrieval test.
        for name in ["control", "rods", "neutrons"]:
            stack.graph.upsert_concept(Concept(
                concept_id=canonical_concept_id(name),
                canonical_name=name,
            ))
        for a, b in [("control", "neutrons"), ("rods", "neutrons")]:
            stack.graph.upsert_edge(ConceptEdge(
                from_concept=canonical_concept_id(a),
                to_concept=canonical_concept_id(b),
                edge_type="co_occurs",
                evidence=["seed-frag"],
            ))

        # Retriever surfaces results keyed by the search terms it sees.
        # The pipeline should ask for both the literal question AND the
        # graph-neighbor concepts.
        class _GraphAwareRetriever:
            def __init__(self):
                self.queries_seen = []

            def retrieve(self, query, *, k):
                from axiom.memory.ask import Citation
                self.queries_seen.append(query)
                return [Citation(
                    title=f"hit-for-{query}",
                    text=f"hit-for-{query}",
                    source_id=f"s-{query}",
                )]

        retriever = _GraphAwareRetriever()
        llm = _StubLLM(response="ok")
        pipeline = AskPipeline(
            memory_stack=stack, retriever=retriever, llm=llm,
            concept_aware_retrieval=True,
        )

        pipeline.ask(AskRequest(
            question="control rods",
            principal_id="alice",
            scope_id="test-ask-scope",
        ))

        # The pipeline should have asked the retriever for both the
        # literal question terms AND for the graph-neighbor "neutrons".
        all_queries = " | ".join(retriever.queries_seen).lower()
        assert "control rods" in all_queries  # literal
        assert "neutrons" in all_queries       # graph neighbor


# ---------------------------------------------------------------------------
# Hooks — extensions specialize without forking
# ---------------------------------------------------------------------------


class TestExtensionHooks:
    def test_extension_can_override_system_prompt_via_hook(self, stack):
        from axiom.memory.ask import AskPipeline, AskRequest

        class _ClassroomHooks:
            """Classroom-style hook: add a domain_context contribution."""
            def contribute_layers(self, request, composer):
                composer.add(
                    "domain_context",
                    name="classroom_overlay",
                    content="You are a tutor for NE 101.",
                    source="classroom",
                )

        retriever = _StubRetriever([])
        llm = _StubLLM(response="ok")
        pipeline = AskPipeline(
            memory_stack=stack, retriever=retriever, llm=llm,
            hooks=_ClassroomHooks(),
        )

        pipeline.ask(AskRequest(
            question="anything",
            principal_id="alice",
            scope_id="test-ask-scope",
        ))

        joined = " ".join(b["text"] for b in llm.invocations[0]["system_blocks"])
        assert "tutor for NE 101" in joined

    def test_extension_can_short_circuit_via_pre_llm_hook(self, stack):
        """An extension can refuse to call the LLM (e.g., a tutor mode
        that asks a Socratic question instead) by returning a synthesized
        AskResult from pre_llm."""
        from axiom.memory.ask import AskPipeline, AskRequest, AskResult

        class _RefusingHooks:
            def contribute_layers(self, request, composer):
                pass

            def pre_llm(self, request, composer, citations):
                if request.mode == "tutor":
                    return AskResult(
                        answer="What do you think causes that?",
                        citations=[],
                        fragments_used=[],
                        mode_used="tutor",
                    )
                return None

        retriever = _StubRetriever([])
        llm = _StubLLM(response="should-not-be-called")
        pipeline = AskPipeline(
            memory_stack=stack, retriever=retriever, llm=llm,
            hooks=_RefusingHooks(),
        )

        result = pipeline.ask(AskRequest(
            question="why does this happen?",
            principal_id="alice",
            scope_id="test-ask-scope",
            mode="tutor",
        ))

        assert "What do you think" in result.answer
        assert llm.invocations == []   # LLM never called


# ---------------------------------------------------------------------------
# Default-deny posture preserved through the pipeline
# ---------------------------------------------------------------------------


class TestDefaultDenyVisibility:
    def test_logged_fragments_default_to_scope_internal(self, stack):
        """Per spec-memory §3.1 + spec-federation-policy: every
        write defaults to SCOPE_INTERNAL; the pipeline doesn't change
        this. Federation gateway is the only place visibility loosens."""
        from axiom.memory.ask import AskPipeline, AskRequest
        from axiom.vega.federation.policy import VisibilityHorizon

        retriever = _StubRetriever([])
        llm = _StubLLM(response="ok")
        pipeline = AskPipeline(
            memory_stack=stack, retriever=retriever, llm=llm,
        )
        pipeline.ask(AskRequest(
            question="anything",
            principal_id="alice",
            scope_id="test-ask-scope",
        ))

        artifacts = list(stack.artifact_registry.list(kind="fragment"))
        for a in artifacts:
            assert a.data["visibility"] == VisibilityHorizon.SCOPE_INTERNAL.value
