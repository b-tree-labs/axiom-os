# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for axiom.memory.projections — Layer 3 first inhabitant.

Per ADR-033 Layer 3 contract: projections are pure functions of
``(events, graph, task)``. Same inputs → same output. Tests pin:

- ``RecentActivityProjection`` returns the most-recent N fragments
  for a given (scope, principal) pair, sorted newest-first.
- Filters by EPISODIC cognitive type (other types pass through other
  projections).
- Filters by scope embedded in fragment content (interim until
  scope becomes a top-level fragment field).
- ``as_of`` enables time-travel projections.
- ``format_recent_for_prompt`` produces appendable LLM context.
"""

from __future__ import annotations

import pytest

from axiom.artifacts.registry import ArtifactRegistry, InMemoryBackend
from axiom.memory.access import AccessGraphs
from axiom.memory.attest import AuditLog
from axiom.memory.composition import CompositionService
from axiom.memory.policy import PolicyCoord
from axiom.memory.projections import (
    RecentActivity,
    RecentActivityProjection,
    TaskSpec,
    format_recent_for_prompt,
)
from axiom.memory.trust import TrustGraph

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def composition_service(tmp_path):
    """Minimal CompositionService for projection-test fixture writes."""
    return CompositionService(
        artifact_registry=ArtifactRegistry(backend=InMemoryBackend()),
        audit_log=AuditLog(tmp_path / "audit.jsonl", signing_keypair=None),
        signing_keypair=None,
        policy_coord=PolicyCoord(global_policy={"write": "private"}),
        access_graphs=AccessGraphs(),
        trust_graph=TrustGraph(),
    )


def _write_episodic(cs, *, principal_id, scope, question, ts, mode="ask"):
    """Helper: write an episodic fragment matching the dual-write shape."""
    return cs.write(
        content={
            "event_time": ts,
            "classroom_id": scope,
            "question": question,
            "had_answer": True,
            "citations_count": 1,
            "mode": mode,
        },
        cognitive_type="episodic",
        principal_id=principal_id,
        agents=set(),
        resources=set(),
    )


# ---------------------------------------------------------------------------
# RecentActivityProjection — basic projection contract
# ---------------------------------------------------------------------------


class TestRecentActivityProjection:
    def test_empty_store_returns_empty_activity(self, composition_service):
        proj = RecentActivityProjection(composition_service.artifact_registry)
        result = proj.project(
            TaskSpec(task_type="recent_activity", scope="NE101"),
            principal_id="alice",
        )
        assert isinstance(result, RecentActivity)
        assert result.fragments == []
        assert result.is_empty is True

    def test_returns_only_matching_principal(self, composition_service):
        _write_episodic(
            composition_service, principal_id="alice", scope="NE101",
            question="alice Q", ts="2026-04-25T10:00:00+00:00",
        )
        _write_episodic(
            composition_service, principal_id="bob", scope="NE101",
            question="bob Q", ts="2026-04-25T11:00:00+00:00",
        )

        proj = RecentActivityProjection(composition_service.artifact_registry)
        alice = proj.project(
            TaskSpec(task_type="recent_activity", scope="NE101"),
            principal_id="alice",
        )
        questions = [f.content["question"] for f in alice.fragments]
        assert questions == ["alice Q"]

    def test_returns_only_matching_scope(self, composition_service):
        _write_episodic(
            composition_service, principal_id="alice", scope="NE101",
            question="ne101 Q", ts="2026-04-25T10:00:00+00:00",
        )
        _write_episodic(
            composition_service, principal_id="alice", scope="NE102",
            question="ne102 Q", ts="2026-04-25T11:00:00+00:00",
        )

        proj = RecentActivityProjection(composition_service.artifact_registry)
        ne101 = proj.project(
            TaskSpec(task_type="recent_activity", scope="NE101"),
            principal_id="alice",
        )
        questions = [f.content["question"] for f in ne101.fragments]
        assert questions == ["ne101 Q"]

    def test_sorted_newest_first(self, composition_service):
        for ts, q in [
            ("2026-04-25T10:00:00+00:00", "old"),
            ("2026-04-25T12:00:00+00:00", "new"),
            ("2026-04-25T11:00:00+00:00", "middle"),
        ]:
            _write_episodic(
                composition_service, principal_id="alice", scope="NE101",
                question=q, ts=ts,
            )

        proj = RecentActivityProjection(composition_service.artifact_registry)
        result = proj.project(
            TaskSpec(task_type="recent_activity", scope="NE101"),
            principal_id="alice",
        )
        assert [f.content["question"] for f in result.fragments] == [
            "new", "middle", "old",
        ]

    def test_truncates_to_window_n(self, composition_service):
        for i in range(10):
            _write_episodic(
                composition_service, principal_id="alice", scope="NE101",
                question=f"Q{i}",
                ts=f"2026-04-25T1{i}:00:00+00:00",
            )

        proj = RecentActivityProjection(
            composition_service.artifact_registry, window_n=3,
        )
        result = proj.project(
            TaskSpec(task_type="recent_activity", scope="NE101"),
            principal_id="alice",
        )
        assert len(result.fragments) == 3

    def test_skips_non_episodic_types(self, composition_service):
        """Only EPISODIC fragments project. Resources, semantic facts,
        etc. flow through extension-specific projections."""
        composition_service.write(
            content={"ref": "doc.pdf"},
            cognitive_type="resource",
            principal_id="alice", agents=set(), resources=set(),
        )
        _write_episodic(
            composition_service, principal_id="alice", scope="NE101",
            question="real Q", ts="2026-04-25T10:00:00+00:00",
        )

        proj = RecentActivityProjection(composition_service.artifact_registry)
        result = proj.project(
            TaskSpec(task_type="recent_activity", scope="NE101"),
            principal_id="alice",
        )
        questions = [f.content["question"] for f in result.fragments]
        assert questions == ["real Q"]

    def test_as_of_excludes_newer_fragments(self, composition_service):
        """Time-travel projection: as_of caps the visible event log."""
        for ts, q in [
            ("2026-04-25T10:00:00+00:00", "before"),
            ("2026-04-25T12:00:00+00:00", "after"),
        ]:
            _write_episodic(
                composition_service, principal_id="alice", scope="NE101",
                question=q, ts=ts,
            )

        proj = RecentActivityProjection(composition_service.artifact_registry)
        result = proj.project(
            TaskSpec(
                task_type="recent_activity",
                scope="NE101",
                as_of="2026-04-25T11:00:00+00:00",
            ),
            principal_id="alice",
        )
        questions = [f.content["question"] for f in result.fragments]
        assert questions == ["before"]


# ---------------------------------------------------------------------------
# Pure-function contract — same inputs → same output
# ---------------------------------------------------------------------------


class TestPureFunctionContract:
    def test_repeated_projection_yields_same_result(self, composition_service):
        for i in range(5):
            _write_episodic(
                composition_service, principal_id="alice", scope="NE101",
                question=f"Q{i}",
                ts=f"2026-04-25T1{i}:00:00+00:00",
            )

        proj = RecentActivityProjection(composition_service.artifact_registry)
        task = TaskSpec(task_type="recent_activity", scope="NE101")
        a = proj.project(task, principal_id="alice")
        b = proj.project(task, principal_id="alice")

        assert [f.id for f in a.fragments] == [f.id for f in b.fragments]


# ---------------------------------------------------------------------------
# Prompt rendering helper
# ---------------------------------------------------------------------------


class TestFormatRecentForPrompt:
    def test_empty_activity_renders_empty_string(self):
        empty = RecentActivity(scope="NE101", principal_id="alice", fragments=[])
        assert format_recent_for_prompt(empty) == ""

    def test_renders_header_plus_one_line_per_fragment(
        self, composition_service,
    ):
        _write_episodic(
            composition_service, principal_id="alice", scope="NE101",
            question="What is criticality?",
            ts="2026-04-25T10:00:00+00:00",
        )

        proj = RecentActivityProjection(composition_service.artifact_registry)
        result = proj.project(
            TaskSpec(task_type="recent_activity", scope="NE101"),
            principal_id="alice",
        )
        out = format_recent_for_prompt(result)
        assert "Recent activity for alice in NE101" in out
        assert "What is criticality?" in out
        assert "(ask)" in out
        assert "✓" in out  # had_answer=True marker
