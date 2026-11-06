# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Dynamic RAG routing — policy-driven, runtime-swappable, with shadow A/B paths.

RAGPolicy is the unit of configuration: which corpora to query, what's
voided, whether a shadow comparison runs. PolicyAwareRetriever reads
the active policy on every retrieve() call — no restart needed for
policy swaps.

This plugs into ClassroomChatPipeline's `rag_retriever` parameter.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

log = logging.getLogger(__name__)

# Type alias: (query, top_k) -> list[dict]
CorpusRetriever = Callable[[str, int], list[dict]]


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class RAGPolicy:
    """Named, versioned RAG routing policy."""

    id: str
    name: str
    corpora: list[dict[str, Any]]  # [{corpus_id, tier_filter?, weight?}]
    void_rules: list[dict[str, Any]] = field(default_factory=list)
    shadow_config: dict[str, Any] | None = None  # {shadow_corpus_id, capture_to}
    created_by: str = ""
    active: bool = True


# ---------------------------------------------------------------------------
# Policy-aware retriever
# ---------------------------------------------------------------------------


class PolicyAwareRetriever:
    """Retriever that reads an active RAGPolicy on every call.

    Supports:
    - Multi-corpus merging (ordered, weighted)
    - Void rules (time-bounded subset exclusion)
    - Shadow/parallel path (A/B comparison, async capture)
    - Runtime policy swap via set_policy()
    """

    def __init__(
        self,
        policy: RAGPolicy,
        corpus_registry: dict[str, CorpusRetriever],
        shadow_callback: Callable[[str, list[dict]], None] | None = None,
        personal_retriever: CorpusRetriever | None = None,
    ) -> None:
        self._policy = policy
        self._registry = corpus_registry
        self._shadow_callback = shadow_callback
        self._personal_retriever = personal_retriever

    def set_policy(self, policy: RAGPolicy) -> None:
        """Swap the active policy. Takes effect on the next retrieve() call."""
        self._policy = policy

    @property
    def policy(self) -> RAGPolicy:
        return self._policy

    def retrieve(self, query: str, top_k: int = 5) -> list[dict]:
        """Run retrieval against the active policy's corpora.

        1. Query each corpus in the policy (skip missing ones)
        2. Filter out voided subsets (active void rules only)
        3. Merge results
        4. If shadow configured, run shadow path and capture
        5. Return primary results only (student never sees shadow)
        """
        policy = self._policy

        # 1+2+3: primary retrieval with void filtering
        primary_results = self._retrieve_primary(query, top_k, policy)

        # 3b: personal corpus (always included, never voided by course rules)
        if self._personal_retriever:
            try:
                personal_results = self._personal_retriever(query, top_k)
                for r in personal_results:
                    r.setdefault("corpus_type", "personal")
                primary_results.extend(personal_results)
            except Exception:
                log.debug("Personal retriever failed; swallowed.")

        # Tag course results that don't have corpus_type yet
        for r in primary_results:
            r.setdefault("corpus_type", "course")

        # 4: shadow path (never blocks primary; failures swallowed)
        if policy.shadow_config:
            self._run_shadow(query, top_k, policy)

        return primary_results

    # -- internals ----------------------------------------------------------

    def _retrieve_primary(self, query: str, top_k: int, policy: RAGPolicy) -> list[dict]:
        """Query configured corpora, apply void rules, merge."""
        all_results: list[dict] = []

        for corpus_ref in policy.corpora:
            corpus_id = corpus_ref.get("corpus_id", "")
            retriever = self._registry.get(corpus_id)
            if retriever is None:
                log.warning("Corpus '%s' not found in registry; skipping.", corpus_id)
                continue

            try:
                chunks = retriever(query, top_k)
            except Exception:
                log.exception("Error retrieving from corpus '%s'", corpus_id)
                continue

            # Apply active void rules for this corpus
            chunks = self._apply_void_rules(chunks, corpus_id, policy.void_rules)
            all_results.extend(chunks)

        return all_results[:top_k]

    def _apply_void_rules(
        self, chunks: list[dict], corpus_id: str, void_rules: list[dict]
    ) -> list[dict]:
        """Remove chunks matching active (non-expired) void rules."""
        now = datetime.now(UTC)
        active_rules = []

        for rule in void_rules:
            if rule.get("corpus_id") != corpus_id:
                continue
            starts = datetime.fromisoformat(rule.get("starts_at", ""))
            expires = datetime.fromisoformat(rule.get("expires_at", ""))
            if starts <= now <= expires:
                active_rules.append(rule)

        if not active_rules:
            return chunks

        filtered = []
        for chunk in chunks:
            voided = False
            for rule in active_rules:
                if self._chunk_matches_void(chunk, rule.get("subset_filter", "")):
                    voided = True
                    break
            if not voided:
                filtered.append(chunk)

        return filtered

    def _chunk_matches_void(self, chunk: dict, subset_filter: str) -> bool:
        """Check if a chunk matches a void rule's subset filter.

        Supported filter patterns:
        - "tag:X" — matches chunks whose 'tags' list contains X
        - "source:X" — matches chunks whose 'source' field == X
        - "*" — matches everything (full corpus void)
        """
        if subset_filter == "*":
            return True

        if subset_filter.startswith("tag:"):
            tag = subset_filter[4:]
            return tag in chunk.get("tags", [])

        if subset_filter.startswith("source:"):
            src = subset_filter[7:]
            return chunk.get("source") == src

        return False

    def _run_shadow(self, query: str, top_k: int, policy: RAGPolicy) -> None:
        """Run the shadow retrieval path. Failures are swallowed."""
        shadow_cfg = policy.shadow_config
        if not shadow_cfg:
            return

        shadow_corpus_id = shadow_cfg.get("shadow_corpus_id", "")
        retriever = self._registry.get(shadow_corpus_id)
        if retriever is None:
            log.warning("Shadow corpus '%s' not in registry.", shadow_corpus_id)
            return

        try:
            shadow_results = retriever(query, top_k)
        except Exception:
            log.debug("Shadow retrieval failed for '%s'; swallowed.", shadow_corpus_id)
            return

        # Capture shadow results via callback or log
        if self._shadow_callback:
            try:
                self._shadow_callback(query, shadow_results)
            except Exception:
                log.debug("Shadow callback failed; swallowed.")
