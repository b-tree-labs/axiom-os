# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for memory lifecycle — TTL, tombstones, retention cascade.

Per MIRIX retention cascade: don't delete — fade.
  Day 1-30   → active (full detail)
  Day 31-90  → compressed (10% summary retained)
  Day 91+    → archived (vault tier, 1% retention)
  Hard delete → tombstone (crypto-shredded content, tombstone propagates)

Tombstones carry the federation-required signature slot so deletion
can propagate across nodes for GDPR right-to-erasure.
"""

from __future__ import annotations


def _frag(ingestion_ts: str | None = None):
    import dataclasses

    from axiom.memory.fragment import Provenance, create_fragment

    base = create_fragment(
        content={"fact": "content here"}, cognitive_type="semantic",
        principal_id="u1", agents=set(), resources=set(),
    )
    if ingestion_ts is None:
        return base
    return dataclasses.replace(
        base,
        provenance=Provenance(
            timestamp=ingestion_ts,
            principal_id="u1", agents=frozenset(), resources=frozenset(),
        ),
    )


# ---------------------------------------------------------------------------
# TTL + expiry
# ---------------------------------------------------------------------------


class TestTTL:
    def test_fragment_without_ttl_not_expired(self):
        from axiom.memory.lifecycle import is_expired

        f = _frag()
        assert is_expired(f, "2099-01-01T00:00:00Z") is False

    def test_fragment_past_ttl_expired(self):
        import dataclasses

        from axiom.memory.lifecycle import is_expired

        f = _frag()
        f = dataclasses.replace(f, ttl="2025-12-31T23:59:59Z")
        assert is_expired(f, "2026-01-01T00:00:00Z") is True
        assert is_expired(f, "2025-12-30T00:00:00Z") is False


class TestTombstone:
    def test_make_tombstone(self):
        from axiom.memory.lifecycle import make_tombstone

        ts = make_tombstone(
            fragment_id="frag-1", reason="user deletion",
            signer_node="example-host.example.org",
        )
        assert ts.fragment_id == "frag-1"
        assert ts.reason == "user deletion"
        assert ts.signer_node == "example-host.example.org"
        assert ts.deleted_at  # ISO 8601 timestamp
        assert ts.signature is None  # federation layer fills in

    def test_tombstone_serializes(self):
        from axiom.memory.lifecycle import make_tombstone

        ts = make_tombstone("frag-1", "gdpr", "n1")
        as_dict = ts.to_dict()
        assert as_dict["fragment_id"] == "frag-1"
        assert as_dict["reason"] == "gdpr"


class TestExpireFragments:
    def test_expired_fragments_produce_tombstones(self):
        import dataclasses

        from axiom.memory.lifecycle import expire_fragments

        f_expired = dataclasses.replace(_frag(), ttl="2025-12-31T23:59:59Z")
        f_live = _frag()

        tombstones = expire_fragments(
            [f_expired, f_live],
            now="2026-01-01T00:00:00Z",
            signer_node="example-host",
        )
        assert len(tombstones) == 1
        assert tombstones[0].fragment_id == f_expired.id
        assert "ttl" in tombstones[0].reason.lower()


# ---------------------------------------------------------------------------
# Retention cascade (MIRIX)
# ---------------------------------------------------------------------------


class TestRetentionTransition:
    def test_new_fragment_stays_active(self):
        from axiom.memory.fragment import RetentionTier
        from axiom.memory.lifecycle import RetentionPolicy, next_retention_tier

        f = _frag(ingestion_ts="2026-04-15T00:00:00Z")
        policy = RetentionPolicy(
            active_to_compressed_days=30,
            compressed_to_archived_days=90,
        )
        tier = next_retention_tier(f, now="2026-04-17T00:00:00Z", policy=policy)
        assert tier == RetentionTier.ACTIVE

    def test_31_day_old_active_moves_to_compressed(self):
        from axiom.memory.fragment import RetentionTier
        from axiom.memory.lifecycle import RetentionPolicy, next_retention_tier

        f = _frag(ingestion_ts="2026-01-01T00:00:00Z")
        policy = RetentionPolicy(
            active_to_compressed_days=30,
            compressed_to_archived_days=90,
        )
        # 107 days later
        tier = next_retention_tier(f, now="2026-04-17T00:00:00Z", policy=policy)
        # 107 > 90 → archived
        assert tier == RetentionTier.ARCHIVED

    def test_intermediate_age_compressed(self):
        from axiom.memory.fragment import RetentionTier
        from axiom.memory.lifecycle import RetentionPolicy, next_retention_tier

        f = _frag(ingestion_ts="2026-03-01T00:00:00Z")
        policy = RetentionPolicy(
            active_to_compressed_days=30,
            compressed_to_archived_days=90,
        )
        # 47 days later: between 30 and 90 → compressed
        tier = next_retention_tier(f, now="2026-04-17T00:00:00Z", policy=policy)
        assert tier == RetentionTier.COMPRESSED


class TestCompression:
    def test_compress_produces_shrunken_content(self):
        from axiom.memory.fragment import RetentionTier
        from axiom.memory.lifecycle import compress_fragment

        f = _frag()
        compressed = compress_fragment(f, summary="condensed")
        assert compressed.retention_tier == RetentionTier.COMPRESSED
        assert compressed.content.get("summary") == "condensed"
        assert compressed.content.get("original_id") == f.id

    def test_archive_further_compresses(self):
        from axiom.memory.fragment import RetentionTier
        from axiom.memory.lifecycle import archive_fragment, compress_fragment

        f = _frag()
        compressed = compress_fragment(f, summary="condensed")
        archived = archive_fragment(compressed)
        assert archived.retention_tier == RetentionTier.ARCHIVED
        assert archived.content.get("original_id") == f.id


class TestCascadeBatch:
    def test_apply_cascade_transitions_all_fragments(self):

        from axiom.memory.lifecycle import (
            RetentionPolicy,
            apply_retention_cascade,
        )

        fragments = [
            _frag(ingestion_ts="2026-04-15T00:00:00Z"),  # active
            _frag(ingestion_ts="2026-03-01T00:00:00Z"),  # compressed
            _frag(ingestion_ts="2025-12-01T00:00:00Z"),  # archived
        ]
        policy = RetentionPolicy(
            active_to_compressed_days=30,
            compressed_to_archived_days=90,
        )
        result = apply_retention_cascade(
            fragments, now="2026-04-17T00:00:00Z", policy=policy
        )
        tiers = sorted(f.retention_tier.value for f in result)
        assert tiers == ["active", "archived", "compressed"]
