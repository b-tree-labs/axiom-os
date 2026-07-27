# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A3 acceptance gate — two Axiom NODES in lock-step over the A2A hop.

The node-to-node analogue of P4's ``test_sync_lockstep`` (harness-to-harness on
one filesystem). Here each node is a **full Axiom store**; they reconcile
store-to-store over the :class:`NodeTransport` seam (ADR-087 D2 import primitive
applied both directions, hub-and-spoke — each node's store is a reconciliation
point). The transport is the in-process ``LoopbackTransport`` double, which
exercises the real send/deliver/poll/import message path; only the machine
boundary is simulated (assessment doc §"The A3 seam").

Proven:

- **Two-node lock-step:** a change landing in node A's store propagates to node
  B's store over the transport, and a change in B propagates back to A.
- **Echo-suppressed:** nothing loops back — node-scoped echo + origin
  idempotency keep a fragment we synced out from ever being re-imported.
- **Kill-and-restart:** kill node B mid-sync, restart over the same store +
  transport — every pending change lands exactly once, no loss, no echo storm.
- **Serving boundary across the node hop:** vault never leaves a node outbound;
  a secret arriving inbound is routed to vault (fail-closed, don't trust the
  peer); controlled content is tier-denied to a remote-tier peer.
- **Trust/authority:** a node only syncs with an authorized peer — no open sync
  to arbitrary nodes, in either direction.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from axiom.memory.rendering import SESSION_BOUNDARY  # noqa: F401 (cadence parity)
from axiom.memory.serving import TIER_LOCAL, TIER_REMOTE
from axiom.memory.sync.detect import ChangeDetector
from axiom.memory.sync.engine import SyncEngine
from axiom.memory.sync.node import (
    NodeCoordinate,
    NodeSyncEngine,
    PeerAuthorizer,
    PeerNotAuthorized,
)
from axiom.memory.sync.transport import LoopbackTransport, NodeSyncMessage

PRINCIPAL = "@alice:home"
ACCOUNT = "acct-alice"
ACCOUNTS = frozenset({ACCOUNT})
T0 = datetime(2026, 7, 15, 12, 0, 0, tzinfo=UTC)

NODE_A = "node-aaaa1111"
NODE_B = "node-bbbb2222"


class _Clock:
    def __init__(self, t: datetime) -> None:
        self.t = t

    def __call__(self) -> datetime:
        return self.t

    def iso(self) -> str:
        return self.t.isoformat()

    def advance(self, seconds: int) -> None:
        self.t = self.t + timedelta(seconds=seconds)


def _make_composition(base: Path):
    from axiom.artifacts.registry import ArtifactRegistry, SQLiteBackend
    from axiom.memory.access import AccessGraphs
    from axiom.memory.attest import AuditLog
    from axiom.memory.composition import CompositionService
    from axiom.memory.policy import PolicyCoord
    from axiom.memory.trust import TrustGraph
    from axiom.vega.identity.keypair import generate_keypair

    base.mkdir(parents=True, exist_ok=True)
    kp = generate_keypair()
    reg = ArtifactRegistry(backend=SQLiteBackend(base / "artifacts.db"))
    audit = AuditLog(base / "audit.jsonl", signing_keypair=kp)
    return CompositionService(
        artifact_registry=reg,
        audit_log=audit,
        signing_keypair=kp,
        policy_coord=PolicyCoord(global_policy={"write": "private"}),
        access_graphs=AccessGraphs(),
        trust_graph=TrustGraph(),
    )


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _live(composition):
    """Live (non-vault) plain fragments in a node's store."""
    return [
        a
        for a in composition.artifact_registry.list(kind="fragment")
        if (a.data or {}).get("cognitive_type") != "vault"
    ]


def _texts(composition) -> str:
    return "\n".join(
        str((a.data or {}).get("content", {})) for a in composition.artifact_registry.list(kind="fragment")
    )


