# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""OCR fork for the PDF extraction pipeline.

The DP-1 stand-up exposed the gap: scanned PDFs (Shayan's CRISP
Literature folder) yield zero text from ``pdftotext`` + ``pypdf`` and
then drop out of the corpus silently. ``looks_like_scanned_pdf`` +
:func:`extract_pdf_with_ocr_fallback` close that path.

This module owns the *fallback shape* (detection + dispatch). The
actual OCR backend is injectable via an :class:`OcrEngine` callable
— the default :class:`TesseractEngine` lazily imports pytesseract +
pypdfium2 so the rest of the RAG layer doesn't depend on them.

OcrResult carries the provenance fields that survive into bronze and
silver:

- ``engine`` — e.g. ``tesseract``, ``mineru``, ``aws-textract``
- ``page_count`` — rasterized page count
- ``confidence`` — engine-reported per-document confidence (0.0–1.0)

Silver-tier de-quarantine / re-screen will read these to decide
whether to elevate OCR'd content into served tiers.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

log = logging.getLogger(__name__)


# Threshold below which we treat the native-extracted text as
# essentially empty and try OCR. Tunable; lit on tonight's CRISP
# Literature sample — pdftotext on scanned 1970s reactor reports
# typically leaks <50 chars of stray glyphs.
_NEAR_EMPTY_CHARS = 100


@dataclass(frozen=True)
class OcrResult:
    """One pass of OCR over one document.

    ``engine`` is the literal value 'native' when the native extractor
    succeeded; otherwise the OCR engine's name.
    """

    text: str
    engine: str
    page_count: int
    confidence: float


class OcrEngine(Protocol):
    """Callable that runs OCR on a PDF file and returns the result."""

    def __call__(self, pdf_path: Path) -> OcrResult: ...


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def looks_like_scanned_pdf(native_text: str | None) -> bool:
    """Heuristic: did the native extractor produce real text?

    Returns ``True`` if the text is empty or short enough that OCR is
    likely worthwhile. ``False`` if native extraction already produced
    a substantial body of text.
    """
    if not native_text:
        return True
    return len(native_text.strip()) < _NEAR_EMPTY_CHARS


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def extract_pdf_with_ocr_fallback(
    pdf_path: Path,
    *,
    native_extract: Callable[[Path], str | None],
    ocr_engine: OcrEngine | None,
) -> OcrResult | None:
    """Extract text from a PDF, falling back to OCR when native is empty.

    Order:
    1. Run ``native_extract(pdf_path)``. If it yields substantial text
       (``looks_like_scanned_pdf`` is False), wrap in OcrResult with
       ``engine="native"`` and return.
    2. Otherwise, if ``ocr_engine`` is provided, run it. Return its
       OcrResult.
    3. If OCR returns empty text or no engine is available, return
       ``None``.
    """
    native = native_extract(pdf_path)
    if not looks_like_scanned_pdf(native):
        assert native is not None  # type narrowing
        return OcrResult(
            text=native,
            engine="native",
            page_count=0,  # unknown to native path
            confidence=1.0,
        )

    if ocr_engine is None:
        return None

    try:
        result = ocr_engine(pdf_path)
    except Exception as exc:  # noqa: BLE001
        log.warning("OCR engine failed on %s: %s", pdf_path, exc)
        return None

    if not result.text.strip():
        return None
    return result


# ---------------------------------------------------------------------------
# Default Tesseract engine (lazy-imported)
# ---------------------------------------------------------------------------


class TesseractEngine:
    """Default OCR backend — tesseract via pytesseract, pages rendered
    by pypdfium2.

    Lazy-imports both libraries so importing :mod:`axiom.rag.ocr` is
    cheap and doesn't require tesseract on the system. Construction is
    free; the imports happen on first call.

    Suitable for the laptop tier. For batch-tier OCR (cluster, large
    corpora), wrap a different engine into the same Protocol — the
    extractor doesn't care.
    """

    def __init__(self, *, dpi: int = 200) -> None:
        self.dpi = dpi

    def __call__(self, pdf_path: Path) -> OcrResult:
        try:
            import pypdfium2 as pdfium
            import pytesseract
        except ImportError as exc:  # pragma: no cover — env guard
            raise RuntimeError(
                "OCR fallback requires `pypdfium2` and `pytesseract`. "
                "Install with: pip install pypdfium2 pytesseract"
            ) from exc

        pdf = pdfium.PdfDocument(str(pdf_path))
        try:
            pages: list[str] = []
            confidences: list[float] = []
            for page in pdf:
                # Render at DPI and OCR
                pil = page.render(scale=self.dpi / 72).to_pil()
                # image_to_data yields per-word confidences;
                # average them for a per-page number.
                data = pytesseract.image_to_data(
                    pil, output_type=pytesseract.Output.DICT,
                )
                words = [
                    w for w, c in zip(data.get("text", []), data.get("conf", []))
                    if w.strip() and _safe_float(c) > 0
                ]
                pages.append(" ".join(words))
                conf_vals = [_safe_float(c) for c in data.get("conf", [])
                             if _safe_float(c) > 0]
                if conf_vals:
                    confidences.append(sum(conf_vals) / len(conf_vals) / 100.0)
            text = "\n\n".join(pages)
            overall = sum(confidences) / len(confidences) if confidences else 0.0
            return OcrResult(
                text=text,
                engine="tesseract",
                page_count=len(pdf),
                confidence=overall,
            )
        finally:
            pdf.close()


def _safe_float(x: object) -> float:
    try:
        return float(x)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def ocr_provenance_extra(result: OcrResult | None) -> dict[str, str]:
    """Map an :class:`OcrResult` into the ``FetchedItem.extra`` shape.

    The bronze writer preserves ``FetchedItem.extra`` verbatim in the
    sidecar manifest, so any source-specific provenance carries to
    silver without a schema change. This helper formats OCR
    provenance into that dict:

    - ``extracted_by`` — ``native`` / ``<engine>`` / ``failed``
    - ``ocr_engine`` — only present when OCR was used
    - ``ocr_page_count`` — string, only when OCR was used
    - ``ocr_confidence`` — string, three decimals, only when OCR was used

    Silver-tier de-quarantine reads these fields to decide whether to
    elevate an OCR'd document into served tiers (a low-confidence
    scan may need human review before promotion).
    """
    if result is None:
        return {"extracted_by": "failed"}
    if result.engine == "native":
        return {"extracted_by": "native"}
    return {
        "extracted_by": result.engine,
        "ocr_engine": result.engine,
        "ocr_page_count": str(result.page_count),
        "ocr_confidence": f"{result.confidence:.3f}",
    }


__all__ = [
    "OcrResult",
    "OcrEngine",
    "TesseractEngine",
    "looks_like_scanned_pdf",
    "extract_pdf_with_ocr_fallback",
    "ocr_provenance_extra",
]
