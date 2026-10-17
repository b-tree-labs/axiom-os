# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Search across all markdown documents in the repository.

Provides keyword-based search with TF-IDF-like ranking. Returns the most
relevant document chunks for a given query — enabling RAG without any
external dependencies.

The index is built lazily on first call and cached in memory.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from axiom import REPO_ROOT as _REPO_ROOT
from axiom.infra.orchestrator.actions import ActionCategory

from ..tools import ToolDef

TOOLS = [
    ToolDef(
        name="search_docs",
        description=(
            "Search all markdown documents in the repository for relevant content. "
            "Returns ranked excerpts from PRDs, specs, analysis docs, meeting notes, "
            "and other project documentation. Use this when the user asks about "
            "specific topics, requirements, decisions, or details that might be in "
            "our documentation."
        ),
        category=ActionCategory.READ,
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query — keywords or a natural language question.",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of chunks to return (default: 5, max: 10).",
                },
            },
            "required": ["query"],
        },
    ),
]

# ---------------------------------------------------------------------------
# Lightweight document index (no external deps)
# ---------------------------------------------------------------------------

CHUNK_SIZE = 800  # chars per chunk
CHUNK_OVERLAP = 200
_STOP_WORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "but",
        "by",
        "for",
        "from",
        "has",
        "have",
        "he",
        "her",
        "his",
        "how",
        "i",
        "in",
        "is",
        "it",
        "its",
        "me",
        "my",
        "no",
        "not",
        "of",
        "on",
        "or",
        "our",
        "she",
        "so",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "to",
        "up",
        "was",
        "we",
        "what",
        "when",
        "where",
        "which",
        "who",
        "will",
        "with",
        "you",
        "your",
    ]
)


@dataclass
class Chunk:
    path: str  # relative to repo root
    title: str  # document title (first heading)
    text: str
    start_line: int


@dataclass
class DocIndex:
    chunks: list[Chunk]
    idf: dict[str, float]  # term -> inverse doc frequency
    tf_matrix: list[Counter]  # chunk_idx -> term counts


_index: DocIndex | None = None


def _tokenize(text: str) -> list[str]:
    """Simple whitespace + punctuation tokenizer."""
    return [
        w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in _STOP_WORDS and len(w) > 1
    ]


def _extract_title(text: str) -> str:
    """Extract first markdown heading."""
    for line in text.splitlines()[:10]:
        line = line.strip()
        if line.startswith("#"):
            return line.lstrip("#").strip()
    return ""


def _build_index() -> DocIndex:
    """Scan all markdown files and build a searchable index."""
    chunks: list[Chunk] = []
    docs_dir = _REPO_ROOT / "docs"
    config_dir = _REPO_ROOT / "runtime" / "config"
    claude_md = _REPO_ROOT / "CLAUDE.md"

    # Collect all .md files to index
    md_files: list[Path] = []
    if docs_dir.exists():
        md_files.extend(docs_dir.rglob("*.md"))
    if config_dir.exists():
        md_files.extend(config_dir.glob("*.md"))
    if claude_md.exists():
        md_files.append(claude_md)

    for md_path in md_files:
        try:
            content = md_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue

        rel_path = str(md_path.relative_to(_REPO_ROOT))
        title = _extract_title(content)

        # Chunk the document
        pos = 0
        line_num = 1
        while pos < len(content):
            end = pos + CHUNK_SIZE
            chunk_text = content[pos:end]

            # Try to break at a paragraph boundary
            if end < len(content):
                last_break = chunk_text.rfind("\n\n")
                if last_break > CHUNK_SIZE // 3:
                    chunk_text = chunk_text[: last_break + 1]
                    end = pos + last_break + 1

            # Compute approximate line number
            line_num = content[:pos].count("\n") + 1

            chunks.append(
                Chunk(
                    path=rel_path,
                    title=title,
                    text=chunk_text.strip(),
                    start_line=line_num,
                )
            )

            pos = end - CHUNK_OVERLAP if end < len(content) else end

    # Build TF-IDF
    tf_matrix = [Counter(_tokenize(c.text)) for c in chunks]

    # Document frequency per term
    df: Counter = Counter()
    for tf in tf_matrix:
        for term in tf:
            df[term] += 1

    n = len(chunks) or 1
    idf = {term: math.log(n / (1 + count)) for term, count in df.items()}

    return DocIndex(chunks=chunks, idf=idf, tf_matrix=tf_matrix)


