# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``embed_bronze_record`` — bronze → RAG embed adapter.

Reads bytes from the bronze content blob (the byte-level substrate of
record), chunks, embeds, and upserts. Citation uses the originating
``FetchedItem.source_path`` (human-readable), not the content-addressed
blob path.

The provenance gate already ran in bronze, so this adapter never
re-gates. It honors disposition: only ``ALLOW`` records reach the
embed/upsert path; ``QUARANTINE`` items wait for human review;
``EXCLUDE`` items have no content to embed.

The embed-failure handling matches the lesson from
``axiom.rag.ingest`` (#7): when a *configured* embedding provider
raises, we do NOT upsert (a partial upsert would record a checksum and
cause future runs to skip as "unchanged" forever). The adapter returns
``indexed=False`` so a retry re-attempts the embed.
"""

from __future__ import annotations

import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

# Re-exported under this module's namespace so tests can monkey-patch
# ``rag_embed.embedder.embed_texts`` and the adapter sees the stub.
from axiom.rag.chunker import chunk_markdown
from axiom.rag.embeddings import (  # noqa: F401 (embed_texts re-exported)
    PersistentEmbeddingError,
    embed_texts,
)
from axiom.rag.extract import SUPPORTED_EXTENSIONS, extract_text
from axiom.rag.ingest_router import Disposition

from ..bronze import BronzeWriteResult
from ..contracts import FetchedItem

log = logging.getLogger(__name__)


_TEXT_CONTENT_TYPES = {"text/markdown", "text/plain", "text/x-markdown", "text/x-rst"}


class _StoreLike(Protocol):
    def upsert_chunks(self, chunks: list[Any], embeddings: list[list[float]] | None = ..., **kwargs: Any) -> None: ...


@dataclass(frozen=True)
class EmbedStats:
    """Outcome of one embed call."""

    indexed: bool
    chunks_created: int = 0
    skipped_reason: str | None = None
    """``quarantine`` / ``exclude`` / ``empty_content`` / ``unsupported`` /
    ``embed_failed`` / ``no_chunks``; ``None`` on success."""


def embed_bronze_record(
    record: BronzeWriteResult,
    item: FetchedItem,
    store: _StoreLike,
    *,
    chunking_tier: str = "fixed",
) -> EmbedStats:
    """Embed one bronze record into ``store`` under ``record.tier``."""

    if record.disposition is Disposition.EXCLUDE:
        return EmbedStats(indexed=False, skipped_reason="exclude")
    if record.disposition is Disposition.QUARANTINE:
        return EmbedStats(indexed=False, skipped_reason="quarantine")

    if record.content_path is None:
        # Defensive: ALLOW with no content path is a bug, not data.
        return EmbedStats(indexed=False, skipped_reason="empty_content")

    text = _read_text(record.content_path, item)
    if text:
        # Postgres text columns can't contain NUL (0x00); some
        # extracted PDFs leak embedded nulls. Strip before chunking.
        text = text.replace("\x00", " ")
    if not text:
        return EmbedStats(indexed=False, skipped_reason="unsupported")

    rel_path = item.source_path or f"{item.source_name}/{item.item_id}"
    chunks = chunk_markdown(text, rel_path)
    if not chunks:
        return EmbedStats(indexed=False, skipped_reason="no_chunks")

    # Embed — text-only is OK (returns None); raises means upstream failure.
    # Resolve at call time so monkeypatched stubs in tests are honored.
    from . import embedder as _self

    try:
        embeddings = _self.embed_texts([c.text for c in chunks])
    except PersistentEmbeddingError as exc:
        # The embedder REJECTED the input (4xx) — retrying is futile. Quarantine
        # with the real reason rather than fail-and-retry-forever.
        log.warning("Embedding REJECTED for %s (quarantined, not retried): %s",
                    rel_path, exc)
        return EmbedStats(indexed=False, skipped_reason=f"embed_rejected: {exc}")
    except Exception as exc:
        # Transient (unreachable / timeout / 5xx) — leave unindexed to retry.
        log.warning("Embedding failed for %s — not indexed, will retry: %s", rel_path, exc)
        return EmbedStats(indexed=False, skipped_reason="embed_failed")

    store.upsert_chunks(
        chunks,
        embeddings,
        checksum=record.content_hash or "",
        content_hash=record.content_hash or "",
        corpus=record.tier or "rag-community",
        data_source=item.source_name,
        chunking_tier=chunking_tier,
        # ADR-091: carry the origin's shareable link + stable id through to
        # `documents` so citations are navigable. item_id IS the origin id
        # (Box file id); source_url is None for URL-less kinds.
        source_url=item.source_url,
        source_ref_id=item.item_id,
    )
    return EmbedStats(indexed=True, chunks_created=len(chunks))


def _read_text(content_path: Path, item: FetchedItem) -> str | None:
    """Return the document text, decoding bytes for text/* and using
    extract_text for binary formats.
    """
    if item.content_type in _TEXT_CONTENT_TYPES or _looks_like_text(item.display_name):
        try:
            return content_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return None

    suffix = Path(item.display_name).suffix.lower()
    if suffix and suffix not in SUPPORTED_EXTENSIONS:
        return None

    # extract_text takes a Path; bronze blobs ARE real files, but their
    # filename is the sha256, not the original — extractors that sniff
    # by suffix would mis-route. Symlink/copy to a temp file with the
    # original extension so extraction routes correctly.
    with tempfile.TemporaryDirectory(prefix="dp1-extract-") as td:
        named = Path(td) / item.display_name
        named.write_bytes(content_path.read_bytes())
        return extract_text(named)


def _looks_like_text(name: str) -> bool:
    return Path(name).suffix.lower() in {".md", ".txt", ".rst"}


__all__ = ["EmbedStats", "embed_bronze_record"]
