# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tabular-upload handling — load the whole table, don't RAG-chunk it.

The Problem
-----------
A user uploads a CSV and asks an analytical question over it ("find the
outliers across all rows"). A vanilla RAG pipeline chunks the file and
retrieves a few semantically-similar rows — a *different* few each turn —
so the model analyses a shifting fragment and its answer wanders (it
"finds" different outliers each time, and invents rows). Analytical
questions need the whole column, not a similarity slice.

The Primitive
-------------
This module gives a serving layer three domain-agnostic pieces:

1. :func:`sniff_tabular` — decide whether an upload is tabular (so it can
   be routed to this path instead of the chunker).
2. :func:`parse_table` + :func:`render_table_block` — parse the file and
   render it *whole* into a context block, within a token budget, telling
   the model honestly if anything was omitted (never a silent slice).
3. :func:`iqr_outliers` / :func:`zscore_outliers` — compute outliers
   deterministically so "find the outliers" is answered by arithmetic over
   the full column, not by the model eyeballing a fragment.

Names no consumer.
"""

from __future__ import annotations

import csv
import io
import statistics
from collections.abc import Sequence
from dataclasses import dataclass

_TABULAR_EXTS = (".csv", ".tsv", ".tab")


@dataclass(frozen=True)
class Table:
    """A parsed tabular file: header + row values, both as strings."""

    columns: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]

    @property
    def n_rows(self) -> int:
        return len(self.rows)

    def column(self, name: str) -> list[str]:
        """All values in a column by header name (empty list if absent)."""
        try:
            idx = self.columns.index(name)
        except ValueError:
            return []
        return [r[idx] if idx < len(r) else "" for r in self.rows]


def sniff_tabular(filename: str | None, sample: str | None = None) -> bool:
    """True if the upload looks tabular — by extension, or by a delimiter
    sniff on a content sample when the name is unhelpful."""
    if filename and filename.lower().endswith(_TABULAR_EXTS):
        return True
    if not sample:
        return False
    head = "\n".join(sample.splitlines()[:5])
    if head.count(",") >= 2 or head.count("\t") >= 2:
        try:
            csv.Sniffer().sniff(head, delimiters=",\t;")
            return True
        except csv.Error:
            return False
    return False


def parse_table(text: str) -> Table:
    """Parse CSV/TSV text into a :class:`Table`. The delimiter is sniffed;
    the first row is treated as the header."""
    if not text or not text.strip():
        return Table(columns=(), rows=())
    sample = "\n".join(text.splitlines()[:10])
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    all_rows = [tuple(cell.strip() for cell in row) for row in reader if row]
    if not all_rows:
        return Table(columns=(), rows=())
    header, *body = all_rows
    return Table(columns=tuple(header), rows=tuple(body))


def _as_floats(values: Sequence[str]) -> list[float]:
    out: list[float] = []
    for v in values:
        try:
            out.append(float(str(v).replace(",", "").strip()))
        except (TypeError, ValueError):
            continue
    return out


def iqr_outliers(values: Sequence[str], k: float = 1.5) -> list[int]:
    """Indices (into ``values``) of numeric outliers by the IQR rule
    (< Q1 - k·IQR or > Q3 + k·IQR). Non-numeric and missing cells are
    ignored for the fence but still counted for indexing. Deterministic."""
    nums = [(i, f) for i, v in enumerate(values) for f in (_one_float(v),) if f is not None]
    xs = [f for _i, f in nums]
    if len(xs) < 4:
        return []
    quants = statistics.quantiles(xs, n=4, method="inclusive")
    q1, q3 = quants[0], quants[2]
    iqr = q3 - q1
    lo, hi = q1 - k * iqr, q3 + k * iqr
    return [i for i, f in nums if f < lo or f > hi]


def zscore_outliers(values: Sequence[str], threshold: float = 3.0) -> list[int]:
    """Indices of numeric outliers whose absolute z-score exceeds
    ``threshold``. Deterministic; returns [] if variance is zero."""
    nums = [(i, f) for i, v in enumerate(values) for f in (_one_float(v),) if f is not None]
    xs = [f for _i, f in nums]
    if len(xs) < 2:
        return []
    mean = statistics.fmean(xs)
    sd = statistics.pstdev(xs)
    if sd == 0:
        return []
    return [i for i, f in nums if abs((f - mean) / sd) > threshold]


def _one_float(v: str) -> float | None:
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def render_table_block(
    table: Table, max_tokens: int = 4000, focus_columns: Sequence[str] | None = None
) -> str:
    """Render the table *whole* into a text block within a token budget.

    Renders every row so an analytical question sees the full column. If the
    full width would exceed the budget, drops to ``focus_columns`` (plus the
    first column as a row key) and states which columns were omitted — never
    silently. If even the focused rows exceed the budget, keeps as many whole
    rows as fit and states how many were omitted. Honesty over silent slicing.
    """
    if not table.columns:
        return "(empty table)"
    char_budget = max(200, max_tokens * 4)

    cols = list(table.columns)
    omitted_cols: list[str] = []
    if focus_columns:
        keep = [cols[0]] + [c for c in focus_columns if c in cols and c != cols[0]]
        omitted_cols = [c for c in cols if c not in keep]
        idxs = [cols.index(c) for c in keep]
    else:
        keep = cols
        idxs = list(range(len(cols)))

    def _line(cells: Sequence[str]) -> str:
        return " | ".join(str(cells[i]) if i < len(cells) else "" for i in idxs)

    header = _line(table.columns)
    lines = [header, "-" * len(header)]
    shown = 0
    for row in table.rows:
        candidate = _line(row)
        if len("\n".join(lines)) + len(candidate) + 1 > char_budget:
            break
        lines.append(candidate)
        shown += 1

    notes = [f"[{table.n_rows} rows total; {len(table.columns)} columns]"]
    if omitted_cols:
        notes.append(f"[columns omitted to fit: {', '.join(omitted_cols)}]")
    if shown < table.n_rows:
        notes.append(f"[{table.n_rows - shown} rows omitted to fit budget — ask to narrow columns]")
    return "\n".join(lines) + "\n" + " ".join(notes)


__all__ = [
    "Table",
    "sniff_tabular",
    "parse_table",
    "render_table_block",
    "iqr_outliers",
    "zscore_outliers",
]
