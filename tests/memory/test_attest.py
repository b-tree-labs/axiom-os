# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for axiom/memory/attest.py — signed provenance + audit log.

Closes the Collaborative Memory paper's biggest production gap (§6
admits provenance is claim-style, not cryptographically attested).

Every fragment is signed at creation. Every access (read/write/
revoke) appends to an append-only audit log. Tampering is detectable.
"""

from __future__ import annotations

import json


def _fragment(agents=None, resources=None):
    from axiom.memory.fragment import create_fragment

    return create_fragment(
        content={"fact": "canonical"},
        cognitive_type="semantic",
        principal_id="u1",
        agents=agents or set(),
        resources=resources or set(),
    )


# ---------------------------------------------------------------------------
# Canonical bytes
# ---------------------------------------------------------------------------


class TestCanonicalBytes:
    def test_same_fragment_same_bytes(self):
        from axiom.memory.attest import canonical_bytes

        f1 = _fragment()
        b1 = canonical_bytes(f1)
        b2 = canonical_bytes(f1)
        assert b1 == b2

    def test_signature_slot_excluded(self):
        """Canonical form must exclude the signature field itself."""
        from axiom.memory.attest import canonical_bytes
        from axiom.memory.fragment import MemoryFragment

        base = _fragment()
        # Same fragment, one with a signature set
        signed = MemoryFragment(
            id=base.id, cognitive_type=base.cognitive_type,
            content=base.content, provenance=base.provenance,
            retention_tier=base.retention_tier, ttl=base.ttl,
            signature="some-fake-sig-bytes-hex",
        )
        assert canonical_bytes(base) == canonical_bytes(signed)

    def test_different_content_different_bytes(self):
        from axiom.memory.attest import canonical_bytes
        from axiom.memory.fragment import create_fragment

        f1 = create_fragment(
            content={"fact": "A"}, cognitive_type="semantic",
            principal_id="u1", agents=set(), resources=set(),
        )
        f2 = create_fragment(
            content={"fact": "B"}, cognitive_type="semantic",
            principal_id="u1", agents=set(), resources=set(),
        )
        assert canonical_bytes(f1) != canonical_bytes(f2)


# ---------------------------------------------------------------------------
# Sign / verify
# ---------------------------------------------------------------------------


class TestSignVerify:
    def test_sign_returns_new_fragment_with_signature(self):
        from axiom.memory.attest import sign_fragment
        from axiom.vega.identity.keypair import generate_keypair

        kp = generate_keypair()
        frag = _fragment()
        signed = sign_fragment(frag, kp)

        assert signed.signature is not None
        assert signed.id == frag.id
        assert signed.content == frag.content

    def test_verify_signed_fragment(self):
        from axiom.memory.attest import sign_fragment, verify_fragment_signature
        from axiom.vega.identity.keypair import generate_keypair

        kp = generate_keypair()
        frag = _fragment()
        signed = sign_fragment(frag, kp)

        assert verify_fragment_signature(signed, kp.public_bytes) is True

    def test_unsigned_fragment_fails_verify(self):
        from axiom.memory.attest import verify_fragment_signature
        from axiom.vega.identity.keypair import generate_keypair

        kp = generate_keypair()
        frag = _fragment()  # no signature
        assert verify_fragment_signature(frag, kp.public_bytes) is False

    def test_tampered_fragment_fails_verify(self):
        """Change content after sign → verification fails."""
        from axiom.memory.attest import sign_fragment, verify_fragment_signature
        from axiom.memory.fragment import MemoryFragment
        from axiom.vega.identity.keypair import generate_keypair

        kp = generate_keypair()
        frag = _fragment()
        signed = sign_fragment(frag, kp)

        # Forge a variant with different content but the same signature
        tampered = MemoryFragment(
            id=signed.id, cognitive_type=signed.cognitive_type,
            content={"fact": "TAMPERED"},
            provenance=signed.provenance,
            retention_tier=signed.retention_tier,
            signature=signed.signature,
        )
        assert verify_fragment_signature(tampered, kp.public_bytes) is False

    def test_wrong_public_key_fails_verify(self):
        from axiom.memory.attest import sign_fragment, verify_fragment_signature
        from axiom.vega.identity.keypair import generate_keypair

        kp1 = generate_keypair()
        kp2 = generate_keypair()
        signed = sign_fragment(_fragment(), kp1)
        assert verify_fragment_signature(signed, kp2.public_bytes) is False


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------


class TestAuditLog:
    def test_append_and_read(self, tmp_path):
        from axiom.memory.attest import AuditLog

        log = AuditLog(tmp_path / "audit.jsonl")
        log.record(
            entry_type="read",
            principal_id="u1",
            agent_id="a1",
            fragment_id="frag-1",
            outcome="allowed",
        )
        entries = list(log.read_all())
        assert len(entries) == 1
        assert entries[0]["entry_type"] == "read"
        assert entries[0]["principal_id"] == "u1"
        assert entries[0]["fragment_id"] == "frag-1"
        assert entries[0]["outcome"] == "allowed"
        assert "timestamp" in entries[0]

    def test_multiple_entries_append_order(self, tmp_path):
        from axiom.memory.attest import AuditLog

        log = AuditLog(tmp_path / "audit.jsonl")
        for i in range(5):
            log.record(
                entry_type="write",
                principal_id="u1",
                agent_id=f"a{i}",
                fragment_id=f"f{i}",
                outcome="ok",
            )
        entries = list(log.read_all())
        assert len(entries) == 5
        assert [e["agent_id"] for e in entries] == ["a0", "a1", "a2", "a3", "a4"]

    def test_jsonl_format(self, tmp_path):
        from axiom.memory.attest import AuditLog

        log = AuditLog(tmp_path / "audit.jsonl")
        log.record(entry_type="read", principal_id="u1", agent_id="a1",
                   fragment_id="f1", outcome="allowed")
        log.record(entry_type="read", principal_id="u1", agent_id="a1",
                   fragment_id="f2", outcome="denied")

        lines = (tmp_path / "audit.jsonl").read_text().strip().split("\n")
        assert len(lines) == 2
        # Each line is valid JSON
        [json.loads(line) for line in lines]

    def test_signed_audit_entries(self, tmp_path):
        """Audit entries can be signed with a node key for integrity."""
        from axiom.memory.attest import AuditLog, verify_audit_entry
        from axiom.vega.identity.keypair import generate_keypair

        kp = generate_keypair()
        log = AuditLog(tmp_path / "audit.jsonl", signing_keypair=kp)
        log.record(entry_type="read", principal_id="u1", agent_id="a1",
                   fragment_id="f1", outcome="allowed")

        entries = list(log.read_all())
        assert entries[0].get("signature") is not None
        assert verify_audit_entry(entries[0], kp.public_bytes) is True

    def test_filter_by_fragment(self, tmp_path):
        from axiom.memory.attest import AuditLog

        log = AuditLog(tmp_path / "audit.jsonl")
        log.record(entry_type="read", principal_id="u1", agent_id="a1",
                   fragment_id="f1", outcome="allowed")
        log.record(entry_type="read", principal_id="u2", agent_id="a1",
                   fragment_id="f2", outcome="allowed")
        log.record(entry_type="read", principal_id="u3", agent_id="a1",
                   fragment_id="f1", outcome="denied")

        hits = list(log.query(fragment_id="f1"))
        assert len(hits) == 2
        assert all(e["fragment_id"] == "f1" for e in hits)
