# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The promotion engine (ADR-001 D4): bronze rows → gold, via a declared
:class:`PromotionMap`, with EAV pivot, field-level source precedence, and SCD-2
supersession.

Pure and DB-free: :func:`promote_rows` takes the incoming rows and the *current*
gold state (key → value-hash) and returns the operations to apply
(:class:`PromotionResult`). A sink (:class:`InMemoryGoldTable` here; a Postgres
``session_for("data_platform")`` table in the heavy tier) applies them. This
keeps the SCD-2 / precedence logic fully testable without a database, exactly as
the P1 bronze sink is.
"""

from __future__ import annotations

import hashlib
import json as _json
from dataclasses import dataclass, field, replace

from .map import PromotionMap


@dataclass(frozen=True)
class GoldRow:
    """One gold row with SCD-2 provenance."""

    key: tuple
    values: dict
    run_id: str
    valid_from: str
    valid_to: str | None
    is_current: bool


@dataclass
class PromotionResult:
    inserted: list[GoldRow] = field(default_factory=list)
    superseded: list[tuple] = field(default_factory=list)  # current keys to close
    unchanged: int = 0


def _value_hash(values: dict) -> str:
    return hashlib.sha256(
        _json.dumps(values, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _pivot(rows: list[dict], pivot) -> list[dict]:
    """Group EAV long rows by (schema_ref, key) → one wide dict each."""
    groups: dict[tuple, dict] = {}
    for r in rows:
        sref = r.get("schema_ref", "")
        kv = r.get(pivot.key)
        g = groups.setdefault((sref, kv), {"schema_ref": sref, pivot.key: kv})
        name = r.get(pivot.name)
        if name is not None:
            g[name] = r.get(pivot.value)
    return list(groups.values())


def _map_columns(wide: dict, pmap: PromotionMap) -> tuple[dict, str]:
    values = {gold: wide.get(bronze) for gold, bronze in pmap.columns.items()}
    return values, wide.get("schema_ref", "")


def _merge_by_precedence(mapped: list[tuple[dict, str]], pmap: PromotionMap) -> dict[tuple, dict]:
    """Field-level merge by source precedence: for each natural key, the highest-
    precedence source with a non-null value wins that field (so predicted can come
    from one source, measured from another). Unknown schema_refs rank lowest."""
    def rank(sref: str) -> int:
        try:
            return pmap.source_precedence.index(sref)
        except ValueError:
            return len(pmap.source_precedence)  # unknown = lowest priority

    by_key: dict[tuple, list[tuple[int, dict]]] = {}
    for values, sref in mapped:
        key = tuple(values.get(k) for k in pmap.natural_key)
        by_key.setdefault(key, []).append((rank(sref), values))

    merged: dict[tuple, dict] = {}
    for key, items in by_key.items():
        vals: dict = {}
        # apply lowest-precedence first so higher precedence overwrites non-null.
        for _, values in sorted(items, key=lambda x: -x[0]):
            for col, v in values.items():
                if v is not None:
                    vals[col] = v
        merged[key] = vals
    return merged


def promote_rows(
    rows: list[dict],
    pmap: PromotionMap,
    current: dict[tuple, str],
    *,
    run_id: str,
    now: str,
) -> PromotionResult:
    """Compute the SCD-2 operations to promote ``rows`` into the target.

    ``current`` maps each present natural key → the value-hash of its current
    gold row. New key → insert; same hash → unchanged; changed hash → supersede
    the old current row and insert the new one.
    """
    wide = _pivot(rows, pmap.pivot) if pmap.pivot else [dict(r) for r in rows]
    mapped = [_map_columns(w, pmap) for w in wide]
    merged = _merge_by_precedence(mapped, pmap)

    result = PromotionResult()
    for key, values in merged.items():
        vh = _value_hash(values)
        cur = current.get(key)
        if cur is None:
            result.inserted.append(GoldRow(key, values, run_id, now, None, True))
        elif cur == vh:
            result.unchanged += 1
        else:
            if pmap.scd2:
                result.superseded.append(key)
            else:
                result.superseded.append(key)  # non-SCD2: overwrite (sink drops old)
            result.inserted.append(GoldRow(key, values, run_id, now, None, True))
    return result


class InMemoryGoldTable:
    """A test/dev gold table holding current + historical rows (SCD-2 aware)."""

    def __init__(self, pmap: PromotionMap) -> None:
        self._pmap = pmap
        self.rows: list[GoldRow] = []

    def current_hashes(self) -> dict[tuple, str]:
        return {r.key: _value_hash(r.values) for r in self.rows if r.is_current}

    def current_rows(self) -> list[GoldRow]:
        return [r for r in self.rows if r.is_current]

    def apply(self, result: PromotionResult, *, now: str) -> None:
        sup = set(result.superseded)
        rebuilt: list[GoldRow] = []
        for r in self.rows:
            if r.is_current and r.key in sup:
                if self._pmap.scd2:
                    rebuilt.append(replace(r, is_current=False, valid_to=now))
                # non-SCD2: drop the old current row entirely (overwrite)
            else:
                rebuilt.append(r)
        rebuilt.extend(result.inserted)
        self.rows = rebuilt


def promote(
    rows: list[dict],
    pmap: PromotionMap,
    gold: InMemoryGoldTable,
    *,
    run_id: str,
    now: str,
) -> PromotionResult:
    """Convenience: compute + apply in one call against an in-memory gold table."""
    result = promote_rows(rows, pmap, gold.current_hashes(), run_id=run_id, now=now)
    gold.apply(result, now=now)
    return result


__all__ = [
    "GoldRow",
    "InMemoryGoldTable",
    "PromotionResult",
    "promote",
    "promote_rows",
]
