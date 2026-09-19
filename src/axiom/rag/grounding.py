# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Post-retrieval grounding gate — addresses RAG's fails-open weakness.

The Problem
-----------
Vanilla RAG pipelines pass whatever citations the retriever returned (or
nothing at all) into the LLM and trust the model to "be honest" about
gaps. In practice, when retrieval doesn't surface confident grounding,
the LLM falls back to its training-data prior and fabricates plausible
but unsourced claims. The user can't tell from the answer alone whether
the model was actually grounded in retrieved evidence.

The Primitive
-------------
This module provides a domain-agnostic gate that runs *between*
retrieval and LLM synthesis:

1. ``evaluate_grounding`` scores the retrieved citations against a
   per-step ``GroundingThreshold`` (citation count, top score, average
   score, distinct sources) and returns a ``GroundingDecision`` with a
   human-readable rationale.

2. ``make_uncertainty_notice`` renders a domain-agnostic warning when
   the decision is below threshold.

3. ``GroundingHooks`` is a drop-in ``AskHooks`` implementation that
   integrates the gate with the generic ``AskPipeline`` in three modes:

   - ``prepend`` — Prepend an uncertainty notice to the LLM's answer.
     Cheapest defense; preserves the answer but flags it.
   - ``substitute`` — Skip the LLM call entirely (via ``pre_llm``);
     return the notice as the answer. Strongest defense; cheapest at
     inference time when grounding has already failed.
   - ``audit_only`` — Don't alter the user-visible answer; just record
     the decision for offline review. Useful for tuning thresholds
     before flipping to prepend or substitute.

Every extension that uses ``AskPipeline`` benefits without writing any
RAG-specific glue. This module never names a domain consumer.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from axiom.memory.ask import AskRequest, AskResult, Citation

# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GroundingThreshold:
    """Per-step thresholds for what counts as "grounded enough".

    Defaults are intentionally permissive (one citation, top score
    >= 0.5) so the gate doesn't surprise extensions that haven't tuned
    it. Production deployments should raise ``min_top_score`` and set
    ``min_distinct_sources`` >= 2 to catch the common single-source
    echo-chamber failure mode.

    Attributes
    ----------
    min_citations
        Floor on number of retrieved citations. Empty retrieval is
        always considered ungrounded regardless of this value.
    min_top_score
        At least one citation must have ``score >= min_top_score``.
        Citations whose ``score`` is ``None`` are treated as 0.0 so a
        retriever that doesn't emit scores cannot accidentally pass.
    min_avg_score
        Mean of all citation scores must be ``>= min_avg_score``. Set
        to 0.0 (default) to disable.
    min_distinct_sources
        Citations must reference at least this many distinct
        ``source_id`` values. Defends against single-document
        echo-chamber retrieval.
    """

    min_citations: int = 1
    min_top_score: float = 0.5
    min_avg_score: float = 0.0
    min_distinct_sources: int = 1


@dataclass(frozen=True)
class GroundingDecision:
    """Outcome of evaluating citations against a ``GroundingThreshold``.

    ``rationale`` is human-readable and names the failed metric(s) when
    ``grounded`` is False. Suitable to surface in audit logs, dev tools,
    or (truncated) in user-facing notices.
    """

    grounded: bool
    rationale: str
    citation_count: int
    top_score: float | None
    avg_score: float | None
    distinct_sources: int
    threshold: GroundingThreshold


@dataclass(frozen=True)
class UncertaintyNotice:
    """Rendered when grounding is below threshold.

    The text is domain-agnostic and short enough to prepend to a chat
    response without overwhelming it. ``decision`` is round-tripped so
    downstream code can inspect the underlying metrics without re-parsing
    the text.
    """

    text: str
    decision: GroundingDecision


# ---------------------------------------------------------------------------
# evaluate_grounding
# ---------------------------------------------------------------------------


_DEFAULT_THRESHOLD = GroundingThreshold()


def _score_or_zero(c: Citation) -> float:
    """Treat ``None`` scores as 0.0 so retrievers without scoring can't
    silently bypass the gate."""
    return float(c.score) if c.score is not None else 0.0


def evaluate_grounding(
    citations: Sequence[Citation],
    threshold: GroundingThreshold = _DEFAULT_THRESHOLD,
) -> GroundingDecision:
    """Evaluate ``citations`` against ``threshold``.

    Returns a ``GroundingDecision`` whose ``grounded`` flag is True only
    when every threshold metric is satisfied. ``rationale`` lists the
    failed metric names in a stable order so downstream code can parse
    it deterministically if needed.
    """
    citation_count = len(citations)

    if citation_count == 0:
        return GroundingDecision(
            grounded=False,
            rationale="no citations returned by retrieval",
            citation_count=0,
            top_score=None,
            avg_score=None,
            distinct_sources=0,
            threshold=threshold,
        )

    scores = [_score_or_zero(c) for c in citations]
    top_score = max(scores)
    avg_score = sum(scores) / len(scores)
    distinct_sources = len({c.source_id for c in citations})

    failures: list[str] = []

    if citation_count < threshold.min_citations:
        failures.append(
            f"citation_count={citation_count} < min_citations={threshold.min_citations}"
        )
    if top_score < threshold.min_top_score:
        failures.append(
            f"top_score={top_score:.2f} < min_top_score={threshold.min_top_score:.2f}"
        )
    if avg_score < threshold.min_avg_score:
        failures.append(
            f"avg_score={avg_score:.2f} < min_avg_score={threshold.min_avg_score:.2f}"
        )
    if distinct_sources < threshold.min_distinct_sources:
        failures.append(
            f"distinct_sources={distinct_sources} < "
            f"min_distinct_sources={threshold.min_distinct_sources}"
        )

    if failures:
        rationale = "below threshold: " + "; ".join(failures)
        grounded = False
    else:
        rationale = "all threshold metrics satisfied"
        grounded = True

    return GroundingDecision(
        grounded=grounded,
        rationale=rationale,
        citation_count=citation_count,
        top_score=top_score,
        avg_score=avg_score,
        distinct_sources=distinct_sources,
        threshold=threshold,
    )


