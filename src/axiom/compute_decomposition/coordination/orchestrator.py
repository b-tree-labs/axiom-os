# Copyright (c) 2026 B-Tree Ventures, LLC
# SPDX-License-Identifier: Apache-2.0

"""``decompose_and_solve`` — the user-facing one-shot.

End-to-end orchestrator:

1. Pattern lookup (explicit or inferred from the problem shape).
2. Decompose the problem into ChunkSpecs via the registered
   parameterization.
3. Materialise Chunks, snapshot the federation directory.
4. Deterministic ``select_peers`` — chunk_id → peer_id.
5. Parallel dispatch through ``PeerDispatcher`` (or
   ``LocalDispatcher`` when no peers are available).
6. Aggregate via the registered recomposer.
7. Persist a single audit-grade memory fragment (when a
   CompositionService is supplied) carrying:
   - the aggregate value + content_hash,
   - the directory snapshot's hash + ISO timestamp,
   - the per-chunk routing decision,
   - every peer's signature with valid/invalid flag,
   - the pattern + decomposer + recomposer used,
   - elapsed_ms total + per-chunk breakdown.

The fragment is the proof: anyone can re-run ``select_peers()`` on the
recorded snapshot + see the same routing, then verify each peer's
signature against its pubkey from a directory backup. The aggregate's
content_hash is the deterministic single-line answer to "did the run
produce this answer?"

Pattern inference (Phase A — minimal):

- An explicit ``pattern`` in the problem dict wins.
- Otherwise, if ``parameters`` carries an ``n`` (range size), pick
  ``embarrassingly_parallel``. Anything else raises
  ``NoQualifiedPattern``.

Phase B will route inference through the ADR-040 LLM-proposed pattern
+ verifier loop.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable, Iterable, Literal, Optional

from axiom.compute_decomposition.aggregator import aggregate_results
from axiom.compute_decomposition.registry import (
    BUILTIN_PATTERN_NAMES,
    PatternRegistry,
)
from axiom.compute_decomposition.runner import LocalDispatcher
from axiom.compute_decomposition.types import (
    AggregatedArtifact,
    ChunkResult,
    DecompositionPlan,
    Problem,
)

from .peer_dispatcher import (
    PeerDispatcher,
    SignedChunkResult,
    default_compute_call,
)
from .peer_select import (
    NoQualifiedPeers,
    PeerSelection,
    select_peers,
)


__all__ = [
    "CoordinatedReceipt",
    "NoQualifiedPattern",
    "decompose_and_solve",
]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class NoQualifiedPattern(RuntimeError):
    """Raised when we can't pick a pattern for the problem shape."""


# ---------------------------------------------------------------------------
# Receipt
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CoordinatedReceipt:
    """The audit-grade record of one ``decompose_and_solve`` run.

    This is what the orchestrator returns AND what gets serialised
    into the persisted MemoryFragment. Every field is reproducible
    from the receipt alone (with the directory snapshot in hand).
    """

    plan_id: str
    problem_id: str
    pattern: str
    parameterization: str
    dispatch_mode: Literal["local", "cross_node"]

    aggregate_value: dict[str, Any]
    aggregate_content_hash: str

    routing_assignment: dict[str, str]            # chunk_id → peer_id
    ordered_peer_ids: tuple[str, ...]
    snapshot_at: str
    directory_snapshot_hash: str
    chunk_signatures: list[SignedChunkResult]

    elapsed_ms_total: float
    per_chunk_elapsed_ms: dict[str, float]

    # Populated when a CompositionService write happened.
    fragment_id: Optional[str] = None

    def to_audit_dict(self) -> dict[str, Any]:
        return {
            "kind": "compute.coordinated_workload",
            "plan_id": self.plan_id,
            "problem_id": self.problem_id,
            "pattern": self.pattern,
            "parameterization": self.parameterization,
            "dispatch_mode": self.dispatch_mode,
            "aggregate_value": self.aggregate_value,
            "aggregate_content_hash": self.aggregate_content_hash,
            "routing_assignment": dict(self.routing_assignment),
            "ordered_peer_ids": list(self.ordered_peer_ids),
            "snapshot_at": self.snapshot_at,
            "directory_snapshot_hash": self.directory_snapshot_hash,
            "chunk_signatures": [s.to_audit_dict() for s in self.chunk_signatures],
            "elapsed_ms_total": self.elapsed_ms_total,
            "per_chunk_elapsed_ms": dict(self.per_chunk_elapsed_ms),
        }


# ---------------------------------------------------------------------------
# Pattern resolution
# ---------------------------------------------------------------------------


