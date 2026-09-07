# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Spec-memory §3.7.3 tests — axiom_memory__retrieve scope rules.

The default scope is MIRIX-aware:
- ``episodic`` fragments are session-bound (current session only).
- ``core`` / ``procedural`` / ``resource`` cross sessions unconditionally.
- ``semantic`` crosses by relevance (phase-1 = no relevance filter; all
  semantic fragments come through).
- ``vault`` is never returned (handled by retraction layer).

Explicit scope literals (``"strict"``, ``"all"``, ``"session:<id>"``)
override the type-aware default.
"""

from __future__ import annotations

import asyncio

from axiom.extensions.builtins.mcp.platform_primitives import PlatformPrimitives


def _handler(name: str):
    contribution = PlatformPrimitives.contribution()
    h = contribution.dispatch.get(name)
    assert h is not None, f"missing handler for {name}"
    return h


def _compose(*, kind: str, principal: str, session_id: str, text: str):
    """Helper — write one fragment with explicit session_id."""
    compose = _handler("axiom_memory__compose")
    result = asyncio.run(
        compose(
            {
                "kind": kind,
                "content": _content_for_kind(kind, text),
                "principal": principal,
                "accountable_human_id": principal,
                "session_id": session_id,
            }
        )
    )
    assert result.get("ok") is True, result
    return result["fragment_id"]


def _content_for_kind(kind: str, text: str) -> dict:
    if kind == "episodic":
        return {"event_time": "2026-05-18T10:00:00+00:00", "summary": text}
    if kind == "procedural":
        return {"steps": [text], "summary": text}
    if kind == "resource":
        return {"ref": f"blob://{text}", "summary": text}
    return {"summary": text}


def _ids_in(result: dict) -> set[str]:
    return {f["id"] for f in result.get("fragments") or []}


class TestStrictScope:
    """``scope="strict"`` returns only fragments from a single session,
    regardless of cognitive type."""

    def test_strict_filters_to_explicit_session_id(self, tmp_axiom_home):
        eid_a = _compose(
            kind="episodic", principal="@u:x",
            session_id="session://A", text="in-A",
        )
        eid_b = _compose(
            kind="episodic", principal="@u:x",
            session_id="session://B", text="in-B",
        )
        retrieve = _handler("axiom_memory__retrieve")
        out = asyncio.run(
            retrieve({
                "principal": "@u:x",
                "limit": 50,
                "scope": "strict",
                "session_id": "session://A",
            })
        )
        ids = _ids_in(out)
        assert eid_a in ids
        assert eid_b not in ids


class TestAllScope:
    """``scope="all"`` ignores session_id entirely — every fragment for
    the principal comes through (subject to other filters)."""

    def test_all_returns_fragments_from_every_session(self, tmp_axiom_home):
        eid_a = _compose(
            kind="episodic", principal="@u:x",
            session_id="session://A", text="all-A",
        )
        eid_b = _compose(
            kind="episodic", principal="@u:x",
            session_id="session://B", text="all-B",
        )
        retrieve = _handler("axiom_memory__retrieve")
        out = asyncio.run(
            retrieve({
                "principal": "@u:x",
                "limit": 50,
                "scope": "all",
            })
        )
        ids = _ids_in(out)
        assert eid_a in ids
        assert eid_b in ids


class TestSessionLiteralScope:
    """``scope="session:<id_or_name>"`` filters to exactly that session."""

    def test_session_literal_filters_to_named_session(self, tmp_axiom_home):
        eid_a = _compose(
            kind="episodic", principal="@u:x",
            session_id="session://named-A", text="named-A",
        )
        eid_b = _compose(
            kind="episodic", principal="@u:x",
            session_id="session://named-B", text="named-B",
        )
        retrieve = _handler("axiom_memory__retrieve")
        out = asyncio.run(
            retrieve({
                "principal": "@u:x",
                "limit": 50,
                "scope": "session:session://named-A",
            })
        )
        ids = _ids_in(out)
        assert eid_a in ids
        assert eid_b not in ids


class TestDefaultScopeMIRIXAligned:
    """``scope="default"`` (the default) applies the MIRIX rule:
    episodic stays session-bound; core/procedural/resource cross-session
    unconditionally; legacy fragments (session_id="") always cross."""

    def test_episodic_default_filters_to_current_session(self, tmp_axiom_home):
        # "Current" is whichever session the caller supplies via the
        # session_id argument (the MCP server otherwise defaults to its
        # own process session, which is not what cross-vendor callers
        # want — they pass their session through).
        eid_current = _compose(
            kind="episodic", principal="@u:x",
            session_id="session://current", text="ep-current",
        )
        eid_other = _compose(
            kind="episodic", principal="@u:x",
            session_id="session://other", text="ep-other",
        )
        retrieve = _handler("axiom_memory__retrieve")
        out = asyncio.run(
            retrieve({
                "principal": "@u:x",
                "limit": 50,
                "session_id": "session://current",
                # scope omitted → "default"
            })
        )
        ids = _ids_in(out)
        assert eid_current in ids
        assert eid_other not in ids

    def test_procedural_default_crosses_sessions(self, tmp_axiom_home):
        # Write a procedural fragment in session B; retrieve from session A.
        eid_proc = _compose(
            kind="procedural", principal="@u:x",
            session_id="session://B", text="how-to-X",
        )
        retrieve = _handler("axiom_memory__retrieve")
        out = asyncio.run(
            retrieve({
                "principal": "@u:x",
                "limit": 50,
                "session_id": "session://A",
                "kind": "procedural",
            })
        )
        assert eid_proc in _ids_in(out)

    def test_resource_default_crosses_sessions(self, tmp_axiom_home):
        eid_res = _compose(
            kind="resource", principal="@u:x",
            session_id="session://B", text="dataset-Y",
        )
        retrieve = _handler("axiom_memory__retrieve")
        out = asyncio.run(
            retrieve({
                "principal": "@u:x",
                "limit": 50,
                "session_id": "session://A",
                "kind": "resource",
            })
        )
        assert eid_res in _ids_in(out)

    def test_legacy_empty_session_always_visible(self, tmp_axiom_home):
        """A fragment written without a session_id (legacy) returns
        regardless of scope — empty session is interpreted as 'pre-
        session-introduction, treat as cross-session'."""
        eid_legacy = _compose(
            kind="episodic", principal="@u:x",
            session_id="", text="legacy",
        )
        retrieve = _handler("axiom_memory__retrieve")
        out = asyncio.run(
            retrieve({
                "principal": "@u:x",
                "limit": 50,
                "session_id": "session://current",
                # default scope; legacy fragment should still appear
            })
        )
        assert eid_legacy in _ids_in(out)


class TestProvenanceIncludesSession:
    """Retrieve responses surface session_id so callers can audit
    cross-session bleed and attribution."""

    def test_retrieve_response_carries_session_id(self, tmp_axiom_home):
        eid = _compose(
            kind="episodic", principal="@u:x",
            session_id="session://attribute", text="audit",
        )
        retrieve = _handler("axiom_memory__retrieve")
        out = asyncio.run(
            retrieve({"principal": "@u:x", "limit": 50, "scope": "all"})
        )
        match = next(
            (f for f in out.get("fragments") or [] if f.get("id") == eid),
            None,
        )
        assert match is not None, out
        prov = match.get("provenance") or {}
        assert prov.get("session_id") == "session://attribute", match
