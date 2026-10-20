# Copyright (c) 2026 B-Tree Ventures, LLC
# SPDX-License-Identifier: Apache-2.0

"""Coordination layer — cross-NODE compute assembly on top of FMP.

The base ``compute_decomposition`` package gives us:

- A closed pattern registry (``embarrassingly_parallel`` real, others stub).
- A ``LocalDispatcher`` and ``SubprocessDispatcher`` for per-leaf execution.
- An aggregator that produces a deterministic ``AggregatedArtifact``.

This subpackage stitches those pieces to **federation peers**:

- ``select_peers()`` — pure function: ``(plan, peers, snapshot_at)
  -> {chunk_id: peer_id}``. The deterministic routing decision; same
  inputs always produce the same output (no randomness, no time of day).
- ``PeerDispatcher`` — Dispatcher Protocol implementation that fans
  chunks out to federation peers via the cross-NODE compute path
  (``axiom.extensions.builtins.scidisplay.compute.compute``). Each
  per-chunk result carries the executing peer's Ed25519 signature so
  the aggregator can record it on the receipt.
- ``decompose_and_solve()`` — the user-facing one-shot orchestrator.
  Picks a pattern, decomposes the problem, snapshots the federation
  directory, deterministically routes chunks to peers, dispatches in
  parallel, aggregates with signature collection, and writes a single
  audit-grade memory fragment carrying the full receipt.

Closed pattern vocabulary tonight: only ``embarrassingly_parallel`` is
real-impl. The other five names exist in ``BUILTIN_PATTERN_NAMES`` but
have no parameterizations; calling ``decompose_and_solve()`` on them
raises ``NoQualifiedPattern``. Other patterns are Phase B work.
"""

from .peer_select import (
    NoQualifiedPeers,
    PeerAssignment,
    PeerSelection,
    select_peers,
)
from .peer_dispatcher import (
    PeerDispatcher,
    PeerExecution,
    SignedChunkResult,
)
from .orchestrator import (
    CoordinatedReceipt,
    NoQualifiedPattern,
    decompose_and_solve,
)
from .mcp_tool import axiom_compute__decompose_and_solve


__all__ = [
    "NoQualifiedPeers",
    "PeerAssignment",
    "PeerSelection",
    "select_peers",
    "PeerDispatcher",
    "PeerExecution",
    "SignedChunkResult",
    "CoordinatedReceipt",
    "NoQualifiedPattern",
    "decompose_and_solve",
    "axiom_compute__decompose_and_solve",
]
