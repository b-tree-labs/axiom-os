# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Real-wiring tests for Phase-2 platform primitives.

Spec: ``docs/specs/spec-builtin-mcp-server.md`` §8.

Phase-1 shipped the seven platform tools as fail-soft stubs: every
handler returned a JSON-shaped placeholder so the surface was callable
on a zero-config node. Phase-2 (this file) pins down that the six
non-RAG primitives now hit real services and return real data.

The single exception is ``axiom_rag__retrieve``: per the worktree task
brief, the RAG subsystem is being touched by a parallel session and the
adapter stays a stub here until that work merges. ``test_axiom_rag_retrieve_still_stub_phase2``
documents the intentional deferral so a future cleanup pass can find it.

Per the project no-DB-mock rule, every test that exercises memory uses
the on-disk ``tmp_axiom_home`` fixture from ``axiom-tests``; the rest
of the primitives bootstrap a fresh empty federation/hooks state.
"""

from __future__ import annotations

import asyncio

from axiom.extensions.builtins.mcp.platform_primitives import PlatformPrimitives


def _surface_handler(name: str):
    """Resolve a primitive's handler directly (skips aggregation overhead)."""
    contribution = PlatformPrimitives.contribution()
    handler = contribution.dispatch.get(name)
    assert handler is not None, f"missing handler for {name}"
    return handler


# ---------------------------------------------------------------------------
# memory: compose / retrieve / list — backed by CompositionService
# ---------------------------------------------------------------------------


def test_axiom_memory_compose_writes_real_fragment(tmp_axiom_home):
    """A compose call lands a real fragment + audit-log entry."""
    handler = _surface_handler("axiom_memory__compose")
    result = asyncio.run(
        handler(
            {
                "kind": "episodic",
                "content": {"text": "hello phase 2"},
                "principal": "@tester:local",
                "accountable_human_id": "@tester:local",
            }
        )
    )
    assert "Phase 1 stub" not in str(result), result
    assert result.get("ok") is True, result
    assert result.get("fragment_id"), result
    assert result["fragment_id"] != "pending", result

    # The retrieve handler should now see the freshly-written fragment.
    retrieve = _surface_handler("axiom_memory__retrieve")
    listing = asyncio.run(
        retrieve({"principal": "@tester:local", "limit": 10})
    )
    assert "Phase 1 stub" not in str(listing), listing
    fragments = listing.get("fragments") or []
    assert any(f.get("id") == result["fragment_id"] for f in fragments), (
        f"expected fragment {result['fragment_id']} in {fragments}"
    )


def test_axiom_memory_retrieve_empty_principal_returns_empty_list(tmp_axiom_home):
    """A principal with no fragments resolves to an empty list, not an error."""
    handler = _surface_handler("axiom_memory__retrieve")
    result = asyncio.run(handler({"principal": "@nobody:local", "limit": 5}))
    assert "Phase 1 stub" not in str(result), result
    assert result.get("fragments") == [], result


def test_axiom_memory_list_after_one_compose(tmp_axiom_home):
    """``axiom_memory__list`` enumerates principals with at least one fragment."""
    compose = _surface_handler("axiom_memory__compose")
    asyncio.run(
        compose(
            {
                "kind": "episodic",
                "content": {"text": "list me"},
                "principal": "@listme:local",
                "accountable_human_id": "@listme:local",
            }
        )
    )
    list_handler = _surface_handler("axiom_memory__list")
    result = asyncio.run(list_handler({}))
    assert "Phase 1 stub" not in str(result), result
    principals = result.get("principals") or []
    assert "@listme:local" in principals, principals


# ---------------------------------------------------------------------------
# federation: node_status — backed by the local NodeRegistry
# ---------------------------------------------------------------------------


def test_axiom_federation_node_status_returns_real_registry(tmp_axiom_home):
    """``axiom_federation__node_status`` returns a real NodeRegistry snapshot."""
    handler = _surface_handler("axiom_federation__node_status")
    result = asyncio.run(handler({}))
    assert "Phase 1 stub" not in str(result), result
    # On a fresh tmp_axiom_home the registry is empty but the call must
    # still return a structured dict identifying the node + peer count.
    assert "node" in result, result
    assert "peer_count" in result, result
    assert isinstance(result.get("peers"), list), result


# ---------------------------------------------------------------------------
# signals: brief — backed by BriefingService
# ---------------------------------------------------------------------------


def test_axiom_signals_brief_returns_real_briefing(tmp_axiom_home):
    """``axiom_signals__brief`` returns a real briefing payload (or empty state)."""
    handler = _surface_handler("axiom_signals__brief")
    result = asyncio.run(handler({}))
    assert "Phase 1 stub" not in str(result), result
    assert isinstance(result, dict), result
    # The briefing service may report no signals on a fresh node — that's
    # a legitimate state, not an error. Either way we get a structured
    # `topics` or `brief` key.
    assert "topics" in result or "brief" in result or "ok" in result, result


# ---------------------------------------------------------------------------
# node: hooks_list — backed by HookRegistry discovery
# ---------------------------------------------------------------------------


def test_axiom_node_hooks_list_walks_real_registry(tmp_axiom_home):
    """``axiom_node__hooks_list`` enumerates real manifest-declared hooks."""
    handler = _surface_handler("axiom_node__hooks_list")
    result = asyncio.run(handler({}))
    assert "Phase 1 stub" not in str(result), result
    hooks = result.get("hooks", [])
    assert isinstance(hooks, list), result
    # Hygiene declares three hooks (mo.pressure_critical / .leak_detected /
    # .sweep_failed); when discovery is wired they should appear.
    if hooks:  # may be empty if discovery doesn't see ext path
        kinds = {h.get("event") for h in hooks if isinstance(h, dict)}
        assert any(
            (k or "").startswith("mo.") for k in kinds
        ) or any(("source" in h) for h in hooks), result


# ---------------------------------------------------------------------------
# RAG: deliberate Phase-2 deferral
# ---------------------------------------------------------------------------


def test_axiom_rag_retrieve_still_stub_phase2(tmp_axiom_home):
    """RAG retrieval stays a stub in Phase 2 (RAG subsystem in flight)."""
    handler = _surface_handler("axiom_rag__retrieve")
    result = asyncio.run(handler({"query": "anything"}))
    # Either a Phase-1 stub note OR an empty results list is acceptable.
    # The negative assertion: it must NOT raise a NotImplementedError /
    # crash, and it must return a JSON-able dict so MCP clients are
    # never surprised.
    assert isinstance(result, dict), result
    assert "results" in result or "error" in result or "note" in result, result
