# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""EC screening: severity classification + screen-on-all-tiers.

Screening must run regardless of target tier (the old code only screened the
community tier, so restricted-tier ingests were never checked), must catch the
nuclear export-control + vendor-proprietary markers that matter here (10 CFR
810, EAR/ECCN, ITAR, proprietary/licensed), and must REJECT true export-
controlled content rather than route it to the restricted tier — because the
restricted tier on a non-EC-authorized node is not a safe home for it either.
"""

from __future__ import annotations

from axiom.rag.ec_screening import screen_document


def test_clean_published_paper_allowed_to_community():
    r = screen_document(
        "papers/ans_2025.pdf",
        "A neutronics benchmark study presented at ANS 2025. Cleared for public release.",
        target_corpus="rag-community",
    )
    assert r.severity == "none"
    assert r.allowed_community is True
    assert r.recommendation == "community"


def test_export_controlled_information_is_rejected_not_routed():
    r = screen_document(
        "doc.pdf",
        "This document contains Export Controlled Information under 10 CFR Part 810.",
        target_corpus="rag-community",
    )
    assert r.severity == "controlled"
    assert r.allowed_community is False
    assert r.allowed_restricted is False  # never on a non-EC node
    assert r.recommendation == "reject"


def test_10cfr810_detected_even_when_targeting_org():
    # The old code skipped screening entirely for non-community tiers.
    r = screen_document(
        "x.pdf",
        "Transfer of this technology is subject to 10 CFR 810 authorization.",
        target_corpus="rag-org",
    )
    assert r.severity == "controlled"
    assert r.recommendation == "reject"


def test_vendor_proprietary_license_is_controlled():
    r = screen_document(
        "GA - License Agreement.pdf",
        "GENERAL ATOMICS PROPRIETARY INFORMATION. Use subject to the executed license agreement.",
        target_corpus="rag-org",
    )
    assert r.severity == "controlled"
    assert r.allowed_restricted is False


def test_ear_eccn_detected():
    r = screen_document(
        "x.pdf",
        "ECCN 0E001. Subject to the Export Administration Regulations (EAR).",
        target_corpus="rag-community",
    )
    assert r.severity == "controlled"


def test_itar_detected():
    r = screen_document(
        "x.pdf",
        "This technical data is controlled under ITAR (22 CFR 120-130, USML Category VI).",
        target_corpus="rag-community",
    )
    assert r.severity == "controlled"


def test_ouo_is_sensitive_review_not_reject():
    r = screen_document(
        "x.pdf",
        "OFFICIAL USE ONLY - SECURITY RELATED",
        target_corpus="rag-community",
    )
    assert r.severity == "sensitive"
    assert r.allowed_community is False
    assert r.recommendation == "review"


def test_filename_marker_still_flags():
    r = screen_document(
        "Reactor_Manual_OUO.pdf",
        "ordinary operational text with no content markings",
        target_corpus="rag-community",
    )
    assert r.markers_found
    assert r.allowed_community is False
