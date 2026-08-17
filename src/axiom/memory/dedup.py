# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Entity-resolution dedup (ADR-087 D3) — not a boolean.

Pipeline: **blocking** (lexical + structured keys) → **matching**
(content hash / idempotency key → vector similarity → adjudication
band) → **clustering** → **canonicalization**. Three confidence tiers:

- **exact** — auto-collapse;
- **near-duplicate** — reversible merge: the folded fragment is
  tombstoned ``deduped:merged-into:<id>`` and the alias set of every
  folded source coordinate is preserved, so per-source extraction
  survives merging and :func:`unmerge` restores both witnesses;
- **conflict/ambiguous** — kept-both + queued, never auto-merged.

Two clocks: the write-time near-neighbor check
(:meth:`DedupEngine.write_time_check`, wired through the importer's
``dedup`` seam) and the scheduled corpus-health re-cluster pass —
shipped here as the invocable :func:`recluster` (the
``memory.dedup_recluster`` skill); **no scheduler wiring in P2**.

Durable record shapes ride the artifact registry beside the fragments
they describe (the P0 import-path precedent; every *memory* write stays
behind ``CompositionService``):

- ``memory_conflict`` — an open kept-both pair awaiting human review.
  P2 exposes the queue read-only (``axi memory conflicts list``);
  adjudication verbs are deliberately out of scope.
- ``memory_alias`` — one folded source coordinate → its canonical
  fragment.
- ``memory_merge`` — one reversible merge (what folded into what, and
  under which tier).

Thresholds are fixed defaults in P2 — fixed-vs-learned is ADR-087 open
question 1 and stays open (docs/working/cross-mem-p2-open-questions.md).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from difflib import SequenceMatcher
from enum import Enum
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from axiom.memory.fragment import MemoryFragment

CONFLICT_KIND = "memory_conflict"
ALIAS_KIND = "memory_alias"
MERGE_KIND = "memory_merge"

MERGE_TOMBSTONE_PREFIX = "deduped:merged-into:"

_AGENT = "axi-memory"

_STOPWORDS = frozenset({
    "the", "a", "an", "is", "are", "was", "were", "in", "on", "at",
    "of", "to", "and", "or", "for", "with", "from", "by", "it", "its",
    "this", "that", "be", "as", "not",
})


def _pair_key(fragment_ids: list[str]) -> str:
    """Stable conflict-record name for a set of fragment ids."""
    joined = "|".join(sorted(fragment_ids))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def queue_conflict(
    composition: Any,
    *,
    principal: str,
    fragment_ids: list[str],
    reason: str,
    sources: list[dict] | None = None,
) -> str | None:
    """Queue a kept-both conflict record (idempotent per id-pair).

    Returns the registry artifact id, or ``None`` when the same pair is
    already queued (re-running detection never spams the queue).
    """
    name = _pair_key(fragment_ids)
    existing = composition.artifact_registry.find_by_name(CONFLICT_KIND, name)
    if existing:
        return None
    record = {
        "principal": principal,
        "fragment_ids": sorted(fragment_ids),
        "reason": reason,
        "sources": sources or [],
        "detected_at": datetime.now(UTC).isoformat(),
        "status": "open",
    }
    artifact_id = composition.artifact_registry.register(
        kind=CONFLICT_KIND, name=name, data=record,
    )
    composition.audit_log.record(
        entry_type="dedup_conflict_queued",
        principal_id=principal,
        agent_id=_AGENT,
        fragment_id=",".join(sorted(fragment_ids)),
        outcome=reason,
    )
    return artifact_id


def list_conflicts(
    composition: Any, *, principal: str | None = None
) -> list[dict]:
    """Read-only view of the conflict queue, oldest first.

    ``principal`` filters when given. Each entry is the stored record
    plus its registry ``artifact_id`` so future adjudication tooling
    (out of P2 scope) can address it.
    """
    out: list[dict] = []
    for artifact in composition.artifact_registry.list(kind=CONFLICT_KIND):
        data = dict(artifact.data or {})
        if principal is not None and data.get("principal") != principal:
            continue
        data["artifact_id"] = artifact.id
        out.append(data)
    return out


# ---------------------------------------------------------------------------
# Alias set — folded source coordinates survive merges (D3)
# ---------------------------------------------------------------------------


