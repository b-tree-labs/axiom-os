# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``PromotionMap`` — a declared bronze→gold promotion, as data (ADR-001 D4).

A promotion map is *config, not code*: it names the target table, the SCD-2
business key, the column mapping (gold ← bronze/pivoted field), an optional EAV
pivot (for long attribute/value tables), and a source precedence (which source
wins, field by field, when several feed one target). This is the direct
replacement for hand-coded promoter functions — a new tabular target is a TOML
file, not a Python change.

TOML shape::

    [promotion]
    target = "gold.series"
    natural_key = ["obs_date"]
    source_precedence = ["db.v1", "api.v1", "csv.v1"]   # highest first
    scd2 = true

    [promotion.columns]        # gold_column = bronze/pivoted field
    obs_date = "slot"
    predicted = "metric.predicted"
    measured  = "metric.measured"

    [promotion.pivot]          # optional: EAV long rows → wide
    key = "slot"
    name = "name"
    value = "value"
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Pivot:
    """EAV pivot spec: group long rows by ``key``, spreading ``name``→``value``."""

    key: str
    name: str
    value: str


@dataclass(frozen=True)
class PromotionMap:
    target: str
    natural_key: tuple[str, ...]
    columns: dict[str, str] = field(default_factory=dict)
    pivot: Pivot | None = None
    source_precedence: tuple[str, ...] = ()
    scd2: bool = True

    def validate(self) -> list[str]:
        errs: list[str] = []
        if not self.target:
            errs.append("promotion map requires 'target'")
        if not self.natural_key:
            errs.append("promotion map requires a non-empty 'natural_key'")
        if not self.columns:
            errs.append("promotion map requires 'columns'")
        for k in self.natural_key:
            if k not in self.columns:
                errs.append(f"natural_key column {k!r} is not in [promotion.columns]")
        if self.pivot is not None:
            for f in ("key", "name", "value"):
                if not getattr(self.pivot, f):
                    errs.append(f"[promotion.pivot] requires '{f}'")
        return errs


def parse_promotion_map(data: dict) -> PromotionMap:
    """Build a :class:`PromotionMap` from parsed TOML (or an equivalent dict)."""
    p = data.get("promotion") or {}
    pivot = None
    pv = p.get("pivot")
    if isinstance(pv, dict):
        pivot = Pivot(key=pv.get("key", ""), name=pv.get("name", ""), value=pv.get("value", ""))
    return PromotionMap(
        target=p.get("target", ""),
        natural_key=tuple(p.get("natural_key") or ()),
        columns=dict(p.get("columns") or {}),
        pivot=pivot,
        source_precedence=tuple(p.get("source_precedence") or ()),
        scd2=bool(p.get("scd2", True)),
    )


def load_promotion_map(path: str | Path) -> PromotionMap:
    """Load + parse a promotion map TOML. Raises on unreadable/invalid TOML;
    semantic problems are reported by :meth:`PromotionMap.validate`."""
    return parse_promotion_map(tomllib.loads(Path(path).read_text(encoding="utf-8")))


__all__ = ["Pivot", "PromotionMap", "load_promotion_map", "parse_promotion_map"]
