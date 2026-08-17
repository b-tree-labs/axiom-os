# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for axiom.rag.grounding — post-retrieval grounding gate.

Generic primitive that addresses RAG's fails-open weakness: when retrieval
doesn't surface confident grounding, vanilla LLM calls invent facts. This
gate detects "below grounding threshold" and either prepends an
uncertainty notice or routes to a fallback (extension-configurable).

Domain-agnostic by construction — every extension that does RAG benefits.
No consumer-specific naming appears here.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Fixtures / helpers — local stubs so the module is import-isolated
# ---------------------------------------------------------------------------


def _cite(*, title: str, source_id: str, score: float | None, text: str = "x"):
    from axiom.memory.ask import Citation
    return Citation(title=title, text=text, source_id=source_id, score=score)


# ---------------------------------------------------------------------------
# evaluate_grounding
# ---------------------------------------------------------------------------


class TestEvaluateGroundingEmpty:
    def test_no_citations_is_not_grounded(self):
        from axiom.rag.grounding import GroundingThreshold, evaluate_grounding

        decision = evaluate_grounding([], GroundingThreshold())
        assert decision.grounded is False
        assert decision.citation_count == 0
        assert decision.top_score is None
        assert decision.avg_score is None
        assert decision.distinct_sources == 0
        # Rationale must explain why.
        assert "citation" in decision.rationale.lower()


class TestEvaluateGroundingTopScore:
    def test_single_citation_above_threshold_is_grounded(self):
        from axiom.rag.grounding import GroundingThreshold, evaluate_grounding

        threshold = GroundingThreshold(min_top_score=0.5)
        citations = [_cite(title="A", source_id="s1", score=0.8)]
        decision = evaluate_grounding(citations, threshold)
        assert decision.grounded is True
        assert decision.top_score == 0.8
        assert decision.citation_count == 1

    def test_citation_below_top_score_threshold_is_not_grounded(self):
        from axiom.rag.grounding import GroundingThreshold, evaluate_grounding

        threshold = GroundingThreshold(min_top_score=0.7)
        citations = [_cite(title="A", source_id="s1", score=0.4)]
        decision = evaluate_grounding(citations, threshold)
        assert decision.grounded is False
        # Rationale names the failed metric.
        assert "top_score" in decision.rationale or "top score" in decision.rationale.lower()


class TestEvaluateGroundingDistinctSources:
    def test_three_citations_one_source_fails_distinct_sources(self):
        from axiom.rag.grounding import GroundingThreshold, evaluate_grounding

        threshold = GroundingThreshold(
            min_citations=1, min_top_score=0.0, min_distinct_sources=2,
        )
        citations = [
            _cite(title="A", source_id="s1", score=0.9),
            _cite(title="B", source_id="s1", score=0.85),
            _cite(title="C", source_id="s1", score=0.8),
        ]
        decision = evaluate_grounding(citations, threshold)
        assert decision.grounded is False
        assert decision.distinct_sources == 1
        assert "distinct_sources" in decision.rationale or "distinct" in decision.rationale.lower()

    def test_three_citations_three_sources_passes(self):
        from axiom.rag.grounding import GroundingThreshold, evaluate_grounding

        threshold = GroundingThreshold(
            min_citations=1, min_top_score=0.0, min_distinct_sources=3,
        )
        citations = [
            _cite(title="A", source_id="s1", score=0.6),
            _cite(title="B", source_id="s2", score=0.55),
            _cite(title="C", source_id="s3", score=0.5),
        ]
        decision = evaluate_grounding(citations, threshold)
        assert decision.grounded is True
        assert decision.distinct_sources == 3


class TestEvaluateGroundingAverageScore:
    def test_average_score_above_threshold_passes(self):
        from axiom.rag.grounding import GroundingThreshold, evaluate_grounding

        threshold = GroundingThreshold(
            min_citations=1, min_top_score=0.0, min_avg_score=0.5,
        )
        citations = [
            _cite(title="A", source_id="s1", score=0.6),
            _cite(title="B", source_id="s2", score=0.6),
        ]
        decision = evaluate_grounding(citations, threshold)
        assert decision.grounded is True
        assert decision.avg_score is not None
        assert abs(decision.avg_score - 0.6) < 1e-9

    def test_average_score_below_threshold_fails(self):
        from axiom.rag.grounding import GroundingThreshold, evaluate_grounding

        threshold = GroundingThreshold(
            min_citations=1, min_top_score=0.0, min_avg_score=0.7,
        )
        citations = [
            _cite(title="A", source_id="s1", score=0.6),
            _cite(title="B", source_id="s2", score=0.5),
        ]
        decision = evaluate_grounding(citations, threshold)
        assert decision.grounded is False
        # Rationale names the failed metric.
        assert "avg" in decision.rationale.lower() or "average" in decision.rationale.lower()


