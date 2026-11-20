# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for mat-4 compaction (summarize cadence; stage 4 of the maturation lifecycle).

Per `spec-memory-compaction.md` §2, summarize-compaction is the first
compressive operation in the lifecycle. Lossy by design: full-detail
episodes get replaced with shorter summary fragments. The original is
marked superseded (substrate append-only — supersession is captured by
a separate fragment, not in-place mutation).

mat-4 MVP scope:

- Summarizer protocol + DefaultSummarizer (deterministic length-reduction)
- CompactionSummarizeStageHandler with audit-chain enforcement
- Only episodes whose semantic derivatives already exist get compacted
  (the audit-chain rule: never compact before consolidation has captured
  the insight)

Archive (stage 5) and forget (stage 6) follow in subsequent commits.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def isolated_composition(tmp_path: Path):
    from axiom.artifacts.registry import ArtifactRegistry, SQLiteBackend
    from axiom.memory.access import AccessGraphs
    from axiom.memory.attest import AuditLog
    from axiom.memory.composition import CompositionService
    from axiom.memory.policy import PolicyCoord
    from axiom.memory.trust import TrustGraph
    from axiom.vega.identity.keypair import generate_keypair

    base = tmp_path / "memory"
    base.mkdir()
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


def _write_episode(
    composition,
    *,
    user_input: str,
    assistant_output: str = "",
    scope: str = "test-scope",
    event_time: str = "2026-05-01T10:00:00Z",
    principal_id: str = "ben@example.com",
) -> str:
    fragment = composition.write(
        content={
            "event_time": event_time,
            "scope": scope,
            "fact_kind": "chat_turn",
            "tool": "test",
            "model": "stub",
            "user_input": user_input,
            "assistant_output": assistant_output,
            "summary": user_input[:80],
        },
        cognitive_type="episodic",
        principal_id=principal_id,
        agents={"test"},
        resources=set(),
    )
    return fragment.id


def _write_semantic_citing(
    composition,
    *,
    summary: str,
    derived_from: list[str],
    scope: str = "test-scope",
    event_time: str = "2026-05-02T10:00:00Z",
    principal_id: str = "ben@example.com",
) -> str:
    """Write a semantic insight that cites the given episode ids."""
    fragment = composition.write(
        content={
            "event_time": event_time,
            "scope": scope,
            "fact_kind": "semantic_insight",
            "summary": summary,
            "derived_from": list(derived_from),
            "cadence": "daily",
            "extractor": "TestFixture",
            "extractor_kind": "deterministic",
            "confidence": 0.8,
        },
        cognitive_type="semantic",
        principal_id=principal_id,
        agents={"test"},
        resources={f"axiom://memory/{uid}" for uid in derived_from},
    )
    return fragment.id


# ---------------------------------------------------------------------------
# Module surface
# ---------------------------------------------------------------------------


def test_compaction_module_imports():
    from axiom.memory.maturation.compaction import (
        CompactedContent,
        CompactionSummarizeStageHandler,
        CompactionSummarizer,
        DefaultSummarizer,
    )

    assert CompactionSummarizer is not None
    assert DefaultSummarizer is not None
    assert CompactedContent is not None
    assert CompactionSummarizeStageHandler is not None


def test_compaction_handler_has_canonical_stage():
    from axiom.memory.maturation import Stage
    from axiom.memory.maturation.compaction import CompactionSummarizeStageHandler

    h = CompactionSummarizeStageHandler(
        composition=None, summarizer=None, principal_id="x"
    )
    assert h.stage == Stage.COMPACTION_SUMMARIZE


# ---------------------------------------------------------------------------
# DefaultSummarizer — deterministic length-reduction
# ---------------------------------------------------------------------------


def test_default_summarizer_reduces_length():
    """The default summarizer's output must be shorter than the source."""
    from axiom.memory.fragment import create_fragment
    from axiom.memory.maturation.compaction import DefaultSummarizer

    long_assistant = "Detailed multi-paragraph reasoning. " * 30  # ~1100 chars
    source = create_fragment(
        cognitive_type="episodic",
        content={
            "event_time": "2026-05-01T10:00:00Z",
            "fact_kind": "chat_turn",
            "user_input": "What is the Q3 plan?",
            "assistant_output": long_assistant,
            "summary": "Q3 plan question",
        },
        principal_id="ben@example.com",
        agents={"test"},
        resources=set(),
    )

    compacted = DefaultSummarizer().summarize(source)
    assert compacted.original_length_chars > 0
    assert len(compacted.summary_text) < compacted.original_length_chars
    # Plausibly ≥ 50% reduction (per spec: "must reduce length by ≥ 50%")
    assert len(compacted.summary_text) <= compacted.original_length_chars * 0.5


