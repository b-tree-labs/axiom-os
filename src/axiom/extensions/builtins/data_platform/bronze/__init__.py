# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Bronze layer — the substrate of record per ADR-049.

The bronze writer combines two seams:
1. the provenance gate (``axiom.rag.ingest_router``) — classifies an
   item as ALLOW / QUARANTINE / EXCLUDE based on path-rules a consumer
   supplies;
2. a :class:`BronzeSink` — the storage backend (filesystem default;
   Iceberg in the ``[heavy]`` extra).

The Dagster sensor + asset in Slice 3 wraps :class:`BronzeWriter` so the
same writer drives both the lean (filesystem) and heavy (Iceberg) tiers.
"""

from __future__ import annotations

from .router import (
    BronzeSink,
    BronzeWriter,
    BronzeWriteResult,
    TabularBronzeSink,
    TabularBronzeWriter,
    TabularWriteResult,
)
from .sinks import FilesystemBronzeSink, FilesystemTabularBronzeSink

__all__ = [
    "BronzeSink",
    "BronzeWriter",
    "BronzeWriteResult",
    "FilesystemBronzeSink",
    "FilesystemTabularBronzeSink",
    "TabularBronzeSink",
    "TabularBronzeWriter",
    "TabularWriteResult",
]
