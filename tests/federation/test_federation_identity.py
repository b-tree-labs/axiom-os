# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for axiom.vega.federation — identity, agent cards, discovery, trust."""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


class TestGenerateIdentity:
    def test_creates_valid_keypair(self, tmp_path: Path) -> None:
        from axiom.vega.federation.identity import generate_identity

        ident = generate_identity("alice@example.com", keys_dir=tmp_path)
        assert ident.node_id
        assert len(ident.node_id) == 16
        assert ident.public_key  # base64
        assert ident.private_key_path.exists()

    def test_node_id_deterministic_from_pubkey(self, tmp_path: Path) -> None:
        from axiom.vega.federation.identity import generate_identity

        ident = generate_identity("bob@example.com", keys_dir=tmp_path)
        pub_bytes = base64.b64decode(ident.public_key)
        expected = hashlib.sha256(pub_bytes).hexdigest()[:16]
        assert ident.node_id == expected

    def test_agent_id_format(self, tmp_path: Path) -> None:
        from axiom.vega.federation.identity import generate_identity

        ident = generate_identity("carol@example.com", keys_dir=tmp_path)
        aid = ident.agent_id("rag", "0.1.0")
        assert aid == "carol@example.com:rag:0.1.0"

    def test_profile_default(self, tmp_path: Path) -> None:
        from axiom.vega.federation.identity import generate_identity

        ident = generate_identity("d@example.com", keys_dir=tmp_path)
        assert ident.profile == "standard"

    def test_custom_profile(self, tmp_path: Path) -> None:
        from axiom.vega.federation.identity import generate_identity

        ident = generate_identity("d@example.com", profile="coordinator", keys_dir=tmp_path)
        assert ident.profile == "coordinator"

    def test_private_key_permissions(self, tmp_path: Path) -> None:
        from axiom.vega.federation.identity import generate_identity

        ident = generate_identity("e@example.com", keys_dir=tmp_path)
        mode = ident.private_key_path.stat().st_mode & 0o777
        assert mode == 0o600


class TestIdentityPersistence:
    def test_save_load_roundtrip(self, tmp_path: Path) -> None:
        from axiom.vega.federation.identity import generate_identity, load_identity

        original = generate_identity("f@example.com", "My Node", keys_dir=tmp_path)
        loaded = load_identity(keys_dir=tmp_path)
        assert loaded is not None
        assert loaded.node_id == original.node_id
        assert loaded.public_key == original.public_key
        assert loaded.owner == original.owner
        assert loaded.display_name == original.display_name

    def test_load_missing_returns_none(self, tmp_path: Path) -> None:
        from axiom.vega.federation.identity import load_identity

        assert load_identity(keys_dir=tmp_path / "nonexistent") is None


class TestToManifest:
    def test_fields(self, tmp_path: Path) -> None:
        from axiom.vega.federation.identity import generate_identity

        ident = generate_identity("g@example.com", "G Node", keys_dir=tmp_path)
        m = ident.to_manifest()
        assert m["node_id"] == ident.node_id
        assert m["owner"] == "g@example.com"
        assert m["public_key"] == ident.public_key


# ---------------------------------------------------------------------------
# NodeManifest
# ---------------------------------------------------------------------------


class TestNodeManifest:
    def test_serialization_roundtrip(self) -> None:
        from axiom.vega.federation.identity import NodeManifest

        nm = NodeManifest(node_id="abc123", owner="x@y.com", capabilities=["llm"])
        d = nm.to_dict()
        assert d["node_id"] == "abc123"
        assert d["capabilities"] == ["llm"]
        parsed = json.loads(nm.to_json())
        assert parsed == d


# ---------------------------------------------------------------------------
# AgentCard
# ---------------------------------------------------------------------------


class TestAgentCard:
    def test_a2a_compliance(self, tmp_path: Path) -> None:
        from axiom.vega.federation.agent_card import build_agent_card
        from axiom.vega.federation.identity import generate_identity

        ident = generate_identity("h@example.com", "H Node", keys_dir=tmp_path)
        card = build_agent_card(ident, base_url="http://localhost:9090")
        d = card.to_dict()
        # A2A required fields
        assert "name" in d
        assert "description" in d
        assert "url" in d
        assert "version" in d
        assert "capabilities" in d
        assert "skills" in d
        # Axiom extensions
        assert d["axiom_node_id"] == ident.node_id
        assert d["axiom_profile"] == "standard"

    def test_json_parseable(self, tmp_path: Path) -> None:
        from axiom.vega.federation.agent_card import build_agent_card
        from axiom.vega.federation.identity import generate_identity

        ident = generate_identity("i@example.com", keys_dir=tmp_path)
        card = build_agent_card(ident)
        parsed = json.loads(card.to_json())
        assert parsed["name"] == ident.display_name


# ---------------------------------------------------------------------------
# NodeRegistry (discovery)
# ---------------------------------------------------------------------------


