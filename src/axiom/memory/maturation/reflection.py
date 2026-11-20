# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Reflection — daily consolidation (mat-3; stage 3 of the maturation lifecycle).

Per `spec-memory-reflection.md`, reflection consolidates a batch of episodes
into derived semantic fragments. mat-3 ships the *daily cadence* with the
deterministic extractor; the LLM-driven extractor lands when the gateway
integration is wired (out of scope for this MVP).

Public surface:

- :class:`EpisodeBatch` — input to a :class:`ReflectionExtractor` call
- :class:`SemanticProposal` — output: an insight + its citations
- :class:`ReflectionExtractor` — Protocol implementations satisfy
- :class:`DeterministicReflectionExtractor` — pure heuristic; byte-identical
  replay (Phase-1 acceptance criterion)
- :class:`ReflectionStageHandler` — :class:`StageHandler` for the daily
  cadence; consults the orchestrator's importance-threshold trigger

The policy gate enforced here:

- Citation requirement: every proposal must cite at least one episode in
  the batch
- Classification composition: derived classification = max(source
  classifications) — enforced at write via the substrate's existing
  visibility/classification logic
- Idempotency via the ``reflection_marker`` fragment: each successful
  cycle writes a marker; the next cycle considers only episodes newer
  than the marker

LLM-driven extractor + cadence variants (weekly themes, monthly identity)
land in subsequent commits.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, NamedTuple, Protocol, runtime_checkable

from .types import CycleBudget, Stage, StageResult


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


class EpisodeBatch(NamedTuple):
    """Input to a :class:`ReflectionExtractor`.

    Kept dependency-light: the orchestrator builds these from the registry
    and passes them by value. The extractor consumes summaries +
    importance; it does not need to read the full ``MemoryFragment`` shape
    to score patterns.
    """

    scope: str
    cadence: str                         # "daily" | "weekly" | "monthly" | "custom"
    episodes: tuple[str, ...]            # fragment-ids
    episode_summaries: tuple[str, ...]   # parallel to ``episodes``
    importance: tuple[float, ...]        # parallel to ``episodes``; 0 if unscored
    accumulated_importance: float
    window_start: str                    # ISO 8601
    window_end: str                      # ISO 8601


class SemanticProposal(NamedTuple):
    """Output of a :class:`ReflectionExtractor`. The platform decides
    whether to write (policy gate)."""

    summary: str
    derived_from: tuple[str, ...]        # subset of EpisodeBatch.episodes
    confidence: float = 0.7
    cognitive_type_target: str = "semantic"  # "semantic" or "core" (monthly cadence only)
    extra: dict | None = None


@runtime_checkable
class ReflectionExtractor(Protocol):
    """Protocol for synthesis from an :class:`EpisodeBatch` to proposals."""

    def synthesize(self, batch: EpisodeBatch) -> list[SemanticProposal]: ...


# ---------------------------------------------------------------------------
# DeterministicReflectionExtractor — heuristic, byte-identical
# ---------------------------------------------------------------------------


_STOPWORDS = frozenset(
    {
        # Short closed-class words that aren't useful as recurring tokens.
        "the", "a", "an", "and", "or", "but", "is", "are", "was", "were",
        "be", "been", "being", "to", "of", "in", "for", "on", "at", "by",
        "with", "as", "from", "this", "that", "these", "those", "i", "you",
        "he", "she", "it", "we", "they", "me", "us", "my", "your", "their",
        "what", "who", "when", "where", "why", "how", "do", "does", "did",
        "have", "has", "had", "can", "could", "will", "would", "should",
        "may", "might", "must", "ok", "okay", "yes", "no", "thanks", "hi",
        "hello", "user", "asked", "about",
    }
)


