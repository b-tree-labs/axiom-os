# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Per-harness MCP-client conformance (cross-mem A2 scope item 2).

Exercises the axiom-memory MCP server the way a harness's MCP client actually
drives it — over the real MCP stdio JSON-RPC protocol: spawn
``python -m axiom.extensions.builtins.memory.mcp_server``, then
``initialize`` -> ``tools/list`` -> ``tools/call axiom_memory_recall``. All six
MCP-capable harness configs {Claude Code, Codex, Cursor, Cline, Continue, Roo}
speak the same MCP, so a single parametrized protocol-level test proves the
client-facing contract for each: the recall tool is **discoverable**,
**callable**, and does not leak **gated** content (a seeded vault secret and a
seeded cross-account item never appear in the payload).

OQ-A2-1 resolution (ADR-026 ownership base case): the default state-dir
service builds an empty in-memory ``AccessGraphs()`` (persisted nowhere).
``is_visible`` now grants read as a base case when the requesting principal
OWNS the fragment, so the recall path's ``read()`` no longer drops the user's
OWN candidates before the serving gate runs. Over stdio the gate is therefore
reachable and positively exercised: own memory serves (``served>0``) and a
seeded cross-account fragment reaches the gate and is denied there
(``cross_account``). The base case opens no cross-principal path and does not
override vault-never — proven by ``TestOwnershipBaseCaseSafety``.

The layers, and what each proves:

- ``TestMcpStdioConformance`` — the real client contract over stdio, per
  harness: the recall tool is discoverable + callable, own memory now crosses
  the wire (``served>0``), the serving gate runs over the wire (a seeded
  cross-account fragment is gate-denied), and gated content (vault + the
  cross-account phrase) never crosses.
- ``TestRecallServesOwnMemory`` — own-recall over the default (empty-graph)
  service returns ``served>0`` in-process (the OQ-A2-1 regression pin).
- ``TestOwnershipBaseCaseSafety`` — the base case restores self-recall WITHOUT
  opening a cross-principal read and WITHOUT overriding vault-never.
- ``TestRecallGateDenies`` — the authoritative in-process gate proof against a
  fully-wired service (``cross_account`` / tier denial recorded, served-when-
  allowed non-vacuity). Retained; the gate was never weakened to make anything
  pass.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import sys
from pathlib import Path

import pytest

# The six MCP-capable harness configs A2 must prove the recall contract for.
MCP_HARNESSES = ["claude-code", "codex", "cursor", "cline", "continue", "roo"]

VENV_PY = sys.executable
PRINCIPAL = "@alice:home"
VAULT_PHRASE = "VAULTLEAKPHRASE"
CROSS_PHRASE = "CROSSACCTLEAKPHRASE"
OWN_PHRASE = "OWNSERVEDPHRASE"


# ---------------------------------------------------------------------------
# stdio layer — the real client contract, per harness
# ---------------------------------------------------------------------------


def _seed_default_state(env: dict) -> None:
    """Seed the ``AXI_STATE_DIR`` the subprocess will read.

    Writes three fragments the subprocess's recall must handle correctly:

    - a vault secret (``VAULT_PHRASE``) — never projected, must never serve;
    - a cross-account item (``CROSS_PHRASE``) — owned by the principal but from
      a *different* storage account, so it now reaches the serving gate (the
      owner may read it) and is denied there ``cross_account``;
    - an own, same-account, servable item (``OWN_PHRASE``) — must serve
      (``served>0``), proving own memory now crosses the wire.

    Provider env is cleared so embedding degrades to offline keyword FTS.
    """
    saved = {k: os.environ.get(k) for k in env}
    os.environ.update({k: str(v) for k, v in env.items()})
    try:
        from axiom.extensions.builtins.memory.serving_endpoint import (
            build_default_serving_service,
        )
        from axiom.memory.fragment import SourceOrigin

        svc = build_default_serving_service()
        svc.composition.write(
            content={"secret": f"{VAULT_PHRASE} APIKEY hunter2"},
            cognitive_type="vault", principal_id=PRINCIPAL,
            agents={"axi"}, resources=set(),
        )
        origin = SourceOrigin(
            harness="chatgpt", account="personal-openai",
            source_ref="row-7", imported_at="2026-07-01T00:00:00+00:00",
        )
        frag = svc.composition.write(
            content={"fact": f"{CROSS_PHRASE} from a personal account"},
            cognitive_type="semantic", principal_id=PRINCIPAL,
            agents={"axi"}, resources=set(), origin=origin,
        )
        svc.composition.recall_index.index_fragment(frag)
        # Own, native (same-account), servable at the local tier.
        own = svc.composition.write(
            content={"fact": f"{OWN_PHRASE} alice prefers dark roast"},
            cognitive_type="semantic", principal_id=PRINCIPAL,
            agents={"axi"}, resources=set(),
        )
        svc.composition.recall_index.index_fragment(own)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


