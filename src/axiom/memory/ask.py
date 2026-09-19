# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Generic ask pipeline — the canonical "user/agent asks a question" flow.

Every Axiom extension that implements an "ask" affordance — classroom's
student ask, chat's conversational turn, research-loop's investigator
query, future domain extensions' decision queries — follows the same
shape: build context (episodic memory + retrieved corpus + concept-
graph neighbors), assemble a prompt via PromptComposer, invoke an LLM,
log the interaction, return an answer with citations.

This module is that shape, made explicit. Extensions consume it:

- **By composition.** Build an `AskPipeline` with extension-specific
  Retriever + LLM + Hooks; call `pipeline.ask(request)`.
- **By subclassing.** Inherit `AskPipeline`, override individual
  protected methods for fine-grained customization.
- **By hooks.** Pass an `AskHooks` instance to layer in
  extension-specific contributions to PromptComposer, optionally
  short-circuit the LLM call, or transform the response.

The motivation per the user's architectural feedback: classroom built
its own ask path with raw string concatenation, divergent from chat's
PromptComposer-based path. Going forward, generic ask flow lives here;
classroom (and others) inherit it.

Per spec-memory §1, every memorable write through this pipeline goes
through CompositionService. Per Stage 2 + ADR-033 Layer 2, every write
fires concept extractors. Per Stage 3, the prompt context includes a
RecentActivityProjection contribution. The pipeline composes these
into one call site so authors don't reinvent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC
from typing import (
    TYPE_CHECKING,
    Any,
    Protocol,
)

from axiom.infra.prompt_composer import PromptComposer
from axiom.memory.graph import canonical_concept_id
from axiom.memory.projections import (
    TaskSpec,
    format_recent_for_prompt,
)

if TYPE_CHECKING:
    from axiom.memory.bootstrap import MemoryStack


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Citation:
    """A retrieved source surfaced into the prompt + the result.

    Extensions specialize via the Retriever protocol; this is the
    common shape every retriever returns.
    """

    title: str
    text: str
    source_id: str
    score: float | None = None


@dataclass(frozen=True)
class AskRequest:
    """One ask invocation.

    ``mode`` is an extension-specific overlay name (classroom uses
    learning modes; chat uses task types; research uses investigation
    phases). The pipeline doesn't interpret it; hooks do.

    ``cite_only`` skips LLM synthesis — surfaces citations only.
    Useful for offline / cost-sensitive paths.

    ``extra_context`` is a free-form dict for hook coordination —
    e.g. classroom puts the resolved learning-mode policy here.
    """

    question: str
    principal_id: str
    scope_id: str
    mode: str | None = None
    cite_only: bool = False
    k_citations: int = 3
    recent_window: int = 5
    extra_context: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AskResult:
    """The pipeline's typed return.

    ``fragments_used`` cites every L1 fragment_id the projection layer
    composed — satisfies spec-memory I16 (provenance integrity).
    """

    answer: str
    citations: list[Citation]
    fragments_used: list[str]
    mode_used: str
    raw_response: str | None = None


# ---------------------------------------------------------------------------
# Protocols extensions implement
# ---------------------------------------------------------------------------


class Retriever(Protocol):
    """Extension-specific corpus retrieval.

    Most extensions wrap an FTS index, a vector store, or both.
    The pipeline calls retrieve() one or more times — for the literal
    question, and (when concept-aware retrieval is on) for each
    1-hop concept-graph neighbor of concepts in the question.
    """

    def retrieve(self, query: str, *, k: int) -> list[Citation]: ...


class LLM(Protocol):
    """The LLM invocation surface.

    Receives PromptComposer-rendered system blocks + the user message
    + a task tag. Returns the raw response string (or None if no
    provider is available — the pipeline treats this as
    cite-only fallback).
    """

    def __call__(
        self,
        *,
        system_blocks: list[dict[str, Any]],
        user_message: str,
        task: str,
    ) -> str | None: ...


