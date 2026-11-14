# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Bronze → RAG embed adapter.

Takes a :class:`BronzeWriteResult` (already gated, content-addressed)
plus the originating :class:`FetchedItem` (carries the citation-friendly
``source_path``) and feeds the existing
:mod:`axiom.rag` chunk + embed + store pipeline. The provenance gate
ran in bronze — this stage never re-gates.

This is the final RAG-embed step of the DP-1 pipeline. Pure Python; the
runner calls it.

Interim shortcut. This reads bronze straight into the RAG and skips conform,
silver and gold. The designed route is bronze -> conform -> silver -> gold ->
RAG (ADR-079): the RAG is a projection of gold. Don't build new parsing,
access labelling or curation on this path; parsing belongs in conform and
labels and deduplication in gold curation.
"""

from __future__ import annotations

from .embedder import EmbedStats, embed_bronze_record

__all__ = ["EmbedStats", "embed_bronze_record"]
