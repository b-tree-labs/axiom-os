# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Tests for CompositionService.recall() + RecallIndex (ADR-087 D5).

Hybrid dense+sparse → RRF over the rag-memory corpus; hits resolve
back through read() so access checks, signature verification, and
tombstone exclusion are enforced on every recall result; forget()
evicts projected chunks; degraded (no-embedder) recall stays working
FTS-only and says so in the result metadata."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

PRINCIPAL = "@alice:recall"
AGENT = "axi"


def _fake_embedder(texts):
    """Deterministic 8-dim bag-of-chars embedding — no provider needed."""
    out = []
    for t in texts:
        h = hashlib.sha256(t.lower().encode()).digest()
        out.append([b / 255.0 for b in h[:8]])
    return out


def _build(tmp_path: Path, *, embedder):
    from axiom.artifacts.registry import ArtifactRegistry, SQLiteBackend
    from axiom.memory.access import (
        AccessGraphs,
        add_user_agent_edge,
    )
    from axiom.memory.attest import AuditLog
    from axiom.memory.composition import CompositionService
    from axiom.memory.policy import PolicyCoord
    from axiom.memory.recall import RecallIndex
    from axiom.memory.trust import TrustGraph
    from axiom.rag.sqlite_store import SQLiteRAGStore
    from axiom.vega.identity.keypair import generate_keypair

    kp = generate_keypair()
    graphs = add_user_agent_edge(AccessGraphs(), PRINCIPAL, AGENT)
    store = SQLiteRAGStore(f"sqlite:///{tmp_path}/recall.db")
    store.connect()
    index = RecallIndex(store=store, embedder=embedder)
    return CompositionService(
        artifact_registry=ArtifactRegistry(
            backend=SQLiteBackend(tmp_path / "a.db")
        ),
        audit_log=AuditLog(tmp_path / "audit.jsonl", signing_keypair=kp),
        signing_keypair=kp,
        policy_coord=PolicyCoord(global_policy={"write": "private"}),
        access_graphs=graphs,
        trust_graph=TrustGraph(),
        recall_index=index,
    )


@pytest.fixture
def service(tmp_path):
    return _build(tmp_path, embedder=None)  # FTS-only, deterministic


@pytest.fixture
def hybrid_service(tmp_path):
    return _build(tmp_path, embedder=_fake_embedder)


def _write(service, ctype, content, principal=PRINCIPAL):
    return service.write(
        content=content, cognitive_type=ctype,
        principal_id=principal, agents={AGENT}, resources=set(),
    )


class TestRecallBasics:
    def test_write_indexes_and_recall_finds(self, service):
        frag = _write(service, "semantic",
                      {"fact": "prefers test driven development"})
        _write(service, "semantic", {"fact": "morning meetings are blocked"})
        result = service.recall(
            "test driven", user=PRINCIPAL, agent=AGENT,
        )
        assert [f.id for f in result.fragments] == [frag.id]
        assert result.degraded is True  # no embedder configured

    def test_hybrid_not_degraded(self, hybrid_service):
        frag = _write(hybrid_service, "semantic",
                      {"fact": "prefers test driven development"})
        result = hybrid_service.recall(
            "test driven", user=PRINCIPAL, agent=AGENT,
        )
        assert frag.id in [f.id for f in result.fragments]
        assert result.degraded is False

    def test_recall_without_index_raises(self, tmp_path):
        import dataclasses

        service = _build(tmp_path, embedder=None)
        bare = dataclasses.replace(service, recall_index=None)
        with pytest.raises(RuntimeError, match="recall index"):
            bare.recall("anything", user=PRINCIPAL, agent=AGENT)

    def test_recall_scoped_to_principal_corpus(self, service):
        _write(service, "semantic", {"fact": "alice likes almond croissants"})
        _write(service, "semantic",
               {"fact": "bob likes almond croissants"}, principal="@bob:x")
        result = service.recall(
            "almond croissants", user=PRINCIPAL, agent=AGENT,
        )
        principals = {
            f.provenance.principal_id for f in result.fragments
        }
        assert principals == {PRINCIPAL}


