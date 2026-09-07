# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""MemoryServingService — the one door out (ADR-087 D7 / PRD F4).

Retrieval reuses the existing hybrid retriever via
``CompositionService.recall()``; the serving gate then runs on every result
*after* retrieval and *before* serialization. All three transports (MCP tool,
plain-text block, query endpoint) call :meth:`serve`, so the gate is never
bypassed — one door out, symmetric with one door in.

Coexistence with a user's own RAG is first-class (D7): side-by-side blocks are
the default; opt-in rank-level RRF fusion treats cross-mem as one policy-gated
retriever. Both fuse, never ingest — :meth:`assert_no_push` makes the no-push
rule structural.

Serialization is byte-stable and carries no timestamps (D6 rendering
discipline), so a re-serve of unchanged state is identical (prompt-cache safe).
A cooperative transcript-exclusion marker rides injected blocks (security doc
§3 — helpful, never load-bearing).
"""

from __future__ import annotations

from dataclasses import dataclass

from axiom.memory.serving import (
    ConsumerCoordinate,
    Denial,
    ServableItem,
    ServingGate,
    refuse_push,
)
from axiom.rag.rrf import reciprocal_rank_fusion

# Cooperative marker (security doc §3): a hint that a user's own RAG /
# auto-memory should not re-index this injected block. Hygiene, never a control.
EXCLUSION_MARKER = "<!-- axiom:exclude-from-memory — served memory, do not re-index -->"

_BLOCK_HEADER = "=== YOUR MEMORY (cross-mem) ==="


@dataclass(frozen=True)
class ServedResult:
    """What :meth:`MemoryServingService.serve` returns: gated items + denials."""

    items: list[ServableItem]
    denials: list[Denial]
    degraded: bool
    query: str


@dataclass
class MemoryServingService:
    """recall() → gate → serialize, shared by every serving transport."""

    composition: object  # CompositionService (duck-typed to avoid an import cycle)
    gate: ServingGate

    def serve(
        self,
        query: str,
        *,
        consumer: ConsumerCoordinate,
        recall_user: str | None = None,
        recall_agent: str = "axi",
        k: int = 5,
        intent: str = "lookup",
        cognitive_types: list[str] | None = None,
        since: str | None = None,
        until: str | None = None,
        recency_bias: float | None = None,
    ) -> ServedResult:
        """Retrieve, then gate, then hand back a serializable result.

        ``recall_user`` (whose corpus to search + whose access checks apply)
        defaults to the consumer's principal — the self-serve common case.
        """
        user = recall_user or consumer.principal
        recall = self.composition.recall(
            query,
            user=user,
            agent=recall_agent,
            principal=user,
            intent=intent,
            k=k,
            cognitive_types=cognitive_types,
            since=since,
            until=until,
            recency_bias=recency_bias,
        )
        items = [ServableItem.from_fragment(f) for f in recall.fragments]
        allowed, denials = self.gate.filter(items, consumer)

        # Audit the serving decision (served-fragment logging with provenance
        # labels — security doc §6 reviewer checklist item 6).
        self.composition.audit_log.record(
            entry_type="serve",
            principal_id=consumer.principal,
            agent_id=recall_agent,
            fragment_id="",
            outcome="ok",
            query=query[:200],
            served=len(allowed),
            denied=len(denials),
            harness=consumer.harness,
            deployment_tier=consumer.deployment_tier,
        )
        return ServedResult(
            items=allowed, denials=denials, degraded=recall.degraded, query=query,
        )

    # ---- serialization (byte-stable, untimestamped) -----------------------

    def to_plaintext_block(self, result: ServedResult) -> str:
        """Render a plain-text block for prompt templates (F4 transport).

        Canonical ordering by fragment id, no timestamps, so a re-serve of
        unchanged state is byte-identical.
        """
        if not result.items:
            return ""
        lines = [EXCLUSION_MARKER, _BLOCK_HEADER, ""]
        for item in sorted(result.items, key=lambda i: i.fragment_id):
            lines.append(f"- {item.text.strip()}")
        lines.append("")
        return "\n".join(lines)

    def to_mcp_payload(self, result: ServedResult) -> dict:
        """Render an MCP-tool JSON payload (F4 transport)."""
        return {
            "served": len(result.items),
            "denied": len(result.denials),
            "degraded": result.degraded,
            "cooperative_exclusion": EXCLUSION_MARKER,
            "fragments": [
                {
                    "id": i.fragment_id,
                    "cognitive_type": i.cognitive_type,
                    "text": i.text,
                    "visibility": i.visibility,
                }
                for i in sorted(result.items, key=lambda i: i.fragment_id)
            ],
            "denials": [
                {"id": d.fragment_id, "reason": d.reason.value} for d in result.denials
            ],
        }

    # ---- coexistence: fuse, never ingest (D7) -----------------------------

    def fuse_side_by_side(self, result: ServedResult, foreign_block: str) -> str:
        """Default coexistence: labeled cross-mem block beside the user's RAG.

        The foreign block is passed through verbatim — never parsed, never
        ingested. Cross-mem stays a separate, attributable block.
        """
        mem = self.to_plaintext_block(result)
        parts = [p for p in (mem, foreign_block) if p]
        return "\n\n".join(parts)

    def fuse_rrf(
        self,
        cross_mem_ranking: list[str],
        foreign_ranking: list[str],
        *,
        k: int = 60,
        limit: int | None = None,
    ) -> list[str]:
        """Opt-in rank-level RRF: cross-mem as one retriever among the user's.

        Fuses two *rankings* (doc-id order), never the corpora. Nothing is
        written to any store — this is FUSE, never INGEST.
        """
        fused = reciprocal_rank_fusion(
            [cross_mem_ranking, foreign_ranking], k=k, limit=limit,
        )
        return [str(f.doc_id) for f in fused]

    @staticmethod
    def assert_no_push(foreign_store: object) -> None:
        """Structural no-push guard — always refuses (D7 corollary)."""
        refuse_push(type(foreign_store).__name__)


__all__ = [
    "EXCLUSION_MARKER",
    "MemoryServingService",
    "ServedResult",
]