class AskHooks(Protocol):
    """Extension-specific specialization. Every hook is optional;
    a vanilla pipeline runs without any hooks at all.

    Hooks are called in this order during ask():

    1. ``contribute_layers(request, composer)`` — extensions add
       per-extension contributions to the PromptComposer (e.g.
       classroom's mode-overlay, chat's persona block, regulatory
       headers in classified deployments). Always runs.

    2. ``filter_citations(request, citations)`` — rerank or filter
       retrieved citations (e.g. classroom's "tutor mode peeks but
       doesn't show citations verbatim").

    3. ``pre_llm(request, composer, citations)`` — optionally
       short-circuit the LLM call. Returning an ``AskResult`` skips
       LLM invocation and returns directly. Returning ``None``
       proceeds.

    4. ``post_llm(request, raw_response, citations)`` — transform
       the raw LLM response (e.g. classroom's tutor mode strips
       direct answers). Returning a string replaces the answer;
       returning ``None`` uses the raw response.
    """

    def contribute_layers(
        self, request: AskRequest, composer: PromptComposer,
    ) -> None: ...

    def filter_citations(
        self, request: AskRequest, citations: list[Citation],
    ) -> list[Citation]: ...

    def pre_llm(
        self,
        request: AskRequest,
        composer: PromptComposer,
        citations: list[Citation],
    ) -> AskResult | None: ...

    def post_llm(
        self,
        request: AskRequest,
        raw_response: str | None,
        citations: list[Citation],
    ) -> str | None: ...


class _NullHooks:
    """Default hooks — no-ops. Used when caller passes hooks=None."""

    def contribute_layers(self, request, composer):
        return None

    def filter_citations(self, request, citations):
        return citations

    def pre_llm(self, request, composer, citations):
        return None

    def post_llm(self, request, raw_response, citations):
        return None


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


