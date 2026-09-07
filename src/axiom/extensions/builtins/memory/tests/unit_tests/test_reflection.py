# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for mat-3 reflection (daily cadence).

Per `spec-memory-reflection.md`, reflection consolidates a batch of
episodes into derived semantic fragments. mat-3 ships the daily cadence
with a deterministic extractor; the LLM-driven extractor lands when the
gateway integration is wired (out of scope for this MVP).

Acceptance per spec §12 Phase-1 (deterministic-relevant subset):
- Citation rejection: empty ``derived_from`` is dropped at the policy gate
- Classification composition: max(source classifications) inherited
- Deterministic extractor produces byte-identical output across runs
- Daily-cadence trigger fires at the importance-threshold case
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


def _write_episode_with_importance(
    composition,
    *,
    user_input: str,
    importance: float,
    scope: str = "test-scope",
    event_time: str = "2026-05-13T10:00:00Z",
    principal_id: str = "ben@example.com",
) -> str:
    """Write an episode + a matching importance_score side fragment."""

    fragment = composition.write(
        content={
            "event_time": event_time,
            "scope": scope,
            "fact_kind": "chat_turn",
            "tool": "test",
            "model": "stub",
            "user_input": user_input,
            "assistant_output": "",
            "summary": user_input[:80],
        },
        cognitive_type="episodic",
        principal_id=principal_id,
        agents={"test"},
        resources=set(),
    )
    # Manually attach an importance score via the side fragment, with the
    # provided value rather than running the heuristic.
    composition.write(
        content={
            "event_time": event_time,
            "fact_kind": "importance_score",
            "target_fragment_id": fragment.id,
            "score": float(importance),
            "scorer": "TestFixtureScorer",
            "scorer_version": "v1",
        },
        cognitive_type="episodic",
        principal_id=principal_id,
        agents={"test"},
        resources={f"axiom://memory/{fragment.id}"},
    )
    return fragment.id


# ---------------------------------------------------------------------------
# Module surface
# ---------------------------------------------------------------------------


def test_reflection_module_imports():
    from axiom.memory.maturation.reflection import (
        DeterministicReflectionExtractor,
        EpisodeBatch,
        ReflectionExtractor,
        ReflectionStageHandler,
        SemanticProposal,
    )

    assert ReflectionExtractor is not None
    assert DeterministicReflectionExtractor is not None
    assert EpisodeBatch is not None
    assert SemanticProposal is not None
    assert ReflectionStageHandler is not None


def test_reflection_handler_has_canonical_stage():
    from axiom.memory.maturation import Stage
    from axiom.memory.maturation.reflection import ReflectionStageHandler

    h = ReflectionStageHandler(composition=None, extractor=None, principal_id="x")
    assert h.stage == Stage.CONSOLIDATION_DAILY


# ---------------------------------------------------------------------------
# DeterministicReflectionExtractor — pure synthesis
# ---------------------------------------------------------------------------


def test_deterministic_extractor_produces_proposal_with_citations():
    from axiom.memory.maturation.reflection import (
        DeterministicReflectionExtractor,
        EpisodeBatch,
    )

    batch = EpisodeBatch(
        scope="test",
        cadence="daily",
        episodes=("frag-1", "frag-2", "frag-3"),
        episode_summaries=(
            "User asked about Q3 results",
            "User asked about Q3 deadline",
            "User asked about Q3 reviewer assignments",
        ),
        importance=(5.0, 4.0, 6.0),
        accumulated_importance=15.0,
        window_start="2026-05-13T00:00:00Z",
        window_end="2026-05-14T00:00:00Z",
    )

    extractor = DeterministicReflectionExtractor()
    proposals = extractor.synthesize(batch)

    assert len(proposals) >= 1
    for p in proposals:
        assert p.derived_from  # non-empty
        assert all(uid in batch.episodes for uid in p.derived_from)
        assert 0.0 <= p.confidence <= 1.0
        assert p.cognitive_type_target == "semantic"