def alias_name(origin: dict) -> str:
    """Registry name for an alias record — keyed by the folded
    coordinate, so the same coordinate never registers twice."""
    joined = "|".join(
        (origin.get("harness", ""), origin.get("account", ""),
         origin.get("source_ref", ""))
    )
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def record_alias(
    composition: Any,
    *,
    principal: str,
    canonical_id: str,
    origin: dict,
    merged_fragment_id: str | None = None,
) -> str:
    """Record that a source coordinate now resolves to ``canonical_id``.

    Written whenever a merge/collapse folds an absorbed fragment into a
    canonical one: the folded coordinate keeps pointing at live memory.
    """
    artifact_id = composition.artifact_registry.register(
        kind=ALIAS_KIND,
        name=alias_name(origin),
        data={
            "principal": principal,
            "canonical_id": canonical_id,
            "harness": origin.get("harness", ""),
            "account": origin.get("account", ""),
            "source_ref": origin.get("source_ref", ""),
            "merged_fragment_id": merged_fragment_id,
            "folded_at": datetime.now(UTC).isoformat(),
        },
    )
    composition.audit_log.record(
        entry_type="dedup_alias_recorded",
        principal_id=principal,
        agent_id=_AGENT,
        fragment_id=canonical_id,
        outcome="ok",
        source_ref=origin.get("source_ref", ""),
        harness=origin.get("harness", ""),
    )
    return artifact_id


def aliases_for_source(
    composition: Any, *, harness: str, account: str | None = None
) -> list[dict]:
    """Alias records whose folded coordinate matches the source."""
    out: list[dict] = []
    for artifact in composition.artifact_registry.list(kind=ALIAS_KIND):
        data = artifact.data or {}
        if data.get("harness") != harness:
            continue
        if account is not None and data.get("account") != account:
            continue
        out.append(data)
    return out


# ---------------------------------------------------------------------------
# Matching — normalized text, blocking keys, tiers
# ---------------------------------------------------------------------------


class MatchTier(str, Enum):
    """D3 confidence tiers (plus the everything-else bucket)."""

    EXACT = "exact"
    NEAR_DUP = "near_dup"
    CONFLICT = "conflict"
    DISTINCT = "distinct"


def normalize_text(text: str) -> str:
    """Casefold + whitespace-collapse: the exact-tier equivalence."""
    return " ".join(text.casefold().split())


def blocking_tokens(text: str) -> set[str]:
    """Lexical blocking keys: distinctive tokens of the normalized text."""
    return {
        t
        for t in re.findall(r"[a-z0-9]+", text.casefold())
        if len(t) >= 3 and t not in _STOPWORDS
    }


def fragment_dedup_text(fragment: MemoryFragment) -> str:
    """The text a fragment is matched on (recall-projection rendering)."""
    from axiom.memory.recall_projection import _render_text

    return _render_text(fragment.cognitive_type, fragment.content)


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(y * y for y in b) ** 0.5
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


