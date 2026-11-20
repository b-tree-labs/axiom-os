# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The public retrieval sample: invented documents and a few questions over them.

It exists for two jobs and decides nothing: it shows the question-set format, and it gives
the tests one fixed store that every retrieval surface can be run against (the parity suite,
and ``axi rag eval`` in CI). A site's real question set is built from its own traces over
its own documents and stays on the site's node; it is run with
``axi rag eval --questions <path>``.

:func:`load_corpus` writes :data:`CORPUS` into a store. Point it only at a scratch store:
it writes the ``rag-org`` corpus.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
CORPUS = HERE / "corpus.yaml"
QUESTIONS = HERE / "questions.yaml"


def documents(path: Path = CORPUS) -> list[dict[str, Any]]:
    """The documents of the sample, each chunk normalised to ``{"text", "page"}``."""
    import yaml

    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or []
    out = []
    for doc in raw:
        chunks = [c if isinstance(c, dict) else {"text": c} for c in doc["chunks"]]
        out.append({**doc, "chunks": [{"text": str(c["text"]), "page": c.get("page")} for c in chunks]})
    return out


def load_corpus(
    store: Any,
    *,
    embed: Callable[[list[str]], list[list[float]] | None] | None = None,
    embedding_model: str | None = None,
    path: Path = CORPUS,
) -> int:
    """Write the sample's documents into ``store``, with ``embed``'s vectors when given.

    Returns the number of chunks written. Each document keeps the access labels the sample
    declares, so the access filter has something to refuse.
    """
    from axiom.rag.chunker import Chunk

    written = 0
    for doc in documents(path):
        chunks = [
            Chunk(text=c["text"], source_path=doc["path"], source_title=doc["title"],
                  chunk_index=i, start_line=0, source_type="pdf" if doc["path"].endswith(".pdf") else "markdown")
            for i, c in enumerate(doc["chunks"])
        ]
        vectors = embed([c.text for c in chunks]) if embed is not None else None
        store.upsert_chunks(
            chunks,
            vectors,
            corpus=doc.get("corpus", "rag-org"),
            access_tier=doc.get("access_tier", "public"),
            classification=doc.get("classification", "unclassified"),
            **({"embedding_model": embedding_model} if vectors and embedding_model else {}),
        )
        written += len(chunks)
    return written


__all__ = ["CORPUS", "QUESTIONS", "documents", "load_corpus"]
