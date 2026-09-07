# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for agent pattern sharing and community knowledge packs."""

from __future__ import annotations

import json
import tarfile
from pathlib import Path
from unittest.mock import patch

from axiom.agents.learning import (
    AgentKnowledgeStore,
    Confidence,
    LearnedPattern,
    generate_pattern_id,
)
from axiom.agents.sharing import AgentPatternResource, sync_patterns_with_peer
from axiom.setup.community_pack import CommunityPackStatus, check_community_pack


def _make_pattern(
    agent: str = "rivet",
    signature: str = "error.*test",
    confidence: Confidence = Confidence.RED,
    verified_count: int = 0,
    verified_by: list | None = None,
    category: str = "ci_failure",
) -> LearnedPattern:
    return LearnedPattern(
        pattern_id=generate_pattern_id(agent, signature),
        agent=agent,
        category=category,
        signature=signature,
        description=f"Test pattern: {signature}",
        diagnosis="Test diagnosis",
        resolution="Test resolution",
        confidence=confidence,
        verified_count=verified_count,
        verified_by=verified_by or [],
    )


def _seed_patterns(tmp_path: Path, patterns: list[LearnedPattern]) -> None:
    """Write patterns to tmp_path/.axi/agents/<agent>/patterns.json."""
    by_agent: dict[str, list] = {}
    for p in patterns:
        by_agent.setdefault(p.agent, []).append(p.to_dict())
    for agent, data in by_agent.items():
        d = tmp_path / ".axi" / "agents" / agent
        d.mkdir(parents=True, exist_ok=True)
        (d / "patterns.json").write_text(json.dumps(data, indent=2))


def _make_pack(tmp_path: Path, patterns: list[LearnedPattern]) -> Path:
    """Create a .axiompack from patterns for testing."""
    pack_dir = tmp_path / "pack-build"
    pack_dir.mkdir()
    agents_dir = pack_dir / "agents"

    by_agent: dict[str, list] = {}
    for p in patterns:
        by_agent.setdefault(p.agent, []).append(p.to_dict())

    for agent, data in by_agent.items():
        d = agents_dir / agent
        d.mkdir(parents=True, exist_ok=True)
        (d / "patterns.json").write_text(json.dumps(data, indent=2))

    manifest = {"content_type": "agent_patterns", "version": "1.0.0"}
    (pack_dir / "manifest.json").write_text(json.dumps(manifest))

    pack_path = tmp_path / "test.axiompack"
    with tarfile.open(str(pack_path), "w:gz") as tar:
        tar.add(str(pack_dir / "manifest.json"), arcname="manifest.json")
        for agent_dir in sorted(agents_dir.iterdir()):
            if agent_dir.is_dir():
                tar.add(
                    str(agent_dir / "patterns.json"),
                    arcname=f"agents/{agent_dir.name}/patterns.json",
                )
    return pack_path


class TestAgentPatternResource:
    def test_catalog_with_patterns(self, tmp_path):
        patterns = [
            _make_pattern("rivet", "sig1", Confidence.GREEN, 5),
            _make_pattern("rivet", "sig2", Confidence.YELLOW, 2),
            _make_pattern("secur-t", "sig3", Confidence.RED, 0),
        ]
        with patch("axiom.agents.sharing.load_all_agent_patterns") as mock_load:
            mock_load.return_value = {"rivet": patterns[:2], "secur-t": [patterns[2]]}
            resource = AgentPatternResource()
            catalog = resource.catalog()

        assert len(catalog) == 2
        rivet = [c for c in catalog if c["agent"] == "rivet"][0]
        assert rivet["pattern_count"] == 2
        assert rivet["green_count"] == 1

    def test_catalog_empty(self):
        with patch("axiom.agents.sharing.load_all_agent_patterns", return_value={}):
            resource = AgentPatternResource()
            assert resource.catalog() == []


