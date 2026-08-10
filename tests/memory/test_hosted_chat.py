# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A4 acceptance gate — two Axiom nodes in harmony (the HOSTED-CHAT test).

The runtime the hosted-endpoint architecture describes, riding A3's node
transport seam (in-process doubles; the live A2A wire is OQ-A3-1). Two
capabilities, both gated at the SOURCE node before anything leaves it:

- **Query-time foreign_block borrow.** A user queries a HOSTED serving endpoint;
  their LOCAL node contributes a gated + minimized projection of their personal
  memory as the endpoint's ``foreign_block``, at query time, over the transport.
  The hosting node fuses it into the answer but NEVER persists it (its store
  gains no fragments from the borrow — asserted).
- **Session-shard hosting.** A per-user session shard lives on the hosting node
  (owned by the user, principal-isolated, TTL working copy). Chat turns append
  to it; at the session boundary it SYNCS HOME to the user's local node via A3's
  ``NodeSyncEngine`` (origin-preserved), and the hosting node TTL-expires its
  copy. The local node is the durable home; the hosting node holds a transient
  copy.

Proven here (the acceptance gate):

- a peer's personal foreign_block informs a hosted recall WITHOUT persisting on
  the hosting node;
- a session shard written on the hosting node round-trips HOME to the local node
  via sync (appears there, origin-preserved);
