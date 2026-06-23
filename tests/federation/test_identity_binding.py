# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the v0.10.4 peer identity-binding hardening.

Before v0.10.4, `axi nodes add` stored a fake node_id derived from the
SSH host string with no public key, making signature verification
impossible. These tests guard the repaired design: transport registration
is separate from identity fetch; identity fetch retrieves the peer's real
node_id + pubkey; TOFU with loud refusal on silent key change.
"""

from __future__ import annotations

import base64
import json

from axiom.vega.federation.discovery import KnownNode, NodeRegistry, NodeState
from axiom.vega.federation.identity import fingerprint


def _fake_peer_status(node_id="peer0011", pubkey_b64=None, owner="x@example.com"):
    if pubkey_b64 is None:
        pubkey_b64 = base64.b64encode(b"\x01" * 32).decode()
    return {
        "initialized": True,
        "node_id": node_id,
        "public_key": pubkey_b64,
        "owner": owner,
        "display_name": "peer",
        "profile": "standard",
    }


def _make_runner(status_dict, rc=0, peer_version="0.10.5"):
    """Build an ssh_runner that responds to the preflight `axi --version`
    and then returns the fixed federation-status payload."""

    def _runner(user, host, cmd):
        if cmd == "axi --version":
            return 0, f"axi {peer_version}\n", ""
        assert cmd == "axi federation status --json", cmd
        return rc, json.dumps(status_dict), ""

    return _runner


def _registry(tmp_path):
    return NodeRegistry(registry_path=tmp_path / "nodes.yaml")


class TestFingerprint:
    def test_deterministic(self):
        key = base64.b64encode(b"\x42" * 32).decode()
        assert fingerprint(key) == fingerprint(key)

    def test_grouped_by_fours(self):
        fp = fingerprint(base64.b64encode(b"\x42" * 32).decode())
        # 64 hex chars + 15 spaces
        assert len(fp) == 64 + 15
        assert all(len(g) == 4 for g in fp.split(" "))

    def test_different_keys_different_fingerprints(self):
        a = fingerprint(base64.b64encode(b"\x01" * 32).decode())
        b = fingerprint(base64.b64encode(b"\x02" * 32).decode())
        assert a != b


class TestFetchIdentitySSH:
    def test_fresh_bind_promotes_to_verified(self, tmp_path):
        reg = _registry(tmp_path)
        placeholder = reg.discover_ssh("example-host", "bbooth", "example-host.example.com")
        assert placeholder.state == NodeState.DISCOVERED
        assert placeholder.public_key == ""

        peer_status = _fake_peer_status(node_id="real_node0011")
        ok, msg = reg.fetch_identity_ssh(
            placeholder.node_id,
            ssh_runner=_make_runner(peer_status),
        )
        assert ok, msg
        # The placeholder is gone, replaced by an entry keyed by real node_id
        assert reg.get(placeholder.node_id) is None
        bound = reg.get("real_node0011")
        assert bound is not None
        assert bound.state == NodeState.VERIFIED
        assert bound.public_key == peer_status["public_key"]
        assert bound.fingerprint == fingerprint(peer_status["public_key"])
        assert bound.owner == peer_status["owner"]
        assert bound.has_verified_identity is True

    def test_refuses_silent_key_change(self, tmp_path):
        """TOFU invariant — if the peer's pubkey changed silently, refuse."""
        reg = _registry(tmp_path)

        # First fetch: bind key_A
        key_a = base64.b64encode(b"\xaa" * 32).decode()
        placeholder = reg.discover_ssh("example-host", "bbooth", "example-host.example.com")
        ok, _ = reg.fetch_identity_ssh(
            placeholder.node_id,
            ssh_runner=_make_runner(_fake_peer_status(pubkey_b64=key_a)),
        )
        assert ok

        # Simulate a new add: transport rediscovers, identity-fetch returns key_B
        # at the same real node_id. (The real scenario: attacker MITMs the SSH
        # channel and serves a different status response.)
        key_b = base64.b64encode(b"\xbb" * 32).decode()
        placeholder2 = reg.discover_ssh("example-host", "bbooth", "example-host.example.com")
        ok, msg = reg.fetch_identity_ssh(
            placeholder2.node_id,
            ssh_runner=_make_runner(_fake_peer_status(pubkey_b64=key_b)),
        )

        assert ok is False
        assert "KEY ROTATION DETECTED" in msg
        assert "out-of-band" in msg.lower()
        # Stored key is unchanged
        assert reg.get("peer0011").public_key == key_a

    def test_refuses_key_rotation_when_node_id_also_changes(self, tmp_path):
        """Real-world key rotation changes BOTH pubkey AND node_id.

        Earlier TOFU logic keyed refusal on same-node_id-different-pubkey,
        but node_id = sha256(pubkey)[:16] so rotation always mints a new
        node_id. A rotated peer at the same transport would silently
        register as a brand-new identity instead of triggering refusal —
        defeating the invariant "no silent overwrite of a known peer's
        identity."

        This test exercises the real attack / real rotation: same SSH
        transport (ssh_user@ssh_host), different pubkey, different
        node_id. Refusal MUST still fire.
        """
        reg = _registry(tmp_path)

        # First fetch: bind key_A under node_id_A
        key_a = base64.b64encode(b"\xaa" * 32).decode()
        placeholder = reg.discover_ssh("example-host", "bbooth", "example-host.example.com")
        ok, _ = reg.fetch_identity_ssh(
            placeholder.node_id,
            ssh_runner=_make_runner(_fake_peer_status(node_id="node_id_aaa", pubkey_b64=key_a)),
        )
        assert ok
        assert reg.get("node_id_aaa") is not None

        # Second fetch from the SAME transport (bbooth@example-host.example.com)
        # returns key_B under a DIFFERENT node_id_B — the real post-rotation
        # or MITM scenario.
        key_b = base64.b64encode(b"\xbb" * 32).decode()
        placeholder2 = reg.discover_ssh("example-host", "bbooth", "example-host.example.com")
        ok, msg = reg.fetch_identity_ssh(
            placeholder2.node_id,
            ssh_runner=_make_runner(_fake_peer_status(node_id="node_id_bbb", pubkey_b64=key_b)),
        )
        assert ok is False, (
            "TOFU should refuse rotation on same transport regardless of "
            f"whether node_id also changed; got ok=True, msg={msg!r}"
        )
        assert "KEY ROTATION DETECTED" in msg
        assert "bbooth@example-host.example.com" in msg, (
            "refusal message should cite the transport identity"
        )
        # Original binding must survive untouched
        assert reg.get("node_id_aaa") is not None
        assert reg.get("node_id_aaa").public_key == key_a
        # New rotation-candidate entry must NOT have been persisted
        assert reg.get("node_id_bbb") is None

    def test_confirm_key_change_accepts_rotation(self, tmp_path):
        """Legitimate rotation with operator consent should proceed."""
        reg = _registry(tmp_path)

        key_a = base64.b64encode(b"\xaa" * 32).decode()
        placeholder = reg.discover_ssh("example-host", "bbooth", "example-host.example.com")
        reg.fetch_identity_ssh(
            placeholder.node_id,
            ssh_runner=_make_runner(_fake_peer_status(pubkey_b64=key_a)),
        )

        key_b = base64.b64encode(b"\xbb" * 32).decode()
        placeholder2 = reg.discover_ssh("example-host", "bbooth", "example-host.example.com")
        ok, msg = reg.fetch_identity_ssh(
            placeholder2.node_id,
            ssh_runner=_make_runner(_fake_peer_status(pubkey_b64=key_b)),
            on_key_change="accept",
        )
        assert ok, msg
        assert reg.get("peer0011").public_key == key_b

    def test_fetch_fails_when_peer_uninitialized(self, tmp_path):
        reg = _registry(tmp_path)
        placeholder = reg.discover_ssh("example-host", "bbooth", "example-host.example.com")
        ok, msg = reg.fetch_identity_ssh(
            placeholder.node_id,
            ssh_runner=_make_runner({"initialized": False}),
        )
        assert ok is False
        assert "federation init" in msg

    def test_fetch_fails_with_ssh_error(self, tmp_path):
        reg = _registry(tmp_path)
        placeholder = reg.discover_ssh("example-host", "bbooth", "example-host.example.com")
        def runner(u, h, c):
            return (255, "", "Permission denied")
        ok, msg = reg.fetch_identity_ssh(placeholder.node_id, ssh_runner=runner)
        assert ok is False
        assert "Permission denied" in msg