@dataclass
class DedupEngine:
    """Tier classifier + the write-time near-neighbor clock.

    ``embedder`` is ``list[str] -> list[list[float]] | None`` (the
    recall stack's shape); ``None`` or a failing provider degrades
    matching to lexical similarity — dedup never breaks on an embedding
    outage. Thresholds are fixed P2 defaults (ADR-087 OQ1 stays open):
    similarity ≥ *near* is a near-duplicate; ≥ *conflict* is the
    ambiguous adjudication band; below is distinct.
    """

    embedder: Callable[[list[str]], list[list[float]] | None] | None = None
    near_threshold: float = 0.92
    conflict_threshold: float = 0.75
    lexical_near_threshold: float = 0.95
    lexical_conflict_threshold: float = 0.65
    _vec_cache: dict[str, list[float] | None] = field(
        default_factory=dict, repr=False,
    )
    _degraded: bool = field(default=False, repr=False)

    # ---- matching -----------------------------------------------------------

    def _vector(self, text: str) -> list[float] | None:
        if self.embedder is None or self._degraded:
            return None
        if text not in self._vec_cache:
            try:
                vectors = self.embedder([text])
            except Exception:
                vectors = None
            if not vectors:
                self._degraded = True
                self._vec_cache[text] = None
            else:
                self._vec_cache[text] = vectors[0]
        return self._vec_cache[text]

    def classify_scored(
        self, text_a: str, text_b: str
    ) -> tuple[MatchTier, float]:
        """One pair through the matching ladder: hash → vector → lexical."""
        norm_a, norm_b = normalize_text(text_a), normalize_text(text_b)
        if norm_a == norm_b:
            return MatchTier.EXACT, 1.0

        vec_a, vec_b = self._vector(norm_a), self._vector(norm_b)
        if vec_a is not None and vec_b is not None:
            score = _cosine(vec_a, vec_b)
            if score >= self.near_threshold:
                return MatchTier.NEAR_DUP, score
            if score >= self.conflict_threshold:
                return MatchTier.CONFLICT, score
            return MatchTier.DISTINCT, score

        score = SequenceMatcher(None, norm_a, norm_b).ratio()
        if score >= self.lexical_near_threshold:
            return MatchTier.NEAR_DUP, score
        if score >= self.lexical_conflict_threshold:
            return MatchTier.CONFLICT, score
        return MatchTier.DISTINCT, score

    def classify_pair(self, text_a: str, text_b: str) -> MatchTier:
        return self.classify_scored(text_a, text_b)[0]

    # ---- clock 1: write-time near-neighbor check ----------------------------

    def write_time_check(
        self, composition: Any, fragment: MemoryFragment, *, principal: str
    ) -> str:
        """Resolve a just-written fragment against its neighbors.

        Returns ``"collapsed_exact"``, ``"merged_near_dup"``,
        ``"conflict_queued"``, or ``"distinct"``. Exact and near-dup
        fold the *new* fragment into the earliest/most-similar existing
        one (reversibly — witness tombstoned, alias set preserved);
        the ambiguous band keeps both and queues; vault never plays.
        """
        from axiom.memory.fragment import CognitiveType, fragment_from_dict

        if fragment.cognitive_type is CognitiveType.VAULT:
            return "distinct"

        text = fragment_dedup_text(fragment)
        tokens = blocking_tokens(text)

        best_exact: MemoryFragment | None = None
        best_near: tuple[float, MemoryFragment] | None = None
        best_conflict: tuple[float, MemoryFragment] | None = None

        for artifact in _live_fragment_artifacts(composition, principal):
            data = artifact.data or {}
            if data.get("id") == fragment.id:
                continue
            if data.get("cognitive_type") != fragment.cognitive_type.value:
                continue
            other = fragment_from_dict(data)
            other_text = fragment_dedup_text(other)
            if not (tokens & blocking_tokens(other_text)):
                continue  # blocked out
            tier, score = self.classify_scored(text, other_text)
            if tier is MatchTier.EXACT and best_exact is None:
                best_exact = other
            elif tier is MatchTier.NEAR_DUP:
                if best_near is None or score > best_near[0]:
                    best_near = (score, other)
            elif tier is MatchTier.CONFLICT:
                if best_conflict is None or score > best_conflict[0]:
                    best_conflict = (score, other)

        if best_exact is not None:
            merge_fragments(
                composition, canonical_id=best_exact.id, merged=fragment,
                principal=principal, tier=MatchTier.EXACT.value,
            )
            return "collapsed_exact"
        if best_near is not None:
            merge_fragments(
                composition, canonical_id=best_near[1].id, merged=fragment,
                principal=principal, tier=MatchTier.NEAR_DUP.value,
            )
            return "merged_near_dup"
        if best_conflict is not None:
            queue_conflict(
                composition,
                principal=principal,
                fragment_ids=[best_conflict[1].id, fragment.id],
                reason="write_time_ambiguous",
                sources=(
                    [fragment.provenance.origin.to_dict()]
                    if fragment.provenance.origin
                    else []
                ),
            )
            return "conflict_queued"
        return "distinct"


# ---------------------------------------------------------------------------
# Merge / unmerge — reversible by construction
# ---------------------------------------------------------------------------


def merge_fragments(
    composition: Any,
    *,
    canonical_id: str,
    merged: MemoryFragment,
    principal: str,
    tier: str,
) -> str:
    """Fold ``merged`` into ``canonical_id`` (D3 supersede-via-tombstone).

    The witness rows stay on disk (tombstoned with the merge reason),
    the folded source coordinate joins the alias set, and a
    ``memory_merge`` record makes the operation addressable — so
    :func:`unmerge` can restore both witnesses byte-identically.
    """
    reason = f"{MERGE_TOMBSTONE_PREFIX}{canonical_id}"
    for artifact in composition.artifact_registry.find_by_name(
        "fragment", merged.id
    ):
        composition.artifact_registry.delete(artifact.id, reason=reason)
    if composition.recall_index is not None:
        try:
            composition.recall_index.evict(
                merged.id, merged.provenance.principal_id
            )
        except Exception:
            pass  # projection is rebuildable; never blocks the merge

    origin_dict = (
        merged.provenance.origin.to_dict()
        if merged.provenance.origin is not None
        else None
    )
    if origin_dict is not None:
        record_alias(
            composition,
            principal=principal,
            canonical_id=canonical_id,
            origin=origin_dict,
            merged_fragment_id=merged.id,
        )
    merge_id = composition.artifact_registry.register(
        kind=MERGE_KIND,
        name=merged.id,
        data={
            "canonical_id": canonical_id,
            "merged_id": merged.id,
            "tier": tier,
            "principal": principal,
            "origin": origin_dict,
            "merged_at": datetime.now(UTC).isoformat(),
        },
    )
    composition.audit_log.record(
        entry_type="dedup_merged",
        principal_id=principal,
        agent_id=_AGENT,
        fragment_id=merged.id,
        outcome=tier,
        canonical_id=canonical_id,
    )
    return merge_id