- cross-user isolation (B cannot see A's shard) at both ``read()`` and the gate;
- vault / secret / tier / cross-account denials hold across BOTH the borrow and
  the shard sync;
- TTL expiry drops the hosting-node copy while the home copy persists.
"""

from __future__ import annotations

import dataclasses
import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from axiom.memory.hosted import (
    ForeignBlockBorrower,
    HostedEndpoint,
    LoopbackBorrowTransport,
    SessionShardManager,
    borrow_transport,
)
from axiom.memory.serving import (
    TIER_LOCAL,
    TIER_REMOTE,
    ConsumerCoordinate,
    DenyReason,
    ServableItem,
    ServingGate,
)
from axiom.memory.serving_service import MemoryServingService
from axiom.memory.sync.node import NodeCoordinate, NodeSyncEngine, PeerAuthorizer
from axiom.memory.sync.transport import LoopbackTransport
from axiom.vega.federation.policy import VisibilityHorizon

# ---------------------------------------------------------------------------
# Principals / accounts / node ids
# ---------------------------------------------------------------------------

ALICE = "@alice:home"
BOB = "@bob:home"
# A user's personal-memory account is their principal identity (the self-serve
# convention the P3 serving tests use: native fragments carry account =
# principal_id, so the consumer authenticates to that same account).
ACCT_A = ALICE
ACCT_B = BOB
AGENT = "axi"

LOCAL_A = "node-localalice1"   # alice's local durable node
LOCAL_B = "node-localbob0001"  # bob's local durable node
HOST = "node-hostedserving0"   # the hosting serving endpoint node

T0 = datetime(2026, 7, 15, 12, 0, 0, tzinfo=UTC)


class Clock:
    """Injectable clock — the only time source (no wall-clock in logic)."""

    def __init__(self, t: datetime) -> None:
        self.t = t

    def now(self) -> datetime:
        return self.t

    def iso(self) -> str:
        return self.t.isoformat()

    def advance(self, seconds: int) -> None:
        self.t = self.t + timedelta(seconds=seconds)


def _fake_embedder(texts):
    out = []
    for t in texts:
        h = hashlib.sha256(t.lower().encode()).digest()
        out.append([b / 255.0 for b in h[:8]])
    return out


def _make_composition(base: Path, principal: str, *, with_recall: bool):
    from axiom.artifacts.registry import ArtifactRegistry, SQLiteBackend
    from axiom.memory.access import AccessGraphs, add_user_agent_edge
    from axiom.memory.attest import AuditLog
    from axiom.memory.composition import CompositionService
    from axiom.memory.policy import PolicyCoord
    from axiom.memory.recall import RecallIndex
    from axiom.memory.trust import TrustGraph
    from axiom.rag.sqlite_store import SQLiteRAGStore
    from axiom.vega.identity.keypair import generate_keypair

    base.mkdir(parents=True, exist_ok=True)
    kp = generate_keypair()
    graphs = add_user_agent_edge(AccessGraphs(), principal, AGENT)
    index = None
    if with_recall:
        store = SQLiteRAGStore(f"sqlite:///{base}/recall.db")
        store.connect()
        index = RecallIndex(store=store, embedder=_fake_embedder)
    return CompositionService(
        artifact_registry=ArtifactRegistry(backend=SQLiteBackend(base / "a.db")),
        audit_log=AuditLog(base / "audit.jsonl", signing_keypair=kp),
        signing_keypair=kp,
        policy_coord=PolicyCoord(global_policy={"write": "private"}),
        access_graphs=graphs,
        trust_graph=TrustGraph(),
        recall_index=index,
    )


def _write(
    composition, principal, content, *, ctype="semantic", visibility=None,
    origin_account=None,
):
    """Write a fragment; optionally origin-stamp a storage account and/or
    re-stamp visibility (test-only, mirrors the serving-service test helper),
    re-indexing so recall + gate see it."""
    from axiom.memory.attest import sign_fragment
    from axiom.memory.fragment import SourceOrigin

    origin = None
    if origin_account is not None:
        origin = SourceOrigin(
            harness="axiom://" + LOCAL_A, account=origin_account,
            source_ref="ref-" + hashlib.sha256(str(content).encode()).hexdigest()[:12],
            imported_at=T0.isoformat(),
        )
    frag = composition.write(
        content=content, cognitive_type=ctype, principal_id=principal,
        agents={AGENT}, resources=set(), origin=origin,
    )
    if visibility is not None:
        reg = composition.artifact_registry
        for a in reg.find_by_name("fragment", frag.id):
            reg.delete(a.id)
        frag = dataclasses.replace(frag, visibility=visibility)
        frag = sign_fragment(frag, composition.signing_keypair)
        reg.register(kind="fragment", name=frag.id, data=frag.to_dict())
        if composition.recall_index is not None:
            composition.recall_index.index_fragment(frag)
    return frag


def _servable_from_store(composition, fragment_id):
    from axiom.memory.fragment import fragment_from_dict

    data = composition.artifact_registry.find_by_name("fragment", fragment_id)[0].data
    return ServableItem.from_fragment(fragment_from_dict(data))


def _live_fragments(composition):
    return [
        a for a in composition.artifact_registry.list(kind="fragment")
        if (a.data or {}).get("cognitive_type") != "vault"
    ]


def _fragment_texts(composition) -> str:
    return "\n".join(
        str((a.data or {}).get("content", {}))
        for a in composition.artifact_registry.list(kind="fragment")
    )


# ---------------------------------------------------------------------------
# The two-node harmony environment
# ---------------------------------------------------------------------------


@pytest.fixture
def env(tmp_path):
    clock = Clock(T0)
    sync_transport = LoopbackTransport()      # A3 sync channel (shard sync-home)
    borrow_wire = borrow_transport(shared=LoopbackBorrowTransport())

    # Alice's local durable node — full store with recall so it can serve.
    local_a = _make_composition(tmp_path / "local_a", ALICE, with_recall=True)
    local_a_serving = MemoryServingService(composition=local_a, gate=ServingGate())
    local_a_coord = NodeCoordinate(
        node_id=LOCAL_A, account=ACCT_A, deployment_tier=TIER_LOCAL,
    )
    # The borrow responder on alice's local node: default-deny, HOST authorized.
    borrower_a = ForeignBlockBorrower(
        serving=local_a_serving,
        node=local_a_coord,
        authorizer=PeerAuthorizer({HOST}),
    )
    borrow_wire.register(LOCAL_A, borrower_a.respond)

    # The hosting serving endpoint node.
    host = _make_composition(tmp_path / "host", ALICE, with_recall=True)
    host_serving = MemoryServingService(composition=host, gate=ServingGate())
    host_coord = NodeCoordinate(
        node_id=HOST, account=ACCT_A, deployment_tier=TIER_LOCAL,
    )
    endpoint = HostedEndpoint(
        node=host_coord,
        composition=host,
        serving=host_serving,
        borrow_transport=borrow_wire,
        now_fn=clock.now,
    )
    shard_mgr = SessionShardManager(
        node=host_coord,
        composition=host,
        transport=sync_transport,
        authorizer=PeerAuthorizer({LOCAL_A, LOCAL_B}),
        now_fn=clock.now,
    )

    return {
        "clock": clock,
        "sync_transport": sync_transport,
        "borrow_wire": borrow_wire,
        "local_a": local_a,
        "local_a_coord": local_a_coord,
        "local_a_serving": local_a_serving,
        "host": host,
        "host_serving": host_serving,
        "host_coord": host_coord,
        "endpoint": endpoint,
        "shard_mgr": shard_mgr,
        "tmp_path": tmp_path,
    }


# ---------------------------------------------------------------------------
# (1) Query-time foreign_block borrow — informs, never persists
# ---------------------------------------------------------------------------


class TestForeignBlockBorrow:
    def test_personal_context_shapes_hosted_answer_without_persisting(self, env):
        local_a, endpoint, host = env["local_a"], env["endpoint"], env["host"]
        # Alice's personal memory lives on her local node (default SCOPE_INTERNAL).
        _write(local_a, ALICE, {"fact": "alice prefers dark roast coffee"})

        before = len(host.artifact_registry.list(kind="fragment"))
        answer = endpoint.recall(
            "dark roast coffee", principal=ALICE, account=ACCT_A,
            borrow_from=env["local_a_coord"],
        )
        after = len(host.artifact_registry.list(kind="fragment"))

        # The personal foreign_block came over the transport and shaped the answer.
        assert answer.borrowed.served >= 1
        assert "dark roast" in answer.foreign_block
        assert "dark roast" in answer.block
        assert "axiom://" + LOCAL_A in answer.foreign_block  # attributable
        # NEVER persisted on the hosting node — the store gained no fragments.
        assert after == before

    def test_borrow_is_minimized_top_k_and_size_capped(self, env):
        local_a, endpoint = env["local_a"], env["endpoint"]
        for i in range(8):
            _write(local_a, ALICE, {"fact": f"alice coffee preference number {i}"})
        answer = endpoint.recall(
            "alice coffee preference", principal=ALICE, account=ACCT_A,
            borrow_from=env["local_a_coord"],
        )
        # Minimized: a compact block, not a dump of all eight.
        assert 1 <= answer.borrowed.served <= endpoint.borrow_k
        assert len(answer.foreign_block) <= endpoint.borrow_char_budget + 512

    def test_unauthorized_hosting_endpoint_gets_nothing(self, env, tmp_path):
        # A hosting node alice's local node never declared as a peer cannot borrow.
        from axiom.memory.sync.node import PeerNotAuthorized

        local_a = env["local_a"]
        _write(local_a, ALICE, {"fact": "alice prefers dark roast coffee"})
        rogue_coord = NodeCoordinate(
            node_id="node-roguehost00", account=ACCT_A, deployment_tier=TIER_LOCAL,
        )
        rogue = HostedEndpoint(
            node=rogue_coord, composition=env["host"], serving=env["host_serving"],
            borrow_transport=env["borrow_wire"], now_fn=env["clock"].now,
        )
        with pytest.raises(PeerNotAuthorized):
            rogue.recall(
                "dark roast", principal=ALICE, account=ACCT_A,
                borrow_from=env["local_a_coord"],
            )


# ---------------------------------------------------------------------------
# (2) Session shard — round-trips home, TTL-expires on the hosting node
# ---------------------------------------------------------------------------


class TestSessionShardRoundTripHome:
    def _home_node(self, env):
        """Alice's local node wired to receive the shard sync-home."""
        engine = NodeSyncEngine(
            engine=_sync_engine(env["local_a"], ALICE, ACCT_A, env["clock"]),
            local_node=env["local_a_coord"],
            transport=env["sync_transport"],
            authorizer=PeerAuthorizer({HOST}),
            now_fn=env["clock"].iso,
        )
        return engine

    def test_shard_written_on_host_syncs_home_origin_preserved(self, env):
        host, local_a, shard_mgr = env["host"], env["local_a"], env["shard_mgr"]
        session = "session://hosted-chat-001"
        shard_mgr.open(principal=ALICE, account=ACCT_A, session_id=session)
        shard_mgr.append_turn(
            principal=ALICE, account=ACCT_A, session_id=session,
            text="alice is debugging the auth token refresh flow",
        )
        # The transient copy is on the hosting node.
        assert "auth token refresh" in _fragment_texts(host)

        # Session boundary: sync home to alice's local durable node.
        home_engine = self._home_node(env)
        shard_mgr.sync_home(principal=ALICE, account=ACCT_A, home=env["local_a_coord"])
        home_engine.receive()

        # It lands HOME, and the home copy preserves the hosting-node origin.
        assert "auth token refresh" in _fragment_texts(local_a)
        home_frag = next(
            a for a in local_a.artifact_registry.list(kind="fragment")
            if "auth token refresh" in str((a.data or {}).get("content"))
        )
        origin = (home_frag.data.get("provenance") or {}).get("origin") or {}
        assert origin.get("harness") == "axiom://" + HOST  # origin-preserved

    def test_ttl_expiry_drops_hosting_copy_home_persists(self, env):
        clock, host, local_a, shard_mgr = (
            env["clock"], env["host"], env["local_a"], env["shard_mgr"],
        )
        session = "session://hosted-chat-002"
        shard_mgr.open(
            principal=ALICE, account=ACCT_A, session_id=session, ttl_seconds=1800,
        )
        shard_mgr.append_turn(
            principal=ALICE, account=ACCT_A, session_id=session,
            text="alice pinned the deploy to a tagged release",
        )
        home_engine = self._home_node(env)
        shard_mgr.sync_home(principal=ALICE, account=ACCT_A, home=env["local_a_coord"])
        home_engine.receive()
        assert "tagged release" in _fragment_texts(local_a)

        # Before TTL: expiry is a no-op (the working copy is still live).
        dropped = shard_mgr.expire(principal=ALICE, session_id=session)
        assert dropped == []
        assert "tagged release" in _fragment_texts(host)

        # Past TTL: the hosting node drops its transient copy...
        clock.advance(1801)
        dropped = shard_mgr.expire(principal=ALICE, session_id=session)
        assert len(dropped) == 1
        assert "tagged release" not in _fragment_texts(host)
        # ...while the durable home copy persists untouched.
        assert "tagged release" in _fragment_texts(local_a)


# ---------------------------------------------------------------------------
# Cross-user isolation — B can never see A's shard
# ---------------------------------------------------------------------------


class TestCrossUserIsolation:
    def test_user_b_cannot_read_or_be_served_user_a_shard(self, env):
        host, shard_mgr = env["host"], env["shard_mgr"]
        session = "session://hosted-chat-a"
        shard_mgr.open(principal=ALICE, account=ACCT_A, session_id=session)
        frag = shard_mgr.append_turn(
            principal=ALICE, account=ACCT_A, session_id=session,
            text="alice private note about her salary negotiation",
        )

        # read(): B is not the owner and holds no access-graph edge to A's memory.
        got = host.read([frag.id], user=BOB, agent=AGENT)
        assert got == []

        # gate: A's shard item, evaluated for B's consumer coordinate, is
        # cross-account denied (work/personal never blend).
        item = _servable_from_store(host, frag.id)
        bob_consumer = ConsumerCoordinate(
            principal=BOB, harness="axiom://" + HOST, account=ACCT_B,
            deployment_tier=TIER_LOCAL, compatible_accounts=frozenset({ACCT_B}),
        )
        decision = ServingGate().evaluate(item, bob_consumer)
        assert not decision.allowed
        assert decision.reason is DenyReason.CROSS_ACCOUNT


# ---------------------------------------------------------------------------
# vault / secret / tier / cross-account denials across BOTH legs
# ---------------------------------------------------------------------------


class TestBoundaryDenialsAcrossBothLegs:
    def test_borrow_denies_vault_and_secret(self, env):
        local_a, endpoint = env["local_a"], env["endpoint"]
        _write(local_a, ALICE, {"fact": "alice prefers dark roast coffee"})
        _write(local_a, ALICE, {"secret": "prod db password hunter2"}, ctype="vault")
        _write(
            local_a, ALICE,
            {"note": "aws key AKIAIOSFODNN7EXAMPLE for the pipeline"},
            visibility=VisibilityHorizon.PUBLIC,
        )
        answer = endpoint.recall(
            "dark roast coffee AKIA hunter2 password", principal=ALICE,
            account=ACCT_A, borrow_from=env["local_a_coord"],
        )
        assert "hunter2" not in answer.foreign_block            # vault never serves
        assert "AKIAIOSFODNN7EXAMPLE" not in answer.foreign_block  # secret→vault

    def test_borrow_denies_controlled_content_to_remote_tier(self, env):
        local_a = env["local_a"]
        _write(local_a, ALICE, {"fact": "alice prefers dark roast coffee"})  # SCOPE_INTERNAL
        # A REMOTE-tier hosting endpoint is a different exposure domain.
        remote_host_coord = NodeCoordinate(
            node_id="node-remotehost0", account=ACCT_A, deployment_tier=TIER_REMOTE,
        )
        remote_endpoint = HostedEndpoint(
            node=remote_host_coord, composition=env["host"],
            serving=env["host_serving"], borrow_transport=env["borrow_wire"],
            now_fn=env["clock"].now,
        )
        # Authorize the remote host at the source so the deny is a TIER deny,
        # not an authorization deny.
        env["borrow_wire"].register(
            LOCAL_A,
            ForeignBlockBorrower(
                serving=env["local_a_serving"], node=env["local_a_coord"],
                authorizer=PeerAuthorizer({HOST, "node-remotehost0"}),
            ).respond,
        )
        answer = remote_endpoint.recall(
            "dark roast coffee", principal=ALICE, account=ACCT_A,
            borrow_from=env["local_a_coord"],
        )
        assert "dark roast" not in answer.foreign_block  # tier-denied at the source

    def test_borrow_denies_cross_account(self, env):
        # Alice's WORK-account memory must not serve to a consumer authenticated
        # to her PERSONAL account — work and personal never blend.
        local_a, endpoint = env["local_a"], env["endpoint"]
        _write(
            local_a, ALICE,
            {"fact": "quarterly board deck numbers are confidential"},
            visibility=VisibilityHorizon.PUBLIC, origin_account="acct-alice-work",
        )
        answer = endpoint.recall(
            "quarterly board deck numbers", principal=ALICE, account=ACCT_A,
            borrow_from=env["local_a_coord"],
        )
        assert "board deck" not in answer.foreign_block  # cross-account denied
        assert answer.borrowed.served == 0

    def test_shard_sync_home_denies_vault(self, env):
        host, local_a, shard_mgr = env["host"], env["local_a"], env["shard_mgr"]
        session = "session://hosted-chat-vault"
        shard_mgr.open(principal=ALICE, account=ACCT_A, session_id=session)
        shard_mgr.append_turn(
            principal=ALICE, account=ACCT_A, session_id=session,
            text="alice remembered to water the plants",
        )
        # A vault fragment authored in the session must never sync home.
        host.write(
            content={"secret": "session api token sk-abcdef0123456789ghij"},
            cognitive_type="vault", principal_id=ALICE, agents={AGENT},
            resources=set(),
        )
        home_engine = NodeSyncEngine(
            engine=_sync_engine(local_a, ALICE, ACCT_A, env["clock"]),
            local_node=env["local_a_coord"], transport=env["sync_transport"],
            authorizer=PeerAuthorizer({HOST}), now_fn=env["clock"].iso,
        )
        shard_mgr.sync_home(principal=ALICE, account=ACCT_A, home=env["local_a_coord"])
        home_engine.receive()

        assert "water the plants" in _fragment_texts(local_a)  # ordinary crossed
        assert "sk-abcdef0123456789ghij" not in _fragment_texts(local_a)  # vault did not
        assert not any(
            (a.data or {}).get("cognitive_type") == "vault"
            for a in local_a.artifact_registry.list(kind="fragment")
        )


# ---------------------------------------------------------------------------
# shared helper
# ---------------------------------------------------------------------------


def _sync_engine(composition, principal, account, clock):
    from axiom.memory.sync.engine import SyncEngine

    return SyncEngine(
        composition=composition, principal=principal,
        account_set=frozenset({account}), now_fn=clock.iso,
    )
