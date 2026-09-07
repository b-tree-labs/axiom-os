# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Compaction — stage 4 (summarize) of the maturation lifecycle.

Per `spec-memory-compaction.md`, compaction is three operations grouped
because they all *reduce* the ledger: summarize (stage 4, this module),
archive (stage 5), forget (stage 6). mat-4 ships summarize-cadence;
archive + forget follow.

Audit-chain enforcement is the load-bearing invariant. Per
`spec-memory-compaction.md §6.1`: a fragment cannot be compacted while
no semantic fragment cites it. The audit-chain rule prevents losing
detail before its insight has been captured.

Public surface:

- :class:`CompactedContent` — output of a summarizer (text + original length)
- :class:`CompactionSummarizer` — Protocol; implementations must reduce
  length by ≥ 50%
- :class:`DefaultSummarizer` — deterministic length-reduction heuristic
- :class:`CompactionSummarizeStageHandler` — :class:`StageHandler`
  implementation for ``Stage.COMPACTION_SUMMARIZE``

Substrate is append-only. Compaction emits two fragments per source:

- ``cognitive_type="episodic"`` with ``fact_kind="compacted_chat_turn"``
  and ``content.compacted_from = <original_id>`` (the summary)
- ``cognitive_type="episodic"`` with ``fact_kind="supersession"``
  and ``content.original_fragment_id`` + ``content.summary_fragment_id``
  (the supersession record clients follow)

The original episode is left untouched in the ledger; consumers look up
its supersession via the side fragment and (after archive in stage 5)
fall back to cold-tier fetch if needed.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, NamedTuple, Protocol, runtime_checkable

from ..fragment import MemoryFragment, fragment_from_dict
from .types import CycleBudget, Stage, StageResult


class CompactedContent(NamedTuple):
    """Output of a :class:`CompactionSummarizer`."""

    summary_text: str
    original_length_chars: int
    compaction_version: str = "v1"


@runtime_checkable
class CompactionSummarizer(Protocol):
    """Pure function: source fragment → CompactedContent.

    Implementations must reduce length by ≥ 50% (per
    `spec-memory-compaction.md §3` contract).
    """

    def summarize(self, source: MemoryFragment) -> CompactedContent: ...


