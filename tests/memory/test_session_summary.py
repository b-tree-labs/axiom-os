# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for long-term session memory — retrieval + summary + composer injection.

PromptComposer has a ``session_memory`` layer that was stubbed but
never populated. This module fills it from CompositionService so the
agent sees relevant prior-session context at turn time.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from axiom.artifacts.registry import ArtifactRegistry, SQLiteBackend
from axiom.infra.prompt_composer import PromptComposer
from axiom.memory.access import AccessGraphs
from axiom.memory.attest import AuditLog
from axiom.memory.composition import CompositionService
from axiom.memory.policy import PolicyCoord
from axiom.memory.session_summary import (
    build_session_memory_summary,
    inject_session_memory,
    list_fragments_by_principal,
)
from axiom.memory.trust import TrustGraph
from axiom.vega.identity.keypair import generate_keypair


def _service(tmp_path):
    kp = generate_keypair()
    reg = ArtifactRegistry(backend=SQLiteBackend(tmp_path / "artifacts.db"))
    audit = AuditLog(tmp_path / "audit.jsonl", signing_keypair=kp)
    return CompositionService(
        artifact_registry=reg,
        audit_log=audit,
        signing_keypair=kp,
        policy_coord=PolicyCoord(global_policy={"write": "private"}),
        access_graphs=AccessGraphs(),
        trust_graph=TrustGraph(),
    )


def _write_episodic(svc, principal, *, summary, when=None):
    """Helper: write an episodic fragment with a fact_kind="session_event"."""
    return svc.write(
        content={
            "event_time": (when or datetime.now(UTC)).isoformat(),
            "fact_kind": "session_event",
            "summary": summary,
        },
        cognitive_type="episodic",
        principal_id=principal,
        agents=set(),
        resources=set(),
    )


# ---------------------------------------------------------------------------
# list_fragments_by_principal
# ---------------------------------------------------------------------------


class TestListFragmentsByPrincipal:
    def test_empty_store_returns_empty(self, tmp_path):
        svc = _service(tmp_path)
        assert list_fragments_by_principal(svc, "@alice:demo") == []

    def test_returns_own_fragments_only(self, tmp_path):
        svc = _service(tmp_path)
        _write_episodic(svc, "@alice:demo", summary="alice session 1")
        _write_episodic(svc, "@alice:demo", summary="alice session 2")
        _write_episodic(svc, "@bob:demo", summary="bob session")

        frags = list_fragments_by_principal(svc, "@alice:demo")
        assert len(frags) == 2
        summaries = {f.content.get("summary") for f in frags}
        assert summaries == {"alice session 1", "alice session 2"}

    def test_orders_by_timestamp_newest_first(self, tmp_path):
        svc = _service(tmp_path)
        now = datetime.now(UTC)
        _write_episodic(svc, "@alice:demo", summary="old", when=now - timedelta(days=2))
        _write_episodic(svc, "@alice:demo", summary="mid", when=now - timedelta(days=1))
        _write_episodic(svc, "@alice:demo", summary="new", when=now)

        frags = list_fragments_by_principal(svc, "@alice:demo")
        assert [f.content["summary"] for f in frags] == ["new", "mid", "old"]

    def test_respects_limit(self, tmp_path):
        svc = _service(tmp_path)
        for i in range(10):
            _write_episodic(svc, "@alice:demo", summary=f"session {i}")

        frags = list_fragments_by_principal(svc, "@alice:demo", limit=3)
        assert len(frags) == 3


# ---------------------------------------------------------------------------
# build_session_memory_summary
# ---------------------------------------------------------------------------


class TestBuildSummary:
    def test_empty_returns_empty_string(self, tmp_path):
        svc = _service(tmp_path)
        summary = build_session_memory_summary(svc, "@alice:demo")
        assert summary == ""

    def test_includes_fragment_summaries(self, tmp_path):
        svc = _service(tmp_path)
        _write_episodic(svc, "@alice:demo", summary="Worked on Newton's 2nd law")
        _write_episodic(svc, "@alice:demo", summary="Clarified momentum vs inertia")

        text = build_session_memory_summary(svc, "@alice:demo")
        assert "Newton" in text
        assert "momentum" in text

    def test_header_tags_with_principal(self, tmp_path):
        svc = _service(tmp_path)
        _write_episodic(svc, "@alice:demo", summary="session 1")
        text = build_session_memory_summary(svc, "@alice:demo")
        assert "@alice:demo" in text

    def test_respects_max_fragments(self, tmp_path):
        svc = _service(tmp_path)
        for i in range(10):
            _write_episodic(svc, "@alice:demo", summary=f"session {i}")

        text = build_session_memory_summary(
            svc, "@alice:demo", max_fragments=3,
        )
        # Only 3 summaries should appear.
        occurrences = sum(1 for i in range(10) if f"session {i}" in text)
        assert occurrences == 3

    def test_skips_non_session_event_fragments(self, tmp_path):
        """Filter to fragments that look like session events (have a
        ``summary`` field or are episodic session kinds). Unrelated
        episodic fragments (e.g. retrieval-audit) are excluded."""
        svc = _service(tmp_path)
        svc.write(
            content={
                "event_time": datetime.now(UTC).isoformat(),
                "fact_kind": "retrieval_audit",
                "query_hash": "deadbeef",
            },
            cognitive_type="episodic",
            principal_id="@alice:demo",
            agents=set(),
            resources=set(),
        )
        _write_episodic(svc, "@alice:demo", summary="Real session event")

        text = build_session_memory_summary(svc, "@alice:demo")
        assert "Real session event" in text
        assert "deadbeef" not in text


# ---------------------------------------------------------------------------
# inject_session_memory — PromptComposer integration
# ---------------------------------------------------------------------------


class TestInjectSessionMemory:
    def test_populates_session_memory_layer(self, tmp_path):
        svc = _service(tmp_path)
        _write_episodic(svc, "@alice:demo", summary="Prior: F=ma")

        composer = PromptComposer()
        inject_session_memory(composer, svc, principal_id="@alice:demo")

        # The session_memory layer now has a contribution.
        debug = composer.debug()
        session_contribs = [c for c in debug if c.layer == "session_memory"]
        assert len(session_contribs) >= 1
        assert any("F=ma" in c.content for c in session_contribs)

    def test_empty_history_no_op(self, tmp_path):
        svc = _service(tmp_path)
        composer = PromptComposer()
        inject_session_memory(composer, svc, principal_id="@alice:demo")
        # No contributions added when there's no history.
        session_contribs = [c for c in composer.debug() if c.layer == "session_memory"]
        assert session_contribs == []

    def test_session_memory_is_optional_in_composer(self, tmp_path):
        """Session-memory contribution is marked ``required=False`` so it
        drops first under token-budget pressure — stale context is the
        first thing to lose."""
        svc = _service(tmp_path)
        _write_episodic(svc, "@alice:demo", summary="prior")
        composer = PromptComposer()
        inject_session_memory(composer, svc, principal_id="@alice:demo")

        contribs = [c for c in composer.debug() if c.layer == "session_memory"]
        assert contribs, "session_memory layer must have been populated"
        assert contribs[0].required is False

    def test_session_memory_is_in_cacheable_layer(self, tmp_path):
        """The session_memory layer (index 4) is among the cacheable
        prefix (LAYERS[:5]). Changing this wouldn't break tests but
        flag it if it moves — cache behavior matters for cost."""
        from axiom.infra.prompt_composer import CACHEABLE_LAYERS

        assert "session_memory" in CACHEABLE_LAYERS
