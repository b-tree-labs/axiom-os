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

from pathlib import Path

from .agent import IngestReport, PlinthAgent
from .contracts import IngestSource, SchemaPack, TransformPack
from .registry import DataPlatformRegistry


def _register_config_schema() -> None:
    """Declare the extension's config knobs (ADR-065; the diagnostics
    dogfood pattern). ``config.schema.json`` next to this module carries
    the site-facing fields — today the ``connector_labels`` attribution
    map consumed by ``data.activity`` / ``data.refresh``. Best-effort: a
    failure here never blocks the extension from loading the rest of its
    surface."""
    try:
        from axiom.infra.config import register_schema_from_jsonschema

        schema_path = Path(__file__).parent / "config.schema.json"
        if schema_path.exists():
            register_schema_from_jsonschema("data_platform", schema_path)
    except Exception:
        pass


_register_config_schema()

__all__ = [
    "DataPlatformRegistry",
    "IngestSource",
    "SchemaPack",
    "TransformPack",
    "PlinthAgent",
    "IngestReport",
]
