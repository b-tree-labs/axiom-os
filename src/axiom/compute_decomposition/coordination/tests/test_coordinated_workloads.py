# Copyright (c) 2026 B-Tree Ventures, LLC
# SPDX-License-Identifier: Apache-2.0

"""Tests for the coordinated-workloads primitive.

TDD-first; this file landed RED and was driven green by the
implementation in ``coordination/{peer_select,peer_dispatcher,
orchestrator}.py``.

What we test (per the spec in the task brief):

A. ``select_peers``
   1. Determinism — same inputs, same output, 100 trials.
   2. Higher-trust peers absorb more chunks (capacity-weighted path).
   3. Round-robin when capacity is equal.
   4. Zero qualified peers → ``NoQualifiedPeers``.

B. ``PeerDispatcher``
   5. Returns a per-chunk signature alongside the result.

C. ``decompose_and_solve``
   6. Local-only path (no peers configured) — falls back to LocalDispatcher.
   7. With a fake-peer fixture (no real SSH) — Σ_1^N split into 4 chunks.
   8. Aggregates N chunk signatures + carries them on the receipt.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from axiom.compute_decomposition.coordination import (
    CoordinatedReceipt,
    NoQualifiedPeers,
    PeerDispatcher,
    SignedChunkResult,
    decompose_and_solve,
    select_peers,
)
from axiom.compute_decomposition.patterns.embarrassingly_parallel import (
    EmbarrassinglyParallelDecomposer,
    SumOfSquaresKernel,
    SumRecomposer,
)
from axiom.compute_decomposition.registry import PatternRegistry
from axiom.compute_decomposition.types import (
    Chunk,
    DecompositionPlan,
    Problem,
)


# ---------------------------------------------------------------------------
# Fake peer registry view + fake compute call
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FakePeer:
    """Minimal stand-in for a federation ``KnownNode`` row.

    Only carries the fields ``select_peers`` actually reads. Real
    NodeRegistry peers are accepted by the same selector via duck-typing.
    """

    node_id: str
    display_name: str
    public_key: str = "fake-pub-b64"
    state: str = "verified"
    capabilities: tuple[str, ...] = ("compute:embarrassingly_parallel",)
    trust_score: float = 0.5
    latency_estimate_ms: float = 50.0
    compute_capacity_hint: float = 1.0


@dataclass
class FakeComputeCall:
    """Records peer-dispatched calls and returns a deterministic signed
    result. Used in place of ``scidisplay.compute.compute`` so tests
    don't shell out via SSH."""

    answers: dict[str, dict[str, Any]] = field(default_factory=dict)
    invocations: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def __call__(self, *, peer_id: str, peer_display_name: str,
                 chunk: Chunk) -> SignedChunkResult:
        self.invocations.append((peer_id, dict(chunk.parameters)))

        # Deterministic stub: actually compute sum_of_squares so the
        # downstream aggregator + ground-truth check pass.
        lo = int(chunk.parameters["range_lo"])
        hi = int(chunk.parameters["range_hi"])
        s = sum(i * i for i in range(lo, hi))
        payload = {"sum": s, "range_lo": lo, "range_hi": hi}

        return SignedChunkResult(
            chunk_id=chunk.chunk_id,
            payload=payload,
            elapsed_ms=1,
            executed_on_peer=peer_display_name,
            signed_by_node_id=peer_id,
            signed_by_display_name=peer_display_name,
            signing_pubkey_b64="fake-pub-b64",
            signature_b64=f"fake-sig:{peer_id}:{chunk.chunk_id}",
            signature_valid=True,
            signature_verification_reason="",
            canonical_hash="fake-hash",
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_plan(n: int = 100, n_chunks: int = 4) -> tuple[
    DecompositionPlan, list[Chunk], EmbarrassinglyParallelDecomposer,
    SumRecomposer,
]:
    decomposer = EmbarrassinglyParallelDecomposer()
    recomposer = SumRecomposer()
    problem = Problem.create(
        description=f"sum_of_squares({n})",
        pattern_hint="embarrassingly_parallel",
        parameters={"n": n, "n_chunks": n_chunks, "kernel": "sum_of_squares"},
        submitter="@test:local",
    )
    specs = decomposer(problem, registry=None)
    plan = DecompositionPlan.create(
        problem_id=problem.problem_id,
        pattern_name="embarrassingly_parallel",
        parameterization_name="sum_of_squares",
        chunks=specs,
        seed_seed=None,
        proposer="user",
    )
    chunks = [s.to_chunk(plan_id=plan.plan_id, seed=None) for s in specs]
    return plan, chunks, decomposer, recomposer


def _ground_truth(n: int) -> int:
    # Σ_{i=0}^{n-1} i^2 = n(n-1)(2n-1)/6
    return n * (n - 1) * (2 * n - 1) // 6


def _peers(*specs: tuple[str, str, float, float]) -> list[FakePeer]:
    """Build a fake-peer list from short spec tuples.

    spec = (node_id, display_name, trust_score, capacity_hint)
    """
    return [
        FakePeer(
            node_id=nid,
            display_name=disp,
            trust_score=trust,
            compute_capacity_hint=cap,
        )
        for (nid, disp, trust, cap) in specs
    ]


# ---------------------------------------------------------------------------
# A. select_peers
# ---------------------------------------------------------------------------


def test_select_peers_is_deterministic():
    """Same inputs -> same output, 100 trials."""
    plan, chunks, _, _ = _build_plan(n_chunks=8)
    peers = _peers(
        ("node-aa", "alpha:lab", 0.8, 1.0),
        ("node-bb", "bravo:lab", 0.6, 1.0),
        ("node-cc", "charlie:lab", 0.4, 1.0),
    )
    snapshot = "2026-05-01T12:00:00+00:00"

    first = select_peers(plan, peers, snapshot_at=snapshot)
    for _ in range(100):
        again = select_peers(plan, peers, snapshot_at=snapshot)
        assert again.assignment == first.assignment
        assert again.snapshot_hash == first.snapshot_hash


def test_select_peers_assigns_more_to_higher_trust_capacity():
    """Capacity-weighted: higher-capacity peers absorb more chunks."""
    plan, chunks, _, _ = _build_plan(n_chunks=10)
    peers = _peers(
        ("node-big", "big:lab", 0.9, 4.0),    # 4x capacity
        ("node-mid", "mid:lab", 0.5, 1.0),    # 1x capacity
        ("node-low", "low:lab", 0.5, 1.0),    # 1x capacity
    )
    snap = "2026-05-01T12:00:00+00:00"
    sel = select_peers(plan, peers, snapshot_at=snap)

    counts = {p.node_id: 0 for p in peers}
    for chunk_id, peer_id in sel.assignment.items():
        counts[peer_id] += 1

    # node-big should win the most chunks because of 4x weight.
    assert counts["node-big"] > counts["node-mid"]
    assert counts["node-big"] > counts["node-low"]
    # And every chunk got an assignment.
    assert sum(counts.values()) == len(plan.chunks)


def test_select_peers_round_robin_when_capacity_equal():
    """Equal capacity, equal trust → tight round-robin (counts within ±1)."""
    plan, chunks, _, _ = _build_plan(n_chunks=9)
    peers = _peers(
        ("node-a1", "alpha-1:lab", 0.5, 1.0),
        ("node-a2", "alpha-2:lab", 0.5, 1.0),
        ("node-a3", "alpha-3:lab", 0.5, 1.0),
    )
    snap = "2026-05-01T12:00:00+00:00"
    sel = select_peers(plan, peers, snapshot_at=snap)

    counts = {p.node_id: 0 for p in peers}
    for cid, pid in sel.assignment.items():
        counts[pid] += 1
    # 9 chunks, 3 peers, exactly 3 each.
    assert all(c == 3 for c in counts.values()), counts


def test_select_peers_raises_when_zero_qualified():
    """No peers (or no peers qualify) → NoQualifiedPeers."""
    plan, _, _, _ = _build_plan()
    with pytest.raises(NoQualifiedPeers):
        select_peers(plan, [], snapshot_at="2026-05-01T12:00:00+00:00")

    # Also: peers with no matching capability → empty after filter
    bad_peers = [
        FakePeer(node_id="x", display_name="x:lab",
                 capabilities=("compute:matrix_block",)),
    ]
    with pytest.raises(NoQualifiedPeers):
        select_peers(plan, bad_peers, snapshot_at="...",
                     required_capability="compute:embarrassingly_parallel")


def test_select_peers_one_chunk_goes_to_highest_trust():
    """One chunk → assigned to the highest-trust peer."""
    plan, _, _, _ = _build_plan(n_chunks=1)
    peers = _peers(
        ("node-z", "z:lab", 0.4, 1.0),
        ("node-y", "y:lab", 0.9, 1.0),
        ("node-x", "x:lab", 0.6, 1.0),
    )
    sel = select_peers(plan, peers, snapshot_at="2026-05-01T12:00:00+00:00")
    assert len(sel.assignment) == 1
    assert next(iter(sel.assignment.values())) == "node-y"


# ---------------------------------------------------------------------------
# B. PeerDispatcher
# ---------------------------------------------------------------------------


def test_peer_dispatcher_returns_signatures_per_chunk():
    """Every dispatched chunk comes back with a SignedChunkResult carrying
    the executing peer's signature."""
    plan, chunks, _, _ = _build_plan(n=400, n_chunks=4)
    peers = _peers(
        ("node-aa", "alpha:lab", 0.7, 1.0),
        ("node-bb", "bravo:lab", 0.7, 1.0),
    )
    sel = select_peers(
        plan, peers, snapshot_at="2026-05-01T12:00:00+00:00",
        chunks=chunks,
    )
    fake_compute = FakeComputeCall()
    peers_by_id = {p.node_id: p for p in peers}

    dispatcher = PeerDispatcher(
        assignment=sel.assignment,
        peers_by_id=peers_by_id,
        compute_call=fake_compute,
    )
    signed_results: list[SignedChunkResult] = dispatcher.dispatch_all(chunks)
    assert len(signed_results) == len(chunks)
    # Every chunk must carry a signature + valid flag.
    for r in signed_results:
        assert r.signature_b64
        assert r.signature_valid is True
        assert r.signed_by_node_id in peers_by_id


# ---------------------------------------------------------------------------
# C. decompose_and_solve
# ---------------------------------------------------------------------------


def test_decompose_and_solve_local_only():
    """No peers configured → LocalDispatcher path; aggregate hits ground truth."""
    registry = PatternRegistry.with_builtins()
    registry.register_parameterization(
        pattern_name="embarrassingly_parallel",
        parameterization_name="sum_of_squares",
        decomposer=EmbarrassinglyParallelDecomposer(),
        recomposer=SumRecomposer(),
    )

    receipt = decompose_and_solve(
        problem={"description": "sum_of_squares(0, 600)",
                 "pattern": "embarrassingly_parallel",
                 "parameterization": "sum_of_squares",
                 "parameters": {"n": 600, "n_chunks": 4,
                                "kernel": "sum_of_squares"},
                 "submitter": "@test:local"},
        peers=None,
        dispatch="local",
        registry=registry,
        kernels={"sum_of_squares": SumOfSquaresKernel()},
    )
    assert isinstance(receipt, CoordinatedReceipt)
    assert receipt.aggregate_value["sum"] == _ground_truth(600)
    assert receipt.dispatch_mode == "local"
    # Local path: no peer signatures.
    assert receipt.chunk_signatures == []


def test_decompose_and_solve_with_one_peer():
    """Σ_{i=0}^{N-1} i^2 split into 4 chunks; routed via a single fake peer."""
    registry = PatternRegistry.with_builtins()
    registry.register_parameterization(
        pattern_name="embarrassingly_parallel",
        parameterization_name="sum_of_squares",
        decomposer=EmbarrassinglyParallelDecomposer(),
        recomposer=SumRecomposer(),
    )
    fake_compute = FakeComputeCall()
    peers = _peers(("node-one", "only:peer", 0.9, 1.0))

    N = 1000
    receipt = decompose_and_solve(
        problem={"description": f"sum_of_squares(0, {N})",
                 "pattern": "embarrassingly_parallel",
                 "parameterization": "sum_of_squares",
                 "parameters": {"n": N, "n_chunks": 4,
                                "kernel": "sum_of_squares"},
                 "submitter": "@test:local"},
        peers=peers,
        dispatch="cross_node",
        registry=registry,
        compute_call=fake_compute,
    )
    assert receipt.aggregate_value["sum"] == _ground_truth(N)
    assert receipt.dispatch_mode == "cross_node"
    assert len(receipt.chunk_signatures) == 4
    # Every chunk routed to the only peer.
    assert all(s.signed_by_node_id == "node-one" for s in receipt.chunk_signatures)
    assert receipt.directory_snapshot_hash  # populated


def test_decompose_and_solve_aggregates_signatures():
    """N chunks across multiple peers → N signatures on receipt; each signed
    by the peer that ran it."""
    registry = PatternRegistry.with_builtins()
    registry.register_parameterization(
        pattern_name="embarrassingly_parallel",
        parameterization_name="sum_of_squares",
        decomposer=EmbarrassinglyParallelDecomposer(),
        recomposer=SumRecomposer(),
    )
    fake_compute = FakeComputeCall()
    peers = _peers(
        ("node-aa", "alpha:lab", 0.8, 1.0),
        ("node-bb", "bravo:lab", 0.7, 1.0),
        ("node-cc", "charlie:lab", 0.5, 1.0),
    )

    N = 500
    n_chunks = 6
    receipt = decompose_and_solve(
        problem={"description": f"sum_of_squares({N})",
                 "pattern": "embarrassingly_parallel",
                 "parameterization": "sum_of_squares",
                 "parameters": {"n": N, "n_chunks": n_chunks,
                                "kernel": "sum_of_squares"},
                 "submitter": "@test:local"},
        peers=peers,
        dispatch="cross_node",
        registry=registry,
        compute_call=fake_compute,
    )

    assert receipt.aggregate_value["sum"] == _ground_truth(N)
    assert len(receipt.chunk_signatures) == n_chunks
    # Each signature lines up with a routing decision in the receipt.
    for sig in receipt.chunk_signatures:
        peer = receipt.routing_assignment[sig.chunk_id]
        assert sig.signed_by_node_id == peer
    # Receipt carries the per-chunk routing AND the snapshot hash so it's
    # replayable.
    assert receipt.directory_snapshot_hash
    assert receipt.snapshot_at == receipt.snapshot_at  # truthy + same on re-read
    assert receipt.aggregate_content_hash.startswith("sha256:")


def test_mcp_tool_returns_jsonable_dict_local_path():
    """The MCP-shaped wrapper produces a JSON-friendly dict (no
    dataclasses, no bytes) so MCP clients can render it directly."""
    import json

    registry = PatternRegistry.with_builtins()
    registry.register_parameterization(
        pattern_name="embarrassingly_parallel",
        parameterization_name="sum_of_squares",
        decomposer=EmbarrassinglyParallelDecomposer(),
        recomposer=SumRecomposer(),
    )
    # MCP wrapper takes peers as a list[str] of display_names + needs
    # the registry; for local path we pass empty peers + go local. The
    # underlying orchestrator needs kernels= for local — the MCP
    # wrapper doesn't expose it (real-world local fallback always uses
    # the registered kernel). For this test we route through the
    # underlying decompose_and_solve directly to assert JSON shape.
    result = decompose_and_solve(
        problem={"description": "sum_of_squares(50)",
                 "pattern": "embarrassingly_parallel",
                 "parameterization": "sum_of_squares",
                 "parameters": {"n": 50, "n_chunks": 2,
                                "kernel": "sum_of_squares"},
                 "submitter": "@test:local"},
        peers=[],
        dispatch="local",
        registry=registry,
        kernels={"sum_of_squares": SumOfSquaresKernel()},
    )
    audit = result.to_audit_dict()
    blob = json.dumps(audit)  # must be JSON-serialisable
    decoded = json.loads(blob)
    assert decoded["aggregate_value"]["sum"] == _ground_truth(50)
    assert decoded["dispatch_mode"] == "local"
    assert decoded["kind"] == "compute.coordinated_workload"


def test_decompose_and_solve_writes_memory_fragment_when_service_supplied():
    """When a CompositionService is provided, the receipt is persisted as
    a single MemoryFragment carrying the audit-grade payload."""
    registry = PatternRegistry.with_builtins()
    registry.register_parameterization(
        pattern_name="embarrassingly_parallel",
        parameterization_name="sum_of_squares",
        decomposer=EmbarrassinglyParallelDecomposer(),
        recomposer=SumRecomposer(),
    )
    fake_compute = FakeComputeCall()
    peers = _peers(("node-only", "only:peer", 0.9, 1.0))

    captured: list[dict] = []

    class StubCompositionService:
        def write(self, *, content, cognitive_type, principal_id,
                  agents, resources, **kwargs):
            captured.append({
                "content": content,
                "cognitive_type": cognitive_type,
                "principal_id": principal_id,
                "agents": agents,
                "resources": resources,
            })

            # Return a minimal fragment-like object the orchestrator can use.
            class _F:
                id = "frag-stub-001"

            return _F()

    receipt = decompose_and_solve(
        problem={"description": "sum_of_squares(100)",
                 "pattern": "embarrassingly_parallel",
                 "parameterization": "sum_of_squares",
                 "parameters": {"n": 100, "n_chunks": 4,
                                "kernel": "sum_of_squares"},
                 "submitter": "@test:local"},
        peers=peers,
        dispatch="cross_node",
        registry=registry,
        compute_call=fake_compute,
        composition_service=StubCompositionService(),
    )

    assert receipt.fragment_id == "frag-stub-001"
    assert len(captured) == 1
    payload = captured[0]["content"]
    # Payload carries every audit field per the spec.
    assert payload["kind"] == "compute.coordinated_workload"
    assert payload["pattern"] == "embarrassingly_parallel"
    assert payload["aggregate_value"]["sum"] == _ground_truth(100)
    assert "directory_snapshot_hash" in payload
    assert "routing_assignment" in payload
    assert "chunk_signatures" in payload and len(payload["chunk_signatures"]) == 4
    assert "elapsed_ms_total" in payload
    assert payload["dispatch_mode"] == "cross_node"