def test_default_summarizer_is_deterministic():
    from axiom.memory.fragment import create_fragment
    from axiom.memory.maturation.compaction import DefaultSummarizer

    source = create_fragment(
        cognitive_type="episodic",
        content={
            "event_time": "2026-05-01T10:00:00Z",
            "fact_kind": "chat_turn",
            "user_input": "What is Q3?",
            "assistant_output": "Q3 is the third quarter, July-September.",
            "summary": "Q3 question",
        },
        principal_id="ben@example.com",
        agents={"test"},
        resources=set(),
    )
    c1 = DefaultSummarizer().summarize(source)
    c2 = DefaultSummarizer().summarize(source)
    assert c1.summary_text == c2.summary_text
    assert c1.original_length_chars == c2.original_length_chars


# ---------------------------------------------------------------------------
# Audit-chain enforcement
# ---------------------------------------------------------------------------


def test_handler_skips_episodes_with_no_semantic_derivative(isolated_composition):
    """Audit-chain rule: an episode without a semantic citing it is NOT compacted."""
    from axiom.memory.maturation import CycleBudget
    from axiom.memory.maturation.compaction import (
        CompactionSummarizeStageHandler,
        DefaultSummarizer,
    )

    # Episode with no semantic citing it
    _write_episode(
        isolated_composition, user_input="orphan episode",
        event_time="2026-05-01T10:00:00Z",
    )

    h = CompactionSummarizeStageHandler(
        composition=isolated_composition,
        summarizer=DefaultSummarizer(),
        principal_id="ben@example.com",
        summarize_age_days=0,  # any episode at any age eligible by age alone
    )
    result = h.run("test-scope", CycleBudget())
    assert result.items_processed == 0  # nothing eligible (audit chain blocks)
    summaries = [
        a for a in isolated_composition.artifact_registry.list(kind="fragment")
        if (a.data or {}).get("content", {}).get("fact_kind") == "compacted_chat_turn"
    ]
    assert summaries == []


def test_handler_compacts_episodes_with_semantic_derivative(isolated_composition):
    from axiom.memory.maturation import CycleBudget
    from axiom.memory.maturation.compaction import (
        CompactionSummarizeStageHandler,
        DefaultSummarizer,
    )

    ep_id = _write_episode(
        isolated_composition,
        user_input="Q3 results came in: 12% growth.",
        assistant_output="Strong Q3. " * 50,
        event_time="2026-05-01T10:00:00Z",
    )
    _write_semantic_citing(
        isolated_composition,
        summary="Recurring topic Q3 in 1 episode",
        derived_from=[ep_id],
    )

    h = CompactionSummarizeStageHandler(
        composition=isolated_composition,
        summarizer=DefaultSummarizer(),
        principal_id="ben@example.com",
        summarize_age_days=0,
    )
    result = h.run("test-scope", CycleBudget())
    assert result.items_processed == 1
    assert result.items_succeeded == 1

    summaries = [
        a for a in isolated_composition.artifact_registry.list(kind="fragment")
        if (a.data or {}).get("content", {}).get("fact_kind") == "compacted_chat_turn"
    ]
    assert len(summaries) == 1
    content = summaries[0].data["content"]
    assert content["compacted_from"] == ep_id
    assert "original_length_chars" in content
    assert "summary" in content


