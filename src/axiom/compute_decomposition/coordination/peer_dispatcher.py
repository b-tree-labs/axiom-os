# Copyright (c) 2026 B-Tree Ventures, LLC
# SPDX-License-Identifier: Apache-2.0

"""``PeerDispatcher`` — federation-aware Dispatcher Protocol impl.

Variant of FMP's ``SubprocessDispatcher`` that fans chunks out to
federation peers via the cross-NODE compute path. For each chunk:

1. Look up the peer assignment from ``select_peers()``.
2. Build a ``ComputationRequest`` carrying the peer's
   ``display_name`` (the cross-NODE compute path SSH-dispatches by
   display_name → registry lookup → ssh_user/ssh_host).
3. Call the injected ``compute_call(...)`` (real call:
   ``axiom.extensions.builtins.scidisplay.compute.compute``; tests:
   a fake that returns a synthetic SignedChunkResult).
4. Wrap the per-peer result back into a ``SignedChunkResult`` carrying
   the executing peer's Ed25519 signature.

Parallelism: chunks are dispatched concurrently via a thread pool.
Each cross-NODE call is mostly SSH-bound (network IO), so threads are
the right primitive. The pool size defaults to ``len(peers)`` so peer
saturation matches the assignment.

The returned ``SignedChunkResult`` records:

- the per-chunk payload (decoded JSON-able dict),
- elapsed_ms (wall clock for the dispatch),
- the executing peer's node_id + display_name + pubkey,
- the Ed25519 signature_b64 + signature_valid flag,
- the canonical_hash the peer signed over.

The aggregator consumes ``SignedChunkResult.payload`` for the
recompose step + carries the signature metadata onto the receipt.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from axiom.compute_decomposition.types import Chunk, ChunkResult, ContentRef


__all__ = [
    "PeerDispatcher",
    "PeerExecution",
    "SignedChunkResult",
    "default_compute_call",
]


# ---------------------------------------------------------------------------
# Per-chunk signed result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SignedChunkResult:
    """A chunk result with the executing peer's Ed25519 attestation.

    This is the wire-format the PeerDispatcher returns. The aggregator
    consumes ``payload`` for the recompose step + carries every other
    field onto the audit-grade receipt.

    Attributes
    ----------
    chunk_id, payload, elapsed_ms
        Standard chunk-result fields (mirror ``ChunkResult``).
    executed_on_peer
        The peer's display_name (e.g. ``"<host>:<user>"``). Empty for
        local-only execution paths.
    signed_by_node_id, signed_by_display_name, signing_pubkey_b64
        Identity of the signer — used to cross-check against the
        federation directory at verify time.
    signature_b64
        base64(Ed25519 sig) over the canonical (latex|mode|value|ast)
        tuple — see ``scidisplay.compute_signing.canonical_message``.
        For non-scidisplay computations the canonical form is the
        chunk's payload JSON; the signing primitive is the same.
    signature_valid
        ``True`` if the signature verified against the expected pubkey
        from the federation directory, ``False`` if not, ``None`` if
        verification was skipped (no expected pubkey available).
    signature_verification_reason
        Populated on ``signature_valid=False`` so the audit log shows
        why a peer's claim was rejected.
    canonical_hash
        sha256 of the canonical message — convenience for cross-receipt
        joins without re-hashing.
    """

    chunk_id: str
    payload: dict[str, Any]
    elapsed_ms: float = 0.0
    executed_on_peer: str = ""
    signed_by_node_id: str = ""
    signed_by_display_name: str = ""
    signing_pubkey_b64: str = ""
    signature_b64: str = ""
    signature_valid: Optional[bool] = None
    signature_verification_reason: str = ""
    canonical_hash: str = ""

    def to_audit_dict(self) -> dict[str, Any]:
        """Receipt-friendly projection (signature kept, payload dropped to
        keep the receipt compact — full payload lives in the artifact
        registry)."""
        return {
            "chunk_id": self.chunk_id,
            "executed_on_peer": self.executed_on_peer,
            "signed_by_node_id": self.signed_by_node_id,
            "signed_by_display_name": self.signed_by_display_name,
            "signing_pubkey_b64": self.signing_pubkey_b64,
            "signature_b64": self.signature_b64,
            "signature_valid": self.signature_valid,
            "signature_verification_reason": self.signature_verification_reason,
            "canonical_hash": self.canonical_hash,
            "elapsed_ms": self.elapsed_ms,
        }

    def to_chunk_result(self, *, plan_id: str) -> ChunkResult:
        """Project into the FMP ``ChunkResult`` shape so the aggregator
        can run unchanged."""
        return ChunkResult(
            chunk_id=self.chunk_id,
            plan_id=plan_id,
            leaf_node_id=self.signed_by_node_id or "@local",
            output=ContentRef.from_payload(self.payload),
            payload=dict(self.payload),
            elapsed_ms=int(self.elapsed_ms),
            seed_used=None,
            signature=self.signature_b64.encode("utf-8") if self.signature_b64 else b"",
        )


@dataclass(frozen=True)
class PeerExecution:
    """A bound (chunk, peer) execution record before dispatch."""

    chunk: Chunk
    peer_id: str
    peer_display_name: str


# ---------------------------------------------------------------------------
# Default cross-NODE compute call (real path)
# ---------------------------------------------------------------------------


def default_compute_call(
    *,
    peer_id: str,
    peer_display_name: str,
    chunk: Chunk,
) -> SignedChunkResult:
    """Real cross-NODE compute dispatch for a chunk.

    The current cross-NODE compute primitive
    (``scidisplay.compute.compute``) only knows how to ship ``latex``
    expressions — that's the scidisplay shape. For the
    embarrassingly_parallel kernel we ship the chunk parameters as a
    sympy expression that evaluates to the per-chunk sum-of-squares
    answer, then unwrap the verified result.

    Closed pattern vocab tonight: the only kernel we know how to ship
    cross-node is ``sum_of_squares``. Everything else raises
    ``NotImplementedError`` and is Phase B work.
    """
    from axiom.extensions.builtins.scidisplay.compute import (
        ComputationRequest,
        compute,
    )

    kernel = chunk.parameters.get("kernel", "sum_of_squares")
    if kernel != "sum_of_squares":
        raise NotImplementedError(
            f"cross-NODE dispatch for kernel {kernel!r} is Phase B work; "
            f"only 'sum_of_squares' is wired tonight"
        )

    lo = int(chunk.parameters["range_lo"])
    hi = int(chunk.parameters["range_hi"])
    # Build a SymPy-evaluable closed-form: Σ_{i=lo}^{hi-1} i^2.
    # The remote worker computes via SymPy + signs the (latex, mode,
    # value, ast) tuple. We unwrap value_repr → int.
    latex = f"\\sum_{{i={lo}}}^{{{hi - 1}}} i^2"

    req = ComputationRequest(
        latex=latex,
        mode="numeric",
        peer=peer_display_name,
    )
    t0 = time.perf_counter()
    res = compute(req)
    elapsed_ms = (time.perf_counter() - t0) * 1000

    if not res.success:
        return SignedChunkResult(
            chunk_id=chunk.chunk_id,
            payload={"sum": None, "error": res.error_message,
                     "error_class": res.error_class,
                     "range_lo": lo, "range_hi": hi},
            elapsed_ms=elapsed_ms,
            executed_on_peer=peer_display_name,
            signature_valid=False,
            signature_verification_reason=f"compute failed: {res.error_class}",
        )

    # value_repr from numeric mode is "<float>"-ish. Round to int because
    # the closed-form sum is integer-valued.
    try:
        value_int = int(round(float(res.value_repr)))
    except (TypeError, ValueError):
        value_int = None

    return SignedChunkResult(
        chunk_id=chunk.chunk_id,
        payload={"sum": value_int, "range_lo": lo, "range_hi": hi,
                 "value_repr": res.value_repr},
        elapsed_ms=elapsed_ms,
        executed_on_peer=res.executed_on_peer or peer_display_name,
        signed_by_node_id=res.signed_by_node_id,
        signed_by_display_name=res.signed_by_display_name,
        signing_pubkey_b64=res.signing_pubkey_b64,
        signature_b64=res.signature_b64,
        signature_valid=res.signature_valid,
        signature_verification_reason=res.signature_verification_reason,
        canonical_hash=res.extra.get("canonical_hash", ""),
    )


# ---------------------------------------------------------------------------
# PeerDispatcher
# ---------------------------------------------------------------------------


@dataclass
class PeerDispatcher:
    """Dispatcher Protocol: ``dispatch_all(chunks) -> [SignedChunkResult]``.

    Parallel by default — uses a thread pool. Each chunk maps through
    ``assignment[chunk.chunk_id]`` to its target peer; the per-call
    function ``compute_call`` is injected so tests can swap it for a
    fake without touching SSH.
    """

    assignment: dict[str, str]                    # chunk_id -> peer_id
    peers_by_id: dict[str, Any]                   # peer_id -> PeerLike
    compute_call: Callable[..., SignedChunkResult] = field(
        default=default_compute_call
    )
    max_workers: Optional[int] = None             # default: len(unique peers)

    def dispatch_one(self, chunk: Chunk) -> SignedChunkResult:
        peer_id = self.assignment.get(chunk.chunk_id)
        if peer_id is None:
            raise KeyError(
                f"no peer assignment for chunk {chunk.chunk_id!r}; "
                f"assignment covers {list(self.assignment.keys())[:3]}..."
            )
        peer = self.peers_by_id.get(peer_id)
        if peer is None:
            raise KeyError(
                f"assignment named peer {peer_id!r} but it isn't in "
                f"peers_by_id (have {list(self.peers_by_id.keys())[:3]}...)"
            )
        return self.compute_call(
            peer_id=peer_id,
            peer_display_name=getattr(peer, "display_name", peer_id),
            chunk=chunk,
        )

    def dispatch_all(self, chunks: list[Chunk]) -> list[SignedChunkResult]:
        if not chunks:
            return []
        n_workers = (self.max_workers
                     or max(1, len({self.assignment.get(c.chunk_id) for c in chunks})))
        results_by_chunk: dict[str, SignedChunkResult] = {}
        # Parallel fan-out; preserve input order on the way back.
        with ThreadPoolExecutor(max_workers=n_workers) as ex:
            futures = {ex.submit(self.dispatch_one, c): c.chunk_id for c in chunks}
            for fut in as_completed(futures):
                cid = futures[fut]
                results_by_chunk[cid] = fut.result()
        return [results_by_chunk[c.chunk_id] for c in chunks]