class TestExportImport:
    def test_import_installs_patterns(self, tmp_path):
        patterns = [_make_pattern("rivet", "import-sig", Confidence.YELLOW, 2)]
        pack_path = _make_pack(tmp_path, patterns)

        resource = AgentPatternResource()
        # Point stores to tmp
        with patch.object(AgentKnowledgeStore, "_find_repo_root", return_value=None):
            store = AgentKnowledgeStore("rivet")
            store._local_dir = tmp_path / "local" / "rivet"
            store._local_dir.mkdir(parents=True, exist_ok=True)

            with patch("axiom.agents.sharing.AgentKnowledgeStore", return_value=store):
                result = resource.import_patterns(pack_path)

        assert result["new"] >= 1

    def test_import_merges_higher_confidence(self, tmp_path):
        # Existing pattern with low count
        existing = _make_pattern("rivet", "merge-sig", Confidence.RED, 1)
        # Incoming pattern with higher count
        incoming = _make_pattern("rivet", "merge-sig", Confidence.GREEN, 5)

        local_dir = tmp_path / "local" / "rivet"
        local_dir.mkdir(parents=True, exist_ok=True)
        (local_dir / "patterns.json").write_text(json.dumps([existing.to_dict()]))

        pack_path = _make_pack(tmp_path, [incoming])
        resource = AgentPatternResource()

        store = AgentKnowledgeStore("rivet", repo_root=None)
        store._local_dir = local_dir

        with patch("axiom.agents.sharing.AgentKnowledgeStore", return_value=store):
            result = resource.import_patterns(pack_path)

        assert result["updated"] >= 1

    def test_import_does_not_downgrade(self, tmp_path):
        # Existing with high count
        existing = _make_pattern("rivet", "no-down-sig", Confidence.GREEN, 10)
        # Incoming with lower count
        incoming = _make_pattern("rivet", "no-down-sig", Confidence.YELLOW, 2)

        local_dir = tmp_path / "local" / "rivet"
        local_dir.mkdir(parents=True, exist_ok=True)
        (local_dir / "patterns.json").write_text(json.dumps([existing.to_dict()]))

        pack_path = _make_pack(tmp_path, [incoming])
        resource = AgentPatternResource()

        store = AgentKnowledgeStore("rivet", repo_root=None)
        store._local_dir = local_dir

        with patch("axiom.agents.sharing.AgentKnowledgeStore", return_value=store):
            resource.import_patterns(pack_path)

        # Should skip, not downgrade
        loaded = store.load()
        match = [p for p in loaded if p.signature == "no-down-sig"]
        assert match[0].verified_count == 10


class TestSyncPatternsWithPeer:
    def test_new_patterns_added(self, tmp_path):
        peer_patterns = [_make_pattern("rivet", "peer-sig", Confidence.YELLOW, 3).to_dict()]

        store = AgentKnowledgeStore("rivet", repo_root=None)
        store._local_dir = tmp_path / "local" / "rivet"
        store._local_dir.mkdir(parents=True, exist_ok=True)

        with patch("axiom.agents.sharing.AgentKnowledgeStore", return_value=store):
            result = sync_patterns_with_peer(peer_patterns, "node-osu")

        assert result["new"] == 1

    def test_merges_verified_by(self, tmp_path):
        existing = _make_pattern("rivet", "merge-by-sig", Confidence.YELLOW, 2, ["node-a"])
        incoming = _make_pattern("rivet", "merge-by-sig", Confidence.YELLOW, 3, ["node-b"])

        local_dir = tmp_path / "local" / "rivet"
        local_dir.mkdir(parents=True, exist_ok=True)
        (local_dir / "patterns.json").write_text(json.dumps([existing.to_dict()]))

        store = AgentKnowledgeStore("rivet", repo_root=None)
        store._local_dir = local_dir

        with patch("axiom.agents.sharing.AgentKnowledgeStore", return_value=store):
            result = sync_patterns_with_peer([incoming.to_dict()], "node-b")

        assert result["updated"] == 1
        loaded = store.load()
        match = [p for p in loaded if p.signature == "merge-by-sig"]
        assert "node-a" in match[0].verified_by
        assert "node-b" in match[0].verified_by


class TestCommunityPack:
    def test_status_not_installed(self, tmp_path, monkeypatch):
        monkeypatch.setattr("axiom.setup.community_pack.Path.home", lambda: tmp_path)
        status = check_community_pack()
        assert not status.installed

    def test_status_serialization(self):
        status = CommunityPackStatus(available=True, installed=False, version="1.0")
        d = status.to_dict()
        assert d["available"] is True
        assert d["installed"] is False