def unmerge(composition: Any, merged_fragment_id: str) -> bool:
    """Reverse a merge: restore the folded witness, retire its aliases.

    Returns ``False`` when no live merge record exists (already
    unmerged, or never merged). Both witnesses are live afterwards —
    the D3 reversibility gate.
    """
    from axiom.memory.fragment import fragment_from_dict

    records = composition.artifact_registry.find_by_name(
        MERGE_KIND, merged_fragment_id
    )
    if not records:
        return False
    record = records[-1]
    principal = (record.data or {}).get("principal", "")

    live = composition.artifact_registry.find_by_name(
        "fragment", merged_fragment_id
    )
    if not live:
        rows = composition.artifact_registry.find_by_name(
            "fragment", merged_fragment_id, include_deleted=True,
        )
        if not rows:
            return False
        payload = rows[-1].data
        composition.artifact_registry.register(
            kind="fragment", name=merged_fragment_id, data=payload,
        )
        if composition.recall_index is not None:
            try:
                composition.recall_index.index_fragment(
                    fragment_from_dict(payload)
                )
            except Exception:
                pass

    for artifact in composition.artifact_registry.list(kind=ALIAS_KIND):
        if (artifact.data or {}).get("merged_fragment_id") == merged_fragment_id:
            composition.artifact_registry.delete(
                artifact.id, reason="unmerged"
            )
    for rec in records:
        composition.artifact_registry.delete(rec.id, reason="unmerged")

    composition.audit_log.record(
        entry_type="dedup_unmerged",
        principal_id=principal,
        agent_id=_AGENT,
        fragment_id=merged_fragment_id,
        outcome="ok",
        canonical_id=(record.data or {}).get("canonical_id", ""),
    )
    return True


# ---------------------------------------------------------------------------
# Clock 2: the invocable re-cluster pass (no scheduler wiring in P2)
# ---------------------------------------------------------------------------


@dataclass
class ReclusterReport:
    """What one corpus-health pass did (or would do, dry-run)."""

    principal: str
    fragments: int = 0
    examined_pairs: int = 0
    clusters: int = 0
    merged: int = 0
    conflicts_queued: int = 0
    dry_run: bool = False


def _live_fragment_artifacts(composition: Any, principal: str) -> list:
    registry = composition.artifact_registry
    backend = getattr(registry, "_backend", None)
    if hasattr(backend, "find_fragments"):
        artifacts = backend.find_fragments(principal_id=principal)
    else:
        artifacts = [
            a for a in registry.list(kind="fragment")
            if (a.data or {}).get("provenance", {}).get("principal_id")
            == principal
        ]
    seen: set[str] = set()
    out = []
    for a in artifacts:
        if a.name in seen:
            continue
        seen.add(a.name)
        out.append(a)
    return out


def _open_conflict_sets(composition: Any, principal: str) -> list[frozenset]:
    return [
        frozenset(entry.get("fragment_ids") or [])
        for entry in list_conflicts(composition, principal=principal)
        if entry.get("status") == "open"
    ]