@dataclass
class AskPipeline:
    """Generic ask pipeline.

    Produces an AskResult from an AskRequest by composing context
    via PromptComposer (seven layers per axiom.infra.prompt_composer),
    invoking the LLM, logging the interaction, returning typed
    output. Extensions specialize via Retriever, LLM, and AskHooks.
    """

    memory_stack: MemoryStack
    retriever: Retriever
    llm: LLM
    hooks: AskHooks = field(default_factory=_NullHooks)

    # When True (default), the pipeline expands the question with
    # 1-hop concept-graph neighbors before retrieval. When False,
    # only the literal question is retrieved against. Extensions
    # turn this off when they want strict keyword retrieval.
    concept_aware_retrieval: bool = True

    # ---- Public entry point ------------------------------------------------

    def ask(self, request: AskRequest) -> AskResult:
        composer = PromptComposer()

        # 1. Hook: extension contributes per-layer bits (identity,
        #    domain_context, policy overlay, etc.). All hook methods
        #    are looked up with getattr so extensions only implement
        #    the ones they care about.
        contribute = getattr(self.hooks, "contribute_layers", None)
        if contribute is not None:
            contribute(request, composer)

        # 2. Retrieval — literal question + concept-graph expansion.
        citations = self._retrieve(request)
        filter_citations = getattr(self.hooks, "filter_citations", None)
        if filter_citations is not None:
            citations = filter_citations(request, citations)
        self._add_retrieved_layer(composer, citations)

        # 3. Episodic memory — RecentActivityProjection (Stage 3).
        self._add_session_memory_layer(composer, request)

        # 4. Optional pre-LLM short-circuit.
        pre_llm = getattr(self.hooks, "pre_llm", None)
        if pre_llm is not None:
            early = pre_llm(request, composer, citations)
            if early is not None:
                self._log_interaction(
                    request, answer=early.answer, citations=citations,
                )
                return early

        # 5. cite_only path — no LLM synthesis, return citations only.
        if request.cite_only:
            answer = ""
            self._log_interaction(
                request, answer=answer, citations=citations,
            )
            return AskResult(
                answer=answer,
                citations=citations,
                fragments_used=[],
                mode_used=request.mode or "default",
            )

        # 6. LLM call.
        system_blocks = composer.render_blocks()
        raw_response = self.llm(
            system_blocks=system_blocks,
            user_message=request.question,
            task="ask",
        )
        post_llm = getattr(self.hooks, "post_llm", None)
        transformed = (
            post_llm(request, raw_response, citations)
            if post_llm is not None else None
        )
        answer = transformed if transformed is not None else (
            raw_response or ""
        )

        # 7. Log to L1 + run extractors.
        self._log_interaction(request, answer=answer, citations=citations)

        return AskResult(
            answer=answer,
            citations=citations,
            fragments_used=[],   # filled by Stage-4 projection-citation work
            mode_used=request.mode or "default",
            raw_response=raw_response,
        )

    # ---- Protected steps — extensions can override ------------------------

    def _retrieve(self, request: AskRequest) -> list[Citation]:
        """Literal question + concept-graph 1-hop neighbors.

        Subclasses can override to add vector retrieval, hybrid
        scoring, or extension-specific reranking.
        """
        seen: set[str] = set()
        out: list[Citation] = []

        def _take(c: Citation) -> None:
            if c.source_id in seen:
                return
            seen.add(c.source_id)
            out.append(c)

        for c in self.retriever.retrieve(request.question, k=request.k_citations):
            _take(c)

        if self.concept_aware_retrieval:
            for neighbor_term in self._concept_neighbors_of(request.question):
                for c in self.retriever.retrieve(
                    neighbor_term, k=max(1, request.k_citations // 2),
                ):
                    _take(c)

        return out

    def _concept_neighbors_of(self, query: str) -> list[str]:
        """Pull canonical-name 1-hop neighbors of any concept in the
        question text that exists in the L2 graph. Returns an ordered
        list of additional retrieval terms (each a canonical name).
        """
        # Lazy import to avoid circular dep when graph is unused.
        try:
            graph = self.memory_stack.graph
        except AttributeError:
            return []

        # Tokenize the query into candidate canonical names. Match the
        # extractor's tokenization rule (re-uses the same stopword set
        # via concept_id round-trip).
        terms: list[str] = []
        for word in _simple_tokens(query):
            cid = canonical_concept_id(word)
            if graph.get_concept(cid) is None:
                continue
            for n in graph.neighbors(cid, hops=1):
                if n.canonical_name not in terms and n.canonical_name != word:
                    terms.append(n.canonical_name)
        return terms

    def _add_retrieved_layer(
        self, composer: PromptComposer, citations: list[Citation],
    ) -> None:
        if not citations:
            return
        rendered = "\n".join(
            f"[{c.title}] {c.text}" for c in citations
        )
        composer.add(
            "retrieved",
            name="ask_citations",
            content=f"Retrieved context:\n{rendered}",
            source="axiom.memory.ask",
            required=False,
        )

    def _add_session_memory_layer(
        self, composer: PromptComposer, request: AskRequest,
    ) -> None:
        proj = self.memory_stack.recent_activity(window_n=request.recent_window)
        result = proj.project(
            TaskSpec(
                task_type="recent_activity",
                scope=request.scope_id,
            ),
            principal_id=request.principal_id,
        )
        rendered = format_recent_for_prompt(result)
        if rendered:
            composer.add(
                "session_memory",
                name="recent_activity",
                content=rendered,
                source="axiom.memory.ask",
                required=False,
            )

    def _log_interaction(
        self,
        request: AskRequest,
        *,
        answer: str,
        citations: list[Citation],
    ) -> None:
        """Write an episodic fragment for this ask + run L2 extractors.

        Per spec-memory §1: every memorable write through L1.
        Per ADR-033 Layer 2 + Stage 2: extractors fire on every write.
        """
        from datetime import datetime
        self.memory_stack.write_with_extraction(
            content={
                "event_time": datetime.now(UTC).isoformat(),
                "scope": request.scope_id,
                "question": request.question,
                "answer": answer,
                "had_answer": bool(answer or citations),
                "citations_count": len(citations),
                "mode": request.mode or "default",
            },
            cognitive_type="episodic",
            principal_id=request.principal_id,
            agents=set(),
            resources={c.source_id for c in citations},
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_TOKEN = re.compile(r"[a-z][a-z0-9']{2,}")


def _simple_tokens(text: str) -> list[str]:
    """Same tokenization rule the deterministic extractor uses, so the
    concept_ids we look up here match the ones extractors produced."""
    return _TOKEN.findall(text.lower())


__all__ = [
    "AskHooks",
    "AskPipeline",
    "AskRequest",
    "AskResult",
    "Citation",
    "LLM",
    "Retriever",
]