def test_handler_writes_supersession_record(isolated_composition):
    """Each compaction emits a supersession side fragment so clients can follow."""
    from axiom.memory.maturation import CycleBudget
    from axiom.memory.maturation.compaction import (
        CompactionSummarizeStageHandler,
        DefaultSummarizer,
    )

    ep_id = _write_episode(
        isolated_composition,
        user_input="Q3 question",
        assistant_output="A" * 500,
        event_time="2026-05-01T10:00:00Z",
    )
    _write_semantic_citing(
        isolated_composition,
        summary="Q3 topic",
        derived_from=[ep_id],
    )

    h = CompactionSummarizeStageHandler(
        composition=isolated_composition,
        summarizer=DefaultSummarizer(),
        principal_id="ben@example.com",
        summarize_age_days=0,
    )
    h.run("test-scope", CycleBudget())

    supersessions = [
        a for a in isolated_composition.artifact_registry.list(kind="fragment")
        if (a.data or {}).get("content", {}).get("fact_kind") == "supersession"
    ]
    assert len(supersessions) == 1
    content = supersessions[0].data["content"]
    assert content["original_fragment_id"] == ep_id
    assert "summary_fragment_id" in content
    assert content["reason"] == "compacted"


# ---------------------------------------------------------------------------
# Trigger semantics
# ---------------------------------------------------------------------------


def test_handler_is_pending_when_eligible_exists(isolated_composition):
    from axiom.memory.maturation.compaction import (
        CompactionSummarizeStageHandler,
        DefaultSummarizer,
    )

    ep_id = _write_episode(
        isolated_composition, user_input="event",
        event_time="2026-05-01T10:00:00Z",
    )
    _write_semantic_citing(
        isolated_composition, summary="insight", derived_from=[ep_id]
    )

    h = CompactionSummarizeStageHandler(
        composition=isolated_composition,
        summarizer=DefaultSummarizer(),
        principal_id="ben@example.com",
        summarize_age_days=0,
    )
    assert h.is_pending("test-scope") is True


def test_handler_is_not_pending_when_nothing_eligible(isolated_composition):
    from axiom.memory.maturation.compaction import (
        CompactionSummarizeStageHandler,
        DefaultSummarizer,
    )

    h = CompactionSummarizeStageHandler(
        composition=isolated_composition,
        summarizer=DefaultSummarizer(),
        principal_id="ben@example.com",
    )
    assert h.is_pending("empty-scope") is False


def test_handler_is_idempotent(isolated_composition):
    """Re-running on the same scope doesn't re-compact already-compacted episodes."""
    from axiom.memory.maturation import CycleBudget
    from axiom.memory.maturation.compaction import (
        CompactionSummarizeStageHandler,
        DefaultSummarizer,
    )

    ep_id = _write_episode(
        isolated_composition,
        user_input="event",
        assistant_output="X" * 500,
        event_time="2026-05-01T10:00:00Z",
    )
    _write_semantic_citing(
        isolated_composition, summary="insight", derived_from=[ep_id]
    )

    h = CompactionSummarizeStageHandler(
        composition=isolated_composition,
        summarizer=DefaultSummarizer(),
        principal_id="ben@example.com",
        summarize_age_days=0,
    )
    h.run("test-scope", CycleBudget())
    # second run: nothing more to compact
    assert h.is_pending("test-scope") is False
    result_2 = h.run("test-scope", CycleBudget())
    assert result_2.items_processed == 0

    summaries = [
        a for a in isolated_composition.artifact_registry.list(kind="fragment")
        if (a.data or {}).get("content", {}).get("fact_kind") == "compacted_chat_turn"
    ]
    assert len(summaries) == 1


# ---------------------------------------------------------------------------
# End-to-end through the orchestrator
# ---------------------------------------------------------------------------


def test_compaction_drives_through_orchestrator(isolated_composition):
    from axiom.memory.maturation import DreamCycleOrchestrator, Stage
    from axiom.memory.maturation.compaction import (
        CompactionSummarizeStageHandler,
        DefaultSummarizer,
    )

    ep_id = _write_episode(
        isolated_composition,
        user_input="event",
        assistant_output="Y" * 500,
        event_time="2026-05-01T10:00:00Z",
    )
    _write_semantic_citing(
        isolated_composition, summary="insight", derived_from=[ep_id]
    )

    orch = DreamCycleOrchestrator(composition=isolated_composition)
    h = CompactionSummarizeStageHandler(
        composition=isolated_composition,
        summarizer=DefaultSummarizer(),
        principal_id="ben@example.com",
        summarize_age_days=0,
    )
    orch.register(h)
    report = orch.run_cycle(scope="test-scope")

    assert len(report.stages_run) == 1
    assert report.stages_run[0].stage == Stage.COMPACTION_SUMMARIZE
    assert report.stages_run[0].items_succeeded == 1
