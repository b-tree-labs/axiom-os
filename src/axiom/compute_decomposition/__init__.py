# Copyright (c) 2026 B-Tree Ventures, LLC
# SPDX-License-Identifier: Apache-2.0

"""Compute Decomposition primitive — Phase A scaffold per ADR-040.

Public surface mirrors spec §3 ``__all__``. Phase A delivers:

- The closed pattern registry (``embarrassingly_parallel`` + 5 stubs).
- Frozen Problem / DecompositionPlan / Chunk / ChunkResult types with
  auto-generated IDs.
- The Decomposer / Recomposer Protocols (runtime-checkable).
- The trait-routing decision table as a pure function.
- Three federation directory record types
  (``COMPUTE_OFFER``, ``COMPUTE_CLAIM``, ``COMPUTE_RESULT``).
- A LocalDispatcher (in-process) and SubprocessDispatcher (via
  ``infra.tasks``) for the per-leaf runner contract.
- A CompositionService-friendly aggregator that returns an
  ``AggregatedArtifact``.

Out of scope for Phase A (per spec §15 + ADR-040):

- Spatial / temporal / matrix / map_reduce / composite kernels.
- Federation gossip wiring for the directory records.
- LLM-proposed plans + verifier.
- Sandbox profile selection + adapter attestation.
- Sci Displays figure auto-rendering + paper drafter.

Domain extensions register their parameterizations through
``register_pattern_parameterization`` against the closed pattern
vocabulary."""

from .aggregator import aggregate_results, compose_memory_fragment
from .directory_records import (
    COMPUTE_CLAIM,
    COMPUTE_OFFER,
    COMPUTE_RESULT,
    ComputeClaim,
    ComputeOffer,
    ComputeRecord,
    ComputeRecordType,
    ComputeResult,
    REGISTERED_RECORD_TYPES,
    is_active_claim,
    record_from_dict,
    record_to_dict,
)
from .registry import (
    BUILTIN_PATTERN_NAMES,
    PatternConflictError,
    PatternRegistry,
    RegisteredParameterization,
    RegistrationReceipt,
    UnknownPatternError,
    register_pattern_parameterization,
)
from .routing import (
    OnCorruptionPolicy,
    RoutingDecision,
    SeedStrategy,
    routing_policy_for_trait,
)
from .runner import (
    Kernel,
    LocalDispatcher,
    SubprocessDispatcher,
    execute_chunk,
)
from .types import (
    AggregatedArtifact,
    Chunk,
    ChunkResult,
    ChunkSpec,
    ContentRef,
    Decomposer,
    DecompositionPlan,
    InvariantStatement,
    Problem,
    Recomposer,
    Trait,
)


__all__ = [
    # Types
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
    # Registry
    "BUILTIN_PATTERN_NAMES",
    "PatternRegistry",
    "RegisteredParameterization",
    "RegistrationReceipt",
    "UnknownPatternError",
    "PatternConflictError",
    "register_pattern_parameterization",
    # Routing
    "OnCorruptionPolicy",
    "RoutingDecision",
    "SeedStrategy",
    "routing_policy_for_trait",
    # Federation directory records
    "COMPUTE_CLAIM",
    "COMPUTE_OFFER",
    "COMPUTE_RESULT",
    "ComputeClaim",
    "ComputeOffer",
    "ComputeRecord",
    "ComputeRecordType",
    "ComputeResult",
    "REGISTERED_RECORD_TYPES",
    "is_active_claim",
    "record_from_dict",
    "record_to_dict",
    # Runner
    "Kernel",
    "LocalDispatcher",
    "SubprocessDispatcher",
    "execute_chunk",
    # Aggregator
    "aggregate_results",
    "compose_memory_fragment",
]