class TestNodeRegistry:
    def test_crud(self, tmp_path: Path) -> None:
        from axiom.vega.federation.discovery import KnownNode, NodeRegistry

        reg = NodeRegistry(registry_path=tmp_path / "nodes.yaml")
        node = KnownNode(node_id="aaa", display_name="A", url="http://a")
        reg.add(node)
        assert reg.get("aaa") is not None
        assert len(reg.list_all()) == 1
        assert reg.remove("aaa") is True
        assert reg.get("aaa") is None
        assert reg.remove("aaa") is False

    def test_persistence_roundtrip(self, tmp_path: Path) -> None:
        from axiom.vega.federation.discovery import KnownNode, NodeRegistry

        path = tmp_path / "nodes.yaml"
        reg = NodeRegistry(registry_path=path)
        reg.add(KnownNode(node_id="bbb", display_name="B", url="http://b"))
        reg.save()

        reg2 = NodeRegistry(registry_path=path)
        assert reg2.get("bbb") is not None
        assert reg2.get("bbb").display_name == "B"

    def test_update_state(self, tmp_path: Path) -> None:
        from axiom.vega.federation.discovery import KnownNode, NodeRegistry, NodeState

        reg = NodeRegistry(registry_path=tmp_path / "n.yaml")
        reg.add(KnownNode(node_id="ccc", display_name="C", url="http://c"))
        reg.update_state("ccc", NodeState.FEDERATED)
        assert reg.get("ccc").state == NodeState.FEDERATED

    def test_update_state_unknown_raises(self, tmp_path: Path) -> None:
        from axiom.vega.federation.discovery import NodeRegistry, NodeState

        reg = NodeRegistry(registry_path=tmp_path / "n.yaml")
        with pytest.raises(KeyError):
            reg.update_state("zzz", NodeState.EVICTED)

    def test_discover_ssh(self, tmp_path: Path) -> None:
        from axiom.vega.federation.discovery import NodeRegistry, NodeState

        reg = NodeRegistry(registry_path=tmp_path / "n.yaml")
        node = reg.discover_ssh("example-host", "user", "example-host.example.org")
        assert node.transport == "ssh"
        assert node.state == NodeState.DISCOVERED
        assert reg.get(node.node_id) is not None

    def test_discover_a2a(self, tmp_path: Path) -> None:
        from axiom.vega.federation.discovery import NodeRegistry, NodeState

        reg = NodeRegistry(registry_path=tmp_path / "n.yaml")
        node = reg.discover_a2a("Remote", "http://remote:8080")
        assert node.transport == "a2a"
        assert node.state == NodeState.DISCOVERED

    def test_check_health(self, tmp_path: Path) -> None:
        from axiom.vega.federation.discovery import KnownNode, NodeRegistry

        reg = NodeRegistry(registry_path=tmp_path / "n.yaml")
        reg.add(KnownNode(node_id="ddd", display_name="D", url="http://d"))
        h = reg.check_health("ddd")
        assert h["node_id"] == "ddd"
        assert reg.check_health("missing")["status"] == "not_found"

    def test_check_all(self, tmp_path: Path) -> None:
        from axiom.vega.federation.discovery import KnownNode, NodeRegistry

        reg = NodeRegistry(registry_path=tmp_path / "n.yaml")
        reg.add(KnownNode(node_id="e1", display_name="E1", url="http://e1"))
        reg.add(KnownNode(node_id="e2", display_name="E2", url="http://e2"))
        assert len(reg.check_all()) == 2


# ---------------------------------------------------------------------------
# NodeState transitions
# ---------------------------------------------------------------------------


class TestNodeState:
    def test_all_values(self) -> None:
        from axiom.vega.federation.discovery import NodeState

        expected = {
            "unknown",
            "discovered",
            "verified",
            "trusted",
            "federated",
            "unreachable",
            "leaving",
            "evicted",
        }
        assert {s.value for s in NodeState} == expected


# ---------------------------------------------------------------------------
# Trust — InvitationToken
# ---------------------------------------------------------------------------


class TestInvitationToken:
    def test_creation(self, tmp_path: Path) -> None:
        from axiom.vega.federation.identity import generate_identity
        from axiom.vega.federation.trust import create_invitation

        ident = generate_identity("j@example.com", keys_dir=tmp_path)
        inv = create_invitation(ident)
        assert inv.token
        assert inv.issuer_node_id == ident.node_id
        assert not inv.accepted

    def test_expiry_future(self, tmp_path: Path) -> None:
        from axiom.vega.federation.identity import generate_identity
        from axiom.vega.federation.trust import create_invitation

        ident = generate_identity("k@example.com", keys_dir=tmp_path)
        inv = create_invitation(ident, ttl_hours=1)
        assert not inv.is_expired()

    def test_expiry_past(self, tmp_path: Path) -> None:
        from axiom.vega.federation.trust import InvitationToken

        now = datetime.now(UTC)
        inv = InvitationToken(
            token="x",
            issuer_node_id="nid",
            issuer_display_name="N",
            created_at=(now - timedelta(hours=2)).isoformat(),
            expires_at=(now - timedelta(hours=1)).isoformat(),
        )
        assert inv.is_expired()

    def test_to_dict(self, tmp_path: Path) -> None:
        from axiom.vega.federation.identity import generate_identity
        from axiom.vega.federation.trust import create_invitation

        ident = generate_identity("l@example.com", keys_dir=tmp_path)
        inv = create_invitation(ident)
        d = inv.to_dict()
        assert d["token"] == inv.token
        assert d["accepted"] is False


class TestTrustValidation:
    def test_valid(self) -> None:
        from axiom.vega.federation.identity import NodeManifest
        from axiom.vega.federation.trust import validate_invitation

        m = NodeManifest(node_id="abc")
        assert validate_invitation("some-token", m) is True

    def test_empty_token(self) -> None:
        from axiom.vega.federation.identity import NodeManifest
        from axiom.vega.federation.trust import validate_invitation

        m = NodeManifest(node_id="abc")
        assert validate_invitation("", m) is False

    def test_empty_manifest(self) -> None:
        from axiom.vega.federation.identity import NodeManifest
        from axiom.vega.federation.trust import validate_invitation

        m = NodeManifest()
        assert validate_invitation("tok", m) is False
