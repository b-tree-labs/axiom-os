# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Memory lifecycle — TTL, tombstones, retention cascade (#37, #45).

Two complementary concerns:

1. **TTL + tombstones** — hard deletion with propagation. When a
   fragment is deleted (user request, GDPR right-to-erasure, TTL
   expiry), we emit a signed Tombstone that federates to peer
   nodes. Fragments themselves can be crypto-shredded locally;
   the tombstone is the authoritative "this fragment no longer
   exists" record.

2. **Retention cascade (MIRIX)** — soft aging. Don't delete — fade.
   Active → Compressed (10% summary) → Archived (Vault, 1% retention).
   Combines with federation so long-term institutional memory shrinks
   gracefully without losing all trace.

Per project_memory_architecture_unified.md.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from datetime import UTC, datetime

from .fragment import MemoryFragment, RetentionTier

# ---------------------------------------------------------------------------
# TTL
# ---------------------------------------------------------------------------


def is_expired(fragment: MemoryFragment, now: str) -> bool:
    """True iff `now >= fragment.ttl`. No TTL → never expired."""
    if fragment.ttl is None:
        return False
    return now >= fragment.ttl


# ---------------------------------------------------------------------------
# Tombstones (federation-aware)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Tombstone:
    """Signed record asserting a fragment has been deleted.

    Propagates across federation so peers drop their copies.
    Signature slot is filled by the federation layer.
    """

    fragment_id: str
    reason: str
    signer_node: str
    deleted_at: str
    signature: str | None = None

    def to_dict(self) -> dict:
        return {
            "fragment_id": self.fragment_id,
            "reason": self.reason,
            "signer_node": self.signer_node,
            "deleted_at": self.deleted_at,
            "signature": self.signature,
        }


def make_tombstone(
    fragment_id: str,
    reason: str,
    signer_node: str,
) -> Tombstone:
    """Build a tombstone for a deleted fragment."""
    return Tombstone(
        fragment_id=fragment_id,
        reason=reason,
        signer_node=signer_node,
        deleted_at=datetime.now(UTC).isoformat(),
    )


def expire_fragments(
    fragments: list[MemoryFragment],
    now: str,
    signer_node: str,
) -> list[Tombstone]:
    """Return tombstones for every fragment past its TTL."""
    return [
        make_tombstone(f.id, reason="ttl expired", signer_node=signer_node)
        for f in fragments
        if is_expired(f, now)
    ]


# ---------------------------------------------------------------------------
# Retention policy + cascade (MIRIX)
# ---------------------------------------------------------------------------


@dataclass
class RetentionPolicy:
    """Age thresholds for retention-tier transitions.

    Defaults aligned with MIRIX: 30 → compress, 90 → archive.
    """

    active_to_compressed_days: int = 30
    compressed_to_archived_days: int = 90


def _parse_iso(ts: str) -> datetime:
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    return datetime.fromisoformat(ts)


def _age_days(ingestion_ts: str, now: str) -> float:
    return (_parse_iso(now) - _parse_iso(ingestion_ts)).total_seconds() / 86400.0


def next_retention_tier(
    fragment: MemoryFragment,
    now: str,
    policy: RetentionPolicy,
) -> RetentionTier:
    """Return the retention tier a fragment should have given its age."""
    age = _age_days(fragment.provenance.timestamp, now)
    if age >= policy.compressed_to_archived_days:
        return RetentionTier.ARCHIVED
    if age >= policy.active_to_compressed_days:
        return RetentionTier.COMPRESSED
    return RetentionTier.ACTIVE


# ---------------------------------------------------------------------------
# Compression / archival transforms
# ---------------------------------------------------------------------------


def compress_fragment(
    fragment: MemoryFragment,
    summary: str,
) -> MemoryFragment:
    """Replace full content with a summary and mark retention COMPRESSED.

    Caller supplies the summary (LLM-generated elsewhere). Preserves
    provenance + valid-time + id linkage for audit + replay.
    """
    return dataclasses.replace(
        fragment,
        content={"summary": summary, "original_id": fragment.id},
        retention_tier=RetentionTier.COMPRESSED,
        signature=None,  # caller re-signs with node key
    )


def archive_fragment(fragment: MemoryFragment) -> MemoryFragment:
    """Move fragment to ARCHIVED tier (Vault). Content left untouched;
    the archival itself signals storage should move to cold tier."""
    return dataclasses.replace(
        fragment,
        retention_tier=RetentionTier.ARCHIVED,
        signature=None,
    )


def apply_retention_cascade(
    fragments: list[MemoryFragment],
    now: str,
    policy: RetentionPolicy,
) -> list[MemoryFragment]:
    """Apply the retention cascade to a batch of fragments.

    For each fragment, compute the target tier based on age and apply
    the appropriate transition. Compression summaries are placeholder
    text here — real deployments wire an LLM summarizer.
    """
    out: list[MemoryFragment] = []
    for f in fragments:
        target = next_retention_tier(f, now, policy)
        if target == f.retention_tier:
            out.append(f)
            continue
        if target == RetentionTier.COMPRESSED:
            out.append(compress_fragment(f, summary="[auto-compressed]"))
        elif target == RetentionTier.ARCHIVED:
            # Stage through compression if we're coming straight from active
            if f.retention_tier == RetentionTier.ACTIVE:
                staged = compress_fragment(f, summary="[auto-compressed]")
                out.append(archive_fragment(staged))
            else:
                out.append(archive_fragment(f))
        else:
            out.append(f)
    return out