def test_deterministic_extractor_is_byte_identical():
    """Same EpisodeBatch in → identical proposals out."""
    from axiom.memory.maturation.reflection import (
        DeterministicReflectionExtractor,
        EpisodeBatch,
    )

    batch = EpisodeBatch(
        scope="test",
        cadence="daily",
        episodes=("a", "b", "c"),
        episode_summaries=("alpha", "beta", "gamma"),
        importance=(3.0, 3.0, 3.0),
        accumulated_importance=9.0,
        window_start="2026-05-13T00:00:00Z",
        window_end="2026-05-14T00:00:00Z",
    )
    p1 = DeterministicReflectionExtractor().synthesize(batch)
    p2 = DeterministicReflectionExtractor().synthesize(batch)
    assert [(p.summary, p.derived_from, p.confidence) for p in p1] == [
        (p.summary, p.derived_from, p.confidence) for p in p2
    ]


# ---------------------------------------------------------------------------
# ReflectionStageHandler — trigger + run
# ---------------------------------------------------------------------------


def test_handler_is_not_pending_when_no_episodes(isolated_composition):
    from axiom.memory.maturation.reflection import (
        DeterministicReflectionExtractor,
        ReflectionStageHandler,
    )

    h = ReflectionStageHandler(
        composition=isolated_composition,
        extractor=DeterministicReflectionExtractor(),
        principal_id="ben@example.com",
    )
    assert h.is_pending("empty-scope") is False


def test_handler_is_pending_when_unconsumed_episodes_exist(isolated_composition):
    from axiom.memory.maturation.reflection import (
        DeterministicReflectionExtractor,
        ReflectionStageHandler,
    )

    _write_episode_with_importance(isolated_composition, user_input="event", importance=5.0)
    h = ReflectionStageHandler(
        composition=isolated_composition,
        extractor=DeterministicReflectionExtractor(),
        principal_id="ben@example.com",
    )
    assert h.is_pending("test-scope") is True


def test_handler_trigger_fires_at_importance_threshold(isolated_composition):
    """Default threshold is 150; below threshold should not fire."""
    from axiom.memory.maturation.reflection import (
        DeterministicReflectionExtractor,
        ReflectionStageHandler,
    )

    # Three episodes of importance 5 each = 15, well below 150 default
    for i in range(3):
        _write_episode_with_importance(
            isolated_composition, user_input=f"event {i}", importance=5.0
        )

    h = ReflectionStageHandler(
        composition=isolated_composition,
        extractor=DeterministicReflectionExtractor(),
        principal_id="ben@example.com",
        importance_threshold=150.0,
    )
    assert h.is_pending("test-scope") is True
    assert h.evaluate_trigger("test-scope") is False  # 15 < 150


def test_handler_trigger_fires_above_threshold(isolated_composition):
    from axiom.memory.maturation.reflection import (
        DeterministicReflectionExtractor,
        ReflectionStageHandler,
    )

    # 20 episodes × 8 each = 160, above 150 threshold
    for i in range(20):
        _write_episode_with_importance(
            isolated_composition, user_input=f"event {i}", importance=8.0
        )

    h = ReflectionStageHandler(
        composition=isolated_composition,
        extractor=DeterministicReflectionExtractor(),
        principal_id="ben@example.com",
        importance_threshold=150.0,
    )
    assert h.evaluate_trigger("test-scope") is True


