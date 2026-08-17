# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for the P4 bidirectional sync engine (ADR-087 D2/D8, PRD F6).

Covers scope items 2 (sync engine, both directions), 4 (streaming LWW
conflict → P2 review queue, never silent), and 6 (OQ6 inbound secret →
vault; secrets never sync outbound).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from axiom.memory.serving import TIER_LOCAL, TIER_REMOTE, ConsumerCoordinate
from axiom.memory.sync.detect import DetectedChange, content_hash
from axiom.memory.sync.engine import SyncEngine

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
    text,
    *,
    harness="harness-a",
    account="acct-a",
    source_ref="r1",
    cognitive_type="semantic",
    imported_at="2026-07-14T00:00:00+00:00",
    content=None,
):
    from axiom.memory.absorb.base import FragmentCandidate
    from axiom.memory.fragment import SourceOrigin

    return FragmentCandidate(
        content=content if content is not None else {"summary": text, "text": text},
        cognitive_type=cognitive_type,
        origin=SourceOrigin(
            harness=harness, account=account, source_ref=source_ref,
            imported_at=imported_at,
        ),
    )


def _change(cand, *, detected_at="2026-07-14T00:00:00+00:00") -> DetectedChange:
    return DetectedChange(
        harness=cand.origin.harness,
        account=cand.origin.account,
        source_ref=cand.origin.source_ref,
        content_hash=content_hash(cand),
        candidates=(cand,),
        detected_at=detected_at,
    )


def _engine(composition, **kw) -> SyncEngine:
    return SyncEngine(
        composition=composition,
        principal=PRINCIPAL,
        account_set=frozenset({"acct-a", "acct-b"}),
        **kw,
    )


def _consumer(harness, account, tier=TIER_LOCAL) -> ConsumerCoordinate:
    return ConsumerCoordinate(
        principal=PRINCIPAL, harness=harness, account=account,
        deployment_tier=tier,
        compatible_accounts=frozenset({"acct-a", "acct-b"}),
    )


def _live(composition):
    return composition.artifact_registry.list(kind="fragment")


# ---------------------------------------------------------------------------
# Inbound: detected change → import with origin-preserving provenance
# ---------------------------------------------------------------------------


class TestInbound:
    def test_change_imports_with_origin(self, composition):
        eng = _engine(composition)
        report = eng.apply_inbound(_change(_candidate("likes tea", source_ref="r1")))
        assert report.imported == 1
        frags = _live(composition)
        assert len(frags) == 1
        origin = frags[0].data["provenance"]["origin"]
        assert origin["harness"] == "harness-a"
        assert origin["source_ref"] == "r1"

    def test_reapplying_same_change_is_a_noop(self, composition):
        eng = _engine(composition)
        change = _change(_candidate("likes tea", source_ref="r1"))
        assert eng.apply_inbound(change).imported == 1
        second = eng.apply_inbound(change)
        assert second.imported == 0
        assert second.skipped_echo == 1
        assert len(_live(composition)) == 1


# ---------------------------------------------------------------------------
# Echo suppression: a fragment we wrote out is never re-imported
# ---------------------------------------------------------------------------


class TestEchoSuppression:
    def test_content_we_wrote_out_is_not_reimported(self, composition):
        from axiom.memory.absorb.importer import _candidate_text
        from axiom.memory.sync.echo import record_echo

        eng = _engine(composition)
        # The peer's detector reads our written-out content back as a candidate.
        cand = _candidate(
            "synced pref", content={"text": "synced pref"},
            harness="harness-b", account="acct-b", source_ref="peer.md",
        )
        # Simulate the outbound record: we wrote this exact text to the peer.
        record_echo(
            composition, principal=PRINCIPAL, target="harness-b/acct-b",
            fragment_id="f-remote", text=_candidate_text(cand),
        )
        report = eng.apply_inbound(_change(cand))
        assert report.imported == 0  # echo suppressed → not re-imported
        assert _live(composition) == []


# ---------------------------------------------------------------------------
# OQ6 inbound: secret-class content routes to vault, never a plain fragment
# ---------------------------------------------------------------------------


class TestSecretRoutedToVaultInbound:
    def test_secret_stored_as_vault_not_plain(self, composition):
        eng = _engine(composition)
        report = eng.apply_inbound(
            _change(_candidate(
                "aws key AKIAIOSFODNN7EXAMPLE",
                content={"summary": "creds", "text": "AKIAIOSFODNN7EXAMPLE"},
                source_ref="secrets.md",
            ))
        )
        assert report.secrets_vaulted == 1
        assert report.imported == 0
        frags = _live(composition)
        assert len(frags) == 1
        assert frags[0].data["cognitive_type"] == "vault"

    def test_vaulted_secret_never_syncs_outbound(self, composition):
        eng = _engine(composition)
        eng.apply_inbound(_change(_candidate("likes tea", source_ref="ok.md")))
        eng.apply_inbound(
            _change(_candidate(
                "ghp token",
                content={"summary": "tok", "text": "ghp_" + "a" * 30},
                source_ref="secrets.md",
            ))
        )
        snap, _ = eng.gated_snapshot(
            _consumer("harness-b", "acct-b"), session_id="s", epoch=0,
        )
        texts = " ".join(e.text for e in snap.entries)
        assert "likes tea" in texts
        assert "ghp_" not in texts  # secret never leaves