class TestPubkeyLookup:
    def test_pubkey_for_verified_peer(self, tmp_path):
        reg = _registry(tmp_path)
        placeholder = reg.discover_ssh("example-host", "bbooth", "example-host.example.com")
        status = _fake_peer_status(
            node_id="abc123", pubkey_b64=base64.b64encode(b"\x77" * 32).decode()
        )
        reg.fetch_identity_ssh(
            placeholder.node_id,
            ssh_runner=_make_runner(status),
        )
        # Lookup by bare node_id
        pubkey = reg.pubkey_for("abc123")
        assert pubkey == b"\x77" * 32

        # Lookup by principal handle
        pubkey2 = reg.pubkey_for("@example-host:abc123")
        assert pubkey2 == b"\x77" * 32

    def test_pubkey_for_unverified_peer_returns_none(self, tmp_path):
        reg = _registry(tmp_path)
        reg.discover_ssh("example-host", "bbooth", "example-host.example.com")
        # No fetch — no pubkey bound yet
        assert reg.pubkey_for("example-host") is None

    def test_pubkey_for_unknown_principal(self, tmp_path):
        reg = _registry(tmp_path)
        assert reg.pubkey_for("@nonexistent:xyz") is None


class TestRegistryPersistenceWithIdentityFields:
    """Round-trip identity fields through YAML to catch silent data loss."""

    def test_save_load_roundtrip_preserves_identity(self, tmp_path):
        reg = _registry(tmp_path)
        pubkey = base64.b64encode(b"\x88" * 32).decode()
        bound = KnownNode(
            node_id="roundtrip01",
            display_name="test",
            url="ssh://user@host",
            transport="ssh",
            state=NodeState.VERIFIED,
            ssh_user="user",
            ssh_host="host",
            public_key=pubkey,
            owner="alice@example.com",
            fingerprint=fingerprint(pubkey),
            identity_verified_at="2026-04-15T12:00:00+00:00",
        )
        reg.add(bound)
        reg.save()

        reg2 = NodeRegistry(registry_path=reg._path)
        loaded = reg2.get("roundtrip01")
        assert loaded is not None
        assert loaded.public_key == pubkey
        assert loaded.owner == "alice@example.com"
        assert loaded.fingerprint == fingerprint(pubkey)
        assert loaded.has_verified_identity