class _Node:
    """One Axiom node: its own store + SyncEngine + NodeSyncEngine, plus a
    local harness root so a change can *originate* on the node the P4 way."""

    def __init__(self, node_id, tier, base, clock, transport, authorized):
        self.node_id = node_id
        self.clock = clock
        self.composition = _make_composition(base / node_id)
        self.root = base / f"{node_id}-harness"
        self.engine = SyncEngine(
            composition=self.composition,
            principal=PRINCIPAL,
            account_set=ACCOUNTS,
            now_fn=clock.iso,
        )
        self.coord = NodeCoordinate(node_id=node_id, account=ACCOUNT, deployment_tier=tier)
        self.node_engine = NodeSyncEngine(
            engine=self.engine,
            local_node=self.coord,
            transport=transport,
            authorizer=PeerAuthorizer(set(authorized)),
            now_fn=clock.iso,
        )

    def land_local(self, text: str, filename: str = "AGENTS.md") -> None:
        """Originate a change in this node's store via its local harness (P4)."""
        from axiom.memory.absorb.markdown_hierarchy import agents_md_adapter

        _write(self.root / filename, text)
        adapter = agents_md_adapter(account=ACCOUNT, roots=[self.root])
        det = ChangeDetector(adapter=adapter, now_fn=self.clock.iso)
        for change in det.poll():
            self.engine.apply_inbound(change)

    def write_native(self, content: dict, cognitive_type: str = "semantic") -> None:
        self.composition.write(
            content=content,
            cognitive_type=cognitive_type,
            principal_id=PRINCIPAL,
            agents={"axi"},
            resources=set(),
        )


@pytest.fixture
def env(tmp_path):
    clock = _Clock(T0)
    transport = LoopbackTransport()
    node_a = _Node(NODE_A, TIER_LOCAL, tmp_path, clock, transport, authorized={NODE_B})
    node_b = _Node(NODE_B, TIER_LOCAL, tmp_path, clock, transport, authorized={NODE_A})
    return {"clock": clock, "transport": transport, "a": node_a, "b": node_b}


# ---------------------------------------------------------------------------
# Two-node lock-step, both directions, echo-suppressed
# ---------------------------------------------------------------------------


class TestTwoNodeLockStep:
    def test_a_to_b_and_b_to_a(self, env):
        a, b, clock = env["a"], env["b"], env["clock"]

        # A learns something (local harness → A's store) → propagates to B.
        a.land_local("# A rules\n\nAlways run ruff before commit.\n")
        a.node_engine.push_to(b.coord)
        b.node_engine.receive()
        assert "Always run ruff before commit." in _texts(b.composition)
        assert len(_live(b.composition)) == 1

        # B learns something new → propagates back to A.
        clock.advance(3600)
        b.land_local("# B rules\n\nDeploy from tags only.\n")
        b.node_engine.push_to(a.coord)
        a.node_engine.receive()
        assert "Deploy from tags only." in _texts(a.composition)

        # Echo suppression: run several full rounds both ways. Nothing loops
        # back — neither store grows past the two real fragments.
        for _ in range(3):
            a.node_engine.push_to(b.coord)
            b.node_engine.receive()
            b.node_engine.push_to(a.coord)
            a.node_engine.receive()
        assert len(_live(a.composition)) == 2
        assert len(_live(b.composition)) == 2


# ---------------------------------------------------------------------------
# Kill node B mid-sync, restart → exactly once, no loss, no echo storm
# ---------------------------------------------------------------------------


class TestKillAndRestart:
    def test_restart_mid_sync_no_loss_no_echo_storm(self, env, tmp_path):
        a, b, clock, transport = env["a"], env["b"], env["clock"], env["transport"]

        a.land_local("# A\n\nPrefer small PRs.\n")
        a.node_engine.push_to(b.coord)
        # B "crashes" before draining its inbox: the message is on the wire,
        # nothing applied to B yet.
        assert len(_live(b.composition)) == 0
        assert len(transport.poll(NODE_B)) == 1  # durable on the wire

        # Restart B: a fresh engine over the SAME store + SAME transport.
        clock.advance(30)
        b2 = _Node(NODE_B, TIER_LOCAL, tmp_path, clock, transport, authorized={NODE_A})
        # (same base dir → same store path → recovers B's persisted state)
        b2.node_engine.receive()
        assert len(_live(b2.composition)) == 1  # landed exactly once, no loss
        assert "Prefer small PRs." in _texts(b2.composition)

        # Recover repeatedly + re-push from A: our own writes never re-import.
        for _ in range(3):
            a.node_engine.push_to(b2.coord)
            b2.node_engine.receive()
            b2.node_engine.push_to(a.coord)
            a.node_engine.receive()
        assert len(_live(b2.composition)) == 1  # no echo storm on B
        assert len(_live(a.composition)) == 1  # A never re-imports its own fragment