class DefaultSummarizer:
    """Deterministic length-reduction heuristic.

    Keeps ``content.user_input`` + ``content.summary`` (already-short
    fields) and drops the verbose ``content.assistant_output`` body,
    replacing it with a one-line truncation. Byte-identical replay.
    """

    def summarize(self, source: MemoryFragment) -> CompactedContent:
        content = source.content
        user_input = (content.get("user_input") or "").strip()
        summary = (content.get("summary") or "").strip()
        assistant_output = (content.get("assistant_output") or "").strip()

        original_length = (
            len(user_input) + len(summary) + len(assistant_output)
        )

        # Construct the compacted text — keep what's already short, truncate
        # the verbose part to a single one-liner.
        parts: list[str] = []
        if summary:
            parts.append(f"Summary: {summary[:200]}")
        elif user_input:
            parts.append(f"User: {user_input[:200]}")
        if assistant_output:
            parts.append(
                f"Assistant (truncated): {assistant_output[:100]}"
                + ("…" if len(assistant_output) > 100 else "")
            )

        summary_text = " | ".join(parts).strip()

        # Defensive bound: if the heuristic accidentally produces a longer
        # string (very-short source where prefixes balloon it), cap it.
        if len(summary_text) > original_length:
            summary_text = summary_text[: max(1, original_length // 2)]

        return CompactedContent(
            summary_text=summary_text,
            original_length_chars=original_length,
            compaction_version="v1",
        )


# ---------------------------------------------------------------------------
# Stage handler — summarize cadence
# ---------------------------------------------------------------------------


class CompactionSummarizeStageHandler:
    """Summarize-cadence compaction (:class:`StageHandler` for
    :data:`Stage.COMPACTION_SUMMARIZE`).

    Audit-chain rule (default): only compacts episodes whose ``id``
    appears in some ``semantic_insight``'s ``content.derived_from``.
    Episodes without a derived semantic are left untouched — consolidation
    hasn't yet captured their insight.
    """

    stage: Stage = Stage.COMPACTION_SUMMARIZE

    def __init__(
        self,
        *,
        composition: Any,
        summarizer: CompactionSummarizer | None,
        principal_id: str,
        summarize_age_days: int = 7,
    ) -> None:
        self.composition = composition
        self.summarizer = summarizer or DefaultSummarizer()
        self.principal_id = principal_id
        self.summarize_age_days = summarize_age_days

    def is_pending(self, scope: str) -> bool:
        return len(self._eligible(scope)) > 0

    def evaluate_trigger(self, scope: str) -> bool:
        # Compaction fires whenever eligible work exists. Per-cycle cooldown
        # in the orchestrator prevents runaway frequency.
        return self.is_pending(scope)

    def run(self, scope: str, budget: CycleBudget) -> StageResult:
        started_at = datetime.now(timezone.utc).isoformat()
        eligible = self._eligible(scope)
        succeeded = 0
        failed = 0

        for fragment_id in eligible:
            try:
                self._compact_one(fragment_id, scope)
                succeeded += 1
            except Exception:
                failed += 1

        completed_at = datetime.now(timezone.utc).isoformat()
        return StageResult(
            stage=self.stage,
            scope=scope,
            started_at=started_at,
            completed_at=completed_at,
            items_processed=len(eligible),
            items_succeeded=succeeded,
            items_failed=failed,
            notes=f"summarize cadence; compacted {succeeded}/{len(eligible)}",
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _eligible(self, scope: str) -> list[str]:
        """Episode ids in scope that satisfy the audit-chain rule + not yet compacted."""
        artifacts = list(self.composition.artifact_registry.list(kind="fragment"))

        # All episodic chat_turn fragments in scope (the candidates).
        chat_turns_in_scope: dict[str, dict] = {}
        for a in artifacts:
            content = (a.data or {}).get("content") or {}
            if content.get("scope") != scope:
                continue
            if content.get("fact_kind") != "chat_turn":
                continue
            chat_turns_in_scope[a.name] = content

        # Episode ids that have a semantic_insight citing them.
        cited_episodes: set[str] = set()
        for a in artifacts:
            content = (a.data or {}).get("content") or {}
            if content.get("fact_kind") != "semantic_insight":
                continue
            for uid in content.get("derived_from") or []:
                cited_episodes.add(uid)

        # Episode ids that are already compacted (supersession exists).
        already_compacted: set[str] = set()
        for a in artifacts:
            content = (a.data or {}).get("content") or {}
            if content.get("fact_kind") != "supersession":
                continue
            orig = content.get("original_fragment_id")
            if orig:
                already_compacted.add(orig)

        eligible = [
            ep_id
            for ep_id in chat_turns_in_scope
            if ep_id in cited_episodes and ep_id not in already_compacted
        ]
        return sorted(eligible)

    def _compact_one(self, fragment_id: str, scope: str) -> None:
        # Resolve the source via name (= fragment-id).
        artifact = next(
            (
                a
                for a in self.composition.artifact_registry.list(kind="fragment")
                if a.name == fragment_id
            ),
            None,
        )
        if artifact is None or artifact.data is None:
            return

        source = fragment_from_dict(artifact.data)
        compacted = self.summarizer.summarize(source)

        if 2 * len(compacted.summary_text) > compacted.original_length_chars:
            # Skip — heuristic produced a non-meaningful reduction. This is a
            # belt-and-suspenders check on top of the summarizer contract.
            return

        now_iso = datetime.now(timezone.utc).isoformat()

        # Write the compacted summary fragment.
        summary_frag = self.composition.write(
            content={
                "event_time": source.content.get("event_time", now_iso),
                "scope": scope,
                "fact_kind": "compacted_chat_turn",
                "summary": compacted.summary_text,
                "compacted_from": fragment_id,
                "original_length_chars": compacted.original_length_chars,
                "compaction_version": compacted.compaction_version,
                "tool": source.content.get("tool", ""),
                "model": source.content.get("model", ""),
            },
            cognitive_type="episodic",
            principal_id=self.principal_id,
            agents={"axi-memory-compactor"},
            resources={f"axiom://memory/{fragment_id}"},
        )

        # Write the supersession side fragment.
        self.composition.write(
            content={
                "event_time": now_iso,
                "scope": scope,
                "fact_kind": "supersession",
                "original_fragment_id": fragment_id,
                "summary_fragment_id": summary_frag.id,
                "reason": "compacted",
            },
            cognitive_type="episodic",
            principal_id=self.principal_id,
            agents={"axi-memory-compactor"},
            resources={
                f"axiom://memory/{fragment_id}",
                f"axiom://memory/{summary_frag.id}",
            },
        )
