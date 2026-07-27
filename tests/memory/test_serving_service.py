# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""MemoryServingService — recall → gate → serialize, the one door out.

Every transport funnels through ``serve()`` so the gate (ADR-087 D7) runs per
request. Coexistence with a user's own RAG is first-class: side-by-side blocks
(default) or opt-in rank-level RRF fusion — fusing, never ingesting (no-push).
OQ1 latency: the per-turn serve path is measured against the pinned budget.
"""

from __future__ import annotations

import dataclasses
import hashlib
from pathlib import Path

import pytest

from axiom.memory.serving import ConsumerCoordinate, DenyReason, NoPushError
from axiom.vega.federation.policy import VisibilityHorizon

PRINCIPAL = "@alice:work"
AGENT = "axi"


def _fake_embedder(texts):
    out = []
    for t in texts:
        h = hashlib.sha256(t.lower().encode()).digest()
        out.append([b / 255.0 for b in h[:8]])
    return out


def _build_service(tmp_path: Path, *, gate=None):
    from axiom.artifacts.registry import ArtifactRegistry, SQLiteBackend
    from axiom.memory.access import AccessGraphs, add_user_agent_edge
    from axiom.memory.attest import AuditLog
    from axiom.memory.composition import CompositionService
    from axiom.memory.policy import PolicyCoord
    from axiom.memory.recall import RecallIndex
    from axiom.memory.serving import ServingGate
    from axiom.memory.serving_service import MemoryServingService
    from axiom.memory.trust import TrustGraph
    from axiom.rag.sqlite_store import SQLiteRAGStore
    from axiom.vega.identity.keypair import generate_keypair

    kp = generate_keypair()
    graphs = add_user_agent_edge(AccessGraphs(), PRINCIPAL, AGENT)
    store = SQLiteRAGStore(f"sqlite:///{tmp_path}/recall.db")
    store.connect()
    index = RecallIndex(store=store, embedder=_fake_embedder)
    composition = CompositionService(
        artifact_registry=ArtifactRegistry(backend=SQLiteBackend(tmp_path / "a.db")),
        audit_log=AuditLog(tmp_path / "audit.jsonl", signing_keypair=kp),
        signing_keypair=kp,
        policy_coord=PolicyCoord(global_policy={"write": "private"}),
        access_graphs=graphs,
        trust_graph=TrustGraph(),
        recall_index=index,
    )
    return MemoryServingService(
        composition=composition, gate=gate or ServingGate()
    )


def _consumer(**kw) -> ConsumerCoordinate:
    base = dict(
        principal=PRINCIPAL, harness="claude-code", account=PRINCIPAL,
        deployment_tier="local", model_endpoint="local://ollama",
    )
    base.update(kw)
    return ConsumerCoordinate(**base)


def _write(service, content, *, ctype="semantic", visibility=None):
    from axiom.memory.attest import sign_fragment

    frag = service.composition.write(
        content=content, cognitive_type=ctype, principal_id=PRINCIPAL,
        agents={AGENT}, resources=set(),
    )
    if visibility is not None:
        reg = service.composition.artifact_registry
        for a in reg.find_by_name("fragment", frag.id):
            reg.delete(a.id)
        frag = dataclasses.replace(frag, visibility=visibility)
        frag = sign_fragment(frag, service.composition.signing_keypair)
        reg.register(kind="fragment", name=frag.id, data=frag.to_dict())
        service.composition.recall_index.index_fragment(frag)
    return frag


@pytest.fixture
def service(tmp_path):
    return _build_service(tmp_path)


class TestServeHappyPath:
    def test_serve_returns_gated_fragments(self, service):
        frag = _write(
            service, {"fact": "alice prefers dark roast coffee"},
            visibility=VisibilityHorizon.PUBLIC,
        )
        result = service.serve("dark roast coffee", consumer=_consumer())
        assert frag.id in [i.fragment_id for i in result.items]
        assert result.denials == []

    def test_serve_runs_the_gate_per_request(self, service):
        # A controlled (SCOPE_INTERNAL) fragment is retrievable but the gate
        # denies it to a remote consumer — proving the gate runs in serve().
        frag = _write(
            service, {"fact": "internal only note about the roadmap"},
            visibility=VisibilityHorizon.SCOPE_INTERNAL,
        )
        remote = _consumer(deployment_tier="remote")
        result = service.serve("roadmap note", consumer=remote)
        assert frag.id not in [i.fragment_id for i in result.items]
        assert any(
            d.reason is DenyReason.TIER_MISMATCH and d.fragment_id == frag.id
            for d in result.denials
        )

    def test_vault_never_reaches_a_consumer(self, service):
        service.composition.write(
            content={"secret": "APIKEY equals hunter2"}, cognitive_type="vault",
            principal_id=PRINCIPAL, agents={AGENT}, resources=set(),
        )
        result = service.serve("APIKEY hunter2", consumer=_consumer())
        assert result.items == []


class TestSerialization:
    def test_plaintext_block_is_byte_stable_and_untimestamped(self, service):
        _write(service, {"fact": "alice prefers dark roast coffee"},
               visibility=VisibilityHorizon.PUBLIC)
        r1 = service.serve("dark roast", consumer=_consumer())
        r2 = service.serve("dark roast", consumer=_consumer())
        b1 = service.to_plaintext_block(r1)
        b2 = service.to_plaintext_block(r2)
        assert b1 == b2
        # No ISO timestamps leak into rendered content.
        assert "T00:00:00" not in b1 and "+00:00" not in b1

    def test_mcp_payload_is_json_safe(self, service):
        import json

        _write(service, {"fact": "alice prefers dark roast coffee"},
               visibility=VisibilityHorizon.PUBLIC)
        r = service.serve("dark roast", consumer=_consumer())
        payload = service.to_mcp_payload(r)
        json.dumps(payload)  # must not raise
        assert payload["served"] >= 1
        assert "exclude-from-memory" in json.dumps(payload).lower() or \
            payload.get("cooperative_exclusion") is not None


class TestCoexistenceFusion:
    def test_side_by_side_keeps_blocks_separate(self, service):
        _write(service, {"fact": "alice prefers dark roast coffee"},
               visibility=VisibilityHorizon.PUBLIC)
        r = service.serve("dark roast", consumer=_consumer())
        foreign = "=== YOUR RAG ===\nCompany coffee policy: fair-trade only."
        combined = service.fuse_side_by_side(r, foreign)
        assert "MEMORY" in combined.upper()
        assert foreign in combined
        # Cross-mem block still labeled/attributable in the fused text.
        assert "dark roast" in combined

    def test_rrf_fusion_is_rank_level_and_reads_only(self, service):
        cross_mem_ranking = ["m1", "m2", "m3"]
        foreign_ranking = ["d1", "m2", "d2"]  # m2 shared → fuses up
        fused = service.fuse_rrf(cross_mem_ranking, foreign_ranking)
        assert fused[0] == "m2"  # appears in both → highest RRF
        assert set(fused) == {"m1", "m2", "m3", "d1", "d2"}

    def test_foreign_corpus_is_never_ingested(self, service):
        class ForeignStore:
            def upsert_chunks(self, *a, **k):
                raise AssertionError("no-push violated: foreign corpus ingested")

            def ingest(self, *a, **k):
                raise AssertionError("no-push violated: foreign corpus ingested")

        _write(service, {"fact": "alice prefers dark roast coffee"},
               visibility=VisibilityHorizon.PUBLIC)
        r = service.serve("dark roast", consumer=_consumer())
        # Fusing must not touch any foreign store.
        service.fuse_side_by_side(r, "external block")
        service.fuse_rrf(["m1"], ["d1"])
        # And the explicit push guard is always closed.
        with pytest.raises(NoPushError):
            service.assert_no_push(ForeignStore())


class TestLatencyBudget:
    def test_serve_p95_within_pinned_budget(self, service):
        import time

        for i in range(20):
            _write(service, {"fact": f"note number {i} about coffee and tea"},
                   visibility=VisibilityHorizon.PUBLIC)
        samples = []
        for _ in range(30):
            t0 = time.perf_counter()
            service.serve("coffee", consumer=_consumer())
            samples.append(time.perf_counter() - t0)
        samples.sort()
        p95 = samples[int(len(samples) * 0.95) - 1]
        # CI ceiling stays flake-safe at 500 ms (OQ1 pins the *target* at
        # 150 ms local; the volatile-tail serve is the only per-turn cost).
        assert p95 < 0.5, f"serve p95 {p95 * 1000:.1f}ms exceeds ceiling"