class TestEvaluateGroundingMinCitations:
    def test_zero_min_citations_with_empty_list_still_not_grounded(self):
        """Pathological config: min_citations=0 still requires real grounding;
        empty citation set always fails because there's no signal at all."""
        from axiom.rag.grounding import GroundingThreshold, evaluate_grounding

        threshold = GroundingThreshold(min_citations=0, min_top_score=0.0)
        decision = evaluate_grounding([], threshold)
        # Even with zero floor, no citations means no top_score and no
        # distinct sources — explicitly NOT grounded.
        assert decision.grounded is False

    def test_below_min_citations_fails(self):
        from axiom.rag.grounding import GroundingThreshold, evaluate_grounding

        threshold = GroundingThreshold(min_citations=3, min_top_score=0.0)
        citations = [
            _cite(title="A", source_id="s1", score=0.9),
            _cite(title="B", source_id="s2", score=0.9),
        ]
        decision = evaluate_grounding(citations, threshold)
        assert decision.grounded is False
        assert "citation" in decision.rationale.lower()


class TestEvaluateGroundingMissingScores:
    def test_citations_with_none_scores_treated_as_zero(self):
        """Score=None is treated as 0.0 for threshold evaluation — so a
        retriever that doesn't emit scores won't accidentally pass."""
        from axiom.rag.grounding import GroundingThreshold, evaluate_grounding

        threshold = GroundingThreshold(min_top_score=0.5)
        citations = [_cite(title="A", source_id="s1", score=None)]
        decision = evaluate_grounding(citations, threshold)
        assert decision.grounded is False


# ---------------------------------------------------------------------------
# make_uncertainty_notice
# ---------------------------------------------------------------------------


class TestUncertaintyNotice:
    def test_notice_text_includes_metrics_and_caveat(self):
        from axiom.rag.grounding import (
            GroundingThreshold,
            evaluate_grounding,
            make_uncertainty_notice,
        )

        threshold = GroundingThreshold(min_top_score=0.7)
        citations = [_cite(title="A", source_id="s1", score=0.4)]
        decision = evaluate_grounding(citations, threshold)
        notice = make_uncertainty_notice(decision)

        # All three metrics surface to the user.
        assert "1" in notice.text  # citation count
        assert "0.4" in notice.text  # top score
        assert "0.7" in notice.text  # threshold
        # The "verify before citing" caveat is non-negotiable.
        assert "verify" in notice.text.lower()
        # Decision is round-tripped on the notice for downstream auditing.
        assert notice.decision is decision

    def test_notice_text_handles_empty_citations(self):
        from axiom.rag.grounding import (
            GroundingThreshold,
            evaluate_grounding,
            make_uncertainty_notice,
        )

        decision = evaluate_grounding([], GroundingThreshold(min_top_score=0.5))
        notice = make_uncertainty_notice(decision)
        # Even with no citations, the notice renders cleanly.
        assert "0" in notice.text
        assert "verify" in notice.text.lower()


# ---------------------------------------------------------------------------
# GroundingHooks — three modes
# ---------------------------------------------------------------------------


class _RecordingComposer:
    """Stand-in for PromptComposer for mode-contract tests; the hook
    doesn't touch the composer except via contribute_layers (which
    GroundingHooks doesn't implement), so a sentinel is enough."""


def _make_request(question="q?"):
    from axiom.memory.ask import AskRequest
    return AskRequest(question=question, principal_id="alice", scope_id="s")


class TestGroundingHooksPrependMode:
    def test_prepend_when_below_threshold(self):
        from axiom.rag.grounding import GroundingHooks, GroundingThreshold

        hooks = GroundingHooks(
            threshold=GroundingThreshold(min_top_score=0.7),
            mode="prepend",
        )
        request = _make_request()
        citations = [_cite(title="A", source_id="s1", score=0.3)]

        # pre_llm should NOT short-circuit in prepend mode.
        early = hooks.pre_llm(request, _RecordingComposer(), citations)
        assert early is None

        # post_llm prepends the notice.
        transformed = hooks.post_llm(request, "raw answer", citations)
        assert transformed is not None
        assert "raw answer" in transformed
        # Notice precedes the answer.
        assert transformed.index("verify") < transformed.index("raw answer") or \
               "verify" in transformed.split("raw answer")[0].lower()

    def test_prepend_passes_through_when_grounded(self):
        from axiom.rag.grounding import GroundingHooks, GroundingThreshold

        hooks = GroundingHooks(
            threshold=GroundingThreshold(min_top_score=0.5),
            mode="prepend",
        )
        request = _make_request()
        citations = [_cite(title="A", source_id="s1", score=0.9)]

        # When grounded, post_llm returns None (use raw answer unchanged).
        transformed = hooks.post_llm(request, "good answer", citations)
        assert transformed is None


