# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Confidence-gated RAG context injection.

CURIO Research Task 1: Learn when RAG context helps vs hurts.

Problem: RAG context can *reduce* accuracy when the LLM already knows
the answer and the retrieved context is related-but-not-exact (context
distraction). The confidence gate decides per-query whether to inject
context based on retrieval similarity scores.

Method:
1. For each query, retrieve RAG context and note top_similarity
2. Run query BOTH with and without context
3. LLM-judge scores both answers
4. Record (similarity, rag_helped) outcome
5. Periodically recalibrate threshold to maximize accuracy

Usage::

    gate = ConfidenceGate.load()  # Loads learned threshold
    results = store.search(query_text=query)
    top_sim = results[0].combined_score if results else 0.0

    if gate.should_inject(top_sim):
        # Inject RAG context into system prompt
        ...
    else:
        # Let LLM answer from parametric knowledge
        ...
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

_DEFAULT_THRESHOLD = 0.45
_MIN_SAMPLES_FOR_RECALIBRATE = 20
_STATE_DIR = Path.home() / ".axi" / "curio"


@dataclass
class GateOutcome:
    """A single observation of whether RAG helped."""

    similarity: float
    rag_helped: bool


class ConfidenceGate:
    """Decides per-query whether to inject RAG context.

    Learns a similarity threshold from observed outcomes.
    Below threshold → skip RAG, let LLM use parametric knowledge.
    Above threshold → inject RAG context.
    """

    def __init__(self, threshold: float = _DEFAULT_THRESHOLD) -> None:
        self.threshold = threshold
        self.outcomes: list[GateOutcome] = []

    def should_inject(self, top_similarity: float) -> bool:
        """Should we inject RAG context for this query?"""
        if top_similarity <= 0.0:
            return False
        return top_similarity >= self.threshold

    def record_outcome(self, similarity: float, rag_helped: bool) -> None:
        """Record whether RAG helped for a query at this similarity level."""
        self.outcomes.append(GateOutcome(similarity=similarity, rag_helped=rag_helped))

    def recalibrate(self) -> None:
        """Recompute threshold from recorded outcomes.

        Finds the similarity value that best separates "RAG helped"
        from "RAG hurt" using a simple scan over candidate thresholds.
        """
        if len(self.outcomes) < _MIN_SAMPLES_FOR_RECALIBRATE:
            return

        # Sort outcomes by similarity
        sorted_outcomes = sorted(self.outcomes, key=lambda o: o.similarity)
        sims = [o.similarity for o in sorted_outcomes]

        # Try each unique similarity as a candidate threshold
        best_threshold = self.threshold
        best_score = -1.0

        candidates = sorted(set(sims))
        for candidate in candidates:
            # Below threshold: RAG skipped → count how many times RAG would have hurt
            # Above threshold: RAG injected → count how many times RAG helped
            correct = 0
            total = len(self.outcomes)
            for o in self.outcomes:
                if o.similarity < candidate:
                    # We'd skip RAG — correct if RAG would have hurt
                    if not o.rag_helped:
                        correct += 1
                else:
                    # We'd inject RAG — correct if RAG helped
                    if o.rag_helped:
                        correct += 1

            accuracy = correct / total if total > 0 else 0
            if accuracy > best_score:
                best_score = accuracy
                best_threshold = candidate

        old = self.threshold
        self.threshold = best_threshold
        if old != best_threshold:
            log.info(
                "Confidence gate recalibrated: %.3f → %.3f (accuracy=%.0f%%, n=%d)",
                old,
                best_threshold,
                best_score * 100,
                len(self.outcomes),
            )

    def save(self, state_dir: Path | None = None) -> None:
        """Persist gate state to disk."""
        d = state_dir or _STATE_DIR
        d.mkdir(parents=True, exist_ok=True)
        state = {
            "threshold": self.threshold,
            "outcomes": [
                {"similarity": o.similarity, "rag_helped": o.rag_helped}
                for o in self.outcomes[-1000:]  # Keep last 1000
            ],
        }
        (d / "confidence_gate.json").write_text(json.dumps(state, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, state_dir: Path | None = None) -> ConfidenceGate:
        """Load gate state from disk, or create with defaults."""
        d = state_dir or _STATE_DIR
        path = d / "confidence_gate.json"
        if not path.exists():
            return cls()
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
            gate = cls(threshold=state.get("threshold", _DEFAULT_THRESHOLD))
            gate.outcomes = [
                GateOutcome(similarity=o["similarity"], rag_helped=o["rag_helped"])
                for o in state.get("outcomes", [])
            ]
            return gate
        except Exception:
            return cls()

    def stats(self) -> dict:
        """Return gate statistics."""
        n = len(self.outcomes)
        helped = sum(1 for o in self.outcomes if o.rag_helped)
        return {
            "threshold": self.threshold,
            "total_observations": n,
            "rag_helped_pct": helped / n if n > 0 else 0,
            "rag_hurt_pct": (n - helped) / n if n > 0 else 0,
        }


def run_ab_for_query(
    query: str,
    store,
    llm_fn,
    judge_fn,
    gold: str = "",
) -> GateOutcome:
    """Run a single query with and without RAG, judge which is better.

    Args:
        query: The user query
        store: RAGStore for retrieval
        llm_fn: Function(question, system_prompt) → answer string
        judge_fn: Function(question, answer, gold) → accuracy float (0-1)
        gold: Gold standard answer (for judging)

    Returns:
        GateOutcome with similarity and whether RAG helped
    """
    # Retrieve context
    results = store.search(query_text=query, limit=4)
    top_sim = results[0].combined_score if results else 0.0

    rag_ctx = ""
    if results:
        rag_ctx = "\n\n".join(f"[{r.source_path}]\n{r.chunk_text[:300]}" for r in results)

    # Answer WITHOUT RAG
    sys_plain = "You are a nuclear engineering assistant. Answer concisely."
    answer_plain = llm_fn(query, sys_plain)

    # Answer WITH RAG
    sys_rag = sys_plain
    if rag_ctx:
        sys_rag += f"\n\nReference material:\n{rag_ctx}"
    answer_rag = llm_fn(query, sys_rag)

    # Judge both
    score_plain = judge_fn(query, answer_plain, gold)
    score_rag = judge_fn(query, answer_rag, gold)

    rag_helped = score_rag >= score_plain

    return GateOutcome(similarity=top_sim, rag_helped=rag_helped)
