# Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""axiom.compute.agree — signed agreement-receipt primitive.

Per Twin Toolkit Demo Spec §5.3 (Seam E) + ADR-017.

The `agree` primitive operationalizes verification axes A3 (vs reference),
A4 (vs another receipt), A5 (vs sensor data) by quantifying agreement and
emitting a signed receipt that itself becomes evidence in the chain.

Usage:

    spec = AgreementSpec(
        axis="A3",
        subject_receipt_uri="axiom://compute/sha256:e5f6...",
        target="reference://nrc-ml2327/triga-netl-P1A/k-eff",
        metric="absolute_pcm",
        tolerance_source="reference_uncertainty",
    )
    result = agree(spec, subject_value=1.00203, target_value=1.00203, target_uncertainty=0.00050)
    # result.uri = "axiom://agree/sha256:..."
    # result.delta_value = 0.0  (pcm)
    # result.within_tolerance = True

Phase 4a takes raw value floats so callers can wire receipts however they
want. Phase 4b layers a higher-level helper that resolves URIs through the
Bronze receipt store + reference registry.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from axiom.compute.dispatch import _load_local_identity, _sign


_VALID_AXES = frozenset({"A1", "A2", "A3", "A4", "A5"})
_VALID_METRICS = frozenset({"absolute_pcm", "relative_error", "rmse", "absolute"})
_VALID_TOLERANCE_SOURCES = frozenset({"literal", "reference_uncertainty"})


@dataclass(frozen=True)
class AgreementSpec:
    """Input contract for the `agree` primitive."""

    axis: Literal["A1", "A2", "A3", "A4", "A5"]
    subject_receipt_uri: str  # axiom://compute/sha256:...
    target: str  # reference://... | receipt://axiom/sha256:... | sensor://...
    metric: Literal["absolute_pcm", "relative_error", "rmse", "absolute"]
    tolerance_source: Literal["literal", "reference_uncertainty"]
    tolerance_value: float | None = None  # required if tolerance_source="literal"

    def __post_init__(self) -> None:
        if self.axis not in _VALID_AXES:
            raise ValueError(
                f"unknown axis {self.axis!r}; must be one of {sorted(_VALID_AXES)}"
            )
        if self.metric not in _VALID_METRICS:
            raise ValueError(
                f"unknown metric {self.metric!r}; must be one of {sorted(_VALID_METRICS)}"
            )
        if self.tolerance_source not in _VALID_TOLERANCE_SOURCES:
            raise ValueError(
                f"unknown tolerance_source {self.tolerance_source!r}; "
                f"must be one of {sorted(_VALID_TOLERANCE_SOURCES)}"
            )
        if self.tolerance_source == "literal" and self.tolerance_value is None:
            raise ValueError(
                "tolerance_source='literal' requires tolerance_value to be set"
            )


@dataclass(frozen=True)
class AgreementResult:
    """Signed agreement receipt — a first-class evidence artifact.

    Verifiable via axiom.compute.verify_signature: the result is structurally
    similar to a DispatchResult so the same verification machinery handles it.
    """

    axis: str
    subject_receipt_uri: str
    target: str
    metric: str
    tolerance_source: str
    delta_value: float
    tolerance_value: float
    within_tolerance: bool
    computed_at: str  # ISO8601
    content_address: str  # sha256 of canonical message
    signature_b64: str
    signing_pubkey_b64: str
    signing_node_id: str

    # Mimic DispatchResult shape so verify_signature works
    kernel: str = "agree"
    executing_peer_id: str = "laptop"
    executed_at: str = ""
    model_id: str = ""
    composition_hash: str = ""
    determinism_class: str = "D-bit"
    determinism_state: dict | None = None
    value_summary: dict | None = None
    halted: bool = False

    @property
    def uri(self) -> str:
        return f"axiom://agree/sha256:{self.content_address}"


