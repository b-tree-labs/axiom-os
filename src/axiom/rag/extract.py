# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Text extraction from PDF, DOCX, PPTX, and ODT files.

Converts binary document formats to plain text for RAG chunking.
Uses stdlib + minimal deps: python-docx for DOCX, pdftotext CLI for PDF.

This module is the "connector" layer — the only part that changes when
moving from local filesystem to S3/data-lake sources.
"""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

#: ``.py`` is here so a corpus can answer questions about how code works, not
#: only about what its docs claim. It is read as text and chunked at symbol
#: boundaries by :mod:`axiom.rag.python_chunker`; prose chunking would cut
#: functions in half.
SUPPORTED_EXTENSIONS = {
    ".pdf", ".docx", ".pptx", ".odt", ".txt", ".md", ".xlsx", ".doc", ".py",
}


def extract_text(path: Path) -> str | None:
    """Extract plain text from a document file.

    Returns None if extraction fails or format is unsupported.
    """
    suffix = path.suffix.lower()

    if suffix in (".md", ".txt", ".py"):
        return _read_text_file(path)
    elif suffix == ".pdf":
        return _extract_pdf(path)
    elif suffix == ".docx":
        return _extract_docx(path)
    elif suffix == ".pptx":
        return _extract_pptx(path)
    elif suffix == ".odt":
        return _extract_odt(path)
    elif suffix == ".xlsx":
        return _extract_xlsx(path)
    elif suffix == ".doc":
        return _extract_doc(path)
    else:
        log.debug("Unsupported format: %s", suffix)
        return None


def _read_text_file(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        log.warning("Failed to read %s: %s", path, e)
        return None


#: A line of a two-column page read straight across has a wide run of spaces
#: between the two columns' text. One such line proves nothing — a table has
#: them too — so the decision is made on the proportion of lines that do.
_GUTTER = re.compile(r"\S {6,}\S")

#: A gutter alone does not distinguish two spliced columns from a table, and
#: the first version of this got that wrong: every row of a three-column table
#: matched, so a table-heavy page scored as multi-column and would have been
#: re-extracted in reading order — throwing away the one thing ``-layout`` is
#: for.
#:
#: Length is what separates them. Two columns read across fill the page: on the
#: paper this was measured against, the median line was 150 characters. A table
#: row is a fraction of that, because a table is narrower than its page.
_SPLICED_MIN_LEN = 100

#: Above this share of gutter lines, ``-layout`` is reproducing a multi-column
#: page rather than a table, and reading order is the better extraction.
_MULTICOLUMN_SHARE = 0.30

#: A row of a real table has a gap between each of ITS columns, so three or
#: more. A line of two page-columns read across has exactly one, between them.
#: That difference is what lets a table be recovered from a layout pass whose
#: prose is unusable.
_TABLE_GAP = re.compile(r"\S {2,}\S")
_TABLE_MIN_GAPS = 3


def _spliced_share(text: str) -> float:
    """How much of ``text`` looks like two columns read across the page."""
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return 0.0
    spliced = sum(
        1
        for line in lines
        if len(line) >= _SPLICED_MIN_LEN and _GUTTER.search(line)
    )
    return spliced / len(lines)


def _pdftotext(path: Path, *, layout: bool) -> str | None:
    args = ["pdftotext"] + (["-layout"] if layout else []) + [str(path), "-"]
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=60)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout
    except FileNotFoundError:
        log.debug("pdftotext not installed — install poppler-utils")
    except subprocess.TimeoutExpired:
        log.warning("pdftotext timed out on %s", path)
    except Exception as e:  # noqa: BLE001 — extraction never takes the caller down
        log.warning("PDF extraction failed for %s: %s", path, e)
    return None


def _recovered_tables(laid_out: str) -> str:
    """Table rows rescued from a layout pass whose prose was unusable.

    Reading order fixes two-column prose and costs multi-column tables: it
    emits a table's row labels as a flat list and its values as another, so
    "what is the modeled value for X" stops being answerable — or worse, is
    answered from an unlabelled sequence.

    Measured on a two-column reactor paper: the layout pass held
    ``keff  1.00165  1.00000  165 pcm`` with its columns intact, and reading
    order held ``keff`` and ``1.00165`` in different places with nothing
    joining them.

    The rows are separable because of what the gaps mean. A real table row has
    a gap between each of its own columns, so three or more; a line of two page
    columns read across has exactly one, between them. On that paper the test
    picked out five lines, and they were the table.

    A recovered row keeps whatever fragment of the neighbouring column was
    spliced onto its end. That is noise, and it is worth it: the noise sits
    after the row, while the alternative severs a value from the parameter it
    belongs to.
    """
    rows = [
        line.rstrip()
        for line in laid_out.splitlines()
        if len(_TABLE_GAP.findall(line)) >= _TABLE_MIN_GAPS
    ]
    if not rows:
        return ""
    return (
        "\n\n[table rows recovered from the page layout]\n" + "\n".join(rows) + "\n"
    )


def _extract_pdf_native(path: Path) -> str | None:
    """Native PDF text extraction: pdftotext → pypdf. Returns ``None``
    when neither yields substantial text (caller decides whether to
    route through OCR).

    ``-layout`` reproduces the page as it looks, which keeps a table's columns
    aligned and is why it was the only mode used. On a **two-column** document
    it does the same thing and the result is unreadable: it reads straight
    across the gutter, splicing a sentence from the left column into an
    unrelated one from the right and truncating what does not fit.

    Measured on a real two-column reactor paper: 60% of non-empty lines spliced,
    184 of them ending mid-word. A chunk like that is not merely noisy, it is
    *plausible* — it reads as prose, so a model answers from it confidently and
    the answer is a splice of two unrelated sentences.

    Most technical papers and many manuals are two-column, so this was the
    common case rather than the corner. Now the layout pass is scored, and a
    page that turns out to be multi-column is re-extracted in reading order,
    which is what ``pdftotext`` does without the flag.
    """
    laid_out = _pdftotext(path, layout=True)
    if laid_out is not None:
        share = _spliced_share(laid_out)
        if share < _MULTICOLUMN_SHARE:
            return laid_out
        reading_order = _pdftotext(path, layout=False)
        if reading_order is not None and reading_order.strip():
            log.debug(
                "%s: %.0f%% of laid-out lines spliced across a gutter; using "
                "reading order instead",
                path.name,
                share * 100,
            )
            return reading_order + _recovered_tables(laid_out)
        return laid_out

    # Fallback: try pypdf if available
    try:
        from pypdf import PdfReader
        reader = PdfReader(str(path))
        pages = [page.extract_text() or "" for page in reader.pages]
        text = "\n\n".join(pages)
        if text.strip():
            return text
    except ImportError:
        pass
    except Exception as e:
        log.warning("pypdf fallback failed for %s: %s", path, e)

    return None


def _extract_pdf(path: Path) -> str | None:
    """Extract text from a PDF, falling back to OCR for scanned docs.

    The OCR engine is opt-in via the ``AXIOM_RAG_OCR_ENABLED=1``
    env var so the rest of the test/CI matrix doesn't drag in
    pytesseract+pypdfium2. When enabled and the native path yields
    no usable text (``looks_like_scanned_pdf``), the document is
    routed through :class:`axiom.rag.ocr.TesseractEngine`.

    Provenance: this entry point returns plain text for back-compat;
    the structured ``OcrResult`` (engine, page_count, confidence) is
    available via :func:`extract_pdf_with_provenance` for the bronze
    writer's downstream consumption.
    """
    result = extract_pdf_with_provenance(path)
    return result.text if result else None


def extract_pdf_with_provenance(path: Path):
    """Returns ``OcrResult`` (text + engine + page_count + confidence)
    or ``None``. The bronze writer should use this entry point so the
    OCR provenance fields survive into the document record."""
    import os

    from axiom.rag.ocr import (
        TesseractEngine,
        extract_pdf_with_ocr_fallback,
    )

    engine = None
    if os.environ.get("AXIOM_RAG_OCR_ENABLED", "").lower() in {"1", "true", "yes"}:
        engine = TesseractEngine()

    return extract_pdf_with_ocr_fallback(
        path, native_extract=_extract_pdf_native, ocr_engine=engine,
    )


def _extract_docx(path: Path) -> str | None:
    """Extract text from Word documents using python-docx."""
    try:
        from docx import Document
        doc = Document(str(path))
        paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
        if paragraphs:
            return "\n\n".join(paragraphs)
    except ImportError:
        log.warning("python-docx not installed — pip install python-docx")
    except Exception as e:
        log.warning("DOCX extraction failed for %s: %s", path, e)
    return None


def _extract_pptx(path: Path) -> str | None:
    """Extract text from PowerPoint files using python-pptx."""
    try:
        from pptx import Presentation
        prs = Presentation(str(path))
        slides = []
        for i, slide in enumerate(prs.slides, 1):
            texts = []
            for shape in slide.shapes:
                if shape.has_text_frame:
                    for para in shape.text_frame.paragraphs:
                        text = para.text.strip()
                        if text:
                            texts.append(text)
            if texts:
                slides.append(f"--- Slide {i} ---\n" + "\n".join(texts))
        if slides:
            return "\n\n".join(slides)
    except ImportError:
        log.warning("python-pptx not installed — pip install python-pptx")
    except Exception as e:
        log.warning("PPTX extraction failed for %s: %s", path, e)
    return None


def _extract_odt(path: Path) -> str | None:
    """Extract text from ODT files using zip + XML parsing."""
    import xml.etree.ElementTree as ET
    import zipfile

    try:
        with zipfile.ZipFile(str(path)) as zf, zf.open("content.xml") as f:
            tree = ET.parse(f)
        # Strip all XML tags, keep text
        text = ET.tostring(tree.getroot(), encoding="unicode", method="text")
        if text.strip():
            return text
    except Exception as e:
        log.warning("ODT extraction failed for %s: %s", path, e)
    return None


def _extract_xlsx(path: Path) -> str | None:
    """Extract text from Excel spreadsheets using openpyxl."""
    try:
        import openpyxl
        wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
        sheets = []
        for sheet in wb.worksheets:
            rows = []
            for row in sheet.iter_rows(values_only=True):
                cells = [str(c) for c in row if c is not None]
                if cells:
                    rows.append("\t".join(cells))
            if rows:
                sheets.append(f"--- Sheet: {sheet.title} ---\n" + "\n".join(rows))
        wb.close()
        if sheets:
            return "\n\n".join(sheets)
    except ImportError:
        log.warning("openpyxl not installed — pip install openpyxl")
    except Exception as e:
        log.warning("XLSX extraction failed for %s: %s", path, e)
    return None


def _extract_doc(path: Path) -> str | None:
    """Extract text from legacy .doc files via antiword CLI (best-effort)."""
    try:
        result = subprocess.run(
            ["antiword", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout
    except FileNotFoundError:
        log.debug("antiword not installed — .doc files will be skipped")
    except subprocess.TimeoutExpired:
        log.warning("antiword timed out on %s", path)
    except Exception as e:
        log.warning("DOC extraction failed for %s: %s", path, e)
    return None


def extract_directory(
    root: Path,
    output_dir: Path | None = None,
) -> dict[str, str]:
    """Extract text from all supported files in a directory tree.

    Returns dict of {relative_path: extracted_text}.
    If output_dir is set, also writes .txt files there.
    """
    results: dict[str, str] = {}
    supported = 0
    extracted = 0
    failed = 0

    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            continue
        if path.name.startswith(".") or "__MACOSX" in str(path):
            continue

        supported += 1
        rel = str(path.relative_to(root))
        text = extract_text(path)

        if text and text.strip():
            results[rel] = text
            extracted += 1

            if output_dir:
                out_path = output_dir / (rel + ".txt")
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_text(text, encoding="utf-8")
        else:
            failed += 1
            log.info("No text extracted from %s", rel)

    log.info(
        "Extraction complete: %d supported, %d extracted, %d failed",
        supported, extracted, failed,
    )
    return results