def recluster(
    composition: Any,
    *,
    principal: str,
    engine: DedupEngine | None = None,
    dry_run: bool = False,
) -> ReclusterReport:
    """Corpus-health entity-resolution pass over one principal's memory.

    Blocking → matching → union-find clustering → canonicalization
    (canonical = earliest write; the rest fold in reversibly). Pairs in
    the adjudication band queue as conflicts. A cluster containing a
    conflicting pair is **frozen whole** — nothing auto-merges across
    the conflict tier, even transitively. Pairs already queued (open
    conflicts) are never re-examined. Vault never plays. Idempotent:
    a second pass over an already-resolved corpus does nothing.
    """
    from axiom.memory.fragment import CognitiveType, fragment_from_dict

    engine = engine if engine is not None else DedupEngine()
    report = ReclusterReport(principal=principal, dry_run=dry_run)

    fragments = [
        fragment_from_dict(a.data)
        for a in _live_fragment_artifacts(composition, principal)
    ]
    fragments = [
        f for f in fragments if f.cognitive_type is not CognitiveType.VAULT
    ]
    fragments.sort(key=lambda f: (f.provenance.timestamp, f.id))
    report.fragments = len(fragments)
    if len(fragments) < 2:
        return report

    order = {f.id: i for i, f in enumerate(fragments)}
    texts = {f.id: fragment_dedup_text(f) for f in fragments}
    by_id = {f.id: f for f in fragments}

    # Blocking: only pairs sharing a distinctive token and a cognitive
    # type are ever matched.
    token_index: dict[str, list[str]] = {}
    for f in fragments:
        for token in blocking_tokens(texts[f.id]):
            token_index.setdefault(token, []).append(f.id)
    pairs: set[tuple[str, str]] = set()
    for ids in token_index.values():
        for i, fa in enumerate(ids):
            for fb in ids[i + 1:]:
                if by_id[fa].cognitive_type != by_id[fb].cognitive_type:
                    continue
                pairs.add(
                    (fa, fb) if order[fa] < order[fb] else (fb, fa)
                )

    open_sets = _open_conflict_sets(composition, principal)

    parent: dict[str, str] = {f.id: f.id for f in fragments}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            # Deterministic: earlier write wins the root.
            if order[ra] <= order[rb]:
                parent[rb] = ra
            else:
                parent[ra] = rb

    conflict_pairs: list[tuple[str, str]] = []
    for fa, fb in sorted(pairs, key=lambda p: (order[p[0]], order[p[1]])):
        if any({fa, fb} <= s for s in open_sets):
            continue  # awaiting adjudication — hands off
        report.examined_pairs += 1
        tier = engine.classify_pair(texts[fa], texts[fb])
        if tier in (MatchTier.EXACT, MatchTier.NEAR_DUP):
            union(fa, fb)
        elif tier is MatchTier.CONFLICT:
            conflict_pairs.append((fa, fb))

    # Freeze any cluster a conflict pair landed in (transitive safety).
    frozen_roots = {
        find(fa) for fa, fb in conflict_pairs if find(fa) == find(fb)
    }

    clusters: dict[str, list[str]] = {}
    for f in fragments:
        clusters.setdefault(find(f.id), []).append(f.id)

    for root, members in clusters.items():
        if len(members) < 2 or root in frozen_roots:
            continue
        report.clusters += 1
        members.sort(key=lambda fid: order[fid])
        canonical = members[0]
        for fid in members[1:]:
            report.merged += 1
            if not dry_run:
                merge_fragments(
                    composition,
                    canonical_id=canonical,
                    merged=by_id[fid],
                    principal=principal,
                    tier=MatchTier.NEAR_DUP.value,
                )

    for fa, fb in conflict_pairs:
        if dry_run:
            existing = composition.artifact_registry.find_by_name(
                CONFLICT_KIND, _pair_key([fa, fb])
            )
            if not existing:
                report.conflicts_queued += 1
            continue
        queued = queue_conflict(
            composition,
            principal=principal,
            fragment_ids=[fa, fb],
            reason="recluster_ambiguous",
        )
        if queued is not None:
            report.conflicts_queued += 1

    if not dry_run:
        composition.audit_log.record(
            entry_type="dedup_recluster",
            principal_id=principal,
            agent_id=_AGENT,
            fragment_id="",
            outcome="ok",
            fragments=report.fragments,
            examined_pairs=report.examined_pairs,
            merged=report.merged,
            conflicts_queued=report.conflicts_queued,
        )
    return report


__all__ = [
    "ALIAS_KIND",
    "CONFLICT_KIND",
    "MERGE_KIND",
    "MERGE_TOMBSTONE_PREFIX",
    "DedupEngine",
    "MatchTier",
    "ReclusterReport",
    "alias_name",
    "aliases_for_source",
    "blocking_tokens",
    "fragment_dedup_text",
    "list_conflicts",
    "merge_fragments",
    "normalize_text",
    "queue_conflict",
    "recluster",
    "record_alias",
    "unmerge",
]