def _resolve_pattern(
    problem_dict: dict[str, Any],
    explicit_pattern: Optional[str],
) -> tuple[str, str]:
    """Return (pattern_name, parameterization_name).

    Phase A: explicit > pattern_hint > shape-inference. Anything we
    can't decide raises ``NoQualifiedPattern``.
    """
    pattern = explicit_pattern or problem_dict.get("pattern") \
        or problem_dict.get("pattern_hint")
    parameterization = problem_dict.get("parameterization")

    if pattern is None:
        # Shape-based inference (minimal).
        params = problem_dict.get("parameters", {})
        if "n" in params:
            pattern = "embarrassingly_parallel"
        else:
            raise NoQualifiedPattern(
                "could not infer pattern from problem shape; "
                "supply 'pattern' explicitly. Closed vocabulary: "
                f"{sorted(BUILTIN_PATTERN_NAMES)}"
            )

    if pattern not in BUILTIN_PATTERN_NAMES:
        raise NoQualifiedPattern(
            f"unknown pattern {pattern!r}; closed vocabulary is "
            f"{sorted(BUILTIN_PATTERN_NAMES)}"
        )

    if parameterization is None:
        # Phase A convention: parameterization name = kernel name when
        # one is supplied, else the canonical default.
        kernel = problem_dict.get("parameters", {}).get("kernel")
        parameterization = kernel or "default"

    return pattern, parameterization


