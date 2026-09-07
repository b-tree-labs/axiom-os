# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Bronze → RAG embed adapter.

Takes a :class:`BronzeWriteResult` (already gated, content-addressed)
plus the originating :class:`FetchedItem` (carries the citation-friendly
``source_path``) and feeds the existing
:mod:`axiom.rag` chunk + embed + store pipeline. The provenance gate
ran in bronze — this stage never re-gates.

This is the final RAG-embed asset in the DP-1 Dagster pipeline (Slice 3
wires Dagster around it). Pure Python; no Dagster dependency.
"""

from __future__ import annotations

from .embedder import EmbedStats, embed_bronze_record

__all__ = ["EmbedStats", "embed_bronze_record"]
