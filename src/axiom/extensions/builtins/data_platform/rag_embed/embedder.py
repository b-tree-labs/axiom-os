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

Extraction goes through the extractor registry (ADR-213), the same one
uploads use, so a contributed extractor runs on connector documents too
(#1946); the extractor names and versions are recorded on the document as
``documents.parser``. Text records are read directly, so the Library's derived
``rag.md`` is not extracted a second time.

Interim shortcut. This reads bronze straight into the RAG and skips conform,
silver and gold. The designed route is bronze -> conform -> silver -> gold ->
RAG (ADR-079): the RAG is a projection of gold. Don't build new parsing,
access labelling or curation on this path; parsing belongs in conform and
labels and deduplication in gold curation.
"""

from __future__ import annotations

import logging
import mimetypes
import os
import shutil
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
    embedded_with,
)
from axiom.rag.extract import (  # noqa: F401 (extract_text re-exported)
    SUPPORTED_EXTENSIONS,
    extract_text,
)
from axiom.rag.ingest_router import Disposition

from ..bronze import BronzeWriteResult
from ..contracts import FetchedItem

log = logging.getLogger(__name__)


_TEXT_CONTENT_TYPES = {"text/markdown", "text/plain", "text/x-markdown", "text/x-rst"}


class _StoreLike(Protocol):
    def upsert_chunks(
        self, chunks: list[Any], embeddings: list[list[float]] | None = ..., **kwargs: Any
    ) -> None: ...


@dataclass(frozen=True)
class EmbedStats:
    """Outcome of one embed call."""

    indexed: bool
    chunks_created: int = 0
    chunks_refused: int = 0
    """Chunks the ingestion guard stopped. Reported, never silent: the corpus
    this guard exists for went unnoticed because ingest logged only additions."""
    skipped_reason: str | None = None
    """``quarantine`` / ``exclude`` / ``empty_content`` / ``unsupported`` /
    ``embed_failed`` / ``no_chunks`` / ``placeholder: <kind>``; ``None`` on
    success."""


#: Env override for the per-document chunk ceiling. A document is prose; 2,000
#: chunks of it is a data file. A production node had one telemetry document at
#: 15,804 chunks, half a percent of a three-million-chunk index on its own.
#: ``0`` disables the ceiling.
MAX_CHUNKS_ENV = "AXIOM_RAG_MAX_CHUNKS_PER_DOC"
DEFAULT_MAX_CHUNKS_PER_DOC = 2000


def max_chunks_per_document() -> int:
    """The ceiling, or 0 when disabled. A bad value is ignored, not fatal:
    ingest must not stop because an env var has a typo in it."""
    raw = os.environ.get(MAX_CHUNKS_ENV, "").strip()
    if not raw:
        return DEFAULT_MAX_CHUNKS_PER_DOC
    try:
        return max(0, int(raw))
    except ValueError:
        log.warning(
            "%s=%r is not an integer; using %d", MAX_CHUNKS_ENV, raw, DEFAULT_MAX_CHUNKS_PER_DOC
        )
        return DEFAULT_MAX_CHUNKS_PER_DOC


def embed_bronze_record(
    record: BronzeWriteResult,
    item: FetchedItem,
    store: _StoreLike,
    *,
    chunking_tier: str = "fixed",
) -> EmbedStats:
    """Embed one bronze record into ``store`` under ``record.tier``."""

    if record.placeholder:
        # Recorded at landing (#1948): a stand-in, not the file it names.
        return EmbedStats(indexed=False, skipped_reason=record.placeholder)
    if record.disposition is Disposition.EXCLUDE:
        return EmbedStats(indexed=False, skipped_reason="exclude")
    if record.disposition is Disposition.QUARANTINE:
        return EmbedStats(indexed=False, skipped_reason="quarantine")

    if record.content_path is None:
        # Defensive: ALLOW with no content path is a bug, not data.
        return EmbedStats(indexed=False, skipped_reason="empty_content")

    # Defensive: a record landed before the gate recognised placeholders (and
    # replayed by a reindex) arrives ALLOW and unmarked. Sniff the blob's head.
    placeholder = _sniff_placeholder(record.content_path, item)
    if placeholder:
        return EmbedStats(indexed=False, skipped_reason=placeholder)

    text, parser = extract_document(record.content_path, item, sha256=record.content_hash)
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
    # The ingestion guard, on the path that actually ingests. `axiom.rag.ingest`
    # has screened chunks since the 5.2-million-chunk telemetry incident; THIS
    # path — the connector pull every node runs — never called it, so nothing
    # screened the corpora that matter. Found 2026-09-26 on a production node,
    # where one telemetry document had become 15,804 chunks and vendored
    # LICENSE.txt files were indexed as documents. Before embedding, so a
    # refused chunk costs no embedding call. Off with AXIOM_RAG_INGEST_GUARD=0.
    from axiom.rag.ingest_suitability import screen_chunks

    chunks, screen = screen_chunks(chunks)
    if screen.refused:
        log.info("Ingestion guard on %s: %s", rel_path, screen.summary())
    if not chunks:
        return EmbedStats(
            indexed=False, skipped_reason="all_chunks_refused", chunks_refused=screen.refused
        )
    ceiling = max_chunks_per_document()
    if ceiling and len(chunks) > ceiling:
        # A document that survives per-chunk screening and still yields thousands
        # of chunks is structured data wearing prose's clothes. Refusing it whole
        # is right: half a data file retrieves no better than all of it, and
        # truncating would silently answer from an arbitrary prefix.
        log.warning(
            "Ingestion guard on %s: %d chunks exceeds the per-document ceiling of %d; "
            "not indexed. This is structured data, not a document — route it to its "
            "own tables, or raise AXIOM_RAG_MAX_CHUNKS_PER_DOC deliberately.",
            rel_path,
            len(chunks),
            ceiling,
        )
        return EmbedStats(
            indexed=False,
            skipped_reason=f"over_chunk_ceiling: {len(chunks)} > {ceiling}",
            chunks_refused=len(chunks),
        )

    # Embed — text-only is OK (returns None); raises means upstream failure.
    # Resolve at call time so monkeypatched stubs in tests are honored.
    from . import embedder as _self

    # A record the connector labelled export-controlled is embedded only where that
    # label may go (RS-E2): this machine, or an embedder the providers file clears.
    tier_kw = (
        {"routing_tier": "export_controlled"}
        if _labels(record).get("access_tier") == "export_controlled"
        else {}
    )
    try:
        embeddings = _self.embed_texts([c.text for c in chunks], **tier_kw)
    except PersistentEmbeddingError as exc:
        # The embedder REJECTED the input (4xx) — retrying is futile. Quarantine
        # with the real reason rather than fail-and-retry-forever.
        log.warning("Embedding REJECTED for %s (quarantined, not retried): %s", rel_path, exc)
        return EmbedStats(indexed=False, skipped_reason=f"embed_rejected: {exc}")
    except Exception as exc:
        # Transient (unreachable / timeout / 5xx) — leave unindexed to retry.
        log.warning("Embedding failed for %s — not indexed, will retry: %s", rel_path, exc)
        return EmbedStats(indexed=False, skipped_reason="embed_failed")

    from axiom.rag.store import EmbeddingModelMismatch

    try:
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
            # The labels the connector declared, carried on the bronze record (#1971).
            # None declared: the store keeps what the document already has.
            **_labels(record),
            # Which extractor(s) produced the text, with versions (#1946). None for a
            # text record read directly; the store keeps an earlier record then.
            parser=parser,
            # The corpus generation declares the model once and refuses another (P0.2).
            embedding_model=embedded_with(embeddings),
        )
    except EmbeddingModelMismatch as exc:
        # The corpus generation declared another model (P0.2). Retrying cannot help until the
        # embedder is the declared one again, or the corpus is rebuilt as a new generation.
        log.warning("Not indexed %s: %s", rel_path, exc)
        return EmbedStats(indexed=False, skipped_reason=f"embedding_model_mismatch: {exc}")
    return EmbedStats(indexed=True, chunks_created=len(chunks))


def _labels(record: BronzeWriteResult) -> dict[str, Any]:
    labels = getattr(record, "access_labels", None) or {}
    return {k: labels[k] for k in ("access_tier", "classification") if labels.get(k)}


def _sniff_placeholder(content_path: Path, item: FetchedItem) -> str | None:
    from ..bronze.placeholder import HEAD_BYTES, detect_placeholder

    try:
        with content_path.open("rb") as fh:
            head = fh.read(HEAD_BYTES)
        size = content_path.stat().st_size
    except OSError:
        return None
    return detect_placeholder(head, item.display_name or item.source_path, size=size)


#: The ``engine`` recorded when no registry extractor yielded text and the
#: built-in ``axiom.rag.extract.extract_text`` did.
FALLBACK_ENGINE = "axiom.rag.extract"
FALLBACK_VERSION = "builtin"


def _is_text_record(item: FetchedItem) -> bool:
    """Already text: read directly, never through the registry. This is how the
    Library's derived ``rag.md`` (``text/markdown``) arrives, so the registry
    does not run twice on a Library item."""
    return item.content_type in _TEXT_CONTENT_TYPES or _looks_like_text(item.display_name)


def extract_document(
    content_path: Path, item: FetchedItem, *, sha256: str | None = None
) -> tuple[str | None, dict[str, Any] | None]:
    """The document's text and the record of what extracted it.

    Text records are read directly (record ``None``). Anything else goes
    through the extractor registry (ADR-213), the same one uploads use: the
    planned extractors run, and the text sections of those whose result is
    ``rag``-tagged are joined. When none yields text, today's
    ``extract_text`` runs, so nothing that extracted before stops extracting.

    The record is ``{"engine", "version", "extractors": [{"name", "version"}],
    "planned": [{"name", "version"}]}``: ``engine``/``version`` name the first
    contributing extractor, ``extractors`` every one that contributed text,
    and ``planned`` every extractor the registry ran on the file.

    Interim shortcut: when ADR-079's bronze -> conform route lands, this
    registry call moves into the document conform step.
    """
    if _is_text_record(item):
        try:
            return content_path.read_text(encoding="utf-8"), None
        except UnicodeDecodeError:
            return None, None

    text, contributed, planned = _extract_via_registry(content_path, item, sha256=sha256)
    if text:
        first = contributed[0]
        return text, {
            "engine": first["name"],
            "version": first["version"],
            "extractors": contributed,
            "planned": planned,
        }
    fallback = _extract_fallback(content_path, item)
    if not fallback:
        return None, None
    return fallback, {
        "engine": FALLBACK_ENGINE,
        "version": FALLBACK_VERSION,
        "extractors": [],
        "planned": planned,
    }


def _context(content_path: Path, item: FetchedItem, sha256: str | None) -> Any:
    from axiom.extensions.builtins.library.extract.contract import ExtractionContext

    # The blob is named by its sha256; extractors match on the original name.
    mime = (
        item.content_type
        or mimetypes.guess_type(item.display_name)[0]
        or "application/octet-stream"
    )
    if sha256:
        return ExtractionContext(
            path=content_path,
            file_name=item.display_name,
            size=content_path.stat().st_size,
            sha256=sha256,
            mime=mime,
            item_id=item.item_id,
        )
    return ExtractionContext.for_path(
        content_path, file_name=item.display_name, mime=mime, item_id=item.item_id
    )


def planned_text_extractors(ctx: Any) -> list[Any]:
    """The registry's plan for ``ctx``, kept to the extractors that run and can produce text."""
    from axiom.extensions.builtins.library.extract.registry import plan

    return [p for p in plan(ctx) if not p.skip and "text" in p.entry.manifest.outputs]


def extraction_plan(
    content_path: Path, item: FetchedItem, *, sha256: str | None = None
) -> list[dict[str, str]] | None:
    """The text extractors the registry would run on this record now, as
    ``[{"name", "version"}]``; ``None`` for a text record, which never goes
    through the registry. Plans only: nothing is extracted."""
    if _is_text_record(item):
        return None
    ctx = _context(content_path, item, sha256)
    return [
        {"name": p.entry.manifest.name, "version": p.entry.manifest.version}
        for p in planned_text_extractors(ctx)
    ]


def _extractor_set(entries: Any) -> frozenset[tuple[str, str]]:
    return frozenset(
        (str(e.get("name", "")), str(e.get("version", "")))
        for e in entries or ()
        if isinstance(e, dict)
    )


def parser_is_current(stored: dict[str, Any] | None, planned: list[dict[str, str]] | None) -> bool:
    """Whether a document's recorded extraction still matches what the registry would run.

    A text record (``planned is None``) is current once indexed: it is read
    directly. A document with no record is current only when the registry has
    nothing to run on it (the built-in extraction made it and still would). A
    document with a record is current when the same extractors, at the same
    versions, would run now. A record written without ``planned`` (another
    writer's shape) is compared on its ``extractors``, else its ``engine`` and
    ``version``.
    """
    if planned is None:
        return True
    want = _extractor_set(planned)
    if not stored:
        return not want
    if "planned" in stored:
        have = _extractor_set(stored["planned"])
    elif stored.get("extractors"):
        have = _extractor_set(stored["extractors"])
    else:
        have = _extractor_set([{"name": stored.get("engine"), "version": stored.get("version")}])
    return have == want


def _extract_via_registry(
    content_path: Path, item: FetchedItem, *, sha256: str | None
) -> tuple[str, list[dict[str, str]], list[dict[str, str]]]:
    """Run the registry's text extractors; return (joined rag text, contributors, planned)."""
    try:
        from axiom.extensions.builtins.library.extract.runner import run

        ctx = _context(content_path, item, sha256)
        planned = planned_text_extractors(ctx)
    except Exception as exc:  # noqa: BLE001 - a registry fault must not lose the record
        log.warning("Extractor registry unavailable for %s: %s", item.display_name, exc)
        return "", [], []

    sections: list[tuple[str, str]] = []
    contributed: list[dict[str, str]] = []
    ran: list[dict[str, str]] = []
    for p in planned:
        m = p.entry.manifest
        ran.append({"name": m.name, "version": m.version})
        outcome = run(p.entry, ctx)
        result = outcome.result
        if outcome.status != "done" or result is None:
            continue
        ctx.prior[m.name] = result
        unpacked = (result.metadata or {}).get("unpacked_dir") if result.children else None
        if unpacked:
            shutil.rmtree(unpacked, ignore_errors=True)  # children are not followed here
        if "rag" not in (set(m.tags) | set(result.tags)) - set(result.drop_tags):
            continue
        texts = [(t.title, t.text) for t in result.text if t.text.strip()]
        if texts:
            sections.extend(texts)
            contributed.append({"name": m.name, "version": m.version})
    return _join_sections(sections), contributed, ran


def _join_sections(sections: list[tuple[str, str]]) -> str:
    """Join text sections the way the Library writes its ``rag.md``."""
    if not sections:
        return ""
    if len(sections) == 1 and not sections[0][0]:
        return sections[0][1]
    return "\n\n".join(
        (f"## {title}\n\n" if title else "") + text.strip() for title, text in sections
    )


def _extract_fallback(content_path: Path, item: FetchedItem) -> str | None:
    """Today's built-in extraction, for a file no registry extractor yielded text for."""
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
        # Resolved at call time so a test's monkeypatched stub is honoured.
        from . import embedder as _self

        return _self.extract_text(named)


def _looks_like_text(name: str) -> bool:
    return Path(name).suffix.lower() in {".md", ".txt", ".rst"}


__all__ = [
    "EmbedStats",
    "embed_bronze_record",
    "extract_document",
    "extraction_plan",
    "parser_is_current",
    "planned_text_extractors",
]
