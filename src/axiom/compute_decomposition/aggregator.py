# Copyright (c) 2026 B-Tree Ventures, LLC
# SPDX-License-Identifier: Apache-2.0

"""Aggregator — drives the recomposer + composes the aggregated artifact.

Per spec §4.2 Step 10, the aggregated artifact is the citeable
output of the run. Phase A produces an ``AggregatedArtifact`` (typed
data carrier) and a content-addressed reference. The
CompositionService write (full ``(T, U, A, R)`` MemoryFragment) is
exposed as an optional helper here so callers without a configured
service still get the deterministic aggregated payload.
"""

from __future__ import annotations

from typing import Any, Optional

from .types import (
    AggregatedArtifact,
    ChunkResult,
    ContentRef,
    DecompositionPlan,
    Recomposer,
)


__all__ = [
    "aggregate_results",
    "compose_memory_fragment",
]


def aggregate_results(
    plan: DecompositionPlan,
    chunks: list[Any],                      # list[Chunk] or list[ChunkSpec]
    results: list[ChunkResult],
    recomposer: Recomposer,
) -> AggregatedArtifact:
    """Apply the registered recomposer; produce a deterministic artifact.

    The recomposer is allowed to return either a raw payload (dict)
    via its ``aggregate(chunks, results)`` shape or a ``ContentRef``
    via the canonical Protocol shape. We accept both for Phase A
    convenience; later phases standardize on the Protocol.
    """
    payload: Optional[dict[str, Any]] = None
    output_ref: Optional[ContentRef] = None

    # Try the convenience .aggregate() method first (returns a dict).
    aggregate_fn = getattr(recomposer, "aggregate", None)
    if aggregate_fn is not None:
        payload = aggregate_fn(chunks, results)
        output_ref = ContentRef.from_payload(payload)
    else:
        # Protocol form: __call__(plan, results) -> ContentRef.
        output_ref = recomposer(plan, results)
        # We don't have the decoded payload in this path; leave it
        # empty + let the caller fetch from the artifact registry
        # (Phase B wiring).
        payload = {"content_hash": output_ref.content_hash}

    return AggregatedArtifact(
        plan_id=plan.plan_id,
        problem_id=plan.problem_id,
        pattern_name=plan.pattern_name,
        parameterization_name=plan.parameterization_name,
        payload=payload or {},
        output_ref=output_ref,
        contributing_chunk_ids=tuple(r.chunk_id for r in results),
        contributing_result_count=len(results),
    )


def compose_memory_fragment(
    artifact: AggregatedArtifact,
    *,
    composition_service: Any,
    submitter_principal: str,
    orchestrator_agent: str,
    decomposition_llm_tier: Optional[str] = None,
    drafting_llm_tier: Optional[str] = None,
    extra_resources: Optional[list[str]] = None,
):
    """Helper that writes the aggregated artifact as a single
    MemoryFragment via CompositionService per spec §4.2 Step 10.

    Phase A: optional. Callers without a configured CompositionService
    skip this and consume ``AggregatedArtifact`` directly.
    """
    agents = {orchestrator_agent}
    if decomposition_llm_tier:
        agents.add(f"llm-tier:{decomposition_llm_tier}")
    if drafting_llm_tier:
        agents.add(f"llm-tier:{drafting_llm_tier}")

    resources = {
        f"plan:{artifact.plan_id}",
        f"problem:{artifact.problem_id}",
        f"pattern:{artifact.pattern_name}/{artifact.parameterization_name}",
        f"output:{artifact.output_ref.content_hash}",
    }
    for cid in artifact.contributing_chunk_ids:
        resources.add(f"chunk_result:{cid}")
    for r in extra_resources or []:
        resources.add(r)

    return composition_service.write(
        content={
            "kind": "compute.aggregate",
            "pattern": artifact.pattern_name,
            "parameterization": artifact.parameterization_name,
            "payload": artifact.payload,
            "output": {
                "content_hash": artifact.output_ref.content_hash,
                "uri": artifact.output_ref.uri,
            },
        },
        cognitive_type="resource",
        principal_id=submitter_principal,
        agents=agents,
        resources=resources,
    )