# ---------------------------------------------------------------------------
# make_uncertainty_notice
# ---------------------------------------------------------------------------


_NOTICE_PREFIX = "⚠"  # warning sign U+26A0


def make_uncertainty_notice(decision: GroundingDecision) -> UncertaintyNotice:
    """Render a user-facing notice for an ungrounded decision.

    The notice is short, domain-agnostic, and ends with the
    "verify before citing" caveat so downstream consumers can't
    accidentally cite a model-prior answer as if it were sourced.
    """
    top = decision.top_score if decision.top_score is not None else 0.0
    text = (
        f"{_NOTICE_PREFIX} Below grounding threshold "
        f"({decision.citation_count} citations, "
        f"top score {top:.2f} < threshold "
        f"{decision.threshold.min_top_score:.2f}). "
        "Answering from training-data prior — verify before citing."
    )
    return UncertaintyNotice(text=text, decision=decision)


# ---------------------------------------------------------------------------
# GroundingHooks — AskHooks integration
# ---------------------------------------------------------------------------


GroundingMode = Literal["prepend", "substitute", "audit_only"]
_VALID_MODES = ("prepend", "substitute", "audit_only")


@dataclass
class GroundingHooks:
    """``AskHooks`` implementation that wires the grounding gate into
    the generic ``AskPipeline``.

    Construct with a ``GroundingThreshold`` and one of three modes:

    - ``"prepend"`` — Lets the LLM run, then prepends an uncertainty
      notice to its answer when grounding fails. Cheapest at design
      time; the user still sees the model's attempt.
    - ``"substitute"`` — Short-circuits ``pre_llm`` when grounding
      fails: the pipeline returns the notice as the answer and never
      calls the LLM. Strongest defense and cheapest at inference time
      when retrieval is empty.
    - ``"audit_only"`` — Never changes the user-visible answer; just
      records the most-recent ``GroundingDecision`` on
      ``self.last_decision`` so calling code (or a tracing hook) can
      log it. Useful for tuning thresholds before flipping behavior.

    The mode is fixed at construction. Extensions that need runtime
    routing (e.g. "prepend in dev, substitute in prod") should
    construct two ``GroundingHooks`` and select between them at
    pipeline-build time.
    """

    threshold: GroundingThreshold
    mode: GroundingMode = "prepend"

    # Last decision computed; populated by both pre_llm and post_llm
    # so audit_only consumers can read it after each pipeline turn.
    last_decision: GroundingDecision | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        if self.mode not in _VALID_MODES:
            raise ValueError(
                f"GroundingHooks.mode={self.mode!r} must be one of {_VALID_MODES}"
            )

    # --- Hook 1: contribute_layers ----------------------------------------
    #
    # Grounding lives downstream of prompt composition; we have nothing
    # to contribute to the system prompt itself. Provided as a no-op so
    # this class satisfies the AskHooks Protocol via duck typing.

    def contribute_layers(self, request: AskRequest, composer: Any) -> None:
        return None

    # --- Hook 2: filter_citations -----------------------------------------
    #
    # Grounding judges citations; it doesn't filter them. The retriever
    # (or a domain-specific filter_citations hook) owns that.

    def filter_citations(
        self, request: AskRequest, citations: list[Citation],
    ) -> list[Citation]:
        return citations

    # --- Hook 3: pre_llm --------------------------------------------------

    def pre_llm(
        self,
        request: AskRequest,
        composer: Any,
        citations: list[Citation],
    ) -> AskResult | None:
        """In ``substitute`` mode, short-circuit the LLM with the notice
        when grounding fails. In other modes, just record the decision
        and let the pipeline proceed."""
        decision = evaluate_grounding(citations, self.threshold)
        self.last_decision = decision

        if decision.grounded:
            return None

        if self.mode == "substitute":
            notice = make_uncertainty_notice(decision)
            return AskResult(
                answer=notice.text,
                citations=list(citations),
                fragments_used=[],
                mode_used=request.mode or "default",
            )

        # prepend / audit_only — no short-circuit; post_llm (or nothing)
        # handles user-visible behavior.
        return None

    # --- Hook 4: post_llm -------------------------------------------------

    def post_llm(
        self,
        request: AskRequest,
        raw_response: str | None,
        citations: list[Citation],
    ) -> str | None:
        """In ``prepend`` mode, prefix the notice to the raw answer when
        grounding fails. In ``substitute`` mode the work was already
        done in ``pre_llm`` — never double-stamp here. In ``audit_only``
        the user-visible answer is never altered.
        """
        if self.mode != "prepend":
            return None

        # Re-use last_decision when pre_llm already evaluated; otherwise
        # evaluate fresh. (Pipelines that skip pre_llm — there aren't
        # any in axiom today, but a future one might — still get the
        # gate.)
        decision = self.last_decision
        if decision is None:
            decision = evaluate_grounding(citations, self.threshold)
            self.last_decision = decision

        if decision.grounded:
            return None

        notice = make_uncertainty_notice(decision)
        body = raw_response or ""
        if body:
            return f"{notice.text}\n\n{body}"
        return notice.text


__all__ = [
    "GroundingDecision",
    "GroundingHooks",
    "GroundingMode",
    "GroundingThreshold",
    "UncertaintyNotice",
    "evaluate_grounding",
    "make_uncertainty_notice",
]