async def _recall_over_stdio(env: dict, *, harness: str, query: str) -> dict:
    """Drive one real MCP stdio session: initialize -> list -> call recall."""
    import axiom
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    child_env = dict(os.environ)
    child_env.update({k: str(v) for k, v in env.items()})
    # The subprocess must import the SAME axiom checkout the suite loaded (the
    # code under development), not whatever a global editable install resolves
    # to. Prepend this checkout's src dir so ``python -m axiom...`` runs the
    # fix under test over the wire.
    src_dir = str(Path(axiom.__file__).resolve().parents[1])
    existing_pp = child_env.get("PYTHONPATH", "")
    child_env["PYTHONPATH"] = (
        src_dir + (os.pathsep + existing_pp if existing_pp else "")
    )
    params = StdioServerParameters(
        command=VENV_PY,
        args=["-m", "axiom.extensions.builtins.memory.mcp_server"],
        env=child_env,
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            tools = await session.list_tools()
            result = await session.call_tool("axiom_memory_recall", {
                "query": query,
                "principal_id": PRINCIPAL,
                "harness": harness,
                "account": PRINCIPAL,
                "deployment_tier": "local",
            })
            return {
                "server_name": init.serverInfo.name,
                "instructions": init.instructions,
                "tools": {t.name: t for t in tools.tools},
                "is_error": bool(result.isError),
                "text": result.content[0].text if result.content else "",
            }


@pytest.fixture
def stdio_env(tmp_path):
    """Env pointing the subprocess at an isolated, seeded, offline state dir."""
    env = {
        "AXI_STATE_DIR": str(tmp_path / "state"),
        "OPENAI_API_KEY": "",
        "NEUT_EMBED_URL": "",
    }
    _seed_default_state(env)
    return env


@pytest.mark.parametrize("harness", MCP_HARNESSES)
class TestMcpStdioConformance:
    def test_recall_discoverable_callable_serves_own_and_gates(
        self, harness, stdio_env
    ):
        pytest.importorskip("mcp")
        out = asyncio.run(_recall_over_stdio(
            stdio_env, harness=harness,
            query=f"{OWN_PHRASE} {VAULT_PHRASE} {CROSS_PHRASE} preferences",
        ))
        # Discoverable — the recall tool is advertised over tools/list.
        assert out["server_name"] == "axiom-memory"
        assert "axiom_memory_recall" in out["tools"]
        schema = out["tools"]["axiom_memory_recall"].inputSchema
        assert "query" in schema.get("properties", {})
        assert "query" in schema.get("required", [])
        # Callable — tools/call returns a well-formed, non-error payload.
        assert out["is_error"] is False
        payload = json.loads(out["text"])
        assert {"served", "denied", "fragments", "denials"} <= set(payload)
        assert "error" not in payload
        blob = json.dumps(payload)
        # Own memory now crosses the wire (OQ-A2-1 resolved): served>0 and the
        # own phrase is present.
        assert payload["served"] >= 1, payload
        assert OWN_PHRASE in blob
        # The serving gate is now REACHABLE over stdio and runs per request:
        # the seeded cross-account fragment reaches it and is denied there.
        assert payload["denied"] >= 1, payload
        assert any(d["reason"] == "cross_account" for d in payload["denials"])
        # Gated — seeded vault + cross-account content never crosses the wire.
        assert VAULT_PHRASE not in blob
        assert CROSS_PHRASE not in blob


class TestMcpStdioServerIdentity:
    def test_all_five_tools_and_instructions_advertised(self, stdio_env):
        pytest.importorskip("mcp")
        out = asyncio.run(_recall_over_stdio(
            stdio_env, harness="claude-code", query="anything",
        ))
        # The recall path is one of the five tools every client discovers.
        assert set(out["tools"]) == {
            "axiom_memory_append", "axiom_memory_show", "axiom_memory_recent",
            "axiom_memory_search", "axiom_memory_recall",
        }
        # The instructions field (write-discipline driver) is served on init.
        assert out["instructions"] and "memory" in out["instructions"].lower()


# ---------------------------------------------------------------------------
# gate layer — the authoritative deny proof (in-process, wired service)
# ---------------------------------------------------------------------------


def _fake_embedder(texts):
    import hashlib

    return [
        [b / 255.0 for b in hashlib.sha256(t.lower().encode()).digest()[:8]]
        for t in texts
    ]


def _wired_service(tmp_path):
    """A serving service whose access graph grants recall (gate is reachable)."""
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
    graphs = add_user_agent_edge(AccessGraphs(), PRINCIPAL, "axi")
    store = SQLiteRAGStore(f"sqlite:///{tmp_path}/recall.db")
    store.connect()
    composition = CompositionService(
        artifact_registry=ArtifactRegistry(backend=SQLiteBackend(tmp_path / "a.db")),
        audit_log=AuditLog(tmp_path / "audit.jsonl", signing_keypair=kp),
        signing_keypair=kp,
        policy_coord=PolicyCoord(global_policy={"write": "private"}),
        access_graphs=graphs,
        trust_graph=TrustGraph(),
        recall_index=RecallIndex(store=store, embedder=_fake_embedder),
    )
    return MemoryServingService(composition=composition, gate=ServingGate())


def _write(service, content, *, ctype="semantic", visibility=None, origin=None):
    from axiom.memory.attest import sign_fragment

    frag = service.composition.write(
        content=content, cognitive_type=ctype, principal_id=PRINCIPAL,
        agents={"axi"}, resources=set(), origin=origin,
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


def _default_service(tmp_path):
    """Build the real default (state-dir-backed) serving service, isolated to
    ``tmp_path`` and forced offline (FTS-only).

    This is the *exact* runtime wiring the deployed MCP recall uses — an empty,
    unpersisted ``AccessGraphs()`` (``serving_endpoint.build_default_serving_service``).
    Exercising it here proves the fix over the default/stdio path in-process,
    without subprocess flake.
    """
    env = {
        "AXI_STATE_DIR": str(tmp_path / "state"),
        "OPENAI_API_KEY": "",
        "NEUT_EMBED_URL": "",
    }
    saved = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        from axiom.extensions.builtins.memory.serving_endpoint import (
            build_default_serving_service,
        )

        return build_default_serving_service()
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class TestRecallServesOwnMemory:
    """OQ-A2-1: a principal recalling their OWN memory over the default
    (empty-``AccessGraphs``) service returns ``served>0``.

    Before the ADR-026 ownership base case, ``read()``/``is_visible`` dropped
    every own candidate under the empty graph, so ``served`` was 0 over the
    whole default/stdio path — the serving gate was unreachable. The base case
    restores the master's intrinsic read right so self-recall works, while
    every cross-principal / vault / cross-account / tier guard stays intact.
    """

    def test_own_memory_is_served_over_default_service(self, tmp_path):
        pytest.importorskip("mcp")
        from axiom.extensions.builtins.memory import mcp_server

        service = _default_service(tmp_path)
        service.composition.write(
            content={"fact": "OWNRECALLPHRASE alice prefers dark roast"},
            cognitive_type="semantic", principal_id=PRINCIPAL,
            agents={"axi"}, resources=set(),
        )
        payload = mcp_server.recall(
            query="OWNRECALLPHRASE dark roast", principal_id=PRINCIPAL,
            account=PRINCIPAL, deployment_tier="local", _service=service,
        )
        assert payload["served"] >= 1, payload
        assert "OWNRECALLPHRASE" in json.dumps(payload)


class TestOwnershipBaseCaseSafety:
    """The ADR-026 base case restores self-recall WITHOUT opening any
    cross-principal path, and WITHOUT overriding vault-never — proven over the
    same default (empty-``AccessGraphs``) service the deployed MCP recall uses.
    """

    def test_cross_principal_read_is_denied_at_the_read_layer(self, tmp_path):
        pytest.importorskip("mcp")
        from axiom.memory.access import is_visible

        service = _default_service(tmp_path)
        frag = service.composition.write(
            content={"fact": "ALICEONLYPHRASE alice private note"},
            cognitive_type="semantic", principal_id=PRINCIPAL,
            agents={"axi"}, resources=set(),
        )
        service.composition.recall_index.index_fragment(frag)

        mallory = "@mallory:evil"
        # Point recall straight at ALICE's corpus but query AS mallory: the
        # candidate is found, then dropped at read()/is_visible because mallory
        # is not the owner and the graph is empty. The base case opened no
        # cross-principal path.
        result = service.composition.recall(
            "ALICEONLYPHRASE private note", user=mallory, agent="axi",
            principal=PRINCIPAL,
        )
        assert result.fragments == []
        # is_visible itself refuses mallory on the very fragment.
        assert is_visible(
            service.composition.access_graphs, user=mallory, agent="axi",
            fragment=frag,
        ) is False
        # ...and the drop is an audited denial, not a silent miss.
        denials = [
            e for e in service.composition.audit_log.read_all()
            if e.get("entry_type") == "read_denied"
        ]
        assert any(e.get("fragment_id") == frag.id for e in denials)

    def test_owner_cross_account_fragment_denied_by_serving_gate(self, tmp_path):
        """Defense in depth: an owner may READ their own imported fragment
        (base case), but when it belongs to a different storage account the
        serving gate denies it ``cross_account`` — work and personal memory
        never blend. Proves the gate is now reachable + emitting over the
        default service the bug was about."""
        pytest.importorskip("mcp")
        from axiom.extensions.builtins.memory import mcp_server
        from axiom.memory.fragment import SourceOrigin

        service = _default_service(tmp_path)
        origin = SourceOrigin(
            harness="chatgpt", account="personal-openai",
            source_ref="row-9", imported_at="2026-07-01T00:00:00+00:00",
        )
        frag = service.composition.write(
            content={"fact": "XACCTPHRASE from a personal account"},
            cognitive_type="semantic", principal_id=PRINCIPAL,
            agents={"axi"}, resources=set(), origin=origin,
        )
        service.composition.recall_index.index_fragment(frag)
        payload = mcp_server.recall(
            query="XACCTPHRASE personal account", principal_id=PRINCIPAL,
            account=PRINCIPAL, deployment_tier="local", _service=service,
        )
        assert payload["served"] == 0
        assert payload["denied"] >= 1
        assert any(d["reason"] == "cross_account" for d in payload["denials"])
        assert "XACCTPHRASE" not in json.dumps(payload)

    def test_owner_own_vault_is_never_served(self, tmp_path):
        """Ownership does NOT override vault-never (ADR-087 D7, unconditional).
        The owner holds the read right, but vault never serves — and vault
        never even enters the recall corpus."""
        pytest.importorskip("mcp")
        from axiom.extensions.builtins.memory import mcp_server

        service = _default_service(tmp_path)
        service.composition.write(
            content={"secret": "OWNVAULTPHRASE APIKEY hunter2"},
            cognitive_type="vault", principal_id=PRINCIPAL,
            agents={"axi"}, resources=set(),
        )
        payload = mcp_server.recall(
            query="OWNVAULTPHRASE APIKEY", principal_id=PRINCIPAL,
            account=PRINCIPAL, deployment_tier="local", _service=service,
        )
        assert payload["served"] == 0
        assert "OWNVAULTPHRASE" not in json.dumps(payload)


class TestRecallGateDenies:
    """The MCP recall handler's gate genuinely denies — reason recorded."""

    def test_cross_account_is_denied_in_payload(self, tmp_path):
        pytest.importorskip("mcp")
        from axiom.extensions.builtins.memory import mcp_server
        from axiom.memory.fragment import SourceOrigin
        from axiom.vega.federation.policy import VisibilityHorizon

        service = _wired_service(tmp_path)
        origin = SourceOrigin(
            harness="chatgpt", account="personal-openai",
            source_ref="row-1", imported_at="2026-07-01T00:00:00+00:00",
        )
        _write(service, {"fact": f"{CROSS_PHRASE} personal note"},
               visibility=VisibilityHorizon.PUBLIC, origin=origin)
        payload = mcp_server.recall(
            query=f"{CROSS_PHRASE} personal note", principal_id=PRINCIPAL,
            account=PRINCIPAL, _service=service,
        )
        # The candidate reached the gate and was denied there — not silently
        # dropped: the payload records the denial with its reason.
        assert payload["served"] == 0
        assert payload["denied"] >= 1
        assert any(d["reason"] == "cross_account" for d in payload["denials"])
        assert CROSS_PHRASE not in json.dumps(payload)

    def test_deployment_tier_is_denied(self, tmp_path):
        pytest.importorskip("mcp")
        from axiom.extensions.builtins.memory import mcp_server
        from axiom.vega.federation.policy import VisibilityHorizon

        service = _wired_service(tmp_path)
        _write(service, {"fact": "TIERLEAKPHRASE internal-only"},
               visibility=VisibilityHorizon.SCOPE_INTERNAL)
        payload = mcp_server.recall(
            query="TIERLEAKPHRASE internal", principal_id=PRINCIPAL,
            deployment_tier="remote", _service=service,
        )
        assert payload["denied"] >= 1
        assert "TIERLEAKPHRASE" not in json.dumps(payload)

    def test_public_same_account_is_served(self, tmp_path):
        """Non-vacuity: with the gate reachable, allowed content IS served."""
        pytest.importorskip("mcp")
        from axiom.extensions.builtins.memory import mcp_server
        from axiom.vega.federation.policy import VisibilityHorizon

        service = _wired_service(tmp_path)
        _write(service, {"fact": "alice loves SERVEDPHRASE espresso"},
               visibility=VisibilityHorizon.PUBLIC)
        payload = mcp_server.recall(
            query="SERVEDPHRASE espresso", principal_id=PRINCIPAL,
            account=PRINCIPAL, _service=service,
        )
        assert payload["served"] >= 1
        assert "SERVEDPHRASE" in json.dumps(payload)

    def test_vault_never_served(self, tmp_path):
        pytest.importorskip("mcp")
        from axiom.extensions.builtins.memory import mcp_server

        service = _wired_service(tmp_path)
        service.composition.write(
            content={"secret": "VAULTGATEPHRASE APIKEY hunter2"},
            cognitive_type="vault", principal_id=PRINCIPAL,
            agents={"axi"}, resources=set(),
        )
        payload = mcp_server.recall(
            query="VAULTGATEPHRASE APIKEY", principal_id=PRINCIPAL,
            account=PRINCIPAL, _service=service,
        )
        assert "VAULTGATEPHRASE" not in json.dumps(payload)
