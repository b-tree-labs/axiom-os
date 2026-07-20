# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Importance scoring (mat-2; stage 2 of the maturation lifecycle).

Per `spec-memory-reflection.md` §4.2 + `spec-memory-maturation.md` §3 stage 2,
importance scoring is opt-in per scope. The score (0–10) feeds the
importance-threshold trigger that gates reflection.

Two scorer variants per spec:

- :class:`DeterministicImportanceScorer` — pure heuristic; byte-identical
  replay. Useful for tests, regulated scopes, and the default platform
  scorer when LLM cost is undesirable.
- ``LLMImportanceScorer`` — gateway-driven (stub in mat-2; full impl
  follows when the gateway integration lands as a stage-aware service).

The :class:`ImportanceScoringStageHandler` is the catch-up sweep that the
dream-cycle orchestrator (mat-1) dispatches to. It finds episodic
fragments in a scope without a matching ``importance_score`` side fragment
and scores them.

Side-fragment representation (substrate stays append-only):

::

    {
        "fact_kind": "importance_score",
        "event_time": <iso8601>,
        "target_fragment_id": <episode_id>,
        "score": <float 0-10>,
        "scorer": <scorer class name>,
        "scorer_version": "v1",
    }

The original episodic fragment is not mutated. Consumers (reflection's
trigger evaluator) join episode → importance_score by ``target_fragment_id``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable

from ..fragment import MemoryFragment, fragment_from_dict
from .types import CycleBudget, Stage, StageResult


# ---------------------------------------------------------------------------
# Scorer protocol + deterministic implementation
# ---------------------------------------------------------------------------


@runtime_checkable
class ImportanceScorer(Protocol):
    """Score a fragment for importance on a 0–10 scale.

    Implementations are stateless. ``score`` is called once per fragment;
    determinism is the implementation's responsibility (the LLM variant
    cannot guarantee determinism; the deterministic variant must).
    """

    def score(self, fragment: MemoryFragment) -> float: ...


class DeterministicImportanceScorer:
    """Pure-heuristic importance scoring; byte-identical across runs.

    The heuristic is intentionally simple: text length, question marks,
    presence of structured content as proxies for substance. Refine in a
    future revision driven by benchmark data — for now, the goal is
    deterministic, defensible, and unambiguously > random on Park et al.'s
    ordering (mundane < substantive; questions > acknowledgments).
    """

    def score(self, fragment: MemoryFragment) -> float:
        return _score_content(fragment.content)


def _score_content(content: dict) -> float:
    """The actual heuristic. Pure function of content dict."""
    user_input = (content.get("user_input") or "")
    assistant_output = (content.get("assistant_output") or "")
    summary = (content.get("summary") or "")
    total_text = (user_input + "\n" + assistant_output + "\n" + summary).strip()
    total_len = len(total_text)

    score = 3.0
    if "?" in user_input or "?" in summary:
        score += 2.0
    if assistant_output.strip():
        score += 1.0
    if total_len > 200:
        score += 1.0
    if total_len > 800:
        score += 1.0
    if total_len < 30:
        score -= 2.0

    if score < 0.0:
        return 0.0
    if score > 10.0:
        return 10.0
    return score


# ---------------------------------------------------------------------------
# score_fragment — top-level helper
# ---------------------------------------------------------------------------


def score_fragment(
    *,
    composition: Any,
    target_fragment_id: str,
    scorer: ImportanceScorer,
    principal_id: str,
    event_time: str | None = None,
) -> str | None:
    """Score the target fragment and write a side ``importance_score`` fragment.

    Args:
        composition: :class:`CompositionService` whose ledger holds the target.
        target_fragment_id: id of the episodic fragment to score.
        scorer: an :class:`ImportanceScorer` implementation.
        principal_id: principal for the side-fragment write.
        event_time: ISO 8601 timestamp; defaults to now (UTC).

    Returns:
        The new side-fragment's id, or ``None`` if the target was not
        found in the registry.
    """
    # Find by fragment-id, which the registry stores in Artifact.name (not .id).
    artifact = next(
        (
            a
            for a in composition.artifact_registry.list(kind="fragment")
            if a.name == target_fragment_id
        ),
        None,
    )
    if artifact is None or artifact.data is None:
        return None

    fragment = fragment_from_dict(artifact.data)
    score = float(scorer.score(fragment))
    now_iso = event_time or datetime.now(timezone.utc).isoformat()

    side = composition.write(
        content={
            "event_time": now_iso,
            "fact_kind": "importance_score",
            "target_fragment_id": target_fragment_id,
            "score": score,
            "scorer": type(scorer).__name__,
            "scorer_version": "v1",
        },
        cognitive_type="episodic",
        principal_id=principal_id,
        agents={"axi-memory-importance-scorer"},
        resources={f"axiom://memory/{target_fragment_id}"},
    )
    return side.id


# ---------------------------------------------------------------------------
# Stage handler — catch-up sweep
# ---------------------------------------------------------------------------


class ImportanceScoringStageHandler:
    """Catch-up sweep: score every unscored episodic fragment in a scope.

    Implements the :class:`StageHandler` protocol so the dream-cycle
    orchestrator (mat-1) can dispatch to it. Idempotent: re-running the
    handler on a fully-scored scope is a no-op.
    """

    stage: Stage = Stage.IMPORTANCE_SCORING

    def __init__(
        self,
        *,
        composition: Any,
        scorer: ImportanceScorer | None,
        principal_id: str,
    ) -> None:
        self.composition = composition
        self.scorer = scorer or DeterministicImportanceScorer()
        self.principal_id = principal_id

    def is_pending(self, scope: str) -> bool:
        return len(self._unscored(scope)) > 0

    def evaluate_trigger(self, scope: str) -> bool:
        # Catch-up sweep fires whenever there's pending work — no
        # additional cadence gating at this stage. Per-cycle cooldown
        # in the orchestrator prevents thrash.
        return self.is_pending(scope)

    def run(self, scope: str, budget: CycleBudget) -> StageResult:
        started_at = datetime.now(timezone.utc).isoformat()
        unscored = self._unscored(scope)
        succeeded = 0
        failed = 0
        for fragment_id in unscored:
            try:
                score_fragment(
                    composition=self.composition,
                    target_fragment_id=fragment_id,
                    scorer=self.scorer,
                    principal_id=self.principal_id,
                )
                succeeded += 1
            except Exception:
                failed += 1
        completed_at = datetime.now(timezone.utc).isoformat()
        return StageResult(
            stage=Stage.IMPORTANCE_SCORING,
            scope=scope,
            started_at=started_at,
            completed_at=completed_at,
            items_processed=len(unscored),
            items_succeeded=succeeded,
            items_failed=failed,
            calls_used=0,
            tokens_used=0,
            notes=f"deterministic; scored {succeeded}/{len(unscored)}",
        )

    def _unscored(self, scope: str) -> list[str]:
        """Episodic ``chat_turn`` fragments in ``scope`` without an importance_score."""
        artifacts = list(self.composition.artifact_registry.list(kind="fragment"))
        episodes_in_scope = {
            a.name
            for a in artifacts
            if (a.data or {}).get("content", {}).get("scope") == scope
            and (a.data or {}).get("content", {}).get("fact_kind") == "chat_turn"
        }
        scored_targets = {
            (a.data or {}).get("content", {}).get("target_fragment_id")
            for a in artifacts
            if (a.data or {}).get("content", {}).get("fact_kind") == "importance_score"
        }
        return sorted(episodes_in_scope - scored_targets)
