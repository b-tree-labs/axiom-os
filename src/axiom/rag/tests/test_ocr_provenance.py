# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for ``ocr_provenance_extra`` — the helper that maps an
``OcrResult`` into the ``FetchedItem.extra`` shape so the bronze writer
preserves the engine/page/confidence triplet without schema changes.
"""

from __future__ import annotations

from axiom.rag.ocr import OcrResult, ocr_provenance_extra


def test_native_extraction_has_engine_native_no_ocr_fields():
    r = OcrResult(text="lots of text", engine="native",
                  page_count=0, confidence=1.0)
    out = ocr_provenance_extra(r)
    assert out == {"extracted_by": "native"}


def test_tesseract_extraction_carries_engine_pages_confidence():
    r = OcrResult(text="OCR'd content",
                  engine="tesseract",
                  page_count=12,
                  confidence=0.83)
    out = ocr_provenance_extra(r)
    assert out["extracted_by"] == "tesseract"
    assert out["ocr_engine"] == "tesseract"
    assert out["ocr_page_count"] == "12"
    assert out["ocr_confidence"] == "0.830"


def test_handles_none_result_gracefully():
    """When extraction failed entirely, return a blank shape so the
    bronze record still has a structured 'we tried' marker."""
    out = ocr_provenance_extra(None)
    assert out == {"extracted_by": "failed"}


def test_confidence_formatting_is_stable():
    """Three decimals so silver-tier promotion thresholds compare
    consistently across runs."""
    for conf, expected in [(0.5, "0.500"), (0.823456, "0.823"), (1.0, "1.000")]:
        r = OcrResult(text="x", engine="tesseract",
                      page_count=1, confidence=conf)
        assert ocr_provenance_extra(r)["ocr_confidence"] == expected
