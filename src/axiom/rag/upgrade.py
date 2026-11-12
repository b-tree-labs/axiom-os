# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""RAG generation upgrade orchestrator — blue/green corpus rebuilds.

Coordinates the full upgrade cycle:
1. Auto-backup before any change
2. Create candidate generation
3. Ingest source documents with specified chunking strategy
4. Embed all new chunks
5. CURIO evaluates candidate vs active
6. Promote or discard based on quality gate

Usage::

    from axiom.rag.upgrade import build_generation

    stats = build_generation(
        store=store,
        source_path=Path("/home/msrp/msr_kb_bundle"),
        corpus="rag-community",
        chunking_tier="semantic",
    )
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class UpgradeStats:
    """Statistics from a generation build."""

    corpus: str
    generation: int
    chunking_tier: str
    files_processed: int = 0
    chunks_created: int = 0
    embeddings_generated: int = 0
    backup_path: str = ""
    success: bool = True
    error: str = ""


def build_generation(
    store,
    source_path: Path,
    corpus: str = "rag-community",
    chunking_tier: str = "semantic",
    auto_backup: bool = True,
    database_url: str = "",
) -> UpgradeStats:
    """Build a new candidate generation from source documents.

    Args:
        store: RAGStore instance
        source_path: Path to source documents (e.g., Ondrej's MSR bundle)
        corpus: Which corpus tier to build for
        chunking_tier: Chunking strategy ("fixed", "semantic", "graph_informed")
        auto_backup: Create pg_dump before starting
        database_url: DB URL for backup (auto-detected from store if empty)

    Returns:
        UpgradeStats with counts and status
    """
    from axiom.rag.generation import GenerationManager

    gen_mgr = GenerationManager(store)

    # Step 1: Backup
    backup_path = ""
    if auto_backup:
        try:
            from axiom.infra.backup import create_backup

            url = database_url or getattr(store, "_dsn", "")
            if url:
                result = create_backup(url, label=f"pre-gen-{corpus}")
                backup_path = str(result.backup_path) if result.success else ""
                log.info("Pre-upgrade backup: %s", backup_path)
        except Exception as e:
            log.warning("Backup failed (continuing anyway): %s", e)

    # Step 2: Create candidate generation
    candidate = gen_mgr.create_candidate(corpus)
    log.info("Building generation %d for %s (tier=%s)", candidate, corpus, chunking_tier)

    # Step 3: Ingest from source
    try:
        from axiom.rag.ingest import ingest_path

        stats = ingest_path(
            path=source_path,
            store=store,
            corpus=corpus,
            chunking_tier=chunking_tier,
            corpus_generation=candidate,
        )

        return UpgradeStats(
            corpus=corpus,
            generation=candidate,
            chunking_tier=chunking_tier,
            files_processed=stats.files_indexed,
            chunks_created=stats.chunks_created,
            backup_path=backup_path,
            success=True,
        )

    except Exception as e:
        log.error("Generation build failed: %s", e)
        gen_mgr.discard(corpus, candidate)
        return UpgradeStats(
            corpus=corpus,
            generation=candidate,
            chunking_tier=chunking_tier,
            backup_path=backup_path,
            success=False,
            error=str(e),
        )
