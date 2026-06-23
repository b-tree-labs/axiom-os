# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Data-platform extension — the home data-platform initiatives plug into.

Houses **PLINTH**, the data-platform orchestrator agent (ingestion,
source-monitoring, medallion pack-flow), and the contribution registry +
protocols a consumer layer registers into.

This is a SKELETON + contribution interfaces, not the heavy lakehouse.
The base install stays light: no Iceberg / Dagster / dbt / duckdb /
superset is imported here. Those are declared as the ``data-platform``
optional extra in the host ``pyproject.toml`` and wired only by a future
heavy layer behind the same dispatch surface.

Public API:
    DataPlatformRegistry          — register/lookup ingest sources + packs
    IngestSource                  — pollable source protocol (list_changed/fetch)
    SchemaPack / TransformPack    — medallion contribution protocols (abstract)
    PlinthAgent                   — orchestrator; run_scheduled_ingest(source)
    IngestReport                  — dispatch outcome
"""

from __future__ import annotations

from .agent import IngestReport, PlinthAgent
from .contracts import IngestSource, SchemaPack, TransformPack
from .registry import DataPlatformRegistry

__all__ = [
    "DataPlatformRegistry",
    "IngestSource",
    "SchemaPack",
    "TransformPack",
    "PlinthAgent",
    "IngestReport",
]
