# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for mat-2 importance scoring.

Per `spec-memory-reflection.md` §4.2 + `spec-memory-maturation.md` §3 stage 2,
importance scoring is opt-in per scope. The score (0–10) feeds the
importance-threshold trigger that gates reflection.

Two scorer variants per spec: ``DeterministicImportanceScorer`` (pure
heuristic, byte-identical replay) and ``LLMImportanceScorer`` (gateway-
driven; out of scope for mat-2 MVP — protocol-level stub only).

The catch-up sweep ``ImportanceScoringStageHandler`` implements the
:class:`StageHandler` protocol so the dream-cycle orchestrator (mat-1)
can drive it. The handler scores episodic fragments in scope that have
no importance score yet, writing side fragments
(``fact_kind="importance_score"``) that reference the original.
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
    principal_id: str = "ben@example.com",
    scope: str = "test-scope",
    user_input: str = "hello",
    assistant_output: str = "hi",
    event_time: str = "2026-05-13T10:00:00Z",
) -> str:
    """Helper: write an episodic chat-turn fragment and return its id."""
    fragment = composition.write(
        content={
            "event_time": event_time,
            "scope": scope,
            "fact_kind": "chat_turn",
            "tool": "test",
            "model": "stub",
            "user_input": user_input,
            "assistant_output": assistant_output,
            "summary": f"{user_input} → {assistant_output}",
        },
        cognitive_type="episodic",
        principal_id=principal_id,
        agents={"test"},
        resources=set(),
    )
    return fragment.id


# ---------------------------------------------------------------------------
# Module surface
# ---------------------------------------------------------------------------


def test_importance_module_imports():
    from axiom.memory.maturation.importance import (
        DeterministicImportanceScorer,
        ImportanceScorer,
        ImportanceScoringStageHandler,
        score_fragment,
    )

    assert ImportanceScorer is not None
    assert DeterministicImportanceScorer is not None
    assert ImportanceScoringStageHandler is not None
    assert score_fragment is not None


def test_importance_stage_handler_has_canonical_stage():
    from axiom.memory.maturation import Stage
    from axiom.memory.maturation.importance import ImportanceScoringStageHandler

    handler = ImportanceScoringStageHandler(
        composition=None, scorer=None, principal_id="x"
    )
    assert handler.stage == Stage.IMPORTANCE_SCORING


# ---------------------------------------------------------------------------
# DeterministicImportanceScorer — pure scoring
# ---------------------------------------------------------------------------


def test_deterministic_scorer_returns_float_in_range():
    from axiom.memory.fragment import create_fragment
    from axiom.memory.maturation.importance import DeterministicImportanceScorer

    fragment = create_fragment(
        cognitive_type="episodic",
        content={
            "event_time": "2026-05-13T10:00:00Z",
            "fact_kind": "chat_turn",
            "user_input": "hello",
            "assistant_output": "hi",
            "summary": "hello",
        },
        principal_id="alice@example.com",
        agents={"test"},
        resources=set(),
    )
    score = DeterministicImportanceScorer().score(fragment)
    assert isinstance(score, float)
    assert 0.0 <= score <= 10.0


def test_deterministic_scorer_is_deterministic():
    """Same fragment in → same score out, across calls."""
    from axiom.memory.fragment import create_fragment
    from axiom.memory.maturation.importance import DeterministicImportanceScorer

    fragment = create_fragment(
        cognitive_type="episodic",
        content={
            "event_time": "2026-05-13T10:00:00Z",
            "fact_kind": "chat_turn",
            "user_input": "Tell me about Q3 results",
            "assistant_output": "Q3 results: 12% growth in users, 18% in revenue.",
            "summary": "Q3 growth numbers",
        },
        principal_id="alice@example.com",
        agents={"test"},
        resources=set(),
    )
    scorer = DeterministicImportanceScorer()
    s1 = scorer.score(fragment)
    s2 = scorer.score(fragment)
    s3 = DeterministicImportanceScorer().score(fragment)
    assert s1 == s2 == s3


