# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for axiom.memory.bootstrap — the one-call extension affordance.

The promise: any extension calls ``build_memory_stack(scope_id)`` and
gets the full L1 + L2 + L3 stack with default-deny visibility,
unclassified classification, signed audit, deterministic text
extraction, and projection-ready ArtifactRegistry. No extension code
should need to wire memory primitives by hand.

These tests pin that promise: a single call produces a stack that
satisfies every layer's contract.
"""

from __future__ import annotations

import pytest

from axiom.memory.bootstrap import MemoryStack, build_memory_stack
from axiom.memory.graph import DeterministicTextExtractor


@pytest.fixture
def stack(tmp_path):
    return build_memory_stack(scope_id="test-scope", data_root=tmp_path)


class TestBuildMemoryStack:
    def test_returns_fully_wired_stack(self, stack):
        assert isinstance(stack, MemoryStack)
        assert stack.scope_id == "test-scope"
        assert stack.composition is not None
        assert stack.graph is not None
        assert stack.extractors is not None
        assert stack.keypair is not None

    def test_default_extractor_registered(self, stack):
        names = [e.capability.name for e in stack.extractors.extractors]
        assert "deterministic_text" in names

    def test_state_persists_across_calls(self, tmp_path):
        # First call seeds the keypair + opens the registry.
        stack_a = build_memory_stack("scoped", data_root=tmp_path)
        a_pubkey = stack_a.keypair.public_bytes
        # Write a fragment so the registry has visible state.
        stack_a.composition.write(
            content={"event_time": "2026-04-26T10:00:00+00:00",
                     "question": "Q", "had_answer": True,
                     "citations_count": 1, "classroom_id": "scoped"},
            cognitive_type="episodic",
            principal_id="alice", agents=set(), resources=set(),
        )

        # Second call resumes — same keypair, same registry on disk.
        stack_b = build_memory_stack("scoped", data_root=tmp_path)
        assert stack_b.keypair.public_bytes == a_pubkey
        # Existing fragment is visible.
        artifacts = list(stack_b.artifact_registry.list(kind="fragment"))
        assert len(artifacts) == 1


class TestStackEndToEndIntegration:
    """L1 + L2 + L3 wired together: write a fragment, extractors fire,
    projection sees it, concepts populate."""

    def test_write_with_extraction_populates_all_layers(self, stack):
        # L1 + L2 in one call via the convenience method.
        fragment = stack.write_with_extraction(
            content={
                "event_time": "2026-04-26T10:00:00+00:00",
                "question": "What is reactor criticality?",
                "had_answer": True, "citations_count": 1,
                "classroom_id": "test-scope",
            },
            cognitive_type="episodic",
            principal_id="alice@u.edu",
            agents=set(), resources=set(),
        )

        # L1 — fragment landed in the registry. Note: artifact.id is the
        # registry's row id (UUID4 per register call); the fragment id
        # lives in the artifact's data payload.
        artifacts = list(stack.artifact_registry.list(kind="fragment"))
        assert len(artifacts) == 1
        assert artifacts[0].data["id"] == fragment.id

        # L2 — concepts extracted from "reactor criticality" text.
        concept_names = sorted(c.canonical_name for c in stack.graph.all_concepts())
        assert "reactor" in concept_names
        assert "criticality" in concept_names

        # L3 — projection picks up the fragment.
        proj = stack.recent_activity(window_n=5)
        from axiom.memory.projections import TaskSpec
        result = proj.project(
            TaskSpec(task_type="recent_activity", scope="test-scope"),
            principal_id="alice@u.edu",
        )
        assert len(result.fragments) == 1
        assert result.fragments[0].id == fragment.id

    def test_interaction_writer_adapter_wires_classroom_store(self, tmp_path):
        """The MemoryStack's interaction_writer plug-in wires
        ClassroomInteractionStore into L1 — the canonical Stage 1 pattern
        any extension can replicate."""
        from axiom.extensions.builtins.classroom.classroom_interaction import (
            ClassroomInteractionStore,
            InteractionRecord,
        )

        stack = build_memory_stack("integration-test", data_root=tmp_path)
        store = ClassroomInteractionStore(
            tmp_path / "interactions",
            memory_writer=stack.interaction_writer(),
            scope_id="integration-test",
        )
        store.append(InteractionRecord(
            student_id="bob@u.edu",
            question="How does criticality work?",
            had_answer=True, citations_count=1,
            timestamp="2026-04-26T11:00:00+00:00",
            classroom_id="integration-test", mode="ask",
        ))

        # JSONL primary write.
        assert len(store.list()) == 1
        # L1 mirror via dual-write.
        artifacts = list(stack.artifact_registry.list(kind="fragment"))
        assert len(artifacts) == 1
        assert artifacts[0].data["content"]["question"] == "How does criticality work?"


class TestStackOverrides:
    def test_disable_signing_keeps_stack_functional(self, tmp_path):
        stack = build_memory_stack(
            "no-sign", data_root=tmp_path, enable_signing=False,
        )
        assert stack.composition.signing_keypair is None
        # Stack still writes + projects.
        f = stack.write_with_extraction(
            content={"event_time": "2026-04-26T10:00:00+00:00",
                     "question": "Q", "had_answer": True,
                     "citations_count": 1, "classroom_id": "no-sign"},
            cognitive_type="episodic",
            principal_id="alice", agents=set(), resources=set(),
        )
        assert f.id

    def test_skip_default_extractors(self, tmp_path):
        stack = build_memory_stack(
            "no-extract",
            data_root=tmp_path,
            register_default_extractors=False,
        )
        assert stack.extractors.extractors == []
        # Extension can register its own.
        stack.extractors.register(DeterministicTextExtractor())
        assert len(stack.extractors.extractors) == 1