class TestEnforcementOnResults:
    def test_recall_respects_access(self, service):
        _write(service, "semantic", {"fact": "prefers test driven development"})
        result = service.recall(
            "test driven", user="@stranger:recall", agent=AGENT,
            principal=PRINCIPAL,
        )
        assert result.fragments == []

    def test_vault_never_recallable(self, service):
        _write(service, "vault", {"secret": "APIKEY equals hunter2"})
        result = service.recall(
            "APIKEY hunter2", user=PRINCIPAL, agent=AGENT,
        )
        assert result.fragments == []
        # ...and nothing vault-shaped ever reached the index.
        store = service.recall_index.store
        stats_rows = store._conn.execute(
            "SELECT count(*) FROM chunks WHERE corpus LIKE 'rag-memory:%'"
        ).fetchone()
        assert stats_rows[0] == 0

    def test_forget_evicts_from_recall(self, service):
        frag = _write(service, "semantic",
                      {"fact": "prefers test driven development"})
        assert service.recall(
            "test driven", user=PRINCIPAL, agent=AGENT,
        ).fragments
        service.forget([frag.id], requester=PRINCIPAL, agent=AGENT)
        result = service.recall(
            "test driven", user=PRINCIPAL, agent=AGENT,
        )
        assert result.fragments == []
        rows = service.recall_index.store._conn.execute(
            "SELECT count(*) FROM chunks WHERE source_path = ?",
            (f"memory://{frag.id}",),
        ).fetchone()
        assert rows[0] == 0

    def test_stale_index_cannot_leak_forgotten(self, service):
        """Even if eviction were skipped, read()-resolution drops
        tombstoned fragments from recall results."""
        frag = _write(service, "semantic",
                      {"fact": "prefers test driven development"})
        # Tombstone the ledger rows directly, bypassing forget()'s eviction.
        for a in service.artifact_registry.find_by_name("fragment", frag.id):
            service.artifact_registry.delete(a.id, reason="test-stale")
        result = service.recall(
            "test driven", user=PRINCIPAL, agent=AGENT,
        )
        assert result.fragments == []


class TestFiltersAndScoring:
    def test_cognitive_type_filter(self, service):
        _write(service, "semantic", {"fact": "deploy checklist exists"})
        epi = _write(service, "episodic",
                     {"summary": "ran the deploy checklist",
                      "event_time": "2026-07-01T00:00:00+00:00"})
        result = service.recall(
            "deploy checklist", user=PRINCIPAL, agent=AGENT,
            cognitive_types=["episodic"],
        )
        assert [f.id for f in result.fragments] == [epi.id]

    def test_time_range_filter(self, service):
        old = _write(service, "episodic",
                     {"summary": "deploy went fine",
                      "event_time": "2026-01-01T00:00:00+00:00"})
        new = _write(service, "episodic",
                     {"summary": "deploy went poorly",
                      "event_time": "2026-07-01T00:00:00+00:00"})
        result = service.recall(
            "deploy", user=PRINCIPAL, agent=AGENT,
            since="2026-06-01T00:00:00+00:00",
        )
        ids = [f.id for f in result.fragments]
        assert new.id in ids and old.id not in ids

    def test_recency_bias_orders_recent_first(self, service):
        old = _write(service, "episodic",
                     {"summary": "reviewed the budget numbers",
                      "event_time": "2024-01-01T00:00:00+00:00"})
        new = _write(service, "episodic",
                     {"summary": "reviewed the budget numbers again",
                      "event_time": "2026-07-01T00:00:00+00:00"})
        result = service.recall(
            "budget numbers", user=PRINCIPAL, agent=AGENT,
            recency_bias=1.0,
        )
        ids = [f.id for f in result.fragments]
        assert ids.index(new.id) < ids.index(old.id)


class TestRebuild:
    def test_delete_and_rebuild_is_safe(self, service):
        frag = _write(service, "semantic",
                      {"fact": "prefers test driven development"})
        _write(service, "vault", {"secret": "APIKEY equals hunter2"})
        index = service.recall_index
        index.store.delete_corpus(f"rag-memory:{PRINCIPAL}")
        assert service.recall(
            "test driven", user=PRINCIPAL, agent=AGENT,
        ).fragments == []

        count = index.rebuild(service, principal=PRINCIPAL)
        assert count == 1  # vault stayed out
        result = service.recall(
            "test driven", user=PRINCIPAL, agent=AGENT,
        )
        assert [f.id for f in result.fragments] == [frag.id]
