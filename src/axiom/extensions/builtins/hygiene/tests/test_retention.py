# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for TIDY retention policy engine.

Proves:
1. Retention config loads correctly (and falls back to example)
2. Expired files are identified based on configured policies
3. Legal hold prevents all deletion
4. Dry run previews without deleting
5. Audit trail is written for every action
6. Integration with TIDY sweep cycle
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False
    yaml = None  # type: ignore[assignment]

from axiom.extensions.builtins.hygiene.retention import (
    RetentionAction,
    RetentionPolicy,
    execute_retention,
    load_retention_config,
    retention_status,
    scan_retention,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _write_retention_yaml(config_dir: Path, overrides: dict | None = None):
    """Write a minimal retention.yaml for testing."""
    assert yaml is not None, "PyYAML required for retention tests"
    cfg = {
        "retention": {
            "raw_voice": {"days": 7, "after": "processed"},
            "raw_signals": {"days": 30, "after": "ingested"},
            "transcripts": {"days": 90, "after": "created"},
            "sessions": {"days": 30, "after": "last_accessed"},
            "drafts": {"days": 14, "after": "created"},
        },
        "legal_hold": {"enabled": False},
        "audit": {
            "log_deletions": True,
            "log_path": "runtime/logs/retention_audit.jsonl",
        },
    }
    if overrides:
        cfg.update(overrides)

    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "retention.yaml").write_text(
        yaml.dump(cfg, default_flow_style=False),
    )


