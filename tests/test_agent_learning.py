# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the unified agent learning framework."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from axiom.agents.learning import (
    AgentKnowledgeStore,
    Confidence,
    LearnedPattern,
    generate_pattern_id,
    load_all_agent_patterns,
)

# ---------------------------------------------------------------------------
# LearnedPattern
# ---------------------------------------------------------------------------


class TestLearnedPattern:
    def test_creation(self):
        p = LearnedPattern(
            pattern_id="test-abc",
            agent="test",
            category="ci_failure",
            signature="error.*foo",
            description="Test pattern",
            diagnosis="Something broke",
            resolution="Fix it",
        )
        assert p.pattern_id == "test-abc"
        assert p.confidence == Confidence.RED
        assert p.verified_count == 0

    def test_serialization_roundtrip(self):
        p = LearnedPattern(
            pattern_id="test-abc",
            agent="test",
            category="ci_failure",
            signature="error.*foo",
            description="Test pattern",
            diagnosis="Something broke",
            resolution="Fix it",
            confidence=Confidence.YELLOW,
            verified_count=2,
            verified_by=["node-a"],
        )
        d = p.to_dict()
        p2 = LearnedPattern.from_dict(d)
        assert p2.pattern_id == p.pattern_id
        assert p2.confidence == Confidence.YELLOW
        assert p2.verified_count == 2
        assert p2.verified_by == ["node-a"]

    def test_confidence_red_to_yellow(self):
        p = LearnedPattern(
            pattern_id="x", agent="t", category="c",
            signature="s", description="d", diagnosis="d", resolution="r",
        )
        assert p.confidence == Confidence.RED
        p.record_success("node-a")
        assert p.confidence == Confidence.YELLOW
        assert p.verified_count == 1

    def test_confidence_yellow_to_green(self):
        p = LearnedPattern(
            pattern_id="x", agent="t", category="c",
            signature="s", description="d", diagnosis="d", resolution="r",
        )
        p.record_success("node-a")
        p.record_success("node-b")
        p.record_success("node-c")
        assert p.confidence == Confidence.GREEN
        assert p.maturity >= 3

    def test_failure_degrades_confidence(self):
        p = LearnedPattern(
            pattern_id="x", agent="t", category="c",
            signature="s", description="d", diagnosis="d", resolution="r",
            confidence=Confidence.YELLOW, verified_count=1,
        )
        p.record_failure()
        p.record_failure()
        assert p.confidence == Confidence.RED

    def test_record_success_no_duplicate_nodes(self):
        p = LearnedPattern(
            pattern_id="x", agent="t", category="c",
            signature="s", description="d", diagnosis="d", resolution="r",
        )
        p.record_success("node-a")
        p.record_success("node-a")
        assert p.verified_by == ["node-a"]
        assert p.verified_count == 2


# ---------------------------------------------------------------------------
# generate_pattern_id
# ---------------------------------------------------------------------------


class TestGeneratePatternId:
    def test_deterministic(self):
        id1 = generate_pattern_id("rivet", "SyntaxError.*backslash")
        id2 = generate_pattern_id("rivet", "SyntaxError.*backslash")
        assert id1 == id2

    def test_different_agents(self):
        id1 = generate_pattern_id("rivet", "error")
        id2 = generate_pattern_id("secur-t", "error")
        assert id1 != id2

    def test_format(self):
        pid = generate_pattern_id("rivet", "test")
        assert pid.startswith("rivet-")
        assert len(pid) == len("rivet-") + 12


# ---------------------------------------------------------------------------
# AgentKnowledgeStore
# ---------------------------------------------------------------------------


