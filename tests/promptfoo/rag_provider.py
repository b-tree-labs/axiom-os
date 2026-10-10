# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""RAG context provider for promptfoo evaluations.

Injects retrieved document chunks into promptfoo test variables so that
chat-agent tests can be grounded in the actual RAG store.

Usage in promptfoo YAML:
    providers:
      - id: python:tests/promptfoo/rag_provider.py
        config:
          database_url: "postgresql://localhost:5432/axiom"
          tier: institutional
          limit: 3

The provider is called by promptfoo with (prompt, options, context).
It returns the prompt with a RAG_CONTEXT variable injected, which the
system prompt template can reference as {{RAG_CONTEXT}}.

Standalone usage (for debugging retrieval):
    python tests/promptfoo/rag_provider.py "example query"
"""

from __future__ import annotations

import os
import sys
from typing import Any

# ---------------------------------------------------------------------------
# RAG retrieval helper
# ---------------------------------------------------------------------------


def _retrieve(query: str, tier: str = "institutional", limit: int = 5) -> str:
    """Return retrieved context as a formatted string, or empty string on error."""
    try:
        # Allow running outside the installed package (e.g. from repo root)
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

        from axiom.rag.embeddings import embed_texts
        from axiom.rag.store import RAGStore

        db_url = os.environ.get(
            "AXIOM_DATABASE_URL",
            "postgresql://localhost:5432/axiom",
        )

        store = RAGStore(db_url)
        store.connect()

        # Try vector search first; fall back to text-only if no embeddings
        query_embedding = None
        embedding_result = embed_texts([query])
        if embedding_result:
            query_embedding = embedding_result[0]

        from axiom.rag.store import ALL_CORPORA, CORPUS_COMMUNITY, CORPUS_INTERNAL, CORPUS_ORG

        # Map legacy tier arg to corpora list for backwards compat
        corpora_map = {
            "community": [CORPUS_COMMUNITY],
            "org": [CORPUS_ORG],
            "internal": [CORPUS_INTERNAL],
            "institutional": ALL_CORPORA,  # legacy alias — search all
            "all": ALL_CORPORA,
        }
        corpora = corpora_map.get(tier, ALL_CORPORA)

        results = store.search(
            query_embedding=query_embedding,
            query_text=query,
            corpora=corpora,
            limit=limit,
        )
        store.close()

        if not results:
            return "(no relevant documents found in RAG store)"

        parts = []
        for i, r in enumerate(results, 1):
            parts.append(
                f"[{i}] {r.source_title or r.source_path} (score: {r.combined_score:.3f})\n"
                f"{r.chunk_text.strip()}"
            )
        return "\n\n---\n\n".join(parts)

    except Exception as exc:  # noqa: BLE001
        return f"(RAG retrieval unavailable: {exc})"


# ---------------------------------------------------------------------------
# promptfoo provider interface
# ---------------------------------------------------------------------------


def call_api(prompt: str, options: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """promptfoo provider entry point.

    Retrieves RAG context for the prompt and returns it as an output.
    The caller's system prompt should include {{RAG_CONTEXT}} which will
    be populated from the test ``vars`` block.

    This provider is used as a *transform* — it enriches vars so the
    downstream LLM provider can use ``{{RAG_CONTEXT}}`` in its prompt.
    """
    cfg = options.get("config", {})
    tier = cfg.get("tier", "institutional")
    limit = int(cfg.get("limit", 5))

    # Extract the user query from vars or fall back to the raw prompt
    query = context.get("vars", {}).get("query", prompt)

    rag_context = _retrieve(query, tier=tier, limit=limit)

    # Return as a dict so promptfoo can inject it into vars
    return {
        "output": rag_context,
        # Also expose as metadata for debugging in the promptfoo UI
        "metadata": {
            "rag_tier": tier,
            "rag_limit": limit,
            "query": query,
        },
    }


# ---------------------------------------------------------------------------
# CLI debug entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    query = " ".join(sys.argv[1:]) or "example query"
    print(f"Query: {query}\n")
    print(_retrieve(query))