def _create_aged_file(path: Path, age_days: int):
    """Create a file and backdate its mtime/atime."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"test data aged {age_days} days")
    old_time = time.time() - (age_days * 86400)
    os.utime(path, (old_time, old_time))


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """Set up a minimal project structure for retention testing."""
    root = tmp_path / "project"
    (root / "runtime" / "config").mkdir(parents=True)
    (root / "runtime" / "logs").mkdir(parents=True)
    return root


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not HAS_YAML, reason="PyYAML not installed")
class TestLoadRetentionConfig:

    def test_loads_from_config_dir(self, project: Path):
        _write_retention_yaml(project / "runtime" / "config")
        policies, legal_hold, audit_path = load_retention_config(
            project / "runtime" / "config",
        )
        assert len(policies) == 5
        assert not legal_hold
        assert "retention_audit.jsonl" in str(audit_path)

    def test_falls_back_to_example_dir(self, project: Path):
        example_dir = project / "runtime" / "config.example"
        _write_retention_yaml(example_dir)
        policies, _legal_hold, _ = load_retention_config(
            project / "runtime" / "config",
            example_dir,
        )
        assert len(policies) == 5

    def test_missing_config_returns_empty(self, project: Path):
        policies, legal_hold, _ = load_retention_config(
            project / "runtime" / "config",
        )
        assert policies == []
        assert not legal_hold

    def test_legal_hold_flag(self, project: Path):
        _write_retention_yaml(
            project / "runtime" / "config",
            overrides={"legal_hold": {"enabled": True}},
        )
        _, legal_hold, _ = load_retention_config(
            project / "runtime" / "config",
        )
        assert legal_hold is True

    def test_policy_fields_parsed(self, project: Path):
        _write_retention_yaml(project / "runtime" / "config")
        policies, _, _ = load_retention_config(
            project / "runtime" / "config",
        )
        voice_policy = next(p for p in policies if p.key == "raw_voice")
        assert voice_policy.days == 7
        assert voice_policy.after == "processed"


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not HAS_YAML, reason="PyYAML not installed")
class TestScanRetention:

    def test_finds_expired_voice_memo(self, project: Path):
        _create_aged_file(
            project / "runtime" / "inbox" / "raw" / "voice" / "old.m4a",
            age_days=10,
        )
        policies = [RetentionPolicy("raw_voice", 7, "processed")]
        actions = scan_retention(project, policies, legal_hold=False)
        assert len(actions) == 1
        assert actions[0].action == "delete"
        assert actions[0].policy_key == "raw_voice"
        assert actions[0].age_days >= 10

    def test_keeps_recent_voice_memo(self, project: Path):
        _create_aged_file(
            project / "runtime" / "inbox" / "raw" / "voice" / "new.m4a",
            age_days=3,
        )
        policies = [RetentionPolicy("raw_voice", 7, "processed")]
        actions = scan_retention(project, policies, legal_hold=False)
        assert actions == []

    def test_legal_hold_marks_skip(self, project: Path):
        _create_aged_file(
            project / "runtime" / "inbox" / "raw" / "voice" / "old.m4a",
            age_days=10,
        )
        policies = [RetentionPolicy("raw_voice", 7, "processed")]
        actions = scan_retention(project, policies, legal_hold=True)
        assert len(actions) == 1
        assert actions[0].action == "skip"
        assert actions[0].reason == "legal_hold"

    def test_multiple_categories(self, project: Path):
        _create_aged_file(
            project / "runtime" / "inbox" / "raw" / "voice" / "a.m4a",
            age_days=10,
        )
        _create_aged_file(
            project / "runtime" / "drafts" / "old_draft.md",
            age_days=20,
        )
        _create_aged_file(
            project / "runtime" / "drafts" / "recent_draft.md",
            age_days=3,
        )
        policies = [
            RetentionPolicy("raw_voice", 7, "processed"),
            RetentionPolicy("drafts", 14, "processed"),  # mtime-based; ctime can't be backdated on Linux
        ]
        actions = scan_retention(project, policies, legal_hold=False)
        assert len(actions) == 2  # old voice + old draft, not recent draft

    def test_missing_directory_is_skipped(self, project: Path):
        policies = [RetentionPolicy("raw_voice", 7, "processed")]
        actions = scan_retention(project, policies, legal_hold=False)
        assert actions == []

    def test_sessions_use_last_accessed(self, project: Path):
        session_path = project / "runtime" / "sessions" / "old.json"
        _create_aged_file(session_path, age_days=35)
        policies = [RetentionPolicy("sessions", 30, "last_accessed")]
        actions = scan_retention(project, policies, legal_hold=False)
        assert len(actions) == 1

    def test_glob_pattern_filtering(self, project: Path):
        """Only .m4a files matched for voice memos, not random files."""
        _create_aged_file(
            project / "runtime" / "inbox" / "raw" / "voice" / "old.m4a",
            age_days=10,
        )
        _create_aged_file(
            project / "runtime" / "inbox" / "raw" / "voice" / "notes.txt",
            age_days=10,
        )
        policies = [RetentionPolicy("raw_voice", 7, "processed")]
        actions = scan_retention(project, policies, legal_hold=False)
        # Only the .m4a should match
        assert len(actions) == 1
        assert actions[0].path.suffix == ".m4a"


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not HAS_YAML, reason="PyYAML not installed")
class TestExecuteRetention:

    def test_deletes_expired_files(self, project: Path):
        voice_file = project / "runtime" / "inbox" / "raw" / "voice" / "old.m4a"
        _create_aged_file(voice_file, age_days=10)
        assert voice_file.exists()

        actions = [RetentionAction(
            path=voice_file,
            policy_key="raw_voice",
            age_days=10,
            action="delete",
            reason="retention_policy",
            size_bytes=voice_file.stat().st_size,
        )]
        audit_path = project / "runtime" / "logs" / "retention_audit.jsonl"
        result = execute_retention(actions, audit_path)

        assert result["deleted"] == 1
        assert result["bytes_freed"] > 0
        assert not voice_file.exists()

    def test_dry_run_preserves_files(self, project: Path):
        voice_file = project / "runtime" / "inbox" / "raw" / "voice" / "old.m4a"
        _create_aged_file(voice_file, age_days=10)

        actions = [RetentionAction(
            path=voice_file,
            policy_key="raw_voice",
            age_days=10,
            action="delete",
            reason="retention_policy",
            size_bytes=100,
        )]
        audit_path = project / "runtime" / "logs" / "retention_audit.jsonl"
        result = execute_retention(actions, audit_path, dry_run=True)

        assert result["deleted"] == 0
        assert voice_file.exists()

    def test_skip_action_not_deleted(self, project: Path):
        voice_file = project / "runtime" / "inbox" / "raw" / "voice" / "held.m4a"
        _create_aged_file(voice_file, age_days=10)

        actions = [RetentionAction(
            path=voice_file,
            policy_key="raw_voice",
            age_days=10,
            action="skip",
            reason="legal_hold",
            size_bytes=100,
        )]
        audit_path = project / "runtime" / "logs" / "retention_audit.jsonl"
        result = execute_retention(actions, audit_path)

        assert result["skipped"] == 1
        assert result["deleted"] == 0
        assert voice_file.exists()

    def test_audit_log_written(self, project: Path):
        voice_file = project / "runtime" / "inbox" / "raw" / "voice" / "old.m4a"
        _create_aged_file(voice_file, age_days=10)

        actions = [RetentionAction(
            path=voice_file,
            policy_key="raw_voice",
            age_days=10,
            action="delete",
            reason="retention_policy",
            size_bytes=100,
        )]
        audit_path = project / "runtime" / "logs" / "retention_audit.jsonl"
        execute_retention(actions, audit_path)

        assert audit_path.exists()
        lines = audit_path.read_text().strip().splitlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["action"] == "delete"
        assert entry["policy"] == "raw_voice"
        assert entry["age_days"] == 10
        assert "timestamp" in entry

    def test_audit_log_appends(self, project: Path):
        """Multiple executions append to the same log file."""
        audit_path = project / "runtime" / "logs" / "retention_audit.jsonl"

        for i in range(3):
            f = project / f"file_{i}.tmp"
            _create_aged_file(f, age_days=10)
            actions = [RetentionAction(
                path=f, policy_key="test", age_days=10,
                action="delete", reason="retention_policy", size_bytes=50,
            )]
            execute_retention(actions, audit_path)

        lines = audit_path.read_text().strip().splitlines()
        assert len(lines) == 3

    def test_error_on_missing_file(self, project: Path):
        """Trying to delete a nonexistent file logs an error."""
        actions = [RetentionAction(
            path=project / "nonexistent.txt",
            policy_key="test",
            age_days=99,
            action="delete",
            reason="retention_policy",
            size_bytes=0,
        )]
        audit_path = project / "runtime" / "logs" / "retention_audit.jsonl"
        result = execute_retention(actions, audit_path)
        assert result["errors"] == 1

        entry = json.loads(audit_path.read_text().strip())
        assert entry["action"] == "error"


# ---------------------------------------------------------------------------
# Status report
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not HAS_YAML, reason="PyYAML not installed")
class TestRetentionStatus:

    def test_empty_when_nothing_expired(self, project: Path):
        policies = [RetentionPolicy("raw_voice", 7, "processed")]
        status = retention_status(project, policies, legal_hold=False)
        assert status["total_files"] == 0
        assert status["total_bytes"] == 0

    def test_reports_expired_files(self, project: Path):
        _create_aged_file(
            project / "runtime" / "inbox" / "raw" / "voice" / "old.m4a",
            age_days=10,
        )
        policies = [RetentionPolicy("raw_voice", 7, "processed")]
        status = retention_status(project, policies, legal_hold=False)
        assert status["total_files"] == 1
        assert status["total_bytes"] > 0
        assert len(status["categories"]) == 1
        assert status["categories"][0]["policy_key"] == "raw_voice"


# ---------------------------------------------------------------------------
# TIDY sweep integration
# ---------------------------------------------------------------------------


class TestMoSweepRetentionIntegration:
    """Verify retention runs as part of TIDY's sweep when project_root is passed."""

    @pytest.mark.skipif(not HAS_YAML, reason="PyYAML not installed")
    def test_sweep_with_retention(self, tmp_path: Path):
        from axiom.extensions.builtins.hygiene.manager import MoManager

        project = tmp_path / "project"
        _write_retention_yaml(project / "runtime" / "config")
        _create_aged_file(
            project / "runtime" / "inbox" / "raw" / "voice" / "old.m4a",
            age_days=10,
        )

        mgr = MoManager(base_dir=tmp_path / "tidy")
        result = mgr.sweep(project_root=project)

        assert "retention" in result
        assert result["retention"]["deleted"] == 1

    def test_sweep_without_project_root_skips_retention(self, tmp_path: Path):
        from axiom.extensions.builtins.hygiene.manager import MoManager

        mgr = MoManager(base_dir=tmp_path / "tidy")
        result = mgr.sweep()
        assert "retention" not in result
