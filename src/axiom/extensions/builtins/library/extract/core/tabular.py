# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Delimited text and Parquet: columns, types, a bounded sample, the true row count, and series shape.

A delimited file is streamed, never loaded: a multi-gigabyte export is counted
and profiled in one pass with a bounded sample. When the first column is time
(or a monotonic number) and others are numeric, the result says it is a series
(``features.series``) and carries a chart representation, which is how a table
that is really a measurement becomes a chart preview.
"""

from __future__ import annotations

import csv
import io
import math
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any

from axiom.extensions.builtins.library.extract.contract import (
    ExtractionContext,
    ExtractionResult,
    ExtractorUnavailable,
    Table,
    TextSection,
)

SAMPLE_ROWS = 50
#: Points kept per series for the chart representation (evenly decimated).
CHART_POINTS = 2000
#: Delimited files up to this size are also indexed as text.
RAG_MAX_BYTES = 2 * 1024 * 1024


def _parse_time(value: str) -> datetime | None:
    v = value.strip()
    if not v or len(v) < 8 or not any(c in v for c in "-/:T"):
        return None
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00").replace("/", "-"))
    except ValueError:
        return None


def _num(value: str) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _cell_type(value: str) -> str:
    v = value.strip()
    if v == "":
        return "empty"
    try:
        int(v)
        return "int"
    except ValueError:
        pass
    if _num(v) is not None:
        return "float"
    if _parse_time(v) is not None:
        return "datetime"
    if v.lower() in ("true", "false"):
        return "bool"
    return "str"


_RANK = {"empty": 0, "bool": 1, "int": 2, "float": 3, "datetime": 4, "str": 5}


def _merge(a: str, b: str) -> str:
    if a == "empty":
        return b
    if b == "empty" or a == b:
        return a
    if {a, b} == {"int", "float"}:
        return "float"
    return "str"


def delimited(ctx: ExtractionContext) -> ExtractionResult:
    head = ctx.head_text or ""
    suffix = PurePosixPath(ctx.file_name.lower()).suffix
    try:
        dialect = csv.Sniffer().sniff(head[:16384], delimiters=",\t;|")
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = "\t" if suffix == ".tsv" else ","
    has_header = True
    try:
        has_header = csv.Sniffer().has_header(head[:16384])
    except csv.Error:
        pass
    with ctx.path.open("r", encoding="utf-8", errors="replace", newline="") as f:
        reader = csv.reader(f, delimiter=delimiter)
        first = next(reader, None)
        if first is None:
            return ExtractionResult(summary="empty table", warnings=["the file has no rows"])
        columns = [c.strip() or f"column {i + 1}" for i, c in enumerate(first)] if has_header else [
            f"column {i + 1}" for i in range(len(first))
        ]
        sample: list[list[Any]] = [] if has_header else [first]
        types = ["empty"] * len(columns)
        count = 0 if has_header else 1
        numeric_min: list[float | None] = [None] * len(columns)
        numeric_max: list[float | None] = [None] * len(columns)
        # Keep every k-th row for the chart; k doubles as the file grows, so the
        # kept set stays bounded without knowing the row count in advance.
        keep: list[list[str]] = []
        stride = 1
        last_report = 0
        for row in reader:
            count += 1
            if len(sample) < SAMPLE_ROWS:
                sample.append(row[: len(columns)] + [""] * max(0, len(columns) - len(row)))
            for i, cell in enumerate(row[: len(columns)]):
                if count <= 5000:
                    types[i] = _merge(types[i], _cell_type(cell))
                n = _num(cell)
                if n is not None:
                    numeric_min[i] = n if numeric_min[i] is None else min(numeric_min[i], n)
                    numeric_max[i] = n if numeric_max[i] is None else max(numeric_max[i], n)
            if count % stride == 0:
                keep.append(row)
                if len(keep) >= CHART_POINTS * 2:
                    keep = keep[::2]
                    stride *= 2
            if count - last_report >= 200_000:
                last_report = count
                try:
                    ctx.report(f.buffer.tell() / max(1, ctx.size))  # type: ignore[attr-defined]
                except Exception:  # noqa: BLE001
                    pass
    col_types = dict(zip(columns, types, strict=False))
    stats = {
        c: {"min": numeric_min[i], "max": numeric_max[i]}
        for i, c in enumerate(columns)
        if types[i] in ("int", "float") and numeric_min[i] is not None
    }
    table = Table(name=PurePosixPath(ctx.file_name).stem, columns=columns, rows=sample, row_count=count, types=col_types)
    features: dict[str, Any] = {"columns": len(columns), "rows": count}
    preview = _series_preview(ctx.file_name, columns, types, keep)
    if preview is not None:
        features["series"] = True
    text: list[TextSection] = []
    if ctx.size <= RAG_MAX_BYTES:
        text.append(TextSection(ctx.read_text(RAG_MAX_BYTES)))
    return ExtractionResult(
        metadata={"delimiter": delimiter, "header": has_header, "columns": columns, "stats": stats},
        features=features,
        tables=[table],
        text=text,
        preview=preview,
        summary=f"Table, {count:,} rows x {len(columns)} columns" + (" (a series)" if preview else ""),
    )


def _series_preview(name: str, columns: list[str], types: list[str], rows: list[list[str]]) -> dict[str, Any] | None:
    if len(columns) < 2 or not rows:
        return None
    x_kind = types[0]
    if x_kind not in ("datetime", "int", "float"):
        return None
    ys = [i for i, t in enumerate(types[1:], start=1) if t in ("int", "float")][:8]
    if not ys:
        return None
    xs: list[Any] = []
    for r in rows:
        if not r:
            xs.append(None)
            continue
        if x_kind == "datetime":
            t = _parse_time(r[0])
            xs.append(t.isoformat() if t else None)
        else:
            xs.append(_num(r[0]))
    if x_kind != "datetime":
        clean = [x for x in xs if x is not None]
        if any(b < a for a, b in zip(clean, clean[1:], strict=False)):
            return None  # not a series axis
    series = []
    for i in ys:
        pts = [[x, _num(r[i]) if i < len(r) else None] for x, r in zip(xs, rows, strict=False) if x is not None]
        series.append({"name": columns[i], "kind": "line", "points": pts[:CHART_POINTS]})
    return {
        "type": "chart",
        "title": PurePosixPath(name).stem,
        "x": {"label": columns[0], "scale": "time" if x_kind == "datetime" else "linear"},
        "y": {"label": ", ".join(columns[i] for i in ys[:3]) + ("…" if len(ys) > 3 else ""), "scale": "linear"},
        "series": series,
    }


def parquet(ctx: ExtractionContext) -> ExtractionResult:
    try:
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise ExtractorUnavailable("pyarrow is not installed (pip install pyarrow)") from exc
    pf = pq.ParquetFile(str(ctx.path))
    schema = pf.schema_arrow
    columns = [f.name for f in schema]
    types = {f.name: str(f.type) for f in schema}
    rows: list[list[Any]] = []
    if pf.metadata.num_row_groups:
        batch = next(pf.iter_batches(batch_size=SAMPLE_ROWS), None)
        if batch is not None:
            rows = [list(r.values()) for r in batch.to_pylist()]
    meta = {
        "columns": columns,
        "row_groups": pf.metadata.num_row_groups,
        "created_by": pf.metadata.created_by,
        "key_value_metadata": {
            k.decode(errors="replace"): v.decode(errors="replace")[:200]
            for k, v in (schema.metadata or {}).items()
            if not k.startswith(b"ARROW:")
        },
    }
    count = pf.metadata.num_rows
    return ExtractionResult(
        metadata=meta,
        features={"columns": len(columns), "rows": count},
        tables=[Table(name=PurePosixPath(ctx.file_name).stem, columns=columns, rows=rows, row_count=count, types=types)],
        summary=f"Parquet, {count:,} rows x {len(columns)} columns",
    )


def read_csv_text(data: str) -> list[list[str]]:
    """Small helper for tests and previewers."""
    return list(csv.reader(io.StringIO(data)))


__all__ = ["delimited", "parquet", "read_csv_text"]
