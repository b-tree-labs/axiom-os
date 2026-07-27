# Copyright (c) 2026 B-Tree Ventures, LLC
# SPDX-License-Identifier: Apache-2.0

"""Tests for the Decomposer / Recomposer protocols and the Problem +
ChunkSpec + Chunk + ChunkResult shared types.

We exercise:
- Auto-generated IDs (per CLAUDE.md core invariant): the caller never
  invents identifiers.
- Frozen dataclasses (no in-place mutation).
- ChunkResult.synthesize convenience constructor produces a valid
  signature stub + content-addressed output.
- Decomposer + Recomposer Protocols are runtime_checkable enough that
  duck-typed implementations register.
"""

from __future__ import annotations

import dataclasses

import pytest

from axiom.compute_decomposition.types import (
    ChunkResult,
    ChunkSpec,
    ContentRef,
    DecompositionPlan,
    Decomposer,
    Problem,
    Recomposer,
    Trait,
)


def test_problem_create_auto_generates_ids():
    """Per ADR + project invariant: caller never invents IDs."""
    p1 = Problem.create(
        description="x",
        pattern_hint="embarrassingly_parallel",
        parameters={},
        submitter="@u:local",
    )
    p2 = Problem.create(
        description="x",
        pattern_hint="embarrassingly_parallel",
        parameters={},
        submitter="@u:local",
    )
    assert p1.problem_id != p2.problem_id
    assert "-" in p1.problem_id  # uuid + slug


def test_problem_is_frozen():
    p = Problem.create(
        description="x", pattern_hint="embarrassingly_parallel",
        parameters={}, submitter="@u:local",
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        p.description = "mutated"  # type: ignore[misc]


def test_chunk_spec_to_chunk_attaches_ids_and_seed():
    spec = ChunkSpec(
        sequence_index=0,
        trait=Trait.DETERMINISTIC,
        parameters={"k": "v"},
        adapter_language="python",
        expected_runtime_s=0.1,
    )
    chunk = spec.to_chunk(plan_id="plan-x", seed=None)
    assert chunk.plan_id == "plan-x"
    assert chunk.sequence_index == 0
    assert chunk.parameters == {"k": "v"}
    assert chunk.chunk_id  # auto-generated


def test_chunk_result_synthesize_carries_chunk_attribution():
    spec = ChunkSpec(
        sequence_index=2,
        trait=Trait.DETERMINISTIC,
        parameters={"x": 1},
        adapter_language="python",
        expected_runtime_s=0.1,
    )
    chunk = spec.to_chunk(plan_id="plan-y", seed=None)
    result = ChunkResult.synthesize(
        chunk=chunk,
        leaf_node_id="@leaf:local",
        output_payload={"answer": 42},
    )
    assert result.chunk_id == chunk.chunk_id
    assert result.plan_id == "plan-y"
    assert result.leaf_node_id == "@leaf:local"
    assert result.output.media_type == "application/json"
    assert result.signature  # stub signature populated


def test_decomposer_protocol_accepts_callable_class():
    class _D:
        def __call__(self, problem, registry):  # noqa: ARG002
            return []
    assert isinstance(_D(), Decomposer)


def test_recomposer_protocol_accepts_callable_class():
    class _R:
        def __call__(self, plan, results):  # noqa: ARG002
            return ContentRef(content_hash="x", uri="axiom://artifact/x",
                              bytes=0, media_type="application/json")
    assert isinstance(_R(), Recomposer)


def test_decomposition_plan_is_frozen_and_signed():
    plan = DecompositionPlan.create(
        problem_id="prob-1",
        pattern_name="embarrassingly_parallel",
        parameterization_name="trivial",
        chunks=[],
        seed_seed=None,
        proposer="user",
    )
    assert plan.plan_id
    assert plan.problem_id == "prob-1"
    with pytest.raises(dataclasses.FrozenInstanceError):
        plan.proposer = "llm"  # type: ignore[misc]


def test_content_ref_is_content_addressed():
    cr = ContentRef.from_payload({"a": 1})
    assert cr.content_hash.startswith("sha256:")
    assert cr.uri == f"axiom://artifact/{cr.content_hash[len('sha256:'):]}"
    # Same payload -> same hash.
    cr2 = ContentRef.from_payload({"a": 1})
    assert cr2.content_hash == cr.content_hash
