# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for `axiom.infra.paths.get_agent_output_dir` (issue #202.1).

The convention: per-extension operational output (heartbeat JSON,
health reports, debug dumps) goes to
``<project_root>/runtime/agent-output/<agent-name>/``. The scaffolded
extension's docs reference this helper so agents don't invent their
own write paths; consumers gitignore the single root, covering all
agents.

Motivating case: a consumer extension's `runtime/reports/` accumulated 32MB of
untracked heartbeat JSON because an agent picked a bespoke path
that the consumer's `.gitignore` didn't enumerate.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def project_root(tmp_path: Path, monkeypatch) -> Path:
    """Pin `get_project_root` to a tmp dir via the AXIOM_ROOT env var."""
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.setenv("AXIOM_ROOT", str(root))
    return root


class TestGetAgentOutputDir:
    def test_returns_runtime_agent_output_agent_path(self, project_root):
        from axiom.infra.paths import get_agent_output_dir
        out = get_agent_output_dir("m-o")
        assert out == project_root / "runtime" / "agent-output" / "m-o"

    def test_creates_directory_on_first_access(self, project_root):
        from axiom.infra.paths import get_agent_output_dir
        out = get_agent_output_dir("fresh-agent")
        assert out.is_dir()

    def test_idempotent(self, project_root):
        from axiom.infra.paths import get_agent_output_dir
        a = get_agent_output_dir("twice")
        b = get_agent_output_dir("twice")
        assert a == b
        assert a.is_dir()

    def test_each_agent_has_isolated_subdir(self, project_root):
        from axiom.infra.paths import get_agent_output_dir
        a = get_agent_output_dir("alpha")
        b = get_agent_output_dir("beta")
        assert a != b
        assert a.is_dir() and b.is_dir()
        assert a.parent == b.parent  # same agent-output/ root


class TestNameValidation:
    """Agent-name passed to `get_agent_output_dir` is part of a filesystem
    path. Reject anything that could escape the agent-output/ root or
    confuse downstream tooling."""

    @pytest.mark.parametrize("bad_name", [
        "",
        ".",
        "..",
        "../escape",
        "with/slash",
        "with\\backslash",
        "with\0null",
    ])
    def test_rejects_path_traversal_or_empty(self, project_root, bad_name):
        from axiom.infra.paths import get_agent_output_dir
        with pytest.raises(ValueError):
            get_agent_output_dir(bad_name)

    @pytest.mark.parametrize("ok_name", [
        "m-o",
        "tidy",
        "agent_with_underscore",
        "alpha.beta",
        "with-dashes-and-numbers-42",
    ])
    def test_accepts_reasonable_names(self, project_root, ok_name):
        from axiom.infra.paths import get_agent_output_dir
        out = get_agent_output_dir(ok_name)
        assert out.is_dir()
