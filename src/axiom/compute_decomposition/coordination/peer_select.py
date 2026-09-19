# Copyright (c) 2026 B-Tree Ventures, LLC
# SPDX-License-Identifier: Apache-2.0

"""Deterministic peer selection — load-bearing routing function.

Per the coordinated-workloads spec, ``select_peers`` is a **pure
function** of (plan, peers, snapshot_at):

- Same inputs always produce the same ``chunk_id -> peer_id`` map.
- No randomness, no time-of-day calls, no dependence on Python
  iteration order.
- Records the directory snapshot's content hash so an auditor can
  replay the decision later from the receipt alone.

Algorithm (deterministic):

1. **Filter** by required capability (e.g. ``compute:embarrassingly_parallel``)
   and by ``state == "verified"`` (or "trusted" / "federated" — anything
   that has cleared identity binding). Empty result raises
   ``NoQualifiedPeers``.

2. **Sort** the qualified peers by:
   - higher trust score first (``-trust_score``),
   - then lower latency estimate first,
   - finally ``node_id`` alphabetical to break the last tie.

   The trust score may come from ``memory.trust_retrieval.
   trust_score_for_node`` upstream; here we just consume the value
   the peer record advertises (``peer.trust_score``).

3. **Assign** chunks. If any peer advertises a ``compute_capacity_hint``,
   we use a deterministic capacity-weighted round-robin (largest-share
   modular arithmetic so total assignment honors the integer floor
   division). If every peer's capacity_hint is the same, that
   degenerates to plain round-robin.

The return value is a frozen ``PeerSelection`` carrying:

- ``assignment``: ``{chunk_id: peer_id}`` (peer_id, not display_name —
  display_name can collide; node_id is the cryptographic identity).
- ``snapshot_hash``: sha256 of the canonical JSON projection of the
  qualified peers + their advertised fields. This is the auditable
  proof that two replays compared the same directory state.
- ``snapshot_at``: the ISO timestamp the caller passed in.
- ``ordered_peer_ids``: the post-sort tie-broken peer ordering so the
  audit can verify our sort matched what was on file.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterable, Optional, Protocol, runtime_checkable

from axiom.compute_decomposition.types import DecompositionPlan


__all__ = [
    "NoQualifiedPeers",
    "PeerLike",
    "PeerAssignment",
    "PeerSelection",
    "select_peers",
]


_DEFAULT_REQUIRED_CAPABILITY = "compute:embarrassingly_parallel"
_VERIFIED_STATES = frozenset({"verified", "trusted", "federated"})


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class NoQualifiedPeers(RuntimeError):
    """Raised when zero peers pass the qualification filter.

    Caller decides whether to fall back to local dispatch or surface
    the error to the operator.
    """


# ---------------------------------------------------------------------------
# Protocol — what we read off a peer record
# ---------------------------------------------------------------------------


@runtime_checkable
class PeerLike(Protocol):
    """Duck-typed peer view. Both ``vega.federation.discovery.KnownNode``
    and the test-fixture ``FakePeer`` satisfy this. Real KnownNode rows
    don't have ``trust_score`` directly — callers compose them via
    ``memory.trust_retrieval.trust_score_for_node`` first.
    """

    node_id: str
    display_name: str

    # Optional fields — accessed via getattr with defaults below so
    # KnownNode instances (which lack them) still work.


def _peer_state(p: Any) -> str:
    s = getattr(p, "state", "verified")
    if hasattr(s, "value"):  # NodeState enum
        return s.value
    return str(s)


def _peer_capabilities(p: Any) -> tuple[str, ...]:
    caps = getattr(p, "capabilities", ())
    return tuple(caps) if caps else ()


def _peer_trust(p: Any) -> float:
    return float(getattr(p, "trust_score", 0.5))


def _peer_latency(p: Any) -> float:
    return float(getattr(p, "latency_estimate_ms", 100.0))


def _peer_capacity(p: Any) -> float:
    cap = getattr(p, "compute_capacity_hint", 1.0)
    cap = float(cap) if cap is not None else 1.0
    if cap <= 0:
        cap = 1.0
    return cap


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PeerAssignment:
    """A single chunk-to-peer routing decision (audit-grade row)."""

    chunk_id: str
    peer_id: str
    peer_display_name: str
    weight_used: float


@dataclass(frozen=True)
class PeerSelection:
    """The full deterministic routing decision.

    Attributes
    ----------
    assignment : dict[str, str]
        ``chunk_id -> peer_id`` — the headline output. peer_id is the
        peer's ``node_id`` (cryptographic identity, not display_name).
    rows : tuple[PeerAssignment, ...]
        The same data as ``assignment`` but in the deterministic
        chunk-order with display_names + weights so audits can render
        it without needing the full peer registry on hand.
    ordered_peer_ids : tuple[str, ...]
        Post-sort, post-filter peer_ids — proves the tie-break order.
    snapshot_hash : str
        ``sha256:<hex>`` of the canonical projection of the qualified
        peer set. Two replays with the same directory state produce
        identical hashes. The auditor uses this to reject replays that
        used a divergent registry snapshot.
    snapshot_at : str
        The caller-provided ISO timestamp. Embedded in the receipt so
        the auditor can match the snapshot to a directory backup.
    required_capability : str
        The capability filter applied. Recorded for replay.
    """

    assignment: dict[str, str]
    rows: tuple[PeerAssignment, ...]
    ordered_peer_ids: tuple[str, ...]
    snapshot_hash: str
    snapshot_at: str
    required_capability: str = _DEFAULT_REQUIRED_CAPABILITY


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _canonical_peer_projection(peers: list[Any]) -> list[dict[str, Any]]:
    """Project each peer into the *minimum* deterministic dict the
    audit actually depends on. Sorted by node_id for stable hashing."""
    rows = []
    for p in peers:
        rows.append({
            "node_id": getattr(p, "node_id", ""),
            "display_name": getattr(p, "display_name", ""),
            "state": _peer_state(p),
            "capabilities": sorted(_peer_capabilities(p)),
            "trust_score": _peer_trust(p),
            "latency_estimate_ms": _peer_latency(p),
            "compute_capacity_hint": _peer_capacity(p),
            "public_key": getattr(p, "public_key", ""),
        })
    rows.sort(key=lambda r: r["node_id"])
    return rows


def _hash_snapshot(qualified_rows: list[dict[str, Any]]) -> str:
    blob = json.dumps(qualified_rows, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"sha256:{hashlib.sha256(blob).hexdigest()}"


# ---------------------------------------------------------------------------
# Capacity-weighted deterministic round-robin
# ---------------------------------------------------------------------------


def _weighted_assign(
    chunk_ids: list[str],
    ordered_peers: list[Any],
) -> list[tuple[str, str, str, float]]:
    """Assign chunks to peers so the per-peer count is proportional to
    capacity, ties broken by the peer's position in ``ordered_peers``.

    Algorithm: largest-remainder method.

    1. Compute exact share = (capacity_i / total_capacity) * n_chunks.
    2. Floor share is the guaranteed allocation; remainder ranks the
       extras (tie-break: peer position in ordered list — already
       deterministic from the upstream sort).
    3. Walk chunks in sequence_index order and pour into peers up to
       their per-peer cap.

    Equal capacity → identical floor share → stable round-robin.
    """
    n = len(chunk_ids)
    total_cap = sum(_peer_capacity(p) for p in ordered_peers)
    shares: list[tuple[Any, int, float]] = []
    floor_used = 0
    for p in ordered_peers:
        exact = _peer_capacity(p) / total_cap * n
        floor = int(exact)
        floor_used += floor
        shares.append((p, floor, exact - floor))
    extras = n - floor_used

    # Distribute extras to the largest fractional remainders, breaking
    # ties by ordered_peers position (already deterministic).
    indexed = sorted(
        enumerate(shares),
        key=lambda x: (-x[1][2], x[0]),
    )
    counts = {p.node_id: floor for (p, floor, _) in shares}
    for i in range(extras):
        peer = indexed[i % len(indexed)][1][0]
        counts[peer.node_id] += 1

    # Walk chunks in order; pour into peers in ordered sequence,
    # respecting per-peer caps. This keeps the assignment deterministic
    # and contiguous-ish so adjacent chunks land on the same peer
    # (cache-locality friendly).
    assignment: list[tuple[str, str, str, float]] = []
    cursor = 0
    remaining = {p.node_id: counts[p.node_id] for p in ordered_peers}
    for cid in chunk_ids:
        # Find next peer with remaining capacity (deterministic walk).
        for offset in range(len(ordered_peers)):
            idx = (cursor + offset) % len(ordered_peers)
            peer = ordered_peers[idx]
            if remaining[peer.node_id] > 0:
                remaining[peer.node_id] -= 1
                assignment.append((cid, peer.node_id, peer.display_name,
                                   _peer_capacity(peer)))
                cursor = idx + 1
                break
    return assignment


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def select_peers(
    plan: DecompositionPlan,
    peers: Iterable[Any],
    *,
    snapshot_at: str,
    chunks: Optional[list[Any]] = None,
    required_capability: str = _DEFAULT_REQUIRED_CAPABILITY,
    require_verified: bool = True,
) -> PeerSelection:
    """Deterministic chunk → peer routing.

    Parameters
    ----------
    plan
        The decomposition plan (carries ``plan.chunks``, the ChunkSpecs
        we route).
    peers
        Federation directory snapshot. Anything PeerLike works — both
        ``vega.federation.discovery.KnownNode`` and test fakes.
    snapshot_at
        ISO timestamp of when the directory was snapshotted. Recorded
        on the receipt for replay/audit.
    chunks
        Optional materialized ``Chunk`` list (each carries a ``chunk_id``).
        When supplied, the assignment is keyed by the real chunk_ids.
        When omitted we synthesize ``chunk-NNNN`` ids from the plan's
        ChunkSpec sequence_index. The orchestrator always passes
        materialized chunks; tests can omit them for pure-function
        determinism checks.
    required_capability
        Filter: only peers whose ``capabilities`` list contains this
        string survive. Default is the embarrassingly_parallel pattern
        cap; the orchestrator passes the pattern's actual name.
    require_verified
        When ``True`` (default), only peers in
        ``{verified, trusted, federated}`` qualify. Tests can pass
        ``False`` to exercise the raw selector logic.

    Returns
    -------
    PeerSelection
        Frozen dataclass — the auditable routing record.

    Raises
    ------
    NoQualifiedPeers
        Zero peers passed the qualification filter.
    """
    # Materialise the input list so we hash a stable snapshot regardless
    # of caller iterator semantics.
    peer_list = list(peers)

    qualified = []
    for p in peer_list:
        if require_verified and _peer_state(p) not in _VERIFIED_STATES:
            continue
        caps = _peer_capabilities(p)
        if required_capability and required_capability not in caps:
            continue
        qualified.append(p)

    if not qualified:
        raise NoQualifiedPeers(
            f"no peers qualified for required_capability={required_capability!r} "
            f"(input had {len(peer_list)} peers; require_verified={require_verified})"
        )

    # Deterministic sort: trust desc, latency asc, node_id asc.
    qualified.sort(
        key=lambda p: (-_peer_trust(p), _peer_latency(p), p.node_id),
    )

    # Snapshot hash spans ALL peers passed in (so the auditor can
    # verify the filter was applied against the same registry the run
    # saw — not just the post-filter list).
    snapshot_hash = _hash_snapshot(_canonical_peer_projection(peer_list))

    src_iter = chunks if chunks is not None else plan.chunks
    chunk_ids = [
        c.chunk_id if hasattr(c, "chunk_id") and getattr(c, "chunk_id", None)
        else f"chunk-{c.sequence_index:04d}"
        for c in src_iter
    ]
    if not chunk_ids:
        return PeerSelection(
            assignment={},
            rows=(),
            ordered_peer_ids=tuple(p.node_id for p in qualified),
            snapshot_hash=snapshot_hash,
            snapshot_at=snapshot_at,
            required_capability=required_capability,
        )

    raw = _weighted_assign(chunk_ids, qualified)
    rows = tuple(
        PeerAssignment(
            chunk_id=cid, peer_id=pid, peer_display_name=disp, weight_used=w
        )
        for (cid, pid, disp, w) in raw
    )
    assignment = {row.chunk_id: row.peer_id for row in rows}

    return PeerSelection(
        assignment=assignment,
        rows=rows,
        ordered_peer_ids=tuple(p.node_id for p in qualified),
        snapshot_hash=snapshot_hash,
        snapshot_at=snapshot_at,
        required_capability=required_capability,
    )
