# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""OCR fork in PDF extraction.

The DP-1 stand-up exposed this gap: CRISP Literature / scanned PDFs
silently return zero text from pdftotext + pypdf, then drop out of
the corpus invisibly. The OCR fork detects 'PDF yielded near-zero
text' and routes through an :class:`OcrEngine` (tesseract by default).

Tests inject a fake engine so they don't require tesseract installed.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from axiom.rag.ocr import (
    OcrResult,
    extract_pdf_with_ocr_fallback,
    looks_like_scanned_pdf,
)


@dataclass
class _FakeOcr:
    """Inject for tests; pretends to OCR and return canned text."""
    text: str = "extracted by OCR"
    page_count: int = 3
    confidence: float = 0.85

    def __call__(self, pdf_path: Path) -> OcrResult:
        return OcrResult(
            text=self.text,
            engine="fake-ocr",
            page_count=self.page_count,
            confidence=self.confidence,
        )


# -- detection ---------------------------------------------------------------


def test_looks_like_scanned_when_native_text_is_empty():
    assert looks_like_scanned_pdf("") is True
    assert looks_like_scanned_pdf(None) is True


def test_looks_like_scanned_when_text_is_near_empty():
    """Some scanned PDFs leak a few stray glyphs; treat <100 chars as scanned."""
    assert looks_like_scanned_pdf("garbled  glyphs only" * 2) is True


def test_does_not_look_like_scanned_when_text_is_substantial():
    assert looks_like_scanned_pdf("a" * 5000) is False


# -- fallback dispatch -------------------------------------------------------


def test_extract_pdf_with_ocr_fallback_uses_native_when_present(tmp_path):
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"%PDF-fake")
    fake_ocr = _FakeOcr()

    def fake_native(path):
        return "a" * 5000   # substantial native text

    result = extract_pdf_with_ocr_fallback(
        f, native_extract=fake_native, ocr_engine=fake_ocr,
    )
    assert result.text.startswith("aaa")
    assert result.engine == "native"
    # OCR was NOT invoked
    assert "OCR" not in result.text


def test_extract_pdf_with_ocr_fallback_routes_to_ocr_on_empty_native(tmp_path):
    f = tmp_path / "scan.pdf"
    f.write_bytes(b"%PDF-fake")
    fake_ocr = _FakeOcr(text="page 1\npage 2\npage 3", page_count=3)

    result = extract_pdf_with_ocr_fallback(
        f, native_extract=lambda p: None, ocr_engine=fake_ocr,
    )
    assert result.text == "page 1\npage 2\npage 3"
    assert result.engine == "fake-ocr"
    assert result.page_count == 3


def test_extract_pdf_with_ocr_fallback_routes_on_near_empty_native(tmp_path):
    f = tmp_path / "scan.pdf"
    f.write_bytes(b"%PDF-fake")
    fake_ocr = _FakeOcr(text="OCR result")

    result = extract_pdf_with_ocr_fallback(
        f, native_extract=lambda p: "stray   glyph",  # < 100 chars
        ocr_engine=fake_ocr,
    )
    assert result.engine == "fake-ocr"


def test_extract_pdf_with_ocr_fallback_returns_none_when_both_fail(tmp_path):
    f = tmp_path / "broken.pdf"
    f.write_bytes(b"%PDF-broken")

    def failing_ocr(path):
        return OcrResult(text="", engine="fake-ocr", page_count=0, confidence=0.0)

    result = extract_pdf_with_ocr_fallback(
        f, native_extract=lambda p: None, ocr_engine=failing_ocr,
    )
    assert result is None


def test_extract_pdf_with_ocr_fallback_handles_ocr_engine_absent(tmp_path):
    """If no OCR engine is configured and native fails, return None — don't crash."""
    f = tmp_path / "scan.pdf"
    f.write_bytes(b"%PDF-fake")
    result = extract_pdf_with_ocr_fallback(
        f, native_extract=lambda p: None, ocr_engine=None,
    )
    assert result is None


# -- OcrResult shape ---------------------------------------------------------


def test_ocr_result_carries_provenance_fields():
    r = OcrResult(text="x", engine="tesseract", page_count=12, confidence=0.92)
    assert r.engine == "tesseract"
    assert r.page_count == 12
    assert 0.0 <= r.confidence <= 1.0
