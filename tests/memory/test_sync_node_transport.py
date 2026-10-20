# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A3 unit gate — node addressing, the A2A transport seam, node-scoped echo.

These are the focused proofs under the two-node lock-step (A3):

- ``axiom://<node-id>`` node coordinates parse/format round-trip.
- The :class:`NodeTransport` send/poll/ack contract, exercised through the
  in-process :class:`LoopbackTransport` double — the same message path the real
  A2A wire will implement (assessment doc §"The A3 seam").
- The ``node_transport`` factory returns the double when no federation wire is
  present, and names the real transport as the drop-in seam.
- Echo suppression extended with node identity (``echo.py`` reused, not
  rewritten): P4's content-only key is unchanged; the node-scoped key is new.
- :class:`PeerAuthorizer` default-deny (a real trust leg, not a double).
"""

from __future__ import annotations

import pytest

from axiom.memory.addressing import (
    format_node_uri,
    is_node_uri,
    parse_node_uri,
)


# ---------------------------------------------------------------------------
# axiom:// node coordinates
# ---------------------------------------------------------------------------


class TestNodeAddressing:
    def test_format_and_parse_round_trip(self):
        uri = format_node_uri("node-abc123")
        assert uri == "axiom://node-abc123"
        assert parse_node_uri(uri) == "node-abc123"
        assert is_node_uri(uri)

    def test_bare_node_uri_is_distinct_from_fragment_uri(self):
        # A node URI has no fragment path component.
        assert is_node_uri("axiom://node-abc123")
        assert not is_node_uri("axiom://node-abc123/frag-1")  # that's a fragment ref
        assert not is_node_uri("https://example.org")
        assert not is_node_uri("node-abc123")

    def test_empty_node_id_rejected(self):
        with pytest.raises(ValueError):
            format_node_uri("")


# ---------------------------------------------------------------------------
# The A2A transport seam
# ---------------------------------------------------------------------------


class TestTransportSeam:
    def test_message_serialises_round_trip(self):
        from axiom.memory.sync.transport import NodeSyncMessage

        msg = NodeSyncMessage(
            origin_node="node-a",
            origin_account="acct-x",
            entries=(("frag-1", "always run ruff"), ("frag-2", "deploy from tags")),
            sent_at="2026-07-15T00:00:00+00:00",
        )
        again = NodeSyncMessage.from_dict(msg.to_dict())
        assert again == msg
        assert again.message_id == msg.message_id  # content-addressed, stable

    def test_loopback_send_poll_ack(self):
        from axiom.memory.sync.transport import LoopbackTransport, NodeSyncMessage

        t = LoopbackTransport()
        msg = NodeSyncMessage("node-a", "acct-x", (("f1", "hi"),), "2026-07-15T00:00:00+00:00")

        # Addressed to node-b; node-a sees nothing.
        t.send("node-b", msg)
        assert t.poll("node-a") == []
        pending = t.poll("node-b")
        assert [m.message_id for m in pending] == [msg.message_id]

        # Poll is non-destructive until ack (crash-safe): re-poll still shows it.
        assert [m.message_id for m in t.poll("node-b")] == [msg.message_id]
        t.ack("node-b", msg.message_id)
        assert t.poll("node-b") == []

    def test_send_is_idempotent_per_message_id(self):
        from axiom.memory.sync.transport import LoopbackTransport, NodeSyncMessage

        t = LoopbackTransport()
        msg = NodeSyncMessage("node-a", "acct-x", (("f1", "hi"),), "2026-07-15T00:00:00+00:00")
        t.send("node-b", msg)
        t.send("node-b", msg)  # redelivery of the same message
        assert len(t.poll("node-b")) == 1  # not double-enqueued

    def test_factory_returns_double_when_no_federation_wire(self):
        from axiom.memory.sync.transport import (
            LoopbackTransport,
            node_transport,
        )

        # No federation layer wired → the honest in-process double.
        assert isinstance(node_transport(), LoopbackTransport)
        assert isinstance(node_transport(federation=None), LoopbackTransport)

    def test_real_transport_seam_is_named_but_unwired(self):
        # The real A2A transport exists as a named seam; until the federation
        # wire lands (assessment doc §"What is MISSING") its send raises, so it
        # can never silently pretend to deliver.
        from axiom.memory.sync.transport import (
            A2AFederationTransport,
            NodeSyncMessage,
        )

        t = A2AFederationTransport(endpoint="axiom://node-remote")
        msg = NodeSyncMessage("node-a", "acct-x", (("f1", "hi"),), "2026-07-15T00:00:00+00:00")
        with pytest.raises(NotImplementedError):
            t.send("node-remote", msg)


# ---------------------------------------------------------------------------
# Node-scoped echo (echo.py reused, extended — not rewritten)
# ---------------------------------------------------------------------------


class TestNodeScopedEcho:
    def test_content_only_key_unchanged_from_p4(self):
        # P4 behaviour is byte-for-byte: no node → the old content-hash key.
        from axiom.memory.sync.echo import echo_hash

        assert echo_hash("hello") == echo_hash("hello")
        assert echo_hash("hello") == echo_hash("hello", node="")

    def test_node_scoping_partitions_the_key(self):
        from axiom.memory.sync.echo import echo_hash

        assert echo_hash("hello", node="node-b") != echo_hash("hello")
        assert echo_hash("hello", node="node-b") != echo_hash("hello", node="node-c")
        assert echo_hash("hello", node="node-b") == echo_hash("hello", node="node-b")

    def test_record_and_check_node_scoped(self, tmp_path):
        from axiom.memory.sync.echo import is_echo, record_echo

        composition = _make_composition(tmp_path / "n")
        record_echo(
            composition,
            principal="@alice:home",
            target="node-b",
            fragment_id="frag-1",
            text="always run ruff",
            node="node-b",
        )
        # Recognised only when the peer node matches (the returning direction).
        assert is_echo(composition, text="always run ruff", node="node-b")
        assert not is_echo(composition, text="always run ruff", node="node-c")
        # And the un-scoped P4 check does not see the node-scoped record.
        assert not is_echo(composition, text="always run ruff")


# ---------------------------------------------------------------------------
# PeerAuthorizer — real default-deny trust leg
# ---------------------------------------------------------------------------


class TestPeerAuthorizer:
    def test_default_deny(self):
        from axiom.memory.sync.node import PeerAuthorizer, PeerNotAuthorized

        auth = PeerAuthorizer({"node-b"})
        assert auth.is_authorized("node-b")
        assert not auth.is_authorized("node-evil")
        auth.require("node-b")  # no raise
        with pytest.raises(PeerNotAuthorized):
            auth.require("node-evil")

    def test_from_trust_profile_declared_peers(self):
        from axiom.memory.sync.node import PeerAuthorizer
        from axiom.vega.federation.policy import TrustProfile

        profile = TrustProfile(scope="@alice:home", declared_peers=frozenset({"node-b"}))
        auth = PeerAuthorizer.from_trust_profile(profile)
        assert auth.is_authorized("node-b")
        assert not auth.is_authorized("node-x")


# ---------------------------------------------------------------------------
# shared helper
# ---------------------------------------------------------------------------


def _make_composition(base):
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