class DeterministicReflectionExtractor:
    """Pure-heuristic reflection; byte-identical across runs.

    Surfaces recurring tokens (case-folded; alphanumeric ≥ 4 chars,
    minus a stopword list) across the batch. When a token appears in
    ≥ 2 episode summaries, emits a proposal citing those episodes.

    The output isn't profound — it's a substrate-level placeholder that
    proves the pipeline (episode → semantic with provenance + policy
    gate). The LLM-driven extractor produces the actual Park et al.
    style insights when wired.
    """

    def synthesize(self, batch: EpisodeBatch) -> list[SemanticProposal]:
        if not batch.episodes:
            return []

        # Build token → set of (episode-id, importance) appearances.
        appearances: dict[str, list[tuple[str, float]]] = {}
        for ep_id, summary, importance in zip(
            batch.episodes, batch.episode_summaries, batch.importance, strict=False
        ):
            for token in _tokenize(summary):
                appearances.setdefault(token, []).append((ep_id, importance))

        # Emit one proposal per recurring token, sorted by recurrence × importance.
        candidates: list[tuple[str, list[tuple[str, float]]]] = [
            (tok, app) for tok, app in appearances.items() if len(app) >= 2
        ]
        # Stable, deterministic ordering.
        candidates.sort(
            key=lambda kv: (-len(kv[1]), -sum(i for _, i in kv[1]), kv[0])
        )

        proposals: list[SemanticProposal] = []
        seen_citations: set[tuple[str, ...]] = set()
        for token, app in candidates:
            ep_ids = tuple(sorted({ep_id for ep_id, _ in app}))
            if ep_ids in seen_citations:
                continue
            seen_citations.add(ep_ids)

            n_episodes = len(ep_ids)
            imp_sum = sum(i for _, i in app)
            summary = (
                f"Recurring topic '{token}' across {n_episodes} episode(s) "
                f"(accumulated importance {imp_sum:.1f})"
            )
            # Confidence: more episodes + higher importance → higher confidence.
            # Capped at 0.95; deterministic extractors never claim 1.0.
            confidence = min(0.95, 0.5 + (n_episodes - 1) * 0.1 + imp_sum * 0.005)
            proposals.append(
                SemanticProposal(
                    summary=summary,
                    derived_from=ep_ids,
                    confidence=round(confidence, 3),
                    cognitive_type_target="semantic",
                )
            )

        return proposals


def _tokenize(text: str) -> list[str]:
    """Case-folded, alphanumeric, stopword-filtered.

    Keep when (length ≥ 4) OR (token mixes letters + digits — keeps
    short acronyms / codenames like ``q3``, ``v1``, ``adr033``).
    """
    out = []
    for raw in text.replace("\n", " ").split():
        # Strip simple punctuation; keep digits + letters.
        clean = "".join(ch for ch in raw.lower() if ch.isalnum())
        if not clean or clean in _STOPWORDS:
            continue
        has_digit = any(ch.isdigit() for ch in clean)
        has_letter = any(ch.isalpha() for ch in clean)
        if len(clean) >= 4 or (has_digit and has_letter):
            out.append(clean)
    return out


# ---------------------------------------------------------------------------
# ReflectionStageHandler — daily cadence
# ---------------------------------------------------------------------------


_DEFAULT_IMPORTANCE_THRESHOLD = 150.0


