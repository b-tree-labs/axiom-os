# Copyright (c) 2026 B-Tree Ventures, LLC
# SPDX-License-Identifier: Apache-2.0

"""Federation directory record schemas per spec §5.

Three new typed records:

- ``COMPUTE_OFFER``  — leaf advertises capacity + supported patterns.
- ``COMPUTE_CLAIM``  — leaf claims a chunk; heartbeat-renewed.
- ``COMPUTE_RESULT`` — leaf publishes a finished chunk's output.

Phase A: schemas defined + a closed registry of record-type names so
the gossip layer (``vega/federation/discovery.py``) can pick them up
in a later phase. Full A2A wiring + signature verification + cohort-
scoped visibility filters land post-Prague.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any, Optional, Union


__all__ = [
    "COMPUTE_OFFER",
    "COMPUTE_CLAIM",
    "COMPUTE_RESULT",
    "REGISTERED_RECORD_TYPES",
    "ComputeRecordType",
    "ComputeOffer",
    "ComputeClaim",
    "ComputeResult",
    "ComputeRecord",
    "is_active_claim",
    "record_to_dict",
    "record_from_dict",
]


# Wire-format string constants (must match spec §5 verbatim).
COMPUTE_OFFER = "COMPUTE_OFFER"
COMPUTE_CLAIM = "COMPUTE_CLAIM"
COMPUTE_RESULT = "COMPUTE_RESULT"


REGISTERED_RECORD_TYPES: frozenset[str] = frozenset({
    COMPUTE_OFFER,
    COMPUTE_CLAIM,
    COMPUTE_RESULT,
})


class ComputeRecordType(Enum):
    COMPUTE_OFFER = COMPUTE_OFFER
    COMPUTE_CLAIM = COMPUTE_CLAIM
    COMPUTE_RESULT = COMPUTE_RESULT


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ComputeOffer:
    authority: str                       # node_id
    signed_at: datetime
    ttl_seconds: int
    cpu_cores_available: int
    cpu_load_avg_5m: float
    ram_gb_available: float
    gpu_kind: str                        # "none" | "nvidia-cuda" | "apple-mps" | "amd-rocm"
    gpu_vram_gb: Optional[float]
    disk_gb_available: float
    network_class: str                   # "lan-fast" | "wan-broadband" | ...
    classification_ceiling: str
    pattern_support: dict[str, list[str]]  # pattern_name -> adapter_languages
    accept_strangers_in_cohort: bool
    accept_cross_cohort: bool
    daily_compute_budget_minutes: Optional[int]
    signature: bytes
    record_type: ComputeRecordType = ComputeRecordType.COMPUTE_OFFER


@dataclass(frozen=True)
class ComputeClaim:
    authority: str
    signed_at: datetime
    ttl_seconds: int
    plan_id: str
    chunk_id: str
    claimed_at: datetime
    expected_completion_at: datetime
    retry_count: int
    adapter_language: str
    sandbox_profile_id: str
    signature: bytes
    record_type: ComputeRecordType = ComputeRecordType.COMPUTE_CLAIM


@dataclass(frozen=True)
class ComputeResult:
    authority: str
    signed_at: datetime
    ttl_seconds: int
    plan_id: str
    chunk_id: str
    output_content_hash: str
    output_uri: str
    output_bytes: int
    output_media_type: str
    adapter_code_hash: str
    adapter_signature: bytes
    runtime: str
    solver_version: Optional[str]
    sandbox_profile_id: str
    trait: str                           # "deterministic" | "stochastic" | "hybrid"
    seed_used: Optional[bytes]
    elapsed_ms: int
    started_at: datetime
    finished_at: datetime
    cpu_cores_used: int
    peak_ram_mb: int
    gpu_used: bool
    signature: bytes
    record_type: ComputeRecordType = ComputeRecordType.COMPUTE_RESULT


ComputeRecord = Union[ComputeOffer, ComputeClaim, ComputeResult]


_RECORD_CLASS: dict[ComputeRecordType, type] = {
    ComputeRecordType.COMPUTE_OFFER: ComputeOffer,
    ComputeRecordType.COMPUTE_CLAIM: ComputeClaim,
    ComputeRecordType.COMPUTE_RESULT: ComputeResult,
}


# ---------------------------------------------------------------------------
# TTL semantics
# ---------------------------------------------------------------------------


def is_active_claim(claim: ComputeClaim, *, now: Optional[datetime] = None) -> bool:
    """Per spec §5.2: a claim is *active* iff signed_at + ttl_seconds > now."""
    now = now or datetime.now(UTC)
    expiry = claim.signed_at + timedelta(seconds=claim.ttl_seconds)
    return expiry > now


# ---------------------------------------------------------------------------
# Round-trip serialization (Phase A: dict; Phase B: A2A wire format)
# ---------------------------------------------------------------------------


def _encode(v: Any) -> Any:
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, bytes):
        return v.hex()
    if isinstance(v, ComputeRecordType):
        return v.value
    if isinstance(v, dict):
        return {k: _encode(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_encode(x) for x in v]
    if isinstance(v, tuple):
        return [_encode(x) for x in v]
    return v


def record_to_dict(record: ComputeRecord) -> dict[str, Any]:
    out = {}
    for f in dataclasses.fields(record):
        out[f.name] = _encode(getattr(record, f.name))
    return out


def _decode_field(field_type: Any, value: Any) -> Any:
    # Resolve the very small set of types we serialize specially.
    if field_type is datetime or field_type == "datetime":
        if isinstance(value, datetime):
            return value
        return datetime.fromisoformat(value)
    if field_type is bytes or field_type == "bytes":
        if isinstance(value, bytes):
            return value
        return bytes.fromhex(value)
    return value


def record_from_dict(d: dict[str, Any]) -> ComputeRecord:
    """Round-trip the dict back to a typed dataclass.

    Detects record_type from the dict's `record_type` field; for each
    declared field decodes datetime / bytes back to native types.
    """
    rtype = ComputeRecordType(d["record_type"])
    cls = _RECORD_CLASS[rtype]
    kwargs = {}
    for f in dataclasses.fields(cls):
        if f.name == "record_type":
            kwargs[f.name] = rtype
            continue
        raw = d.get(f.name)
        if raw is None and not _is_optional(f.type):
            # Best-effort default; the dataclass will raise if required.
            continue
        kwargs[f.name] = _decode_by_annotation(f.type, raw)
    return cls(**kwargs)


def _is_optional(annotation: Any) -> bool:
    text = str(annotation)
    return "Optional" in text or "None" in text


def _decode_by_annotation(annotation: Any, value: Any) -> Any:
    if value is None:
        return None
    text = str(annotation)
    if "datetime" in text and not isinstance(value, datetime):
        return datetime.fromisoformat(value)
    if "bytes" in text and not isinstance(value, bytes):
        return bytes.fromhex(value)
    return value