def test_handler_run_writes_semantic_fragments(isolated_composition):
    from axiom.memory.maturation import CycleBudget
    from axiom.memory.maturation.reflection import (
        DeterministicReflectionExtractor,
        ReflectionStageHandler,
    )

    for i in range(20):
        _write_episode_with_importance(
            isolated_composition, user_input=f"recurring topic event {i}", importance=8.0
        )

    h = ReflectionStageHandler(
        composition=isolated_composition,
        extractor=DeterministicReflectionExtractor(),
        principal_id="ben@example.com",
        importance_threshold=150.0,
    )
    result = h.run("test-scope", CycleBudget())
    assert result.items_succeeded >= 1

    semantics = [
        a for a in isolated_composition.artifact_registry.list(kind="fragment")
        if (a.data or {}).get("cognitive_type") == "semantic"
    ]
    assert len(semantics) >= 1
    for s in semantics:
        content = s.data["content"]
        assert content.get("derived_from")  # non-empty citation
        assert content.get("extractor_kind") in ("deterministic", "llm")
        assert content.get("cadence") == "daily"


def test_handler_writes_reflection_marker(isolated_composition):
    """After a cycle, a fact_kind='reflection_marker' fragment marks the scope's last-fired time."""
    from axiom.memory.maturation import CycleBudget
    from axiom.memory.maturation.reflection import (
        DeterministicReflectionExtractor,
        ReflectionStageHandler,
    )

    for i in range(20):
        _write_episode_with_importance(
            isolated_composition, user_input=f"event {i}", importance=8.0
        )

    h = ReflectionStageHandler(
        composition=isolated_composition,
        extractor=DeterministicReflectionExtractor(),
        principal_id="ben@example.com",
        importance_threshold=150.0,
    )
    h.run("test-scope", CycleBudget())

    markers = [
        a for a in isolated_composition.artifact_registry.list(kind="fragment")
        if (a.data or {}).get("content", {}).get("fact_kind") == "reflection_marker"
    ]
    assert len(markers) == 1
    marker_content = markers[0].data["content"]
    assert marker_content["scope"] == "test-scope"
    assert marker_content["cadence"] == "daily"


# ---------------------------------------------------------------------------
# Policy gate — citation requirement
# ---------------------------------------------------------------------------


def test_policy_gate_rejects_empty_derived_from(isolated_composition):
    """A proposal with empty derived_from is rejected before write."""
    from axiom.memory.maturation import CycleBudget
    from axiom.memory.maturation.reflection import (
        ReflectionStageHandler,
        SemanticProposal,
    )

    class _NoCitationExtractor:
        def synthesize(self, batch):
            return [
                SemanticProposal(
                    summary="bogus insight without citations",
                    derived_from=(),
                    confidence=0.9,
                )
            ]

    for i in range(20):
        _write_episode_with_importance(
            isolated_composition, user_input=f"event {i}", importance=8.0
        )

    h = ReflectionStageHandler(
        composition=isolated_composition,
        extractor=_NoCitationExtractor(),
        principal_id="ben@example.com",
        importance_threshold=150.0,
    )
    result = h.run("test-scope", CycleBudget())

    semantics = [
        a for a in isolated_composition.artifact_registry.list(kind="fragment")
        if (a.data or {}).get("cognitive_type") == "semantic"
    ]
    assert semantics == []
    # The proposal was processed but rejected
    assert result.items_failed >= 1


# ---------------------------------------------------------------------------
# End-to-end through the orchestrator
# ---------------------------------------------------------------------------


def test_reflection_drives_through_orchestrator(isolated_composition):
    from axiom.memory.maturation import DreamCycleOrchestrator, Stage
    from axiom.memory.maturation.reflection import (
        DeterministicReflectionExtractor,
        ReflectionStageHandler,
    )

    for i in range(20):
        _write_episode_with_importance(
            isolated_composition, user_input=f"event {i}", importance=8.0
        )

    orch = DreamCycleOrchestrator(composition=isolated_composition)
    h = ReflectionStageHandler(
        composition=isolated_composition,
        extractor=DeterministicReflectionExtractor(),
        principal_id="ben@example.com",
        importance_threshold=150.0,
    )
    orch.register(h)

    report = orch.run_cycle(scope="test-scope")
    assert len(report.stages_run) == 1
    assert report.stages_run[0].stage == Stage.CONSOLIDATION_DAILY
    assert report.stages_run[0].items_succeeded >= 1
