# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for the P2 absorb adapter seam (ADR-087 D2/D8, PRD F3).

The seam: read-only ``AbsorbAdapter``s scan a harness-native store and
yield normalized ``FragmentCandidate``s stamped with ``SourceOrigin``.
All writes land via the D2 import primitive (``import_candidates``):

- provenance origin-preserving (the candidate's coordinate rides in);
- the ``(harness, account, source_ref)`` idempotency key suppresses
  re-absorb echo — absorb → re-absorb is a no-op;
- same key + different content is a conflict: kept-both + queued,
  never silent loss, never an overwrite;
- vault is never absorbed (ADR-088 invariant asserted inbound);
- per-source extraction returns exactly the fragments that entered
  from that source.
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


def _candidate(
    text: str,
    *,
    harness: str = "test-harness",
    account: str = "acct-1",
    source_ref: str = "ref-1",
    cognitive_type: str = "semantic",
    content: dict | None = None,
):
    from axiom.memory.absorb.base import FragmentCandidate
    from axiom.memory.fragment import SourceOrigin

    return FragmentCandidate(
        content=content if content is not None else {"summary": text, "text": text},
        cognitive_type=cognitive_type,
        origin=SourceOrigin(
            harness=harness,
            account=account,
            source_ref=source_ref,
            imported_at="2026-07-13T00:00:00+00:00",
        ),
    )


def _live_fragments(composition):
    return composition.artifact_registry.list(kind="fragment")


# ---------------------------------------------------------------------------
# CompositionService.write grows an origin kwarg (single door in, D2)
# ---------------------------------------------------------------------------


class TestWriteWithOrigin:
    def test_write_stamps_origin_on_provenance(self, composition):
        from axiom.memory.fragment import SourceOrigin, fragment_from_dict

        origin = SourceOrigin(
            harness="claude-code", account="acct", source_ref="path/x.md",
            imported_at="2026-07-13T00:00:00+00:00",
        )
        frag = composition.write(
            content={"summary": "likes tea"},
            cognitive_type="semantic",
            principal_id=PRINCIPAL,
            agents={"axi-memory"},
            resources=set(),
            origin=origin,
        )
        assert frag.provenance.origin == origin
        # And it persists: read back from the registry.
        arts = composition.artifact_registry.find_by_name("fragment", frag.id)
        stored = fragment_from_dict(arts[0].data)
        assert stored.provenance.origin == origin

    def test_write_without_origin_stays_native(self, composition):
        frag = composition.write(
            content={"summary": "native"},
            cognitive_type="semantic",
            principal_id=PRINCIPAL,
            agents=set(),
            resources=set(),
        )
        assert frag.provenance.origin is None


# ---------------------------------------------------------------------------
# The import primitive
# ---------------------------------------------------------------------------


class TestImportCandidates:
    def test_import_writes_fragments_with_origin(self, composition):
        from axiom.memory.absorb.importer import import_candidates

        cands = [
            _candidate("likes tea", source_ref="r1"),
            _candidate("prefers vim", source_ref="r2"),
        ]
        report = import_candidates(
            composition, cands, principal=PRINCIPAL,
        )
        assert report.imported == 2
        assert report.skipped_echo == 0
        frags = _live_fragments(composition)
        assert len(frags) == 2
        origins = {
            (a.data["provenance"]["origin"]["harness"],
             a.data["provenance"]["origin"]["source_ref"])
            for a in frags
        }
        assert origins == {("test-harness", "r1"), ("test-harness", "r2")}
        # Owner is the local principal.
        for a in frags:
            assert a.data["provenance"]["principal_id"] == PRINCIPAL
            assert a.data["ownership"]["master"] == PRINCIPAL

    def test_reabsorb_is_a_noop(self, composition):
        from axiom.memory.absorb.importer import import_candidates

        cands = [
            _candidate("likes tea", source_ref="r1"),
            _candidate("prefers vim", source_ref="r2"),
        ]
        first = import_candidates(composition, cands, principal=PRINCIPAL)
        assert first.imported == 2
        second = import_candidates(composition, cands, principal=PRINCIPAL)
        assert second.imported == 0
        assert second.skipped_echo == 2
        assert len(_live_fragments(composition)) == 2

    def test_same_key_different_content_kept_both_and_queued(self, composition):
        from axiom.memory.absorb.importer import import_candidates
        from axiom.memory.dedup import list_conflicts

        import_candidates(
            composition, [_candidate("likes tea", source_ref="r1")],
            principal=PRINCIPAL,
        )
        report = import_candidates(
            composition, [_candidate("hates tea now", source_ref="r1")],
            principal=PRINCIPAL,
        )
        # Kept both: the changed source memory still enters the ledger …
        assert report.imported == 1
        assert report.conflicts_queued == 1
        frags = _live_fragments(composition)
        assert len(frags) == 2
        # … and the queue holds one open conflict naming both fragments.
        conflicts = list_conflicts(composition, principal=PRINCIPAL)
        assert len(conflicts) == 1
        entry = conflicts[0]
        assert entry["status"] == "open"
        assert len(entry["fragment_ids"]) == 2
        stored_ids = {a.data["id"] for a in frags}
        assert set(entry["fragment_ids"]) <= stored_ids

    def test_conflict_requeue_not_duplicated(self, composition):
        """Re-absorbing the same changed source doesn't spam the queue."""
        from axiom.memory.absorb.importer import import_candidates
        from axiom.memory.dedup import list_conflicts

        import_candidates(
            composition, [_candidate("likes tea", source_ref="r1")],
            principal=PRINCIPAL,
        )
        changed = _candidate("hates tea now", source_ref="r1")
        import_candidates(composition, [changed], principal=PRINCIPAL)
        again = import_candidates(composition, [changed], principal=PRINCIPAL)
        # Echo: the changed content now exists under the key → no-op.
        assert again.imported == 0
        assert again.skipped_echo == 1
        assert len(list_conflicts(composition, principal=PRINCIPAL)) == 1

    def test_vault_candidate_never_absorbed(self, composition):
        from axiom.memory.absorb.importer import import_candidates

        report = import_candidates(
            composition,
            [_candidate("api key hunter2", cognitive_type="vault")],
            principal=PRINCIPAL,
        )
        assert report.imported == 0
        assert any("vault" in s.reason for s in report.skipped)
        assert _live_fragments(composition) == []

    def test_invalid_candidate_skips_with_audit_never_crashes(self, composition):
        """Shape-invalid candidates degrade to skip-with-audit (D8)."""
        from axiom.memory.absorb.importer import import_candidates

        bad = _candidate(
            "meeting", cognitive_type="episodic",
            content={"note": "no event_time"},  # episodic requires event_time
        )
        good = _candidate("likes tea", source_ref="r-good")
        report = import_candidates(
            composition, [bad, good], principal=PRINCIPAL,
        )
        assert report.imported == 1
        assert len(report.skipped) == 1
        # The skip left an audit trail.
        entries = list(composition.audit_log.query(principal_id=PRINCIPAL))
        assert any(e.get("entry_type") == "absorb_skipped" for e in entries)

    def test_dry_run_writes_nothing(self, composition):
        from axiom.memory.absorb.importer import import_candidates

        report = import_candidates(
            composition, [_candidate("likes tea")],
            principal=PRINCIPAL, dry_run=True,
        )
        assert report.imported == 1  # would import
        assert report.dry_run is True
        assert _live_fragments(composition) == []


# ---------------------------------------------------------------------------
# ADR-088 invariant re-asserted across the absorb path
# ---------------------------------------------------------------------------


class _RecordingStore:
    """Minimal rag-store fake: records what the recall index projects."""

    def __init__(self):
        self.upserted: list[str] = []

    def upsert_chunks(self, chunks, **kwargs):
        self.upserted.extend(c.text for c in chunks)

    def delete_document(self, path, **kwargs):
        pass

    def delete_corpus(self, corpus):
        pass

    def search(self, **kwargs):
        return []


class TestVaultNeverAbsorbedNeverProjected:
    def test_vault_never_reaches_ledger_or_recall_index(self, composition):
        from axiom.memory.absorb.importer import import_candidates
        from axiom.memory.recall import RecallIndex

        store = _RecordingStore()
        composition.recall_index = RecallIndex(store=store, embedder=None)

        report = import_candidates(
            composition,
            [
                _candidate("likes tea", source_ref="ok-1"),
                _candidate("api key hunter2", cognitive_type="vault",
                           source_ref="secret-1"),
            ],
            principal=PRINCIPAL,
        )
        # The semantic memory landed and projected; vault absorbed
        # nothing, projected nothing (ADR-088 floor holds inbound).
        assert report.imported == 1
        assert any("likes tea" in t for t in store.upserted)
        assert not any("hunter2" in t for t in store.upserted)
        assert len(_live_fragments(composition)) == 1

    def test_direct_vault_projection_still_refuses(self, composition):
        import pytest as _pytest

        from axiom.memory.fragment import create_fragment
        from axiom.memory.recall_projection import fragment_to_recall_chunk

        vault_frag = create_fragment(
            content={"secret": "hunter2"}, cognitive_type="vault",
            principal_id=PRINCIPAL, agents=set(), resources=set(),
        )
        with _pytest.raises(ValueError):
            fragment_to_recall_chunk(vault_frag)


# ---------------------------------------------------------------------------
# Per-source extraction
# ---------------------------------------------------------------------------


class TestExtractBySource:
    def test_returns_exactly_the_source_fragments(self, composition):
        from axiom.memory.absorb.extract import extract_by_source
        from axiom.memory.absorb.importer import import_candidates

        a = [
            _candidate("likes tea", harness="h-a", account="acct-a", source_ref="a1"),
            _candidate("prefers vim", harness="h-a", account="acct-a", source_ref="a2"),
        ]
        b = [
            _candidate("deploys on tags", harness="h-b", account="acct-b", source_ref="b1"),
        ]
        import_candidates(composition, a, principal=PRINCIPAL)
        import_candidates(composition, b, principal=PRINCIPAL)

        got_a = extract_by_source(composition, harness="h-a", account="acct-a")
        got_b = extract_by_source(composition, harness="h-b", account="acct-b")
        assert len(got_a) == 2
        assert len(got_b) == 1
        assert {f.provenance.origin.source_ref for f in got_a} == {"a1", "a2"}
        assert {f.provenance.origin.source_ref for f in got_b} == {"b1"}

    def test_native_fragments_never_extracted(self, composition):
        from axiom.memory.absorb.extract import extract_by_source

        composition.write(
            content={"summary": "native"}, cognitive_type="semantic",
            principal_id=PRINCIPAL, agents=set(), resources=set(),
        )
        assert extract_by_source(composition, harness="h-a") == []


# ---------------------------------------------------------------------------
# Adapter protocol shape (structural typing)
# ---------------------------------------------------------------------------


class TestAdapterProtocol:
    def test_fake_adapter_satisfies_protocol_and_feeds_importer(
        self, composition
    ):
        from axiom.memory.absorb.base import AbsorbAdapter, AbsorbScan
        from axiom.memory.absorb.importer import import_candidates

        class FakeAdapter:
            harness = "fake"

            def scan(self) -> AbsorbScan:
                return AbsorbScan(
                    candidates=[_candidate("likes tea", harness="fake")],
                    skipped=[],
                )

        adapter = FakeAdapter()
        assert isinstance(adapter, AbsorbAdapter)
        scan = adapter.scan()
        report = import_candidates(
            composition, scan.candidates, principal=PRINCIPAL,
        )
        assert report.imported == 1
