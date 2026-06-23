# Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Phase 4a — axiom.compute.agree: signed agreement-receipt primitive.

Per Twin Toolkit Demo Spec §5.3 (Seam E) + ADR-017.

The `agree` primitive quantifies how much two computational results match
(or how much a result matches a published reference) and emits a signed
agreement receipt that itself becomes evidence in the chain.

Phase 4a scope:
- AgreementSpec / AgreementResult dataclasses
- agree(spec, subject_value, target_value, target_uncertainty) primitive
- Metric: absolute_pcm (k-eff comparisons in pcm)
- Tolerance source: literal | reference_uncertainty
- Returns signed agreement receipt; verify_signature round-trips
- Three axes supported by metric/source — A3 (reference), A4 (cross-receipt),
  A5 (sensor) — Phase 4a tests focus on A3 + A4

The agreement receipt URI scheme is `axiom://agree/sha256:...` per ADR-017.
"""

from __future__ import annotations

import pytest

from axiom.compute.agree import (
    AgreementSpec,
    AgreementResult,
    agree,
)
from axiom.compute import verify_signature


def test_agreement_within_tolerance_a3_reference():
    """Subject within reference uncertainty → within_tolerance=True."""
    spec = AgreementSpec(
        axis="A3",
        subject_receipt_uri="axiom://compute/sha256:" + "a" * 64,
        target="reference://nrc-ml2327/triga-netl-P1A/k-eff",
        metric="absolute_pcm",
        tolerance_source="reference_uncertainty",
    )
    # Subject k-eff = 1.00203, reference k-eff = 1.00203 (perfect match)
    result = agree(
        spec,
        subject_value=1.00203,
        target_value=1.00203,
        target_uncertainty=0.00050,
    )

    assert isinstance(result, AgreementResult)
    assert result.delta_value == pytest.approx(0.0, abs=1e-6)
    assert result.within_tolerance is True
    assert result.tolerance_value == pytest.approx(50.0, abs=0.01)  # 0.0005 * 100000 = 50 pcm
    assert result.uri.startswith("axiom://agree/sha256:")


def test_agreement_outside_tolerance_a3_reference():
    """Subject outside reference uncertainty → within_tolerance=False."""
    spec = AgreementSpec(
        axis="A3",
        subject_receipt_uri="axiom://compute/sha256:" + "b" * 64,
        target="reference://nrc-ml2327/triga-netl-P1A/k-eff",
        metric="absolute_pcm",
        tolerance_source="reference_uncertainty",
    )
    # Subject k-eff = 1.00500, reference = 1.00203 ± 0.00050 → +297 pcm offset
    result = agree(
        spec,
        subject_value=1.00500,
        target_value=1.00203,
        target_uncertainty=0.00050,
    )

    assert result.delta_value == pytest.approx(297.0, abs=1.0)
    assert result.within_tolerance is False


def test_agreement_a4_cross_validator_with_literal_tolerance():
    """A4: subject vs another compute receipt, with explicit tolerance."""
    spec = AgreementSpec(
        axis="A4",
        subject_receipt_uri="axiom://compute/sha256:" + "c" * 64,
        target="receipt://axiom/sha256:" + "d" * 64,  # MPACT result for same case
        metric="absolute_pcm",
        tolerance_source="literal",
        tolerance_value=200.0,  # 200 pcm cross-code agreement bar
    )
    # OpenMC=1.00342, MPACT=1.00350 → -8 pcm (subject - target; sign indicates direction)
    result = agree(
        spec,
        subject_value=1.00342,
        target_value=1.00350,
    )

    # Signed delta is the right physics convention; within_tolerance uses |delta|.
    assert result.delta_value == pytest.approx(-8.0, abs=1.0)
    assert result.within_tolerance is True  # |−8| < 200 pcm
    assert result.tolerance_value == pytest.approx(200.0)


def test_agreement_receipt_signature_verifies():
    """The agreement receipt itself is signed and verifies on round-trip."""
    spec = AgreementSpec(
        axis="A3",
        subject_receipt_uri="axiom://compute/sha256:" + "e" * 64,
        target="reference://nrc-ml2327/triga-netl-P1A/k-eff",
        metric="absolute_pcm",
        tolerance_source="reference_uncertainty",
    )
    result = agree(
        spec,
        subject_value=1.00203,
        target_value=1.00203,
        target_uncertainty=0.00050,
    )
    # The agreement receipt has its own signature
    assert verify_signature(result) is True


def test_agreement_uri_content_addressed_for_same_inputs():
    """Same agreement → same URI (content-addressable). Different → different."""
    spec1 = AgreementSpec(
        axis="A3",
        subject_receipt_uri="axiom://compute/sha256:" + "f" * 64,
        target="reference://nrc-ml2327/triga-netl-P1A/k-eff",
        metric="absolute_pcm",
        tolerance_source="reference_uncertainty",
    )
    r1 = agree(spec1, subject_value=1.00203, target_value=1.00203, target_uncertainty=0.00050)
    r2 = agree(spec1, subject_value=1.00203, target_value=1.00203, target_uncertainty=0.00050)
    assert r1.uri == r2.uri  # identical inputs → identical content address


def test_agreement_classification_field_per_axis():
    """Result records which axis it answers (A3 / A4 / A5)."""
    spec = AgreementSpec(
        axis="A4",
        subject_receipt_uri="axiom://compute/sha256:" + "g" * 64,
        target="receipt://axiom/sha256:" + "h" * 64,
        metric="absolute_pcm",
        tolerance_source="literal",
        tolerance_value=200.0,
    )
    result = agree(spec, subject_value=1.0, target_value=1.0)
    assert result.axis == "A4"


def test_agreement_records_subject_and_target_uris():
    """The receipt embeds the subject + target URIs for the verify chain."""
    spec = AgreementSpec(
        axis="A3",
        subject_receipt_uri="axiom://compute/sha256:" + "i" * 64,
        target="reference://nrc-ml2327/triga-netl-P1A/k-eff",
        metric="absolute_pcm",
        tolerance_source="reference_uncertainty",
    )
    result = agree(spec, subject_value=1.0, target_value=1.0, target_uncertainty=0.0001)
    assert result.subject_receipt_uri == spec.subject_receipt_uri
    assert result.target == spec.target


def test_agreement_invalid_axis_raises():
    """Unknown axis → ValueError at the boundary."""
    with pytest.raises(ValueError, match="unknown axis"):
        AgreementSpec(
            axis="A99",
            subject_receipt_uri="axiom://compute/sha256:x",
            target="...",
            metric="absolute_pcm",
            tolerance_source="literal",
            tolerance_value=10.0,
        )


def test_agreement_reference_source_requires_uncertainty():
    """tolerance_source='reference_uncertainty' but no uncertainty supplied → error."""
    spec = AgreementSpec(
        axis="A3",
        subject_receipt_uri="axiom://compute/sha256:j",
        target="reference://x/y/z",
        metric="absolute_pcm",
        tolerance_source="reference_uncertainty",
    )
    with pytest.raises(ValueError, match="target_uncertainty"):
        agree(spec, subject_value=1.0, target_value=1.0)
