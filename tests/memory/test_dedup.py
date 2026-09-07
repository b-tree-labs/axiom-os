# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for ADR-087 D3 — dedup as tiered entity resolution.

Pipeline: blocking (lexical + structured keys) → matching (content
hash / idempotency key → vector similarity → adjudication band) →
clustering → canonicalization. Three tiers:

- **exact** — auto-collapse;
- **near-duplicate** — reversible merge: tombstone
  ``deduped:merged-into:<id>``, alias set of every folded source
  coordinate preserved;
- **conflict/ambiguous** — kept-both + queued, never auto-merged.

Two clocks: the write-time near-neighbor check (via the importer's
``dedup`` seam) and the invocable re-cluster pass (no scheduler wiring
in P2). Never silent loss, in any tier, on any path.
"""

from __future__ import annotations

from pathlib import Path

import pytest

PRINCIPAL = "@alice:home"


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


@pytest.fixture
def composition(tmp_path: Path):
    return _make_composition(tmp_path / "node")


def _cand(text: str, *, harness="h", account="a", source_ref=None):
    from axiom.memory.absorb.base import FragmentCandidate
    from axiom.memory.fragment import SourceOrigin

    return FragmentCandidate(
        content={"summary": text, "text": text},
        cognitive_type="semantic",
        origin=SourceOrigin(
            harness=harness, account=account,
            source_ref=source_ref or f"ref-{hash(text) & 0xFFFF}",
            imported_at="2026-07-13T00:00:00+00:00",
        ),
    )


def _live(composition):
    return composition.artifact_registry.list(kind="fragment")


# ---------------------------------------------------------------------------
# Matching tiers
# ---------------------------------------------------------------------------


class TestClassifyPair:
    def test_exact_normalizes_case_and_whitespace(self):
        from axiom.memory.dedup import DedupEngine, MatchTier

        engine = DedupEngine(embedder=None)
        assert engine.classify_pair(
            "Prefers   uv for Python installs",
            "prefers uv for python installs",
        ) is MatchTier.EXACT

    def test_near_duplicate_lexical(self):
        from axiom.memory.dedup import DedupEngine, MatchTier

        engine = DedupEngine(embedder=None)
        assert engine.classify_pair(
            "prefers dark roast coffee in the morning",
            "prefers dark-roast coffee in the morning",
        ) is MatchTier.NEAR_DUP

    def test_contradiction_lands_in_conflict_band(self):
        from axiom.memory.dedup import DedupEngine, MatchTier

        engine = DedupEngine(embedder=None)
        assert engine.classify_pair(
            "the daily standup starts at 09:30 in summer",
            "the daily standup starts at 14:00 in summer",
        ) is MatchTier.CONFLICT

    def test_distinct(self):
        from axiom.memory.dedup import DedupEngine, MatchTier

        engine = DedupEngine(embedder=None)
        assert engine.classify_pair(
            "likes green tea", "deploys releases from tags",
        ) is MatchTier.DISTINCT

    def test_vector_similarity_when_embedder_present(self):
        from axiom.memory.dedup import DedupEngine, MatchTier

        vectors = {
            "likes oolong tea": [1.0, 0.0],
            "enjoys oolong tea": [0.99, 0.141],   # cos ≈ 0.99 → near-dup
            "likes jasmine tea": [0.80, 0.60],    # cos = 0.80 → conflict band
            "collects vinyl records": [0.0, 1.0], # cos = 0 → distinct
        }

        def embedder(texts):
            return [vectors[t] for t in texts]

        engine = DedupEngine(embedder=embedder)
        assert engine.classify_pair(
            "likes oolong tea", "enjoys oolong tea",
        ) is MatchTier.NEAR_DUP
        assert engine.classify_pair(
            "likes oolong tea", "likes jasmine tea",
        ) is MatchTier.CONFLICT
        assert engine.classify_pair(
            "likes oolong tea", "collects vinyl records",
        ) is MatchTier.DISTINCT

    def test_embedder_failure_degrades_to_lexical(self):
        from axiom.memory.dedup import DedupEngine, MatchTier

        def broken(texts):
            raise RuntimeError("embedding service down")

        engine = DedupEngine(embedder=broken)
        assert engine.classify_pair(
            "prefers dark roast coffee in the morning",
            "prefers dark-roast coffee in the morning",
        ) is MatchTier.NEAR_DUP


# ---------------------------------------------------------------------------
# Write-time near-neighbor check (clock 1)
# ---------------------------------------------------------------------------


class TestWriteTimeCheck:
    def test_exact_cross_source_auto_collapses_with_alias(self, composition):
        from axiom.memory.absorb.extract import extract_by_source
        from axiom.memory.absorb.importer import import_candidates
        from axiom.memory.dedup import DedupEngine

        engine = DedupEngine(embedder=None)
        first = import_candidates(
            composition,
            [_cand("prefers uv for python installs",
                   harness="h-a", account="a", source_ref="a1")],
            principal=PRINCIPAL, dedup=engine,
        )
        assert first.imported == 1 and first.collapsed_exact == 0
        canonical_id = first.fragment_ids[0]

        second = import_candidates(
            composition,
            [_cand("Prefers   uv for Python installs",
                   harness="h-b", account="b", source_ref="b1")],
            principal=PRINCIPAL, dedup=engine,
        )
        assert second.collapsed_exact == 1
        # One live fragment; the folded witness is tombstoned, not lost.
        assert len(_live(composition)) == 1
        assert _live(composition)[0].data["id"] == canonical_id
        # Alias set survives the collapse: extraction by the folded
        # source resolves to the canonical fragment (gate).
        got = extract_by_source(composition, harness="h-b", account="b")
        assert [f.id for f in got] == [canonical_id]

    def test_near_dup_merges_reversibly(self, composition):
        from axiom.memory.absorb.importer import import_candidates
        from axiom.memory.dedup import DedupEngine

        engine = DedupEngine(embedder=None)
        first = import_candidates(
            composition,
            [_cand("prefers dark roast coffee in the morning",
                   harness="h-a", account="a", source_ref="a1")],
            principal=PRINCIPAL, dedup=engine,
        )
        canonical_id = first.fragment_ids[0]
        second = import_candidates(
            composition,
            [_cand("prefers dark-roast coffee in the morning",
                   harness="h-b", account="b", source_ref="b1")],
            principal=PRINCIPAL, dedup=engine,
        )
        assert second.merged_near_dup == 1
        merged_id = second.fragment_ids[0]

        live_ids = {a.data["id"] for a in _live(composition)}
        assert live_ids == {canonical_id}
        # Tombstone carries the D3 supersede reason.
        dead = composition.artifact_registry.find_by_name(
            "fragment", merged_id, include_deleted=True,
        )
        assert any(
            (a.deletion_reason or "").startswith("deduped:merged-into:")
            and canonical_id in (a.deletion_reason or "")
            for a in dead
        )

    def test_unmerge_restores_both_witnesses(self, composition):
        from axiom.memory.absorb.importer import import_candidates
        from axiom.memory.dedup import DedupEngine, unmerge

        engine = DedupEngine(embedder=None)
        first = import_candidates(
            composition,
            [_cand("prefers dark roast coffee in the morning",
                   harness="h-a", account="a", source_ref="a1")],
            principal=PRINCIPAL, dedup=engine,
        )
        second = import_candidates(
            composition,
            [_cand("prefers dark-roast coffee in the morning",
                   harness="h-b", account="b", source_ref="b1")],
            principal=PRINCIPAL, dedup=engine,
        )
        merged_id = second.fragment_ids[0]
        assert unmerge(composition, merged_id) is True

        live_ids = {a.data["id"] for a in _live(composition)}
        assert live_ids == {first.fragment_ids[0], merged_id}
        # The restored witness keeps its origin coordinate…
        from axiom.memory.absorb.extract import extract_by_source

        got = extract_by_source(composition, harness="h-b", account="b")
        assert [f.id for f in got] == [merged_id]
        # …and unmerging twice is a no-op.
        assert unmerge(composition, merged_id) is False

    def test_conflict_kept_both_never_merged(self, composition):
        from axiom.memory.absorb.importer import import_candidates
        from axiom.memory.dedup import DedupEngine, list_conflicts

        engine = DedupEngine(embedder=None)
        import_candidates(
            composition,
            [_cand("the daily standup starts at 09:30 in summer",
                   harness="h-a", account="a", source_ref="a1")],
            principal=PRINCIPAL, dedup=engine,
        )
        report = import_candidates(
            composition,
            [_cand("the daily standup starts at 14:00 in summer",
                   harness="h-b", account="b", source_ref="b1")],
            principal=PRINCIPAL, dedup=engine,
        )
        assert report.conflicts_queued == 1
        assert report.merged_near_dup == 0
        # Kept both: two live fragments, one open queue entry.
        assert len(_live(composition)) == 2
        conflicts = list_conflicts(composition, principal=PRINCIPAL)
        assert len(conflicts) == 1
        assert conflicts[0]["status"] == "open"


# ---------------------------------------------------------------------------
# Re-cluster pass (clock 2 — invocable, no scheduler)
# ---------------------------------------------------------------------------


class TestRecluster:
    def _seed(self, composition, texts, cognitive_type="semantic"):
        ids = []
        for text in texts:
            frag = composition.write(
                content={"summary": text, "text": text},
                cognitive_type=cognitive_type,
                principal_id=PRINCIPAL,
                agents={"axi"},
                resources=set(),
            )
            ids.append(frag.id)
        return ids

    def test_recluster_collapses_clusters_to_canonical(self, composition):
        from axiom.memory.dedup import DedupEngine, recluster

        ids = self._seed(composition, [
            "prefers dark roast coffee in the morning",
            "prefers dark-roast coffee in the morning",
            "prefers  dark roast coffee in the morning",
            "deploys releases from tags",
        ])
        report = recluster(
            composition, principal=PRINCIPAL, engine=DedupEngine(embedder=None),
        )
        assert report.merged == 2
        live_ids = {a.data["id"] for a in _live(composition)}
        # Canonical = earliest write; the distinct fragment untouched.
        assert live_ids == {ids[0], ids[3]}

    def test_recluster_queues_conflicts_never_merges_them(self, composition):
        from axiom.memory.dedup import DedupEngine, list_conflicts, recluster

        self._seed(composition, [
            "the daily standup starts at 09:30 in summer",
            "the daily standup starts at 14:00 in summer",
        ])
        report = recluster(
            composition, principal=PRINCIPAL, engine=DedupEngine(embedder=None),
        )
        assert report.merged == 0
        assert report.conflicts_queued == 1
        assert len(_live(composition)) == 2
        assert len(list_conflicts(composition, principal=PRINCIPAL)) == 1

    def test_recluster_is_idempotent(self, composition):
        from axiom.memory.dedup import DedupEngine, recluster

        self._seed(composition, [
            "prefers dark roast coffee in the morning",
            "prefers dark-roast coffee in the morning",
            "the daily standup starts at 09:30 in summer",
            "the daily standup starts at 14:00 in summer",
        ])
        engine = DedupEngine(embedder=None)
        first = recluster(composition, principal=PRINCIPAL, engine=engine)
        second = recluster(composition, principal=PRINCIPAL, engine=engine)
        assert first.merged == 1 and first.conflicts_queued == 1
        assert second.merged == 0 and second.conflicts_queued == 0

    def test_recluster_dry_run_changes_nothing(self, composition):
        from axiom.memory.dedup import DedupEngine, list_conflicts, recluster

        self._seed(composition, [
            "prefers dark roast coffee in the morning",
            "prefers dark-roast coffee in the morning",
        ])
        report = recluster(
            composition, principal=PRINCIPAL,
            engine=DedupEngine(embedder=None), dry_run=True,
        )
        assert report.merged == 1 and report.dry_run is True
        assert len(_live(composition)) == 2
        assert list_conflicts(composition, principal=PRINCIPAL) == []

    def test_recluster_never_touches_vault(self, composition):
        from axiom.memory.dedup import DedupEngine, recluster

        self._seed(composition, [
            "api token for the deploy bot",
            "api token for the deploy bot",
        ], cognitive_type="vault")
        report = recluster(
            composition, principal=PRINCIPAL, engine=DedupEngine(embedder=None),
        )
        assert report.merged == 0
        assert len(_live(composition)) == 2

    def test_recluster_respects_open_conflicts(self, composition):
        """A queued pair is awaiting review — recluster must not merge
        it even if thresholds would now call it near-dup."""
        from axiom.memory.dedup import (
            DedupEngine,
            queue_conflict,
            recluster,
        )

        ids = self._seed(composition, [
            "prefers dark roast coffee in the morning",
            "prefers dark-roast coffee in the morning",
        ])
        queue_conflict(
            composition, principal=PRINCIPAL,
            fragment_ids=ids, reason="planted",
        )
        report = recluster(
            composition, principal=PRINCIPAL, engine=DedupEngine(embedder=None),
        )
        assert report.merged == 0
        assert len(_live(composition)) == 2