class TestAgentKnowledgeStore:
    @pytest.fixture()
    def store(self, tmp_path: Path) -> AgentKnowledgeStore:
        s = AgentKnowledgeStore("test-agent", repo_root=tmp_path)
        s._local_dir = tmp_path / "local" / "test-agent"
        s._local_dir.mkdir(parents=True, exist_ok=True)
        return s

    def test_learn_creates_pattern(self, store: AgentKnowledgeStore):
        p = store.learn(
            category="ci_failure",
            signature="ImportError.*foo",
            description="Missing foo",
            diagnosis="foo not installed",
            resolution="pip install foo",
        )
        assert p.confidence == Confidence.RED
        assert p.agent == "test-agent"

        loaded = store.load()
        assert len(loaded) == 1
        assert loaded[0].pattern_id == p.pattern_id

    def test_match_finds_patterns(self, store: AgentKnowledgeStore):
        store.learn(
            category="ci_failure",
            signature="ImportError.*foo",
            description="Missing foo",
            diagnosis="foo not installed",
            resolution="pip install foo",
        )
        matches = store.match("Traceback: ImportError: cannot import foo")
        assert len(matches) == 1
        assert matches[0].description == "Missing foo"

    def test_match_no_match(self, store: AgentKnowledgeStore):
        store.learn(
            category="ci_failure",
            signature="ImportError.*foo",
            description="Missing foo",
            diagnosis="foo not installed",
            resolution="pip install foo",
        )
        matches = store.match("Everything is fine")
        assert len(matches) == 0

    def test_verify_updates_confidence(self, store: AgentKnowledgeStore):
        p = store.learn(
            category="test",
            signature="sig",
            description="d",
            diagnosis="d",
            resolution="r",
        )
        store.verify(p.pattern_id, success=True, node_id="node-a")
        loaded = store.load()
        assert loaded[0].verified_count == 1
        assert loaded[0].confidence == Confidence.YELLOW

    def test_promote_to_repo(self, store: AgentKnowledgeStore, tmp_path: Path):
        p = store.learn(
            category="test",
            signature="sig",
            description="d",
            diagnosis="d",
            resolution="r",
        )
        result = store.promote_to_repo(p.pattern_id)
        assert result is True

        repo_file = tmp_path / ".axi" / "agents" / "test-agent" / "patterns.json"
        assert repo_file.exists()
        data = json.loads(repo_file.read_text())
        assert len(data) == 1
        assert data[0]["pattern_id"] == p.pattern_id

    def test_repo_patterns_loaded(self, store: AgentKnowledgeStore, tmp_path: Path):
        # Write directly to repo location
        repo_dir = tmp_path / ".axi" / "agents" / "test-agent"
        repo_dir.mkdir(parents=True, exist_ok=True)
        pattern_data = [
            {
                "pattern_id": "test-agent-repo1",
                "agent": "test-agent",
                "category": "test",
                "signature": "repo-sig",
                "description": "Repo pattern",
                "diagnosis": "from repo",
                "resolution": "repo fix",
                "confidence": "green",
                "verified_count": 5,
                "verified_by": ["a", "b"],
                "maturity": 3,
            }
        ]
        (repo_dir / "patterns.json").write_text(json.dumps(pattern_data))

        loaded = store.load()
        assert any(p.pattern_id == "test-agent-repo1" for p in loaded)

    def test_local_overrides_repo_higher_count(
        self, store: AgentKnowledgeStore, tmp_path: Path
    ):
        # Repo pattern with verified_count=2
        repo_dir = tmp_path / ".axi" / "agents" / "test-agent"
        repo_dir.mkdir(parents=True, exist_ok=True)
        (repo_dir / "patterns.json").write_text(
            json.dumps(
                [
                    {
                        "pattern_id": "shared-1",
                        "agent": "test-agent",
                        "category": "test",
                        "signature": "s",
                        "description": "repo version",
                        "diagnosis": "d",
                        "resolution": "r",
                        "confidence": "yellow",
                        "verified_count": 2,
                    }
                ]
            )
        )

        # Local pattern with verified_count=5 (should win)
        store._write_patterns(
            store._local_patterns_file,
            [
                LearnedPattern(
                    pattern_id="shared-1",
                    agent="test-agent",
                    category="test",
                    signature="s",
                    description="local version",
                    diagnosis="d",
                    resolution="r",
                    confidence=Confidence.GREEN,
                    verified_count=5,
                )
            ],
        )

        loaded = store.load()
        match = [p for p in loaded if p.pattern_id == "shared-1"]
        assert len(match) == 1
        assert match[0].description == "local version"
        assert match[0].verified_count == 5

    def test_green_auto_promotes(self, store: AgentKnowledgeStore, tmp_path: Path):
        p = store.learn(
            category="test",
            signature="auto-promote-sig",
            description="d",
            diagnosis="d",
            resolution="r",
        )
        # Verify 3 times from 2 nodes to reach GREEN
        store.verify(p.pattern_id, success=True, node_id="node-a")
        store.verify(p.pattern_id, success=True, node_id="node-b")
        store.verify(p.pattern_id, success=True, node_id="node-c")

        # After 3 verifications from 3 nodes, pattern should be GREEN
        loaded = [x for x in store.load() if x.pattern_id == p.pattern_id][0]
        assert loaded.confidence == Confidence.GREEN
        assert loaded.verified_count == 3

        # GREEN patterns auto-promote to repo
        repo_file = tmp_path / ".axi" / "agents" / "test-agent" / "patterns.json"
        assert repo_file.exists()

    @patch("axiom.agents.learning.AgentKnowledgeStore._index_in_corpus")
    def test_corpus_indexing_called(self, mock_index, store: AgentKnowledgeStore):
        store.learn(
            category="test",
            signature="corpus-test",
            description="d",
            diagnosis="d",
            resolution="r",
        )
        mock_index.assert_called_once()

    def test_match_with_invalid_regex_falls_back(
        self, store: AgentKnowledgeStore
    ):
        store.learn(
            category="test",
            signature="[invalid regex",
            description="bad regex",
            diagnosis="d",
            resolution="r",
        )
        # Should not raise, falls back to substring match
        matches = store.match("this contains [invalid regex literally")
        assert len(matches) == 1


# ---------------------------------------------------------------------------
# Seed patterns from .axi/agents/
# ---------------------------------------------------------------------------


class TestSeedPatterns:
    @pytest.fixture()
    def repo_root(self) -> Path:
        return Path(__file__).resolve().parent.parent

    @pytest.mark.parametrize(
        "agent",
        ["rivet", "secur-t", "scan", "tidy"],
    )
    def test_seed_patterns_load(self, agent: str, repo_root: Path):
        patterns_file = repo_root / ".axi" / "agents" / agent / "patterns.json"
        assert patterns_file.exists(), f"Missing seed patterns for {agent}"
        data = json.loads(patterns_file.read_text())
        assert len(data) > 0
        for entry in data:
            p = LearnedPattern.from_dict(entry)
            assert p.agent == agent
            assert p.pattern_id
            assert p.signature

    def test_all_agent_patterns_cross_query(self, repo_root: Path):
        all_patterns = load_all_agent_patterns(repo_root)
        assert len(all_patterns) >= 4  # rivet, secur-t, scan, tidy
        total = sum(len(v) for v in all_patterns.values())
        assert total >= 10  # at least 10 seed patterns across all agents
