# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Document ingestion orchestrator.

Scans the repository for markdown/text files, chunks them, generates
embeddings, and upserts into the RAG store.  Uses MD5 checksums to
skip unchanged files.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path

from .chunker import chunk_markdown
from .embeddings import embed_texts
from .extract import SUPPORTED_EXTENSIONS, extract_text
from .store import CORPUS_INTERNAL, RAGStore

log = logging.getLogger(__name__)

_TEXT_EXTENSIONS = {".md", ".txt"}
_BINARY_EXTENSIONS = {".pdf", ".docx", ".pptx", ".odt"}


@dataclass
class IngestStats:
    """Outcome of an ingest run.

    Skips are split by *reason* so a bulk onboarding of a heterogeneous
    corpus is honest about what entered the index versus what was dropped:

    - ``files_unchanged``  — already indexed with a matching checksum
    - ``files_unsupported``— no extractor for the extension (e.g. binary spectra, images)
    - ``files_failed``     — supported type but no text extracted (e.g. scanned PDF)
    """

    files_indexed: int = 0
    chunks_created: int = 0
    files_unchanged: int = 0
    files_unsupported: int = 0
    files_failed: int = 0
    files_excluded: int = 0  # provenance rule said never ingest (controlled source)
    files_quarantined: int = 0  # provenance rule said hold for human review
    unsupported_by_ext: dict[str, int] = field(default_factory=dict)
    excluded_by_rule: dict[str, int] = field(default_factory=dict)

    @property
    def files_skipped(self) -> int:
        """Total of every skip kind. Retained for callers that printed one count."""
        return self.files_unchanged + self.files_unsupported + self.files_failed

    def __iadd__(self, other: IngestStats) -> IngestStats:
        self.files_indexed += other.files_indexed
        self.chunks_created += other.chunks_created
        self.files_unchanged += other.files_unchanged
        self.files_unsupported += other.files_unsupported
        self.files_failed += other.files_failed
        self.files_excluded += other.files_excluded
        self.files_quarantined += other.files_quarantined
        for ext, n in other.unsupported_by_ext.items():
            self.unsupported_by_ext[ext] = self.unsupported_by_ext.get(ext, 0) + n
        for rule, n in other.excluded_by_rule.items():
            self.excluded_by_rule[rule] = self.excluded_by_rule.get(rule, 0) + n
        return self

    def drop_report(self) -> str:
        """Human-readable summary of files that did NOT enter the index.

        Empty when nothing was dropped for a content reason (unchanged files
        are not "drops"). Lists unsupported extensions most-common-first so a
        bulk onboarding makes curation gaps visible at a glance.
        """
        if not (
            self.files_unsupported
            or self.files_failed
            or self.files_excluded
            or self.files_quarantined
        ):
            return ""
        parts: list[str] = []
        if self.files_unsupported:
            by_ext = ", ".join(
                f"{ext or '(noext)'}={n}"
                for ext, n in sorted(self.unsupported_by_ext.items(), key=lambda kv: -kv[1])
            )
            parts.append(f"{self.files_unsupported} unsupported ({by_ext})")
        if self.files_failed:
            parts.append(f"{self.files_failed} extraction failures (supported type, no text)")
        if self.files_excluded:
            by_rule = ", ".join(
                f"{r}={n}" for r, n in sorted(self.excluded_by_rule.items(), key=lambda kv: -kv[1])
            )
            parts.append(f"{self.files_excluded} excluded by provenance ({by_rule})")
        if self.files_quarantined:
            parts.append(f"{self.files_quarantined} quarantined for review")
        return "; ".join(parts)


def _md5(path: Path) -> str:
    h = hashlib.md5()
    h.update(path.read_bytes())
    return h.hexdigest()


def walk_candidate_files(root: Path) -> list[Path]:
    """All real files under *root*, excluding hidden paths and ``__MACOSX`` cruft.

    Returns *every* file regardless of extension — categorization
    (supported / unsupported / failed) happens per-file in ``ingest_file``,
    so unsupported files surface as visible drops instead of being filtered
    out before they're ever seen. Anything under a hidden directory
    (``.git``, ``.venv``, …) or a hidden filename is excluded.
    """
    files: list[Path] = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        rel_parts = p.relative_to(root).parts
        if any(part.startswith(".") for part in rel_parts):
            continue
        if "__MACOSX" in rel_parts:
            continue
        files.append(p)
    return sorted(files)


