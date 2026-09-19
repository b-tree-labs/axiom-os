# Copyright (c) 2026 B-Tree Ventures, LLC
# SPDX-License-Identifier: Apache-2.0

"""``axiom_compute__decompose_and_solve`` — MCP-shaped surface.

Thin wrapper around ``orchestrator.decompose_and_solve`` that:

- Accepts a JSON-friendly ``problem`` dict (TOML-decoded if the caller
  used TOML on the wire).
- Resolves ``peers`` from ``display_name`` strings against the
  federation NodeRegistry when no explicit list is supplied.
- Returns a JSON-serialisable receipt dict (via
  ``CoordinatedReceipt.to_audit_dict()``) so the MCP client can render
  it without needing to import the dataclass.

This is the function the chat agent actually calls. Tests cover the
underlying ``decompose_and_solve``; this wrapper has minimal
behaviour beyond the resolution + projection.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from .orchestrator import CoordinatedReceipt, decompose_and_solve


__all__ = [
    "axiom_compute__decompose_and_solve",
]


def axiom_compute__decompose_and_solve(
    *,
    problem: dict[str, Any],
    pattern: Optional[str] = None,
    peers: Optional[list[str]] = None,
    dispatch: Literal["local", "cross_node"] = "cross_node",
    composition_service: Optional[Any] = None,
) -> dict[str, Any]:
    """User-facing one-shot: auto-decompose, route, dispatch, sign, persist.

    Parameters
    ----------
    problem
        Problem description (JSON / TOML-shaped). Must carry a
        ``parameters`` dict; should carry ``description`` and
        ``submitter``.
    pattern
        Explicit pattern from the closed vocabulary. ``None`` →
        inferred from the problem shape (only ``embarrassingly_parallel``
        is real-impl tonight).
    peers
        List of federation peer ``display_name``s. ``None`` → resolved
        from ``NodeRegistry`` (every verified peer); empty list → local
        dispatch only.
    dispatch
        ``"cross_node"`` (default when peers exist) or ``"local"``.
    composition_service
        Optional. When supplied, the receipt is persisted as a single
        MemoryFragment.

    Returns
    -------
    dict
        ``CoordinatedReceipt.to_audit_dict()`` — JSON-safe, fully
        replayable. The headline routing decision is in
        ``["routing_assignment"]``.
    """
    resolved_peers: Optional[list[Any]] = None
    if peers is not None:
        if not peers:
            resolved_peers = []
        else:
            from axiom.vega.federation.discovery import NodeRegistry
            reg = NodeRegistry()
            all_peers = reg.list_all()
            wanted = set(peers)
            resolved_peers = [p for p in all_peers if p.display_name in wanted]
            # Phase A: KnownNode rows don't yet advertise compute caps;
            # the orchestrator filters by `compute:<pattern>`. Inject
            # the declared capability so the selector lets them through.
            for p in resolved_peers:
                cap = f"compute:{pattern or 'embarrassingly_parallel'}"
                if cap not in (p.capabilities or []):
                    p.capabilities = list(p.capabilities or []) + [cap]

    receipt: CoordinatedReceipt = decompose_and_solve(
        problem=problem,
        peers=resolved_peers,
        dispatch=dispatch,
        pattern=pattern,
        composition_service=composition_service,
    )
    out = receipt.to_audit_dict()
    if receipt.fragment_id:
        out["fragment_id"] = receipt.fragment_id
    return out