def _canonical_agreement_message(
    spec: AgreementSpec,
    subject_value: float,
    target_value: float,
    target_uncertainty: float | None,
    delta_value: float,
    tolerance_value: float,
    within_tolerance: bool,
) -> bytes:
    """Build the deterministic canonical message that is signed.

    Identical inputs → identical bytes → identical content address. This is
    the contract that makes agreement receipts content-addressable.
    """
    payload = {
        "axis": spec.axis,
        "subject_receipt_uri": spec.subject_receipt_uri,
        "target": spec.target,
        "metric": spec.metric,
        "tolerance_source": spec.tolerance_source,
        "subject_value": subject_value,
        "target_value": target_value,
        "target_uncertainty": target_uncertainty,
        "delta_value": delta_value,
        "tolerance_value": tolerance_value,
        "within_tolerance": within_tolerance,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _compute_delta(metric: str, subject_value: float, target_value: float) -> float:
    """Compute the metric value for (subject - target).

    - absolute_pcm: (subject - target) * 1e5 (suitable for k-eff comparisons)
    - relative_error: (subject - target) / target (dimensionless)
    - rmse / absolute: |subject - target| (single-point case; RMSE over arrays
      is a Phase 4b extension)
    """
    if metric == "absolute_pcm":
        return (subject_value - target_value) * 1e5
    if metric == "relative_error":
        if target_value == 0:
            return float("inf")
        return (subject_value - target_value) / target_value
    # absolute / rmse on a single value → just |delta|
    return abs(subject_value - target_value)


def _resolve_tolerance(
    spec: AgreementSpec,
    target_uncertainty: float | None,
) -> float:
    """Determine the tolerance value to compare delta against."""
    if spec.tolerance_source == "literal":
        # __post_init__ guarantees tolerance_value is set
        assert spec.tolerance_value is not None
        return spec.tolerance_value
    # reference_uncertainty: convert the uncertainty to the same units as delta
    if target_uncertainty is None:
        raise ValueError(
            "target_uncertainty must be supplied when tolerance_source='reference_uncertainty'"
        )
    if spec.metric == "absolute_pcm":
        return target_uncertainty * 1e5
    return target_uncertainty


def agree(
    spec: AgreementSpec,
    subject_value: float,
    target_value: float,
    target_uncertainty: float | None = None,
) -> AgreementResult:
    """Quantify (subject vs target) agreement; emit signed receipt."""
    delta = _compute_delta(spec.metric, subject_value, target_value)
    tolerance = _resolve_tolerance(spec, target_uncertainty)
    within = abs(delta) <= tolerance

    identity = _load_local_identity()
    message = _canonical_agreement_message(
        spec, subject_value, target_value, target_uncertainty,
        delta, tolerance, within,
    )
    content_address = hashlib.sha256(message).hexdigest()
    signature_b64 = _sign(message, identity)

    return AgreementResult(
        axis=spec.axis,
        subject_receipt_uri=spec.subject_receipt_uri,
        target=spec.target,
        metric=spec.metric,
        tolerance_source=spec.tolerance_source,
        delta_value=delta,
        tolerance_value=tolerance,
        within_tolerance=within,
        computed_at=datetime.now(timezone.utc).isoformat(),
        content_address=content_address,
        signature_b64=signature_b64,
        signing_pubkey_b64=identity.public_key_b64,
        signing_node_id=identity.node_id,
        # DispatchResult-shape passthrough so verify_signature works
        executing_peer_id=identity.node_id,  # signed by the local node
        model_id=spec.subject_receipt_uri,   # for content-address re-derivation
        composition_hash=spec.target,
        determinism_state={
            "subject_value": subject_value,
            "target_value": target_value,
            "target_uncertainty": target_uncertainty,
        },
        value_summary={
            "delta_value": delta,
            "tolerance_value": tolerance,
            "within_tolerance": within,
            "axis": spec.axis,
        },
        halted=False,
    )