# ---------------------------------------------------------------------------
# Serving boundary across the node hop
# ---------------------------------------------------------------------------


class TestServingBoundaryAcrossNodes:
    def test_vault_never_crosses_outbound(self, env):
        a, b = env["a"], env["b"]
        # A native vault fragment + an ordinary memory.
        a.write_native({"summary": "prod db password", "secret": "hunter2"}, cognitive_type="vault")
        a.land_local("# A\n\nPrefer ruff.\n")

        result = a.node_engine.push_to(b.coord)
        b.node_engine.receive()

        # The ordinary memory crossed; the vault secret never did.
        assert "Prefer ruff." in _texts(b.composition)
        assert "hunter2" not in _texts(b.composition)
        assert not any(f.data["cognitive_type"] == "vault" for f in _live(b.composition))
        assert result.sent == 1  # only the servable one left A

    def test_secret_in_inbound_message_routed_to_vault(self, env):
        # A hostile / non-conforming peer sends secret text directly on the
        # wire. The inbound path must NOT trust it — secret → vault, unservable.
        a, b, clock = env["a"], env["b"], env["clock"]
        msg = NodeSyncMessage(
            origin_node=NODE_A,
            origin_account=ACCOUNT,
            entries=(("frag-secret", "aws_key = AKIAIOSFODNN7EXAMPLE"),),
            sent_at=clock.iso(),
        )
        env["transport"].send(NODE_B, msg)
        b.node_engine.receive()

        frags = b.composition.artifact_registry.list(kind="fragment")
        assert any(f.data["cognitive_type"] == "vault" for f in frags)  # routed to vault
        assert not any(
            f.data["cognitive_type"] != "vault"
            and "AKIAIOSFODNN7EXAMPLE" in str(f.data.get("content"))
            for f in frags
        )  # never a plain fragment

        # And a vault fragment never rides back out to A.
        result = b.node_engine.push_to(a.coord)
        a.node_engine.receive()
        assert "AKIAIOSFODNN7EXAMPLE" not in _texts(a.composition)
        assert result.sent == 0

    def test_controlled_content_denied_to_remote_tier_peer(self, env, tmp_path):
        # A remote-tier peer is a different exposure domain: controlled
        # (SCOPE_INTERNAL) content must never ride the wire to it.
        a, clock = env["a"], env["clock"]
        remote = _Node(
            "node-remote999", TIER_REMOTE, tmp_path, clock, env["transport"],
            authorized={NODE_A},
        )
        # authorize A → remote so the deny is a *tier* deny, not an authz deny.
        a.node_engine.authorizer = PeerAuthorizer({NODE_B, "node-remote999"})

        a.land_local("# A\n\nInternal only note.\n")
        result = a.node_engine.push_to(remote.coord)
        remote.node_engine.receive()

        assert result.sent == 0  # nothing left A
        assert any(d.reason.value == "tier_mismatch" for d in result.denials)
        assert len(_live(remote.composition)) == 0  # remote got nothing


# ---------------------------------------------------------------------------
# Trust / authority — no open sync to arbitrary nodes
# ---------------------------------------------------------------------------


class TestTrustAuthority:
    def test_push_to_unauthorized_peer_refused(self, env, tmp_path):
        a, clock = env["a"], env["clock"]
        stranger = _Node(
            "node-stranger", TIER_LOCAL, tmp_path, clock, env["transport"],
            authorized=set(),
        )
        a.land_local("# A\n\nSecret sauce.\n")
        with pytest.raises(PeerNotAuthorized):
            a.node_engine.push_to(stranger.coord)  # A never declared this peer
        assert env["transport"].poll("node-stranger") == []  # nothing sent

    def test_receive_from_unauthorized_peer_rejected(self, env):
        b, clock = env["b"], env["clock"]
        # A message from a node B never declared as a peer.
        msg = NodeSyncMessage(
            origin_node="node-intruder",
            origin_account=ACCOUNT,
            entries=(("f1", "malicious note"),),
            sent_at=clock.iso(),
        )
        env["transport"].send(NODE_B, msg)
        result = b.node_engine.receive()

        assert result.applied == 0
        assert result.rejected == 1
        assert "malicious note" not in _texts(b.composition)
        assert env["transport"].poll(NODE_B) == []  # rejected message dropped, not looping
