# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""MCP courier handlers — the in-harness surface (PRD R16).

Doctrine: deterministic to the words. ``today`` and ``receipt`` return
the server-composed projection VERBATIM; the harness agent relays.
``direct`` is propose-only over MCP (ADR-114 propose floor): it can
record a proposed focus with provenance, never displace an active one.
Any model participation beyond these projections is a separately
governed escalation seat — not wired here, by design.
"""

from __future__ import annotations

from typing import Any


def today(args: dict[str, Any]) -> dict[str, Any]:
    """The oversight brief, byte-stable. Relay the ``text`` field as-is."""
    from axiom.extensions.builtins.receipts.skills.today import run

    result = run({"site": args.get("site"), "snapshot": bool(args.get("snapshot", True))})
    return {"ok": result.ok, **(result.value or {})}


def receipt(args: dict[str, Any]) -> dict[str, Any]:
    """One entity's evidence record, verbatim."""
    from axiom.extensions.builtins.receipts.skills.receipt import run

    result = run(
        {
            "entity_kind": args.get("entity_kind", "node"),
            "entity_id": args.get("entity_id", ""),
            "claim_kind": args.get("claim_kind", ""),
        }
    )
    return {"ok": result.ok, "errors": result.errors, "receipt": result.value}


def direct(args: dict[str, Any]) -> dict[str, Any]:
    """PROPOSE (never set) the day focus from a harness session."""
    from axiom.extensions.builtins.receipts.skills.direct import run

    mode = "get" if args.get("text") is None else "propose"  # MCP floor: propose-only
    result = run(
        {
            "site": args.get("site", ""),
            "text": args.get("text"),
            "mode": mode,
            "set_by": str(args.get("proposer", "@mcp:unattributed")),
        }
    )
    return {"ok": result.ok, "errors": result.errors, **(result.value or {})}
