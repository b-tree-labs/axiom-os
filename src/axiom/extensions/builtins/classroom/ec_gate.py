# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Classroom EC gate — thin wrapper over the core classification gate.

The generic retrieval-time principal-based filter lives in
`axiom.rag.gating`. This module is a classroom-specific adapter:
- Accepts the classroom's legacy chunk shape (`ec_classification`,
  `allowed_nationalities`) and attestation shape (`nationality`).
- Delegates to `filter_chunks_by_classification` in core.

Spec: spec-classroom.md §5.11.4. The generic gate handles EC, PHI,
CUI, and any other domain's classification scheme — classroom just
configures it with EC-specific fields.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from axiom.rag.gating import (
    build_principal_attestation as _build_principal_attestation,
)
from axiom.rag.gating import (
    filter_chunks_by_classification,
)
from axiom.rag.gating import (
    log_denied_accesses as _log_denied_accesses,
)

# ---------------------------------------------------------------------------
# Adapters: classroom EC shape ↔ generic classification shape
# ---------------------------------------------------------------------------


def _adapt_ec_chunk(chunk: dict) -> dict:
    """Translate classroom EC chunk → generic classification chunk."""
    if chunk.get("ec_classification") != "EC":
        # Unclassified — pass through
        return chunk
    return {
        **chunk,
        "classification": "EC",
        "required_attribute": "nationality",
        "allowed_values": chunk.get("allowed_nationalities", []),
    }


def _adapt_ec_attestation(attestation: dict | None) -> dict | None:
    """Translate classroom attestation (has `nationality` field at top level)
    → generic attestation (attribute dict)."""
    if attestation is None:
        return None
    if "attributes" in attestation:
        # Already generic shape
        return attestation
    nationality = attestation.get("nationality")
    attrs = {}
    if nationality:
        attrs["nationality"] = nationality
    return {
        **attestation,
        "attributes": attrs,
    }


# ---------------------------------------------------------------------------
# Public API (kept for classroom callers; same signatures as before)
# ---------------------------------------------------------------------------


_EC_REASON_MAP = {
    # Core generic reasons → classroom-scoped synonyms where they differ
    "attribute_value_not_allowed": "nationality_not_allowed",
}


def filter_chunks_by_ec(
    chunks: list[dict],
    attestation: dict | None,
    verify_signature: Callable[[dict], bool],
) -> tuple[list[dict], list[dict]]:
    """Classroom EC gate — adapts and delegates to core."""
    adapted_chunks = [_adapt_ec_chunk(c) for c in chunks]
    adapted_attestation = _adapt_ec_attestation(attestation)
    allowed, denied = filter_chunks_by_classification(
        adapted_chunks, adapted_attestation, verify_signature
    )
    # Restore original chunk shape (strip adapter fields)
    allowed_out = []
    for c in allowed:
        if c.get("ec_classification") == "EC":
            restored = {k: v for k, v in c.items()
                        if k not in ("classification", "required_attribute",
                                     "allowed_values")}
            allowed_out.append(restored)
        else:
            allowed_out.append(c)

    # Translate denial reasons to classroom EC vocabulary
    denied_out = []
    for d in denied:
        new_d = dict(d)
        new_d["reason"] = _EC_REASON_MAP.get(d.get("reason"), d.get("reason"))
        # Preserve ec_classification key for backward compat
        if "classification" in new_d and new_d["classification"] == "EC":
            new_d["ec_classification"] = new_d.pop("classification")
        denied_out.append(new_d)

    return allowed_out, denied_out


def log_denied_accesses(
    denied: list[dict],
    student_id: str,
    classroom_id: str,
    log_path: Path,
) -> None:
    """Classroom EC denial audit — delegates to core with classroom context."""
    _log_denied_accesses(
        denied=denied,
        principal_id=student_id,
        context={"classroom_id": classroom_id},
        log_path=log_path,
    )


def build_attestation_claim(
    student_id: str,
    nationality: str,
    signer_node: str,
    classroom_id: str,
) -> dict:
    """Build a classroom EC attestation claim — delegates to core builder.

    Keeps the classroom-friendly shape (nationality as a top-level field)
    while also carrying the generic `attributes` dict so the claim can
    be fed directly back into `filter_chunks_by_ec` later.
    """
    generic = _build_principal_attestation(
        principal_id=student_id,
        attributes={"nationality": nationality},
        signer_node=signer_node,
        context={"classroom_id": classroom_id},
    )
    # Flatten classroom-facing fields so existing tests + callers keep working
    return {
        "student_id": student_id,
        "nationality": nationality,
        "classroom_id": classroom_id,
        "signer_node": signer_node,
        "issued_at": generic["issued_at"],
        "attributes": generic["attributes"],
        "context": generic["context"],
        "signature": generic["signature"],
    }
