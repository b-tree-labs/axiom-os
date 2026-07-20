# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for RIVET agent foundation."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from axiom.extensions.builtins.release.agent_cli import build_parser
from axiom.extensions.builtins.release.ci_monitor import PipelineStatus, get_build_status
from axiom.extensions.builtins.release.failure_patterns import (
    BUILTIN_PATTERNS,
    FailurePattern,
    FailurePatternDB,
)
from axiom.extensions.builtins.release.mode import EnvironmentMode, detect_mode

# --- Mode detection ---


def _mock_distribution(editable: bool = False, version: str = "0.5.0"):
    """Create a mock distribution object."""
    dist = MagicMock()
    dist.version = version
    if editable:
        dist.read_text.return_value = json.dumps(
            {"url": "file:///home/user/axiom", "dir_info": {"editable": True}}
        )
    else:
        dist.read_text.return_value = None
    return dist


def test_detect_mode_developer():
    """Editable install should be detected as developer mode."""
    with patch(
        "importlib.metadata.distribution",
        return_value=_mock_distribution(editable=True),
    ):
        mode = detect_mode()
    assert mode.mode == "developer"
    assert mode.axiom_editable is True


def test_detect_mode_operator():
    """Non-editable install should be detected as operator mode."""
    with patch(
        "importlib.metadata.distribution",
        return_value=_mock_distribution(editable=False),
    ):
        mode = detect_mode()
    assert mode.mode == "operator"
    assert mode.axiom_editable is False


def test_environment_mode_to_dict():
    mode = EnvironmentMode(
        mode="developer",
        axiom_version="0.5.0",
        axiom_source="/home/user/axiom",
        axiom_editable=True,
        consumer_version="0.3.0",
        consumer_source="pypi",
        consumer_editable=False,
    )
    d = mode.to_dict()
    assert d["mode"] == "developer"
    assert d["axiom"]["editable"] is True
    assert d["consumer"]["source"] == "pypi"


# --- Failure patterns ---


def test_builtin_patterns_count():
    assert len(BUILTIN_PATTERNS) == 5


def test_failure_pattern_db_loads_builtins(tmp_path):
    db = FailurePatternDB(path=tmp_path / "patterns.json")
    patterns = db.load()
    assert len(patterns) >= 5  # 5 builtins + any locally learned
    builtins = [p for p in patterns if p.source == "builtin"]
    assert len(builtins) >= 5


def test_match_failure_python311(tmp_path):
    db = FailurePatternDB(path=tmp_path / "patterns.json")
    output = (
        'SyntaxError: f-string expression part cannot include a backslash\n  File "foo.py", line 42'
    )
    matches = db.match_failure(output)
    assert len(matches) == 1
    assert "3.11" in matches[0].name or "fstring" in matches[0].name or "backslash" in matches[0].name
    assert matches[0].occurrences >= 1


def test_match_failure_clean_output(tmp_path):
    db = FailurePatternDB(path=tmp_path / "patterns.json")
    matches = db.match_failure("All 42 tests passed.\nBuild succeeded.")
    assert matches == []


def test_add_learned_pattern(tmp_path):
    db = FailurePatternDB(path=tmp_path / "patterns.json")
    new_pattern = FailurePattern(
        name="flaky_network",
        signature=r"ConnectionResetError",
        diagnosis="Flaky network in CI",
        fix="Retry the job",
        prevention="",
        source="learned",
        occurrences=1,
        last_seen="2026-04-07T00:00:00",
    )
    db.add_pattern(new_pattern)

    # Reload and verify
    patterns = db.load()
    assert len(patterns) == 6
    learned = [p for p in patterns if p.name == "flaky_network"]
    assert len(learned) == 1
    assert learned[0].source == "learned"


def test_get_prevention_checks(tmp_path):
    db = FailurePatternDB(path=tmp_path / "patterns.json")
    checks = db.get_prevention_checks()
    assert len(checks) == 5
    assert all(isinstance(c, str) for c in checks)


# --- CI monitor ---


def test_pipeline_status_serialization():
    ps = PipelineStatus(
        repo="axiom",
        provider="github",
        ref="main",
        status="success",
        url="https://github.com/runs/1",
    )
    d = ps.to_dict()
    assert d["repo"] == "axiom"
    assert d["provider"] == "github"
    assert d["status"] == "success"


def test_get_build_status():
    with (
        patch(
            "axiom.extensions.builtins.release.ci_monitor.check_pipelines",
            return_value=[
                PipelineStatus(repo="axiom", provider="github", ref="main", status="success")
            ],
        ),
        patch(
            "axiom.extensions.builtins.release.mode.detect_mode",
            return_value=EnvironmentMode(
                mode="developer",
                axiom_version="0.5.0",
                axiom_source="/src",
                axiom_editable=True,
                consumer_version="",
                consumer_source="not installed",
                consumer_editable=False,
            ),
        ),
    ):
        status = get_build_status()

    assert "mode" in status
    assert "pipelines" in status
    assert status["all_green"] is True
    assert "checked_at" in status


# --- CLI ---


def test_cli_parser_subcommands():
    parser = build_parser()
    for action in ("status", "mode", "patterns", "check", "plan"):
        args = parser.parse_args([action])
        assert args.action == action


def test_cli_plan_format():
    parser = build_parser()
    args = parser.parse_args(["plan", "--format", "json"])
    assert args.format == "json"
