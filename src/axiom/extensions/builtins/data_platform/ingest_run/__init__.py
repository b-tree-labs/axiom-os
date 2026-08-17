# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Generic ingest-run telemetry — the stage funnel for ANY ingest job.

This is a **domain-agnostic** primitive: it speaks ``stage`` / ``in`` /
``out`` / ``dropped`` / ``failed``, never a specific source (Box), sink
(RAG), or domain. Pull ingest, push ingest, CDC refresh, and any future
non-RAG ingest job all build the same :class:`IngestRunReport` so an
operator sees one consistent funnel:

    discovered → to_process → fetched → extracted → transformed →
    loaded → indexed

…with per-stage ``dropped`` (skipped-with-reason) and ``failed``
(failed-with-cause) counts, so "in vs out vs why" is legible at every hop.

The stage *names* above are conventional defaults (see :data:`DEFAULT_STAGES`)
but a job may declare its own ordered stages — a job that doesn't embed/index
simply omits those stages. Nothing here imports a source, a sink, or a
schema; persistence is an injectable seam (:class:`RunStore`).
"""

from __future__ import annotations

from .report import (
    DEFAULT_STAGES,
    IngestRunReport,
    RunStatus,
    StageCounts,
)
from .store import InMemoryRunStore, JsonlRunStore, RunStore

__all__ = [
    "DEFAULT_STAGES",
    "IngestRunReport",
    "RunStatus",
    "StageCounts",
    "RunStore",
    "InMemoryRunStore",
    "JsonlRunStore",
]
