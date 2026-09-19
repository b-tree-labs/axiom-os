# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Axiom medallion: bronze/silver/gold data layers.

Bronze: raw, append-only, source-partitioned. SeaweedFS + Iceberg in prod.
Silver: cleaned, typed, joinable. DuckDB over Iceberg in prod.
Gold: aggregated / curated. Dagster orchestrates the transitions.

This module starts with bronze (Slice 5 code side) and a TraceProvider
sink that lands every observability event into it. Silver/gold transforms
come next; prod storage (SeaweedFS/Iceberg/DuckDB/Dagster) wires in at
deploy time.
"""

from __future__ import annotations

from axiom.medallion.bronze import BronzeStore
from axiom.medallion.receipts import BronzeReceiptStore
from axiom.medallion.trace_sink import BronzeTraceSink

__all__ = ["BronzeReceiptStore", "BronzeStore", "BronzeTraceSink"]