# ---------------------------------------------------------------------------
# Outbound: gated write-back to a peer's instruction file
# ---------------------------------------------------------------------------


class TestOutbound:
    def test_writes_managed_block_to_peer(self, composition, tmp_path):
        from axiom.memory.rendering import SESSION_BOUNDARY, InstructionFileWriteBack

        eng = _engine(composition)
        eng.apply_inbound(_change(_candidate("prefers ruff", source_ref="a.md")))
        agents = tmp_path / "peer" / "AGENTS.md"
        result = eng.propagate_to(
            _consumer("harness-b", "acct-b"),
            targets=[InstructionFileWriteBack(path=agents)],
            cadence=SESSION_BOUNDARY,
            session_id="s", epoch=0,
        )
        assert str(agents) in result.written
        assert "prefers ruff" in agents.read_text()
        assert "axiom:cross-mem:begin" in agents.read_text()

    def test_cross_account_content_denied_outbound(self, composition):
        """Memory from an account outside the sync plan never serves to a peer."""
        eng = _engine(composition)
        # A fragment from a work account that is NOT in the peer's compatible set.
        eng.apply_inbound(
            _change(_candidate("work secret plan", harness="harness-w",
                               account="acct-work", source_ref="w.md"))
        )
        consumer = ConsumerCoordinate(
            principal=PRINCIPAL, harness="harness-b", account="acct-b",
            deployment_tier=TIER_LOCAL,
            compatible_accounts=frozenset({"acct-a", "acct-b"}),
        )
        snap, denials = eng.gated_snapshot(consumer, session_id="s", epoch=0)
        assert all("work secret plan" not in e.text for e in snap.entries)
        assert any(d.reason.value == "cross_account" for d in denials)

    def test_controlled_content_denied_to_remote_tier(self, composition):
        """SCOPE_INTERNAL default content never rides to a remote endpoint."""
        eng = _engine(composition)
        eng.apply_inbound(_change(_candidate("prefers ruff", source_ref="a.md")))
        snap, denials = eng.gated_snapshot(
            _consumer("harness-b", "acct-b", tier=TIER_REMOTE),
            session_id="s", epoch=0,
        )
        assert snap.entries == ()
        assert any(d.reason.value == "tier_mismatch" for d in denials)


# ---------------------------------------------------------------------------
# Streaming LWW conflict → P2 review queue (reused), never silent
# ---------------------------------------------------------------------------


class TestStreamingLWWConflict:
    def test_planted_concurrent_edits_lww_winner_loser_queued(self, composition):
        from axiom.memory.dedup import list_conflicts
        from axiom.memory.sync.conflict import list_resolutions

        eng = _engine(composition)
        # Same logical slot edited twice (same source_ref), later event wins.
        eng.apply_inbound(_change(_candidate(
            "prefers vim", source_ref="editor.md",
            imported_at="2026-07-14T09:00:00+00:00",
        )))
        eng.apply_inbound(_change(_candidate(
            "prefers emacs now", source_ref="editor.md",
            imported_at="2026-07-14T10:00:00+00:00",
        )))

        # Never silent: both kept, one open conflict in the P2 queue.
        assert len(_live(composition)) == 2
        conflicts = list_conflicts(composition, principal=PRINCIPAL)
        assert len(conflicts) == 1
        assert conflicts[0]["status"] == "open"

        # LWW resolution names the later edit the winner.
        resolutions = list_resolutions(composition, principal=PRINCIPAL)
        assert len(resolutions) == 1
        res = resolutions[0]
        assert res["policy"] == "lww_by_event_time"
        assert res["winner_event_time"] == "2026-07-14T10:00:00+00:00"
        assert len(res["loser_ids"]) == 1

    def test_lww_loser_excluded_from_outbound_winner_propagates(self, composition):
        eng = _engine(composition)
        eng.apply_inbound(_change(_candidate(
            "prefers vim", source_ref="editor.md",
            imported_at="2026-07-14T09:00:00+00:00",
        )))
        eng.apply_inbound(_change(_candidate(
            "prefers emacs now", source_ref="editor.md",
            imported_at="2026-07-14T10:00:00+00:00",
        )))
        snap, _ = eng.gated_snapshot(
            _consumer("harness-b", "acct-b"), session_id="s", epoch=0,
        )
        texts = " ".join(e.text for e in snap.entries)
        assert "prefers emacs now" in texts   # winner propagates
        assert "prefers vim" not in texts     # loser suppressed

    def test_resolution_idempotent_across_ticks(self, composition):
        from axiom.memory.sync.conflict import (
            list_resolutions,
            resolve_streaming_conflicts,
        )

        eng = _engine(composition)
        eng.apply_inbound(_change(_candidate(
            "prefers vim", source_ref="editor.md",
            imported_at="2026-07-14T09:00:00+00:00",
        )))
        eng.apply_inbound(_change(_candidate(
            "prefers emacs now", source_ref="editor.md",
            imported_at="2026-07-14T10:00:00+00:00",
        )))
        before = list_resolutions(composition, principal=PRINCIPAL)
        # Re-resolve: must not create a second resolution record.
        resolve_streaming_conflicts(composition, principal=PRINCIPAL)
        after = list_resolutions(composition, principal=PRINCIPAL)
        assert len(before) == len(after) == 1
