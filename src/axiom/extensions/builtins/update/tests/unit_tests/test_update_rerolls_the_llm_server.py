# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""An upgrade re-rolls the llm-server and proves what it serves (#494).

The pod ran a broken model for 68 days because nothing in the upgrade path
touched it: ``update`` updated the Python package, and only a separate
``update --infra`` would have rolled the Deployment. Now the full update rolls
it too (a quiet note on the many nodes without it), and a rollout is not
"complete" until the new pod answers ``/v1/models`` with the model it serves.
A pod that cannot answer is rolled back, not reported done.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

import pytest

from axiom.extensions.builtins.update.cli import Updater


@dataclass
class _Done:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


@pytest.fixture
def updater(tmp_path, monkeypatch):
    u = Updater(repo_root=tmp_path, dry_run=True)
    for step in ("_update_deps", "_run_migrations", "_register_agents", "_validate"):
        monkeypatch.setattr(u, step, lambda: None)
    return u


def test_the_full_update_includes_the_llm_server_step(updater, monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: _Done(returncode=1))
    results = updater.update_all()
    assert "llm-server" in [r.step for r in results]


def test_a_failed_deps_step_still_stops_before_the_llm_server(updater, monkeypatch):
    from axiom.extensions.builtins.update.cli import UpdateResult

    monkeypatch.setattr(
        updater,
        "_update_deps",
        lambda: updater.results.append(UpdateResult(step="deps", success=False, message="x")),
    )
    results = updater.update_all()
    assert "llm-server" not in [r.step for r in results]


def _rollout(monkeypatch, *, models: _Done):
    calls: list[list[str]] = []

    def run(cmd, **kwargs):
        calls.append(list(cmd))
        if cmd[:2] == ["kubectl", "exec"]:
            return models
        return _Done()

    monkeypatch.setattr(subprocess, "run", run)
    return calls


def test_a_rollout_is_complete_only_when_the_new_pod_names_its_model(tmp_path, monkeypatch):
    u = Updater(repo_root=tmp_path)
    calls = _rollout(monkeypatch, models=_Done(stdout='{"data":[{"id":"qwen2.5-3b.gguf"}]}'))
    u._update_llm_server()
    result = u.results[-1]
    assert result.success and result.changed
    assert "qwen2.5-3b.gguf" in result.message
    assert ["kubectl", "rollout", "undo", "deployment/llm-server", "-n", "axiom"] not in calls


def test_a_pod_that_cannot_name_its_model_is_rolled_back(tmp_path, monkeypatch):
    u = Updater(repo_root=tmp_path)
    calls = _rollout(monkeypatch, models=_Done(returncode=7, stderr="connection refused"))
    u._update_llm_server()
    result = u.results[-1]
    assert result.success is False
    assert "rolled back" in result.message
    assert "connection refused" in result.details
    assert ["kubectl", "rollout", "undo", "deployment/llm-server", "-n", "axiom"] in calls
