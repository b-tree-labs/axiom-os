# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""PDF and office documents: text through the retrieval extractor every connector uses, plus their facts."""

from __future__ import annotations

import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from axiom.extensions.builtins.library.extract.contract import (
    ExtractionContext,
    ExtractionResult,
    ExtractorUnavailable,
    Table,
    TextSection,
)


def _named_copy(ctx: ExtractionContext, td: str) -> Path:
    # The stored blob is named by its checksum; the shared extractor routes by
    # suffix, so it reads a link (or copy) carrying the original name.
    named = Path(td) / PurePosixPath(ctx.file_name).name
    try:
        named.symlink_to(ctx.path.resolve())
    except OSError:
        named.write_bytes(ctx.path.read_bytes())
    return named


def _text(ctx: ExtractionContext) -> str:
    from axiom.rag.extract import extract_text

    with tempfile.TemporaryDirectory(prefix="library-extract-") as td:
        return (extract_text(_named_copy(ctx, td)) or "").replace("\x00", " ")


def pdf(ctx: ExtractionContext) -> ExtractionResult:
    meta: dict[str, Any] = {}
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(ctx.path))
        meta["pages"] = len(reader.pages)
        info = reader.metadata or {}
        for key, name in (("/Title", "title"), ("/Author", "author"), ("/Subject", "subject"), ("/Producer", "producer"), ("/CreationDate", "created")):
            value = info.get(key) if hasattr(info, "get") else None
            if value:
                meta[name] = str(value)[:200]
        meta["encrypted"] = bool(reader.is_encrypted)
    except ImportError:
        pass
    text = _text(ctx)
    warnings = [] if text.strip() else ["no text layer; a scanned PDF needs OCR to be searchable"]
    meta["chars"] = len(text)
    pages = meta.get("pages")
    return ExtractionResult(
        metadata=meta,
        features={"has_text": bool(text.strip())},
        text=[TextSection(text)] if text.strip() else [],
        warnings=warnings,
        summary=f"PDF, {pages} page{'s' if pages != 1 else ''}" if pages else "PDF",
    )


def office(ctx: ExtractionContext) -> ExtractionResult:
    """Word, PowerPoint, OpenDocument text and legacy .doc: text plus core properties."""
    suffix = PurePosixPath(ctx.file_name.lower()).suffix
    meta: dict[str, Any] = {"format": suffix.lstrip(".")}
    try:
        if suffix == ".docx":
            import docx

            d = docx.Document(str(ctx.path))
            cp = d.core_properties
            meta.update({k: v for k, v in {"title": cp.title, "author": cp.author, "subject": cp.subject}.items() if v})
            meta["paragraphs"] = len(d.paragraphs)
            meta["tables"] = len(d.tables)
            meta["headings"] = [p.text for p in d.paragraphs if p.style is not None and str(p.style.name).startswith("Heading")][:50]
        elif suffix == ".pptx":
            import pptx

            deck = pptx.Presentation(str(ctx.path))
            meta["slides"] = len(deck.slides)
            cp = deck.core_properties
            meta.update({k: v for k, v in {"title": cp.title, "author": cp.author}.items() if v})
    except ImportError:
        pass
    text = _text(ctx)
    unit = "slides" if "slides" in meta else "paragraphs" if "paragraphs" in meta else ""
    return ExtractionResult(
        metadata=meta,
        text=[TextSection(text)] if text.strip() else [],
        summary=f"{suffix.lstrip('.').upper()} document" + (f", {meta[unit]} {unit}" if unit else ""),
    )


def workbook(ctx: ExtractionContext, *, sample_rows: int = 50) -> ExtractionResult:
    """Excel: every sheet as a table (bounded sample, true row count), and text for retrieval."""
    try:
        import openpyxl
    except ImportError as exc:
        raise ExtractorUnavailable("openpyxl is not installed (pip install openpyxl)") from exc
    wb = openpyxl.load_workbook(str(ctx.path), read_only=True, data_only=True)
    tables: list[Table] = []
    lines: list[str] = []
    try:
        for ws in wb.worksheets:
            rows_iter = ws.iter_rows(values_only=True)
            header = next(rows_iter, None)
            if header is None:
                continue
            columns = [str(c) if c is not None else f"column {i + 1}" for i, c in enumerate(header)]
            sample: list[list[Any]] = []
            count = 0
            for row in rows_iter:
                count += 1
                if len(sample) < sample_rows:
                    sample.append(list(row)[: len(columns)] + [None] * max(0, len(columns) - len(row)))
            types = _column_types(columns, sample)
            tables.append(Table(name=ws.title, columns=columns, rows=sample, row_count=count, types=types))
            lines.append(f"## {ws.title}\n\n" + " | ".join(columns))
            lines.extend(" | ".join("" if v is None else str(v) for v in r) for r in sample[:20])
    finally:
        wb.close()
    return ExtractionResult(
        metadata={"sheets": [t.name for t in tables]},
        tables=tables,
        text=[TextSection("\n".join(lines))] if lines else [],
        summary=f"Workbook, {len(tables)} sheet{'s' if len(tables) != 1 else ''}",
    )


def _column_types(columns: list[str], rows: list[list[Any]]) -> dict[str, str]:
    from datetime import date, datetime

    out: dict[str, str] = {}
    for i, col in enumerate(columns):
        seen = {type(r[i]).__name__ for r in rows if i < len(r) and r[i] is not None}
        if not seen:
            out[col] = "empty"
        elif seen <= {"int"}:
            out[col] = "int"
        elif seen <= {"int", "float"}:
            out[col] = "float"
        elif seen <= {datetime.__name__, date.__name__}:
            out[col] = "datetime"
        elif seen == {"bool"}:
            out[col] = "bool"
        else:
            out[col] = "str"
    return out


__all__ = ["office", "pdf", "workbook"]
