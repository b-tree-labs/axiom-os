# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Retrieval-time classification gate — generic principal-based filtering.

Per spec-classification-boundary.md and the Collaborative Memory paper.

At retrieval time, filter chunks based on:
1. The chunk's classification (if any) — tag + required attribute(s) +
   allowed values. Classification is a per-chunk property baked in at
   ingest time (or attached by policy routing).
2. The principal's attestation — a signed claim carrying attribute
   values (nationality, clearance, citizenship, role, etc.).
3. Signature verification via a caller-supplied verifier (federation
   layer holds the trust chain; this module treats verification as
   a pluggable check).

Not coupled to any specific classification tag or attribute — the
same gate handles EC (nationality), PHI (clearance), CUI (citizenship),
or any domain extension's classification scheme.

Related: `axiom/rag/ec_screening.py` is the *ingest-time* content
screener (keyword matches against security markings). This module is
the *retrieval-time* principal-based gate. Both are needed and they
compose: ec_screening decides where a chunk lives; gating decides who
can retrieve it.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Classification helpers
# ---------------------------------------------------------------------------


def _is_classified(chunk: dict) -> bool:
    """A chunk is classified if it declares a `classification` tag."""
    return bool(chunk.get("classification"))


def _chunk_requirements(chunk: dict) -> list[dict]:
    """Return the chunk's attribute requirements as a list.

    Accepts either the simple shape (single `required_attribute` +
    `allowed_values`) or the compound shape (`required_attributes`
    as a list of {attribute, allowed_values} dicts). Returns a
    uniform list either way.
    """
    compound = chunk.get("required_attributes")
    if compound:
        return list(compound)
    attr = chunk.get("required_attribute")
    values = chunk.get("allowed_values")
    if attr and values is not None:
        return [{"attribute": attr, "allowed_values": values}]
    return []


def _attestation_has_required_attributes(
    attestation: dict, requirements: list[dict]
) -> bool:
    """True iff attestation carries values for every required attribute key."""
    attrs = attestation.get("attributes") or {}
    return all(req["attribute"] in attrs for req in requirements)


def _attestation_values_match(
    attestation: dict, requirements: list[dict]
) -> bool:
    """True iff principal's value for each required attribute is in the allowed set."""
    attrs = attestation.get("attributes") or {}
    for req in requirements:
        principal_value = attrs.get(req["attribute"])
        if principal_value not in set(req.get("allowed_values", [])):
            return False
    return True


# ---------------------------------------------------------------------------
# Core filter
# ---------------------------------------------------------------------------


def filter_chunks_by_classification(
    chunks: list[dict],
    attestation: dict | None,
    verify_signature: Callable[[dict], bool],
) -> tuple[list[dict], list[dict]]:
    """Return (allowed, denied) given a principal's signed attestation.

    Unclassified chunks always pass. Classified chunks require:
        1. Attestation is present.
        2. Attestation has every required attribute for the chunk.
        3. Attestation's signature verifies.
        4. Attestation's value for each required attribute is in the
           chunk's allowed set.

    Any failure places the chunk in `denied` with an explicit reason
    so the caller can audit-log. Denied chunks carry the chunk id,
    the reason, and the classification tag for downstream review.
    """
    allowed: list[dict] = []
    denied: list[dict] = []

    for chunk in chunks:
        if not _is_classified(chunk):
            allowed.append(chunk)
            continue

        requirements = _chunk_requirements(chunk)
        if not requirements:
            # Classified but no requirements declared — fail safe: deny.
            denied.append({
                "chunk_id": chunk.get("id", ""),
                "reason": "classification_malformed",
                "classification": chunk.get("classification"),
            })
            continue

        if attestation is None or not _attestation_has_required_attributes(
            attestation, requirements
        ):
            denied.append({
                "chunk_id": chunk.get("id", ""),
                "reason": "no_attestation",
                "classification": chunk.get("classification"),
            })
            continue

        if not verify_signature(attestation):
            denied.append({
                "chunk_id": chunk.get("id", ""),
                "reason": "attestation_not_verified",
                "classification": chunk.get("classification"),
            })
            continue

        if not _attestation_values_match(attestation, requirements):
            denied.append({
                "chunk_id": chunk.get("id", ""),
                "reason": "attribute_value_not_allowed",
                "classification": chunk.get("classification"),
            })
            continue

        allowed.append(chunk)

    return allowed, denied


# ---------------------------------------------------------------------------
# Audit log (JSONL append-only)
# ---------------------------------------------------------------------------


def log_denied_accesses(
    denied: list[dict],
    principal_id: str,
    context: dict | None,
    log_path: Path,
) -> None:
    """Append denied accesses to a JSONL audit log.

    `context` carries caller-specific metadata (classroom_id, tenant_id,
    request_id, etc.) — the gate itself stays domain-agnostic.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).isoformat()
    with log_path.open("a") as f:
        for d in denied:
            record = {
                "timestamp": ts,
                "principal_id": principal_id,
                **(context or {}),
                **d,
            }
            f.write(json.dumps(record) + "\n")


# ---------------------------------------------------------------------------
# Attestation claim builder (domain-agnostic)
# ---------------------------------------------------------------------------


def build_principal_attestation(
    principal_id: str,
    attributes: dict[str, Any],
    signer_node: str,
    context: dict | None = None,
) -> dict:
    """Produce a signed-claim payload for cross-node principal attestation.

    `attributes` is an open dict — whatever the domain needs to attest
    (nationality, clearance, citizenship, role, organization, etc.).
    `context` is opaque caller metadata (classroom_id, tenant_id, etc.).
    Signature slot is reserved for the federation layer.
    """
    return {
        "principal_id": principal_id,
        "attributes": dict(attributes),
        "signer_node": signer_node,
        "context": dict(context or {}),
        "issued_at": datetime.now(UTC).isoformat(),
        "signature": None,  # federation layer fills in
    }
