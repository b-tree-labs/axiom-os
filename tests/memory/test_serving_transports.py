# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Universal serving transports — gate conformance per transport (F4 gate).

The acceptance gate: the policy-gate conformance suite (vault-never,
unlabeled-deny, error-deny, cross-account-deny, deployment-tier-deny) passes
FOR EVERY TRANSPORT — MCP tool, plain-text block, query endpoint. Plus the
coexistence demo vs a stock external RAG (side-by-side + opt-in RRF, foreign
corpus never ingested, gate runs per request).
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import sqlite3

import pytest

from axiom.extensions.builtins.memory.serving_endpoint import (
    build_memory_router,
    mcp_recall_payload,
    memory_mount_spec,
    plaintext_transport,
)
from axiom.memory.serving import (
    ConsumerCoordinate,
    PolicyUnavailable,
    ServingGate,
)
from axiom.vega.federation.policy import ClassificationStamp, VisibilityHorizon

PRINCIPAL = "@alice:work"
AGENT = "axi"
TRANSPORTS = ["mcp", "plaintext", "endpoint"]


def _fake_embedder(texts):
    out = []
    for t in texts:
        h = hashlib.sha256(t.lower().encode()).digest()
        out.append([b / 255.0 for b in h[:8]])
    return out


def _thread_safe_rag_store(dsn):
    """A SQLiteRAGStore whose connection tolerates cross-thread access.

    The FastAPI TestClient runs the endpoint in a worker thread while the store
    was created in the test thread; the default sqlite3 connection is
    thread-affine. Reopening with ``check_same_thread=False`` (serialized access
    in these tests) lets the same real store back every transport uniformly.
    """
    from axiom.rag.sqlite_store import SQLiteRAGStore

    store = SQLiteRAGStore(dsn)
    store.connect()
    store._conn.close()
    conn = sqlite3.connect(store._db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        import sqlite_vec

        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        store._vec_available = True
    except Exception:
        store._vec_available = False
    store._conn = conn
    return store


def _build_service(tmp_path, *, gate=None):
    from axiom.artifacts.registry import ArtifactRegistry, SQLiteBackend
    from axiom.memory.access import AccessGraphs, add_user_agent_edge
    from axiom.memory.attest import AuditLog
    from axiom.memory.composition import CompositionService
    from axiom.memory.policy import PolicyCoord
    from axiom.memory.recall import RecallIndex
    from axiom.memory.serving_service import MemoryServingService
    from axiom.memory.trust import TrustGraph
    from axiom.vega.identity.keypair import generate_keypair

    kp = generate_keypair()
    graphs = add_user_agent_edge(AccessGraphs(), PRINCIPAL, AGENT)
    store = _thread_safe_rag_store(f"sqlite:///{tmp_path}/recall.db")
    composition = CompositionService(
        artifact_registry=ArtifactRegistry(backend=SQLiteBackend(tmp_path / "a.db")),
        audit_log=AuditLog(tmp_path / "audit.jsonl", signing_keypair=kp),
        signing_keypair=kp,
        policy_coord=PolicyCoord(global_policy={"write": "private"}),
        access_graphs=graphs,
        trust_graph=TrustGraph(),
        recall_index=RecallIndex(store=store, embedder=_fake_embedder),
    )
    return MemoryServingService(composition=composition, gate=gate or ServingGate())


def _consumer(**kw) -> ConsumerCoordinate:
    base = dict(
        principal=PRINCIPAL, harness="claude-code", account=PRINCIPAL,
        deployment_tier="local", model_endpoint="local://ollama",
    )
    base.update(kw)
    return ConsumerCoordinate(**base)


def _write(service, content, *, ctype="semantic", visibility=None,
           classification=None, origin=None):
    from axiom.memory.attest import sign_fragment

    frag = service.composition.write(
        content=content, cognitive_type=ctype, principal_id=PRINCIPAL,
        agents={AGENT}, resources=set(), origin=origin,
    )
    changed = {}
    if visibility is not None:
        changed["visibility"] = visibility
    if classification is not None:
        changed["classification"] = classification
    if changed:
        reg = service.composition.artifact_registry
        # register() appends (new id per call), so drop the original row first,
        # then re-sign the relabeled fragment so read()'s signature check holds.
        for a in reg.find_by_name("fragment", frag.id):
            reg.delete(a.id)
        frag = dataclasses.replace(frag, **changed)
        frag = sign_fragment(frag, service.composition.signing_keypair)
        reg.register(kind="fragment", name=frag.id, data=frag.to_dict())
        service.composition.recall_index.index_fragment(frag)
    return frag


def _served_text(transport, service, query, consumer):
    """The text/JSON a transport actually hands the consumer."""
    if transport == "mcp":
        return json.dumps(mcp_recall_payload(service, query, consumer=consumer))
    if transport == "plaintext":
        return plaintext_transport(service, query, consumer=consumer)
    if transport == "endpoint":
        pytest.importorskip("fastapi")
        from fastapi.testclient import TestClient

        from axiom.extensions.builtins.http.server import create_app

        app = create_app(title="t", version="0", description="")
        app.include_router(build_memory_router(serving_service=service))
        client = TestClient(app)
        resp = client.post("/v1/memory/recall", json={
            "query": query,
            "consumer": {
                "principal": consumer.principal, "harness": consumer.harness,
                "account": consumer.account,
                "deployment_tier": consumer.deployment_tier,
                "model_endpoint": consumer.model_endpoint,
                "compatible_accounts": sorted(consumer.compatible_accounts),
            },
        })
        assert resp.status_code == 200
        return json.dumps(resp.json())
    raise AssertionError(transport)


@pytest.mark.parametrize("transport", TRANSPORTS)
class TestGateConformancePerTransport:
    def test_allow_baseline_serves_public_fragment(self, tmp_path, transport):
        service = _build_service(tmp_path)
        _write(service, {"fact": "alice loves ROASTPHRASE espresso"},
               visibility=VisibilityHorizon.PUBLIC)
        text = _served_text(transport, service, "espresso", _consumer())
        assert "ROASTPHRASE" in text  # transport actually serves when it should

    def test_vault_never(self, tmp_path, transport):
        service = _build_service(tmp_path)
        service.composition.write(
            content={"secret": "VAULTPHRASE APIKEY hunter2"}, cognitive_type="vault",
            principal_id=PRINCIPAL, agents={AGENT}, resources=set(),
        )
        text = _served_text(transport, service, "VAULTPHRASE APIKEY", _consumer())
        assert "VAULTPHRASE" not in text

    def test_unlabeled_deny(self, tmp_path, transport):
        service = _build_service(tmp_path)
        _write(
            service, {"fact": "UNLABELEDPHRASE about the roadmap"},
            visibility=VisibilityHorizon.PUBLIC,
            classification=ClassificationStamp(level="totally-unknown-level"),
        )
        text = _served_text(transport, service, "UNLABELEDPHRASE roadmap", _consumer())
        assert "UNLABELEDPHRASE" not in text

    def test_error_deny_policy_raises(self, tmp_path, transport):
        def boom(item, consumer):
            raise RuntimeError("policy backend exploded")

        service = _build_service(tmp_path, gate=ServingGate(policy=boom))
        _write(service, {"fact": "ERRORPHRASE public preference"},
               visibility=VisibilityHorizon.PUBLIC)
        text = _served_text(transport, service, "ERRORPHRASE preference", _consumer())
        assert "ERRORPHRASE" not in text

    def test_error_deny_policy_unavailable(self, tmp_path, transport):
        def unreachable(item, consumer):
            raise PolicyUnavailable("policy engine unreachable")

        service = _build_service(tmp_path, gate=ServingGate(policy=unreachable))
        _write(service, {"fact": "UNAVAILPHRASE public preference"},
               visibility=VisibilityHorizon.PUBLIC)
        text = _served_text(transport, service, "UNAVAILPHRASE preference", _consumer())
        assert "UNAVAILPHRASE" not in text

    def test_cross_account_deny(self, tmp_path, transport):
        from axiom.memory.fragment import SourceOrigin

        service = _build_service(tmp_path)
        origin = SourceOrigin(
            harness="chatgpt", account="personal-openai",
            source_ref="row-7", imported_at="2026-07-01T00:00:00+00:00",
        )
        _write(service, {"fact": "CROSSACCTPHRASE from personal account"},
               visibility=VisibilityHorizon.PUBLIC, origin=origin)
        # Consumer authenticated to the WORK account only.
        text = _served_text(transport, service, "CROSSACCTPHRASE personal",
                            _consumer(account=PRINCIPAL))
        assert "CROSSACCTPHRASE" not in text

    def test_deployment_tier_deny(self, tmp_path, transport):
        service = _build_service(tmp_path)
        _write(service, {"fact": "TIERPHRASE internal-only note"},
               visibility=VisibilityHorizon.SCOPE_INTERNAL)
        text = _served_text(transport, service, "TIERPHRASE internal",
                            _consumer(deployment_tier="remote"))
        assert "TIERPHRASE" not in text


class TestCoexistenceDemo:
    def test_side_by_side_composes_with_stock_rag_gate_runs_per_request(self, tmp_path):
        pytest.importorskip("fastapi")
        from fastapi.testclient import TestClient

        from axiom.extensions.builtins.http.server import create_app

        service = _build_service(tmp_path)
        _write(service, {"fact": "alice prefers MEMFACT dark roast"},
               visibility=VisibilityHorizon.PUBLIC)
        # A denied fragment proves the gate runs even under coexistence.
        _write(service, {"fact": "INTERNALFACT do not share remotely"},
               visibility=VisibilityHorizon.SCOPE_INTERNAL)

        app = create_app(title="t", version="0", description="")
        app.include_router(build_memory_router(serving_service=service))
        client = TestClient(app)
        foreign = "=== YOUR RAG ===\nCompany policy: fair-trade only."
        resp = client.post("/v1/memory/recall", json={
            "query": "roast coffee",
            "consumer": {"principal": PRINCIPAL, "harness": "claude-code",
                         "account": PRINCIPAL, "deployment_tier": "remote"},
            "fusion": {"mode": "side_by_side", "foreign_block": foreign},
        })
        body = resp.json()
        assert foreign in body["block"]           # user's RAG passed through verbatim
        assert "MEMFACT" in body["block"]          # public memory served
        assert "INTERNALFACT" not in body["block"]  # gate ran per request (tier deny)

    def test_opt_in_rrf_fuses_rankings_never_ingests(self, tmp_path):
        pytest.importorskip("fastapi")
        from fastapi.testclient import TestClient

        from axiom.extensions.builtins.http.server import create_app

        service = _build_service(tmp_path)
        _write(service, {"fact": "shared-note about SHAREDID and coffee"},
               visibility=VisibilityHorizon.PUBLIC)

        app = create_app(title="t", version="0", description="")
        app.include_router(build_memory_router(serving_service=service))
        client = TestClient(app)
        resp = client.post("/v1/memory/recall", json={
            "query": "coffee",
            "consumer": {"principal": PRINCIPAL, "harness": "claude-code",
                         "account": PRINCIPAL, "deployment_tier": "local"},
            "fusion": {"mode": "rrf", "foreign_ranking": ["docA", "docB"]},
        })
        body = resp.json()
        # Rank-level fusion returns a combined ordering; the foreign docs ride
        # along by id — never ingested as content.
        assert "fused_ranking" in body
        assert "docA" in body["fused_ranking"] and "docB" in body["fused_ranking"]


class TestMountSpec:
    def test_memory_mount_spec_prefix_and_authz(self, tmp_path):
        pytest.importorskip("fastapi")
        service = _build_service(tmp_path)
        spec = memory_mount_spec(serving_service=service)
        assert spec.prefix == "/memory"
        assert spec.extension == "memory"
        assert spec.requires_authz is True


class TestMcpServerToolWiring:
    def test_recall_tool_registered(self):
        pytest.importorskip("mcp")
        from axiom.extensions.builtins.memory import mcp_server

        names = {t.name for t in mcp_server._TOOLS}
        assert "axiom_memory_recall" in names
        assert "axiom_memory_recall" in mcp_server._HANDLERS

    def test_recall_tool_gates_via_injected_service(self, tmp_path):
        pytest.importorskip("mcp")
        from axiom.extensions.builtins.memory import mcp_server

        service = _build_service(tmp_path)
        _write(service, {"fact": "alice loves TOOLPHRASE espresso"},
               visibility=VisibilityHorizon.PUBLIC)
        _write(service, {"fact": "INTERNALTOOL do not share remotely"},
               visibility=VisibilityHorizon.SCOPE_INTERNAL)
        payload = mcp_server.recall(
            query="espresso INTERNALTOOL", principal_id=PRINCIPAL,
            deployment_tier="remote", _service=service,
        )
        blob = json.dumps(payload)
        assert "TOOLPHRASE" in blob            # public served
        assert "INTERNALTOOL" not in blob      # gate denied tier to remote
