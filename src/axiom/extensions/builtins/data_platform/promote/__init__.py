# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Declared bronze→gold promotion (ADR-001 D4): map-as-data + SCD-2 engine.

The map (:mod:`.map`) is TOML config validated at register time; the engine
(:mod:`.engine`) applies EAV pivot, field-level source precedence, and SCD-2
supersession — pure and DB-free so it is fully testable without Postgres.
"""

from __future__ import annotations

from .engine import (
    GoldRow,
    InMemoryGoldTable,
    PromotionResult,
    promote,
    promote_rows,
)
from .map import (
    Pivot,
    PromotionMap,
    load_promotion_map,
    parse_promotion_map,
)

__all__ = [
    "GoldRow",
    "InMemoryGoldTable",
    "Pivot",
    "PromotionMap",
    "PromotionResult",
    "load_promotion_map",
    "parse_promotion_map",
    "promote",
    "promote_rows",
]
