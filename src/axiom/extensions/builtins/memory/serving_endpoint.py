# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Universal serving transports (ADR-087 D7 / PRD F4).

Three ways a harness consumes a user's memory, each funneling through the one
serving gate via :class:`MemoryServingService`:

1. **MCP retrieval tool** — :func:`mcp_recall_payload` returns a JSON tool
   result (wired into the axiom-memory MCP server).
2. **Plain-text block** — :func:`plaintext_transport` returns a template-ready
   block.
3. **Query endpoint** — :func:`build_memory_router` mounts on the composed #607
   app (``/memory``), usable from a user's existing RAG. Coexistence is
   first-class: side-by-side blocks by default, opt-in rank-level RRF fusion
   (fuse, never ingest — cross-mem is one policy-gated retriever, never a corpus
   the user's pipeline absorbs).

No transport re-implements policy: they all call ``serve()``, so the gate runs
per request. This is the same "one door out" symmetry as the write path.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from axiom.memory.serving import ConsumerCoordinate

if TYPE_CHECKING:
    from axiom.memory.serving_service import MemoryServingService


def consumer_from_dict(data: dict) -> ConsumerCoordinate:
    """Build a :class:`ConsumerCoordinate` from a request/tool argument dict.

    Missing ``principal``/``account`` produce an unresolved coordinate — which
    the gate denies (fail-closed), exactly as intended.
    """
    return ConsumerCoordinate(
        principal=str(data.get("principal", "")),
        harness=str(data.get("harness", "")),
        account=str(data.get("account", "")),
        deployment_tier=str(data.get("deployment_tier", "local")),
        model_endpoint=str(data.get("model_endpoint", "")),
        compatible_accounts=frozenset(data.get("compatible_accounts", []) or []),
    )


# ---------------------------------------------------------------------------
# Transport 1 — MCP retrieval tool
# ---------------------------------------------------------------------------


def mcp_recall_payload(
    service: MemoryServingService,
    query: str,
    *,
    consumer: ConsumerCoordinate,
    k: int = 5,
) -> dict:
    """MCP tool result: recall → gate → JSON payload."""
    result = service.serve(query, consumer=consumer, k=k)
    return service.to_mcp_payload(result)


# ---------------------------------------------------------------------------
# Transport 2 — plain-text block for prompt templates
# ---------------------------------------------------------------------------


def plaintext_transport(
    service: MemoryServingService,
    query: str,
    *,
    consumer: ConsumerCoordinate,
    k: int = 5,
) -> str:
    """Plain-text block: recall → gate → template-ready text."""
    result = service.serve(query, consumer=consumer, k=k)
    return service.to_plaintext_block(result)


# ---------------------------------------------------------------------------
# Transport 3 — query endpoint for a user's existing RAG
# ---------------------------------------------------------------------------


def build_memory_router(*, serving_service: MemoryServingService):
    """Build the ``/memory`` query-endpoint router (F4 transport).

    ``POST /v1/memory/recall`` runs recall → gate → serialize. ``fusion.mode``:
    ``side_by_side`` (default) returns a labeled memory block beside the user's
    own ``foreign_block``; ``rrf`` returns a rank-level fusion of the served
    fragment ids with the caller's ``foreign_ranking``. The foreign corpus is
    never ingested (no-push).
    """
    from fastapi import APIRouter

    router = APIRouter()

    @router.post("/v1/memory/recall")
    def recall(body: dict) -> Any:
        query = str(body.get("query", ""))
        consumer = consumer_from_dict(body.get("consumer", {}))
        k = int(body.get("k", 5))
        fusion = body.get("fusion") or {}
        mode = str(fusion.get("mode", "side_by_side"))

        result = serving_service.serve(query, consumer=consumer, k=k)
        payload = serving_service.to_mcp_payload(result)

        if mode == "side_by_side":
            payload["block"] = serving_service.fuse_side_by_side(
                result, str(fusion.get("foreign_block", ""))
            )
        elif mode == "rrf":
            cross_mem_ranking = [i.fragment_id for i in result.items]
            payload["fused_ranking"] = serving_service.fuse_rrf(
                cross_mem_ranking, list(fusion.get("foreign_ranking", []) or [])
            )
        return payload

    return router


def memory_mount_spec(serving_service: MemoryServingService | None = None):
    """MountSpec for the ``/memory`` query endpoint (discovered by compose_app).

    ``serving_service`` is injectable for tests; a real deployment gets the
    default (state-dir-backed) service. ``requires_authz=True`` so the fail-
    closed substrate refuses to serve it without an authz hook.
    """
    from axiom.extensions.builtins.http.registry import MountSpec

    svc = serving_service if serving_service is not None else build_default_serving_service()
    return MountSpec(
        prefix="/memory",
        router=build_memory_router(serving_service=svc),
        extension="memory",
        bind="127.0.0.1",
        trust_zone="loopback",
    )


def build_default_serving_service() -> MemoryServingService:
    """Default state-dir-backed serving service (composition + recall index + gate)."""
    from axiom.artifacts.registry import ArtifactRegistry, SQLiteBackend
    from axiom.infra.paths import get_user_state_dir
    from axiom.memory.access import AccessGraphs
    from axiom.memory.attest import AuditLog
    from axiom.memory.composition import CompositionService
    from axiom.memory.policy import PolicyCoord
    from axiom.memory.recall import RecallIndex
    from axiom.memory.serving import ServingGate
    from axiom.memory.serving_service import MemoryServingService
    from axiom.memory.trust import TrustGraph
    from axiom.rag.sqlite_store import SQLiteRAGStore
    from axiom.vega.identity.keypair import Keypair, generate_keypair

    base = get_user_state_dir() / "memory"
    base.mkdir(parents=True, exist_ok=True)
    key_path = base / "node.key"
    if key_path.exists():
        kp = Keypair.from_private_bytes(key_path.read_bytes())
    else:
        kp = generate_keypair()
        key_path.write_bytes(kp.export_private())

    store = SQLiteRAGStore(f"sqlite:///{base / 'recall.db'}")
    store.connect()
    composition = CompositionService(
        artifact_registry=ArtifactRegistry(backend=SQLiteBackend(base / "artifacts.db")),
        audit_log=AuditLog(base / "audit.jsonl", signing_keypair=kp),
        signing_keypair=kp,
        policy_coord=PolicyCoord(global_policy={"write": "private"}),
        access_graphs=AccessGraphs(),
        trust_graph=TrustGraph(),
        recall_index=RecallIndex(store=store),
    )
    return MemoryServingService(composition=composition, gate=ServingGate())


__all__ = [
    "build_default_serving_service",
    "build_memory_router",
    "consumer_from_dict",
    "mcp_recall_payload",
    "memory_mount_spec",
    "plaintext_transport",
]