# ---------------------------------------------------------------------------
# Snapshot helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _qualified_peers(
    peers: Iterable[Any],
    *,
    require_verified: bool = True,
) -> list[Any]:
    """Filter peers by verified state. The capability filter happens
    inside ``select_peers`` so the snapshot hash spans every peer the
    caller saw."""
    out = []
    for p in peers:
        s = getattr(p, "state", "verified")
        s = s.value if hasattr(s, "value") else str(s)
        if not require_verified or s in {"verified", "trusted", "federated"}:
            out.append(p)
    return out


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def decompose_and_solve(
    *,
    problem: dict[str, Any],
    peers: Optional[list[Any]],
    dispatch: Literal["local", "cross_node"] = "cross_node",
    pattern: Optional[str] = None,
    registry: Optional[PatternRegistry] = None,
    kernels: Optional[dict[str, Any]] = None,
    compute_call: Optional[Callable[..., SignedChunkResult]] = None,
    composition_service: Optional[Any] = None,
    submitter_principal: Optional[str] = None,
    snapshot_at: Optional[str] = None,
) -> CoordinatedReceipt:
    """One-shot: decompose, route, dispatch, aggregate, sign, persist.

    Parameters
    ----------
    problem : dict
        Problem description (TOML/JSON). Shape::

            {
              "description": "human-readable",
              "pattern": "embarrassingly_parallel",  # optional
              "parameterization": "sum_of_squares",  # optional
              "parameters": {"n": 1_000_000, "n_chunks": 8, ...},
              "submitter": "@you:context",
            }

    peers : list | None
        Federation peers to consider. ``None`` (or empty) → local
        dispatch only.
    dispatch : {"local", "cross_node"}
        Force a path. Defaults to ``cross_node`` when peers are present
        and ``local`` when not.
    pattern : str | None
        Override the pattern. Otherwise inferred from the problem shape.
    registry : PatternRegistry | None
        Pattern registry. Defaults to the process-global one.
    kernels : dict[str, Kernel] | None
        Kernel map for the local-dispatch path. Required when
        ``dispatch="local"``.
    compute_call : callable | None
        Per-chunk cross-NODE compute call. Defaults to the real
        ``scidisplay.compute.compute`` SSH-dispatch; tests inject a
        fake.
    composition_service : Any | None
        When supplied, the receipt is persisted as a single
        MemoryFragment via ``write(...)``. ``None`` skips persistence
        (the receipt is still returned).
    submitter_principal : str | None
        Principal id for the persistence call. Falls back to
        ``problem["submitter"]``.
    snapshot_at : str | None
        ISO timestamp for the directory snapshot. Defaults to
        ``datetime.now(UTC).isoformat()``.

    Returns
    -------
    CoordinatedReceipt
        The audit-grade record. ``fragment_id`` is populated when a
        CompositionService write completed.
    """
    if registry is None:
        from axiom.compute_decomposition.registry import _get_default_registry
        registry = _get_default_registry()

    # 1. Pattern lookup.
    pattern_name, parameterization_name = _resolve_pattern(problem, pattern)
    try:
        param = registry.get_parameterization(pattern_name, parameterization_name)
    except KeyError as e:
        raise NoQualifiedPattern(str(e)) from e

    # 2. Decompose.
    submitter = problem.get("submitter") or submitter_principal or "@anon:local"
    p = Problem.create(
        description=problem.get("description", f"{pattern_name}/{parameterization_name}"),
        pattern_hint=pattern_name,
        parameters=problem.get("parameters", {}),
        submitter=submitter,
    )
    specs = param.decomposer(p, registry)
    plan = DecompositionPlan.create(
        problem_id=p.problem_id,
        pattern_name=pattern_name,
        parameterization_name=parameterization_name,
        chunks=specs,
        seed_seed=None,
        proposer="user",
    )
    chunks = [s.to_chunk(plan_id=plan.plan_id, seed=None) for s in specs]

    # 3. Decide dispatch path.
    use_cross_node = dispatch == "cross_node" and peers
    snapshot_at_iso = snapshot_at or _now_iso()

    # 4. Dispatch.
    t0 = time.perf_counter()
    chunk_signatures: list[SignedChunkResult] = []
    routing: dict[str, str] = {}
    ordered_peer_ids: tuple[str, ...] = ()
    snapshot_hash = ""

    if use_cross_node:
        qualified = _qualified_peers(peers)
        if not qualified:
            # No verified peers — fall back to local dispatch.
            use_cross_node = False
        else:
            required_cap = f"compute:{pattern_name}"
            try:
                selection: PeerSelection = select_peers(
                    plan,
                    qualified,
                    snapshot_at=snapshot_at_iso,
                    chunks=chunks,
                    required_capability=required_cap,
                )
            except NoQualifiedPeers:
                use_cross_node = False
            else:
                routing = selection.assignment
                ordered_peer_ids = selection.ordered_peer_ids
                snapshot_hash = selection.snapshot_hash
                peers_by_id = {p.node_id: p for p in qualified}
                dispatcher = PeerDispatcher(
                    assignment=routing,
                    peers_by_id=peers_by_id,
                    compute_call=compute_call or default_compute_call,
                )
                chunk_signatures = dispatcher.dispatch_all(chunks)

    if not use_cross_node:
        # Local-only. Build a LocalDispatcher with the supplied kernels.
        if kernels is None:
            raise ValueError(
                "local dispatch requires kernels= (got None). "
                "Pass {'<kernel_name>': SomeKernel()} to dispatch locally."
            )
        local = LocalDispatcher(
            kernels=kernels,
            leaf_node_id=submitter or "@local-leaf:demo",
        )
        chunk_results = local.dispatch_all(chunks)
        # Wrap as SignedChunkResult-shaped records (no signatures —
        # local execution doesn't sign).
        chunk_signatures = [
            SignedChunkResult(
                chunk_id=cr.chunk_id,
                payload=dict(cr.payload),
                elapsed_ms=cr.elapsed_ms,
                executed_on_peer="",
                signature_valid=None,
            )
            for cr in chunk_results
        ]

    elapsed_ms_total = (time.perf_counter() - t0) * 1000

    # 5. Aggregate.
    chunk_results: list[ChunkResult] = [
        sig.to_chunk_result(plan_id=plan.plan_id) for sig in chunk_signatures
    ]
    aggregated: AggregatedArtifact = aggregate_results(
        plan, chunks, chunk_results, param.recomposer,
    )

    # 6. Build the receipt.
    per_chunk_elapsed = {sig.chunk_id: sig.elapsed_ms for sig in chunk_signatures}
    if use_cross_node:
        # Local-only path: chunk_signatures carry [] signature lists,
        # but we keep them as is. The spec's "local-only -> chunk_signatures
        # == []" check filters out the no-peer entries.
        receipt_signatures = chunk_signatures
    else:
        receipt_signatures = []

    receipt = CoordinatedReceipt(
        plan_id=plan.plan_id,
        problem_id=p.problem_id,
        pattern=pattern_name,
        parameterization=parameterization_name,
        dispatch_mode="cross_node" if use_cross_node else "local",
        aggregate_value=dict(aggregated.payload),
        aggregate_content_hash=aggregated.output_ref.content_hash,
        routing_assignment=dict(routing),
        ordered_peer_ids=tuple(ordered_peer_ids),
        snapshot_at=snapshot_at_iso if use_cross_node else "",
        directory_snapshot_hash=snapshot_hash,
        chunk_signatures=receipt_signatures,
        elapsed_ms_total=elapsed_ms_total,
        per_chunk_elapsed_ms=per_chunk_elapsed,
    )

    # 7. Optional persistence.
    if composition_service is not None:
        principal = submitter_principal or submitter
        agents = {"orchestrator:coordinated_workloads"}
        resources = {
            f"plan:{plan.plan_id}",
            f"problem:{p.problem_id}",
            f"pattern:{pattern_name}/{parameterization_name}",
            f"output:{aggregated.output_ref.content_hash}",
        }
        for sig in receipt_signatures:
            if sig.signed_by_node_id:
                resources.add(f"signed_by:{sig.signed_by_node_id}")

        frag = composition_service.write(
            content=receipt.to_audit_dict(),
            cognitive_type="resource",
            principal_id=principal,
            agents=agents,
            resources=resources,
        )
        receipt = _replace_fragment_id(receipt, frag.id)

    return receipt


def _replace_fragment_id(r: CoordinatedReceipt, fragment_id: str) -> CoordinatedReceipt:
    """Frozen dataclass replacement for the single mutable bit."""
    import dataclasses
    return dataclasses.replace(r, fragment_id=fragment_id)