def test_deterministic_scorer_orders_intuitively():
    """Substantive content should score higher than purely mundane.

    Park et al.: 1 = brushing teeth ; 10 = break up. This scorer is
    heuristic, not LLM, so we only assert relative ordering.
    """
    from axiom.memory.fragment import create_fragment
    from axiom.memory.maturation.importance import DeterministicImportanceScorer

    def _frag(text: str):
        return create_fragment(
            cognitive_type="episodic",
            content={
                "event_time": "2026-05-13T10:00:00Z",
                "fact_kind": "chat_turn",
                "user_input": text,
                "assistant_output": "",
                "summary": text[:80],
            },
            principal_id="alice@example.com",
            agents={"test"},
            resources=set(),
        )

    mundane = _frag("ok")
    substantive = _frag(
        "I've decided to move the launch to Q3 because the Q2 numbers came "
        "in lower than projected. The board approved this morning. Need to "
        "loop in marketing and engineering by EOD."
    )
    question = _frag("What is the deadline for the Q3 report?")

    scorer = DeterministicImportanceScorer()
    s_mundane = scorer.score(mundane)
    s_substantive = scorer.score(substantive)
    s_question = scorer.score(question)

    assert s_substantive > s_mundane
    assert s_question > s_mundane


# ---------------------------------------------------------------------------
# score_fragment — top-level helper
# ---------------------------------------------------------------------------


def test_score_fragment_writes_side_fragment(isolated_composition):
    """``score_fragment`` writes a fact_kind='importance_score' fragment
    referencing the target via ``content.target_fragment_id``."""
    from axiom.memory.maturation.importance import (
        DeterministicImportanceScorer,
        score_fragment,
    )

    target_id = _write_episode(isolated_composition)
    score_record_id = score_fragment(
        composition=isolated_composition,
        target_fragment_id=target_id,
        scorer=DeterministicImportanceScorer(),
        principal_id="ben@example.com",
    )
    assert score_record_id is not None

    scores = [
        a for a in isolated_composition.artifact_registry.list(kind="fragment")
        if (a.data or {}).get("content", {}).get("fact_kind") == "importance_score"
    ]
    assert len(scores) == 1
    content = scores[0].data["content"]
    assert content["target_fragment_id"] == target_id
    assert isinstance(content["score"], float)
    assert 0.0 <= content["score"] <= 10.0
    assert content["scorer"] == "DeterministicImportanceScorer"


# ---------------------------------------------------------------------------
# ImportanceScoringStageHandler — catch-up sweep
# ---------------------------------------------------------------------------


def test_handler_is_pending_when_unscored_episodes_exist(isolated_composition):
    from axiom.memory.maturation.importance import (
        DeterministicImportanceScorer,
        ImportanceScoringStageHandler,
    )

    _write_episode(isolated_composition, scope="s1")

    handler = ImportanceScoringStageHandler(
        composition=isolated_composition,
        scorer=DeterministicImportanceScorer(),
        principal_id="ben@example.com",
    )
    assert handler.is_pending("s1") is True


def test_handler_is_not_pending_when_no_unscored_episodes(isolated_composition):
    """Empty scope has no pending work."""
    from axiom.memory.maturation.importance import (
        DeterministicImportanceScorer,
        ImportanceScoringStageHandler,
    )

    handler = ImportanceScoringStageHandler(
        composition=isolated_composition,
        scorer=DeterministicImportanceScorer(),
        principal_id="ben@example.com",
    )
    assert handler.is_pending("empty-scope") is False


def test_handler_run_scores_unscored_episodes(isolated_composition):
    from axiom.memory.maturation import CycleBudget
    from axiom.memory.maturation.importance import (
        DeterministicImportanceScorer,
        ImportanceScoringStageHandler,
    )

    _write_episode(isolated_composition, scope="s1", user_input="event 1")
    _write_episode(isolated_composition, scope="s1", user_input="event 2")
    _write_episode(isolated_composition, scope="s1", user_input="event 3")

    handler = ImportanceScoringStageHandler(
        composition=isolated_composition,
        scorer=DeterministicImportanceScorer(),
        principal_id="ben@example.com",
    )
    result = handler.run("s1", CycleBudget())
    assert result.items_processed == 3
    assert result.items_succeeded == 3
    assert result.items_failed == 0

    # Three importance_score fragments now in the ledger
    scores = [
        a for a in isolated_composition.artifact_registry.list(kind="fragment")
        if (a.data or {}).get("content", {}).get("fact_kind") == "importance_score"
    ]
    assert len(scores) == 3