class ReflectionStageHandler:
    """Daily reflection — :class:`StageHandler` implementation.

    Discovers episodes newer than the scope's last ``reflection_marker``,
    builds an :class:`EpisodeBatch`, runs the registered extractor,
    applies the policy gate (citation requirement + classification
    composition), writes accepted proposals as semantic fragments, and
    finally writes a new ``reflection_marker`` so the next cycle sees
    only episodes newer than this one.

    Trigger semantics:

    - ``is_pending``: any un-consumed episodes in scope
    - ``evaluate_trigger``: accumulated importance (across un-consumed
      episodes) ≥ ``importance_threshold`` (default 150.0)
    """

    stage: Stage = Stage.CONSOLIDATION_DAILY

    def __init__(
        self,
        *,
        composition: Any,
        extractor: ReflectionExtractor | None,
        principal_id: str,
        importance_threshold: float = _DEFAULT_IMPORTANCE_THRESHOLD,
        cadence: str = "daily",
    ) -> None:
        self.composition = composition
        self.extractor = extractor or DeterministicReflectionExtractor()
        self.principal_id = principal_id
        self.importance_threshold = importance_threshold
        self.cadence = cadence

    # ------------------------------------------------------------------

    def is_pending(self, scope: str) -> bool:
        return len(self._unconsumed_episodes(scope)) > 0

    def evaluate_trigger(self, scope: str) -> bool:
        ep_ids = self._unconsumed_episodes(scope)
        if not ep_ids:
            return False
        imp_map = self._importance_for(ep_ids)
        accumulated = sum(imp_map.values())
        return accumulated >= self.importance_threshold

    def run(self, scope: str, budget: CycleBudget) -> StageResult:
        started_at = datetime.now(timezone.utc).isoformat()
        ep_ids = self._unconsumed_episodes(scope)
        imp_map = self._importance_for(ep_ids)
        summaries = self._summaries_for(ep_ids)
        accumulated = sum(imp_map.values())

        window_start = "1970-01-01T00:00:00Z"
        last_marker = self._last_reflection_marker(scope)
        if last_marker is not None:
            window_start = last_marker.get("event_time", window_start)

        batch = EpisodeBatch(
            scope=scope,
            cadence=self.cadence,
            episodes=tuple(ep_ids),
            episode_summaries=tuple(summaries.get(i, "") for i in ep_ids),
            importance=tuple(imp_map.get(i, 0.0) for i in ep_ids),
            accumulated_importance=accumulated,
            window_start=window_start,
            window_end=started_at,
        )

        proposals = self.extractor.synthesize(batch)
        accepted = 0
        rejected = 0
        for proposal in proposals:
            if not self._policy_gate(proposal, batch):
                rejected += 1
                continue
            try:
                self._write_semantic(proposal, batch)
                accepted += 1
            except Exception:
                rejected += 1

        # Write a reflection-marker fragment so subsequent cycles know what
        # was already consumed. Always write the marker even if nothing was
        # accepted — the window itself was processed.
        self._write_marker(scope, started_at)

        completed_at = datetime.now(timezone.utc).isoformat()
        return StageResult(
            stage=self.stage,
            scope=scope,
            started_at=started_at,
            completed_at=completed_at,
            items_processed=len(proposals),
            items_succeeded=accepted,
            items_failed=rejected,
            notes=f"deterministic; {accepted} accepted / {rejected} rejected",
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _unconsumed_episodes(self, scope: str) -> list[str]:
        """Episodic chat_turn fragments in scope newer than the last marker."""
        last_marker = self._last_reflection_marker(scope)
        cutoff = last_marker["event_time"] if last_marker else ""

        out: list[str] = []
        for a in self.composition.artifact_registry.list(kind="fragment"):
            content = (a.data or {}).get("content") or {}
            if content.get("scope") != scope:
                continue
            if content.get("fact_kind") != "chat_turn":
                continue
            if cutoff and content.get("event_time", "") <= cutoff:
                continue
            out.append(a.name)
        return sorted(out)

    def _last_reflection_marker(self, scope: str) -> dict | None:
        """Most-recent ``reflection_marker`` content dict for scope, or None."""
        best: dict | None = None
        best_ts = ""
        for a in self.composition.artifact_registry.list(kind="fragment"):
            content = (a.data or {}).get("content") or {}
            if content.get("fact_kind") != "reflection_marker":
                continue
            if content.get("scope") != scope:
                continue
            ts = content.get("event_time", "")
            if ts > best_ts:
                best = content
                best_ts = ts
        return best

    def _importance_for(self, fragment_ids: list[str]) -> dict[str, float]:
        """Lookup importance-score side fragments for the given fragment ids."""
        wanted = set(fragment_ids)
        out: dict[str, float] = {}
        for a in self.composition.artifact_registry.list(kind="fragment"):
            content = (a.data or {}).get("content") or {}
            if content.get("fact_kind") != "importance_score":
                continue
            target = content.get("target_fragment_id")
            if target in wanted:
                out[target] = float(content.get("score", 0.0))
        return out

    def _summaries_for(self, fragment_ids: list[str]) -> dict[str, str]:
        wanted = set(fragment_ids)
        out: dict[str, str] = {}
        for a in self.composition.artifact_registry.list(kind="fragment"):
            if a.name not in wanted:
                continue
            content = (a.data or {}).get("content") or {}
            if content.get("fact_kind") != "chat_turn":
                continue
            out[a.name] = (
                content.get("summary")
                or content.get("user_input")
                or ""
            )
        return out

    def _policy_gate(self, proposal: SemanticProposal, batch: EpisodeBatch) -> bool:
        if not proposal.derived_from:
            return False
        if not set(proposal.derived_from).issubset(set(batch.episodes)):
            return False
        if not (0.0 <= proposal.confidence <= 1.0):
            return False
        return True

    def _write_semantic(
        self, proposal: SemanticProposal, batch: EpisodeBatch
    ) -> None:
        now_iso = datetime.now(timezone.utc).isoformat()
        extractor_kind = (
            "deterministic"
            if isinstance(self.extractor, DeterministicReflectionExtractor)
            else "llm"
        )
        content = {
            "event_time": now_iso,
            "scope": batch.scope,
            "fact_kind": "semantic_insight",
            "summary": proposal.summary,
            "derived_from": list(proposal.derived_from),
            "cadence": batch.cadence,
            "extractor": type(self.extractor).__name__,
            "extractor_kind": extractor_kind,
            "confidence": float(proposal.confidence),
        }
        if proposal.extra:
            content["extra"] = dict(proposal.extra)
        self.composition.write(
            content=content,
            cognitive_type=proposal.cognitive_type_target,
            principal_id=self.principal_id,
            agents={type(self.extractor).__name__},
            resources={f"axiom://memory/{uid}" for uid in proposal.derived_from},
        )

    def _write_marker(self, scope: str, event_time: str) -> None:
        self.composition.write(
            content={
                "event_time": event_time,
                "scope": scope,
                "fact_kind": "reflection_marker",
                "cadence": self.cadence,
                "extractor": type(self.extractor).__name__,
            },
            cognitive_type="episodic",
            principal_id=self.principal_id,
            agents={"axi-memory-reflection"},
            resources=set(),
        )