class TestGroundingHooksSubstituteMode:
    def test_substitute_short_circuits_below_threshold(self):
        from axiom.rag.grounding import GroundingHooks, GroundingThreshold

        hooks = GroundingHooks(
            threshold=GroundingThreshold(min_top_score=0.7),
            mode="substitute",
        )
        request = _make_request()
        citations = [_cite(title="A", source_id="s1", score=0.2)]

        early = hooks.pre_llm(request, _RecordingComposer(), citations)
        assert early is not None
        # The short-circuit AskResult carries the notice as the answer.
        assert "verify" in early.answer.lower()
        # Citations are surfaced even when the LLM is skipped — caller
        # may still want to show what little was retrieved.
        assert early.citations == citations

    def test_substitute_does_not_short_circuit_when_grounded(self):
        from axiom.rag.grounding import GroundingHooks, GroundingThreshold

        hooks = GroundingHooks(
            threshold=GroundingThreshold(min_top_score=0.5),
            mode="substitute",
        )
        request = _make_request()
        citations = [_cite(title="A", source_id="s1", score=0.9)]

        early = hooks.pre_llm(request, _RecordingComposer(), citations)
        assert early is None
        # And post_llm must NOT mutate the answer when grounded.
        transformed = hooks.post_llm(request, "good answer", citations)
        assert transformed is None

    def test_substitute_post_llm_is_no_op(self):
        """Substitute mode does its work in pre_llm; post_llm must NEVER
        alter the answer (otherwise we'd double-stamp the notice when
        grounded fails in some race)."""
        from axiom.rag.grounding import GroundingHooks, GroundingThreshold

        hooks = GroundingHooks(
            threshold=GroundingThreshold(min_top_score=0.7),
            mode="substitute",
        )
        request = _make_request()
        citations = [_cite(title="A", source_id="s1", score=0.2)]

        # Even though grounding would fail, post_llm should return None
        # (mode contract: substitute uses pre_llm exclusively).
        transformed = hooks.post_llm(request, "raw", citations)
        assert transformed is None


class TestGroundingHooksAuditOnlyMode:
    def test_audit_only_pre_llm_returns_none(self):
        from axiom.rag.grounding import GroundingHooks, GroundingThreshold

        hooks = GroundingHooks(
            threshold=GroundingThreshold(min_top_score=0.9),
            mode="audit_only",
        )
        request = _make_request()
        citations = [_cite(title="A", source_id="s1", score=0.1)]

        early = hooks.pre_llm(request, _RecordingComposer(), citations)
        assert early is None

    def test_audit_only_post_llm_returns_none(self):
        from axiom.rag.grounding import GroundingHooks, GroundingThreshold

        hooks = GroundingHooks(
            threshold=GroundingThreshold(min_top_score=0.9),
            mode="audit_only",
        )
        request = _make_request()
        citations = [_cite(title="A", source_id="s1", score=0.1)]

        transformed = hooks.post_llm(request, "raw answer", citations)
        # audit_only never changes the user-visible answer.
        assert transformed is None

    def test_audit_only_records_decision(self):
        """audit_only's whole point is to capture the GroundingDecision
        for offline review without changing what the user sees. The hook
        exposes the most-recent decision via .last_decision."""
        from axiom.rag.grounding import GroundingHooks, GroundingThreshold

        hooks = GroundingHooks(
            threshold=GroundingThreshold(min_top_score=0.9),
            mode="audit_only",
        )
        request = _make_request()
        citations = [_cite(title="A", source_id="s1", score=0.1)]

        hooks.pre_llm(request, _RecordingComposer(), citations)
        assert hooks.last_decision is not None
        assert hooks.last_decision.grounded is False


# ---------------------------------------------------------------------------
# GroundingHooks — invariants
# ---------------------------------------------------------------------------


class TestGroundingHooksContract:
    def test_unknown_mode_raises(self):
        from axiom.rag.grounding import GroundingHooks, GroundingThreshold

        try:
            GroundingHooks(
                threshold=GroundingThreshold(),
                mode="bogus",  # type: ignore[arg-type]
            )
        except ValueError:
            return
        raise AssertionError("expected ValueError on unknown mode")

    def test_filter_citations_is_passthrough(self):
        """GroundingHooks must not silently drop citations — that's the
        retriever's job."""
        from axiom.rag.grounding import GroundingHooks, GroundingThreshold

        hooks = GroundingHooks(
            threshold=GroundingThreshold(),
            mode="prepend",
        )
        request = _make_request()
        cites = [_cite(title="A", source_id="s1", score=0.5)]
        out = hooks.filter_citations(request, cites)
        assert out == cites

    def test_contribute_layers_is_no_op(self):
        from axiom.rag.grounding import GroundingHooks, GroundingThreshold

        hooks = GroundingHooks(
            threshold=GroundingThreshold(),
            mode="prepend",
        )
        # Should not raise even with a stand-in composer.
        hooks.contribute_layers(_make_request(), _RecordingComposer())


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


class TestPublicSurface:
    def test_exposed_via_axiom_rag_grounding(self):
        import axiom.rag.grounding as g

        for name in (
            "GroundingThreshold",
            "GroundingDecision",
            "UncertaintyNotice",
            "evaluate_grounding",
            "make_uncertainty_notice",
            "GroundingHooks",
        ):
            assert hasattr(g, name), f"missing {name}"