def test_handler_is_idempotent(isolated_composition):
    """Re-running the handler on the same scope doesn't double-score."""
    from axiom.memory.maturation import CycleBudget
    from axiom.memory.maturation.importance import (
        DeterministicImportanceScorer,
        ImportanceScoringStageHandler,
    )

    _write_episode(isolated_composition, scope="s1", user_input="event 1")

    handler = ImportanceScoringStageHandler(
        composition=isolated_composition,
        scorer=DeterministicImportanceScorer(),
        principal_id="ben@example.com",
    )
    handler.run("s1", CycleBudget())
    # Second run: nothing more to score
    assert handler.is_pending("s1") is False
    result_2 = handler.run("s1", CycleBudget())
    assert result_2.items_processed == 0

    scores = [
        a for a in isolated_composition.artifact_registry.list(kind="fragment")
        if (a.data or {}).get("content", {}).get("fact_kind") == "importance_score"
    ]
    assert len(scores) == 1


def test_handler_scopes_to_scope(isolated_composition):
    """Handler only scores episodes in the target scope."""
    from axiom.memory.maturation import CycleBudget
    from axiom.memory.maturation.importance import (
        DeterministicImportanceScorer,
        ImportanceScoringStageHandler,
    )

    _write_episode(isolated_composition, scope="s1", user_input="event in s1")
    _write_episode(isolated_composition, scope="s2", user_input="event in s2")

    handler = ImportanceScoringStageHandler(
        composition=isolated_composition,
        scorer=DeterministicImportanceScorer(),
        principal_id="ben@example.com",
    )
    handler.run("s1", CycleBudget())

    scores = [
        a for a in isolated_composition.artifact_registry.list(kind="fragment")
        if (a.data or {}).get("content", {}).get("fact_kind") == "importance_score"
    ]
    assert len(scores) == 1
    # Verify it scored the s1 episode, not s2
    targets = {a.data["content"]["target_fragment_id"] for a in scores}
    s1_episodes = {
        a.name
        for a in isolated_composition.artifact_registry.list(kind="fragment")
        if (a.data or {}).get("content", {}).get("scope") == "s1"
        and (a.data or {}).get("content", {}).get("fact_kind") == "chat_turn"
    }
    assert targets == s1_episodes


def test_handler_evaluate_trigger_is_true_when_pending(isolated_composition):
    """The handler's trigger fires whenever there's pending work."""
    from axiom.memory.maturation.importance import (
        DeterministicImportanceScorer,
        ImportanceScoringStageHandler,
    )

    _write_episode(isolated_composition, scope="s1")
    handler = ImportanceScoringStageHandler(
        composition=isolated_composition,
        scorer=DeterministicImportanceScorer(),
        principal_id="ben@example.com",
    )
    assert handler.evaluate_trigger("s1") is True


def test_handler_satisfies_stage_handler_protocol(isolated_composition):
    """Structural check: the handler implements StageHandler."""
    from axiom.memory.maturation import StageHandler
    from axiom.memory.maturation.importance import (
        DeterministicImportanceScorer,
        ImportanceScoringStageHandler,
    )

    handler = ImportanceScoringStageHandler(
        composition=isolated_composition,
        scorer=DeterministicImportanceScorer(),
        principal_id="ben@example.com",
    )
    assert isinstance(handler, StageHandler)


# ---------------------------------------------------------------------------
# Orchestrator integration
# ---------------------------------------------------------------------------


def test_handler_drives_through_orchestrator(isolated_composition):
    """End-to-end: register the handler with the orchestrator, run a cycle."""
    from axiom.memory.maturation import DreamCycleOrchestrator, Stage
    from axiom.memory.maturation.importance import (
        DeterministicImportanceScorer,
        ImportanceScoringStageHandler,
    )

    _write_episode(isolated_composition, scope="my-scope", user_input="event 1")
    _write_episode(isolated_composition, scope="my-scope", user_input="event 2")

    orch = DreamCycleOrchestrator(composition=isolated_composition)
    handler = ImportanceScoringStageHandler(
        composition=isolated_composition,
        scorer=DeterministicImportanceScorer(),
        principal_id="ben@example.com",
    )
    orch.register(handler)

    report = orch.run_cycle(scope="my-scope")
    assert len(report.stages_run) == 1
    assert report.stages_run[0].stage == Stage.IMPORTANCE_SCORING
    assert report.stages_run[0].items_succeeded == 2

    scores = [
        a for a in isolated_composition.artifact_registry.list(kind="fragment")
        if (a.data or {}).get("content", {}).get("fact_kind") == "importance_score"
    ]
    assert len(scores) == 2