def ingest_file(
    path: Path,
    store: RAGStore,
    repo_root: Path | None = None,
    corpus: str = CORPUS_INTERNAL,
    owner: str | None = None,
    chunking_tier: str = "fixed",
    corpus_generation: int = 1,
    rules=None,
) -> IngestStats:
    """Ingest a single file into the RAG store.

    Returns stats for this file (1 indexed or 1 skipped). When *rules* (a list of
    ProvenanceRule) is given, the file's path is routed first: EXCLUDE/QUARANTINE
    dispositions gate it out before it is ever read; an ALLOW rule may redirect it
    to a different corpus tier.
    """
    stats = IngestStats()

    rel_path = str(path.relative_to(repo_root)) if repo_root else str(path)

    # Provenance/artifact gate — runs before reading bytes, so a controlled
    # source is never extracted or embedded. Determination is by what the
    # artifact is and where it came from, not by prose keyword scanning.
    if rules:
        from .ingest_router import Disposition, route_path

        decision = route_path(
            rel_path, rules, default_disposition=Disposition.ALLOW, default_tier=corpus
        )
        if decision.disposition is Disposition.EXCLUDE:
            log.info("Provenance EXCLUDE %s (%s)", rel_path, decision.reason)
            stats.files_excluded += 1
            key = decision.matched or "(default)"
            stats.excluded_by_rule[key] = stats.excluded_by_rule.get(key, 0) + 1
            return stats
        if decision.disposition is Disposition.QUARANTINE:
            log.info("Provenance QUARANTINE %s (%s)", rel_path, decision.reason)
            stats.files_quarantined += 1
            return stats
        if decision.tier:
            corpus = decision.tier

    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        log.debug("Skipping unsupported file: %s", path)
        stats.files_unsupported += 1
        stats.unsupported_by_ext[suffix] = stats.unsupported_by_ext.get(suffix, 0) + 1
        return stats

    checksum = _md5(path)

    # Check if already indexed with same checksum
    existing = store.get_document(rel_path)
    if existing and existing.get("checksum") == checksum:
        log.debug("Unchanged, skipping: %s", rel_path)
        stats.files_unchanged += 1
        return stats

    # Extract text — plain read for md/txt, extraction for binary formats
    if suffix in _TEXT_EXTENSIONS:
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            log.warning("Cannot read %s: %s", path, exc)
            stats.files_failed += 1
            return stats
    else:
        content = extract_text(path)
        if not content:
            log.warning("No text extracted from %s", rel_path)
            stats.files_failed += 1
            return stats

    # EC screening — prevent security-marked content from entering community tier
    if corpus == "rag-community":
        from .ec_screening import screen_document

        screening = screen_document(rel_path, content, target_corpus=corpus)
        if not screening.allowed_community:
            log.warning(
                "EC screening blocked %s from community tier: %s → routing to org",
                rel_path,
                screening.markers_found,
            )
            corpus = "rag-org"  # Auto-route to restricted tier

    # Chunk using the requested strategy
    if chunking_tier == "semantic":
        from .semantic_chunker import chunk_semantic

        chunks = chunk_semantic(content, rel_path)
    else:
        chunks = chunk_markdown(content, rel_path)

    if not chunks:
        stats.files_failed += 1
        return stats

    # Generate embeddings. embed_texts returns None only when NO provider is
    # configured (intentional text-only). It RAISES when a provider is configured
    # but failed (e.g. a network drop to the embedder) — in that case we must not
    # commit, or the doc would be stored text-only with its checksum recorded and
    # skipped as "unchanged" forever. Count it failed so a re-run retries it.
    texts = [c.text for c in chunks]
    try:
        embeddings = embed_texts(texts)
    except Exception as exc:
        log.warning(
            "Embedding failed for %s — not indexed, will retry on re-run: %s", rel_path, exc
        )
        stats.files_failed += 1
        return stats

    if embeddings is None:
        log.info(
            "Indexing %s (%d chunks, tier=%s, text-only — no embedding provider)",
            rel_path,
            len(chunks),
            chunking_tier,
        )
    else:
        log.info(
            "Indexing %s (%d chunks, tier=%s, with embeddings)",
            rel_path,
            len(chunks),
            chunking_tier,
        )

    store.upsert_chunks(
        chunks,
        embeddings,
        checksum=checksum,
        corpus=corpus,
        owner=owner,
        chunking_tier=chunking_tier,
        corpus_generation=corpus_generation,
    )

    stats.files_indexed = 1
    stats.chunks_created = len(chunks)
    return stats