def _search(query: str, max_results: int = 5) -> list[dict[str, Any]]:
    """Search the index and return ranked results."""
    global _index
    if _index is None:
        _index = _build_index()

    query_terms = _tokenize(query)
    if not query_terms:
        return []

    # Score each chunk
    scores: list[tuple[float, int]] = []
    for idx, tf in enumerate(_index.tf_matrix):
        score = 0.0
        for term in query_terms:
            if term in tf:
                tf_val = tf[term] / (sum(tf.values()) or 1)
                idf_val = _index.idf.get(term, 0)
                score += tf_val * idf_val
            # Partial match bonus (prefix)
            else:
                for doc_term in tf:
                    if doc_term.startswith(term) or term.startswith(doc_term):
                        tf_val = tf[doc_term] / (sum(tf.values()) or 1)
                        idf_val = _index.idf.get(doc_term, 0)
                        score += tf_val * idf_val * 0.5
                        break

        if score > 0:
            scores.append((score, idx))

    scores.sort(reverse=True)

    # Deduplicate — don't return multiple chunks from same section
    seen_paths: dict[str, int] = {}
    results: list[dict[str, Any]] = []
    for score, idx in scores:
        chunk = _index.chunks[idx]
        path_key = f"{chunk.path}:{chunk.start_line // 50}"
        if path_key in seen_paths:
            continue
        seen_paths[path_key] = idx

        results.append(
            {
                "path": chunk.path,
                "title": chunk.title,
                "excerpt": chunk.text[:1200],
                "line": chunk.start_line,
                "score": round(score, 4),
            }
        )
        if len(results) >= max_results:
            break

    return results


def _search_pgvector(query: str, max_results: int = 5) -> list[dict[str, Any]] | None:
    """Search using pgvector if DATABASE_URL is configured.

    Returns ``None`` if pgvector is unavailable (caller should fall back).
    """
    import os

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        return None

    try:
        from axiom.rag.embeddings import embed_texts
        from axiom.rag.store import RAGStore
    except ImportError:
        return None

    # Try to get embeddings; fall back to full-text only. embed_texts raises
    # when a provider is configured but unreachable — at query time that should
    # degrade to keyword search, not error the tool.
    try:
        embeddings = embed_texts([query])
    except Exception:
        embeddings = None
    query_emb = embeddings[0] if embeddings else None

    try:
        store = RAGStore(database_url)
        store.connect()
        hits = store.search(
            query_embedding=query_emb,
            query_text=query,
            limit=max_results,
        )
        store.close()
    except Exception:
        return None

    return [
        {
            "path": h.source_path,
            "title": h.source_title,
            "excerpt": h.chunk_text[:1200],
            "line": 0,
            "score": round(h.combined_score, 4),
        }
        for h in hits
    ]


def execute(name: str, params: dict) -> dict:
    if name != "search_docs":
        return {"error": f"Unknown tool: {name}"}

    query = params.get("query", "").strip()
    if not query:
        return {"error": "query parameter is required"}

    max_results = min(params.get("max_results", 5), 10)

    # Try pgvector first, fall back to TF-IDF
    results = _search_pgvector(query, max_results)
    source = "pgvector"
    if results is None:
        results = _search(query, max_results)
        source = "tfidf"

    return {
        "query": query,
        "source": source,
        "total_indexed_chunks": len(_index.chunks) if _index else 0,
        "results": results,
    }
