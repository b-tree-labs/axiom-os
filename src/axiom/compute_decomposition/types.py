# Copyright (c) 2026 B-Tree Ventures, LLC
# SPDX-License-Identifier: Apache-2.0

"""Shared types for the compute-decomposition primitive.

Per spec §3.1 these are the pure data carriers of the pipeline. Code
that consumes the primitive (extensions registering parameterizations,
the planner, the dispatcher, the aggregator) all speak in these types.

Phase A scope:

- ``Problem`` / ``DecompositionPlan`` / ``ChunkSpec`` / ``Chunk`` /
  ``ChunkResult`` / ``ContentRef`` are concrete frozen dataclasses with
  auto-generated IDs.
- ``Decomposer`` / ``Recomposer`` are runtime-checkable Protocols so
  duck-typed extension implementations register without subclassing.
- ``Trait`` is the closed enum used by the routing policy.
- ``InvariantStatement`` carries the verifier's checklist entries.

Provenance ``(T, U, A, R)`` lives on the aggregated MemoryFragment
produced by the aggregator (via CompositionService) — not on these raw
types. See ``aggregator.py``.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any, Optional, Protocol, runtime_checkable


__all__ = [
    "Trait",
    "ContentRef",
    "InvariantStatement",
    "ChunkSpec",
    "Chunk",
    "ChunkResult",
    "Problem",
    "DecompositionPlan",
    "Decomposer",
    "Recomposer",
    "AggregatedArtifact",
]


def _short_uuid() -> str:
    return uuid.uuid4().hex[:8]


def _slug(s: str, n: int = 8) -> str:
    """Trim a human-readable slug from a description for ID legibility."""
    keep = []
    for ch in s.lower():
        if ch.isalnum():
            keep.append(ch)
        elif keep and keep[-1] != "-":
            keep.append("-")
        if len(keep) >= n:
            break
    return "".join(keep).strip("-") or "x"


def _now() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Trait
# ---------------------------------------------------------------------------


class Trait(Enum):
    """Per spec §10. Closed enum; the routing policy is a pure function
    of this value."""

    DETERMINISTIC = "deterministic"
    STOCHASTIC = "stochastic"
    HYBRID = "hybrid"


# ---------------------------------------------------------------------------
# Content references
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContentRef:
    """Content-addressed handle to an artifact.

    Phase A treats artifacts as opaque dicts serialized to JSON; later
    phases plug in the artifact registry's blob storage. The hash is
    the canonical identifier — bit-identical inputs always yield the
    same hash.
    """

    content_hash: str           # "sha256:<hex>"
    uri: str                    # "axiom://artifact/<hex>"
    bytes: int
    media_type: str

    @classmethod
    def from_payload(cls, payload: Any, media_type: str = "application/json") -> ContentRef:
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                          default=_json_default).encode("utf-8")
        h = hashlib.sha256(blob).hexdigest()
        return cls(
            content_hash=f"sha256:{h}",
            uri=f"axiom://artifact/{h}",
            bytes=len(blob),
            media_type=media_type,
        )


def _json_default(o: Any) -> Any:
    if isinstance(o, datetime):
        return o.isoformat()
    if isinstance(o, bytes):
        return o.hex()
    if dataclasses.is_dataclass(o):
        return dataclasses.asdict(o)
    raise TypeError(f"unserializable: {type(o)!r}")


# ---------------------------------------------------------------------------
# Invariants
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InvariantStatement:
    """A single named verifier check. Phase A is descriptive; Phase B
    wires the actual verifier callbacks. The ``check`` is optional so
    declared-only invariants compile without runtime support."""

    name: str
    description: str
    severity: str = "error"     # "error" | "warning"


# ---------------------------------------------------------------------------
# Chunks
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChunkSpec:
    """The plan-time description of a chunk; turned into a Chunk by
    binding to a plan_id (which only exists after the plan is signed).
    """

    sequence_index: int
    trait: Trait
    parameters: dict[str, Any] = field(default_factory=dict)
    adapter_language: str = "python"
    expected_runtime_s: float = 0.0
    inputs: dict[str, ContentRef] = field(default_factory=dict)

    def to_chunk(self, *, plan_id: str, seed: Optional[bytes]) -> Chunk:
        chunk_id = f"chunk-{self.sequence_index:04d}-{_short_uuid()}"
        cache_key = None
        if self.trait is Trait.DETERMINISTIC:
            cache_key = ContentRef.from_payload(
                {"params": self.parameters, "inputs": {k: v.content_hash
                                                        for k, v in self.inputs.items()}},
            ).content_hash
        return Chunk(
            chunk_id=chunk_id,
            plan_id=plan_id,
            sequence_index=self.sequence_index,
            trait=self.trait,
            parameters=dict(self.parameters),
            inputs=dict(self.inputs),
            seed=seed,
            adapter_language=self.adapter_language,
            expected_runtime_s=self.expected_runtime_s,
            cache_key=cache_key,
        )


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    plan_id: str
    sequence_index: int
    trait: Trait
    parameters: dict[str, Any]
    inputs: dict[str, ContentRef] = field(default_factory=dict)
    seed: Optional[bytes] = None
    adapter_language: str = "python"
    expected_runtime_s: float = 0.0
    cache_key: Optional[str] = None


@dataclass(frozen=True)
class ChunkResult:
    chunk_id: str
    plan_id: str
    leaf_node_id: str
    output: ContentRef
    payload: dict[str, Any]                 # decoded view of the artifact for in-process consumers
    elapsed_ms: int = 0
    seed_used: Optional[bytes] = None
    started_at: datetime = field(default_factory=_now)
    finished_at: datetime = field(default_factory=_now)
    signature: bytes = b""

    @classmethod
    def synthesize(
        cls,
        *,
        chunk: Chunk,
        leaf_node_id: str,
        output_payload: dict[str, Any],
        elapsed_ms: int = 0,
    ) -> ChunkResult:
        """Phase A convenience: build a well-formed ChunkResult around
        a JSON-serializable output payload. The signature is a stub
        (zero bytes) until the leaf-node keypair lands."""
        ref = ContentRef.from_payload(output_payload)
        return cls(
            chunk_id=chunk.chunk_id,
            plan_id=chunk.plan_id,
            leaf_node_id=leaf_node_id,
            output=ref,
            payload=dict(output_payload),
            elapsed_ms=elapsed_ms,
            seed_used=chunk.seed,
            signature=b"\x00" * 64,
        )


# ---------------------------------------------------------------------------
# Problem + Plan
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Problem:
    problem_id: str
    description: str
    pattern_hint: Optional[str]
    parameters: dict[str, Any]
    submitter: str
    submitted_at: datetime = field(default_factory=_now)
    classification: str = "public"
    visibility: str = "cohort"

    @classmethod
    def create(
        cls,
        *,
        description: str,
        pattern_hint: Optional[str],
        parameters: dict[str, Any],
        submitter: str,
        classification: str = "public",
        visibility: str = "cohort",
    ) -> Problem:
        problem_id = f"prob-{_slug(description)}-{_short_uuid()}"
        return cls(
            problem_id=problem_id,
            description=description,
            pattern_hint=pattern_hint,
            parameters=dict(parameters),
            submitter=submitter,
            classification=classification,
            visibility=visibility,
        )


@dataclass(frozen=True)
class DecompositionPlan:
    plan_id: str
    problem_id: str
    pattern_name: str
    parameterization_name: str
    chunks: tuple[ChunkSpec, ...]
    seed_seed: Optional[bytes]
    proposer: str               # "user" | "llm"
    created_at: datetime = field(default_factory=_now)

    @classmethod
    def create(
        cls,
        *,
        problem_id: str,
        pattern_name: str,
        parameterization_name: str,
        chunks: list[ChunkSpec],
        seed_seed: Optional[bytes],
        proposer: str,
    ) -> DecompositionPlan:
        plan_id = f"plan-{_short_uuid()}"
        return cls(
            plan_id=plan_id,
            problem_id=problem_id,
            pattern_name=pattern_name,
            parameterization_name=parameterization_name,
            chunks=tuple(chunks),
            seed_seed=seed_seed,
            proposer=proposer,
        )


# ---------------------------------------------------------------------------
# Aggregated artifact (post-recomposition view)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AggregatedArtifact:
    """The recomposer's pure output — handed to CompositionService for
    the (T, U, A, R) MemoryFragment write. Keeping this distinct from
    MemoryFragment lets the aggregator stay testable without the full
    composition stack wired up."""

    plan_id: str
    problem_id: str
    pattern_name: str
    parameterization_name: str
    payload: dict[str, Any]
    output_ref: ContentRef
    contributing_chunk_ids: tuple[str, ...]
    contributing_result_count: int
    aggregated_at: datetime = field(default_factory=_now)


# ---------------------------------------------------------------------------
# Protocols
# ---------------------------------------------------------------------------


@runtime_checkable
class Decomposer(Protocol):
    """Pure function: Problem -> list[ChunkSpec]. Per spec §3.1 this
    must be deterministic given inputs."""

    def __call__(self, problem: Problem, registry: Any) -> list[ChunkSpec]: ...


@runtime_checkable
class Recomposer(Protocol):
    """Pure function: (plan, results) -> ContentRef. Per spec §3.1 this
    must be deterministic given the same ChunkResult set."""

    def __call__(self, plan: DecompositionPlan, results: list[ChunkResult]) -> ContentRef: ...