def ingest_path(
    path: Path,
    store: RAGStore,
    corpus: str = CORPUS_INTERNAL,
    owner: str | None = None,
    chunking_tier: str = "fixed",
    corpus_generation: int = 1,
    rules=None,
) -> IngestStats:
    """Ingest all supported documents under *path* into *corpus*.

    Unlike ingest_repo(), this function has no opinion about directory
    structure — it walks the given path and indexes everything it can
    extract text from. Useful for one-off warm-up ingests (e.g. Box
    knowledge dumps, external doc collections). *rules* (ProvenanceRule list)
    gate/route each file by source before it is read.
    """
    stats = IngestStats()

    if path.is_file():
        stats += ingest_file(
            path,
            store,
            repo_root=path.parent,
            corpus=corpus,
            owner=owner,
            chunking_tier=chunking_tier,
            corpus_generation=corpus_generation,
            rules=rules,
        )
        return stats

    # Walk *all* files (not just supported extensions) so unsupported files
    # are visible as drops in the stats rather than silently never-seen.
    # ingest_file categorizes each and returns early for unsupported types
    # before reading their bytes.
    files = walk_candidate_files(path)

    log.info("ingest_path: %d files under %s → corpus=%s", len(files), path, corpus)

    for fpath in files:
        stats += ingest_file(
            fpath,
            store,
            repo_root=path,
            corpus=corpus,
            owner=owner,
            chunking_tier=chunking_tier,
            corpus_generation=corpus_generation,
            rules=rules,
        )

    log.info(
        "ingest_path complete: %d indexed, %d chunks, %d unchanged, %d unsupported, %d failed",
        stats.files_indexed,
        stats.chunks_created,
        stats.files_unchanged,
        stats.files_unsupported,
        stats.files_failed,
    )
    return stats


def ingest_repo(
    repo_root: Path,
    store: RAGStore,
    corpus: str = CORPUS_INTERNAL,
    personal: bool = True,
) -> IngestStats:
    """Scan and ingest all supported documents under *repo_root*.

    Indexes:
      - docs/, runtime/config/, runtime/knowledge/  (project docs)
      - CLAUDE.md                                   (project context)
      - runtime/sessions/*.json                     (chat transcripts) [personal]
      - runtime/inbox/processed/*.json              (sense signals)   [personal]
      - git repos under runtime/knowledge/          (commit logs)     [personal]

    Set *personal=False* to skip the personal corpus sources (sessions,
    signals, git logs) — useful when indexing a shared or community corpus.
    """
    stats = IngestStats()

    # -- Static document sources ---------------------------------------------
    search_dirs = [
        repo_root / "docs",
        repo_root / "runtime" / "config",
        repo_root / "runtime" / "knowledge",  # Box, SharePoint, external docs
    ]
    extra_files = [repo_root / "CLAUDE.md"]

    files: list[Path] = []
    for d in search_dirs:
        if d.is_dir():
            files.extend(walk_candidate_files(d))
    for f in extra_files:
        if f.is_file():
            files.append(f)

    log.info("Found %d files to consider", len(files))

    for fpath in sorted(files):
        stats += ingest_file(fpath, store, repo_root=repo_root, corpus=corpus)

    # -- Personal corpus sources (sessions, signals, git logs) ---------------
    if personal:
        from .personal import ingest_git_logs, ingest_sessions, ingest_signals

        sessions_dir = repo_root / "runtime" / "sessions"
        if sessions_dir.is_dir():
            indexed, skipped = ingest_sessions(sessions_dir, store, corpus=corpus)
            stats.files_indexed += indexed
            stats.files_unchanged += skipped

        inbox_dir = repo_root / "runtime" / "inbox" / "processed"
        if inbox_dir.is_dir():
            indexed, skipped = ingest_signals(inbox_dir, store, corpus=corpus)
            stats.files_indexed += indexed
            stats.files_unchanged += skipped

        knowledge_dir = repo_root / "runtime" / "knowledge"
        if knowledge_dir.is_dir():
            indexed, skipped = ingest_git_logs(knowledge_dir, store, corpus=corpus)
            stats.files_indexed += indexed
            stats.files_unchanged += skipped

    log.info(
        "Ingestion complete: %d indexed, %d chunks, %d skipped",
        stats.files_indexed,
        stats.chunks_created,
        stats.files_skipped,
    )
    return stats
