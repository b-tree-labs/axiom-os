# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""End-to-end wiring: a real guarded ``branch_prune`` tick produces
action-provenance records queryable via the MCP tool function (#665).

No hygiene-specific ledger code exists — the records are emitted by
``guarded_act`` itself, proving zero-consumer-change composition. The
only consumer-side surface exercised is the optional ``undo_ref_for``
callback (tidy-archive ref as the undo handle).

Fixture isolation per ``feedback_test_fixture_isolation_required.md``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from axiom.extensions.builtins.hygiene._git_isolation import (
    assert_test_tmp_path,
    git_isolated_env,
)


def _git(repo: Path, *args: str) -> str:
    assert_test_tmp_path(repo)
    result = subprocess.run(
        ["git", *args], cwd=repo, check=True,
        capture_output=True, text=True, env=git_isolated_env(),
    )
    return result.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repo on `main` with one merged (prunable) feature branch."""
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "test@example.com")
    _git(r, "config", "user.name", "Test")
    (r / "README.md").write_text("seed\n")
    _git(r, "add", "README.md")
    _git(r, "commit", "-q", "-m", "init")

    _git(r, "checkout", "-q", "-b", "feat/done")
    (r / "done.txt").write_text("x\n")
    _git(r, "add", "done.txt")
    _git(r, "commit", "-q", "-m", "feat")
    _git(r, "checkout", "-q", "main")
    _git(r, "merge", "-q", "--no-ff", "-m", "merge feat", "feat/done")
    return r


@pytest.fixture
def state_dir(tmp_path, monkeypatch) -> Path:
    from axiom.policy import action_ledger
    sd = tmp_path / "state"
    sd.mkdir()
    monkeypatch.setenv("AXIOM_ACTION_LEDGER_BACKEND", "jsonl")
    monkeypatch.delenv("AXIOM_AUDIT_HMAC_KEY", raising=False)
    monkeypatch.setenv("AXI_STATE_DIR", str(sd))
    action_ledger._reset_backend_cache()
    yield sd
    action_ledger._reset_backend_cache()


def test_prune_tick_journals_and_is_mcp_queryable(repo, state_dir):
    from axiom.extensions.builtins.hygiene.branch_prune import prune
    from axiom.extensions.builtins.memory import mcp_server

    result = prune(repo, state_dir=state_dir)
    assert result.proceed is True
    assert result.pruned == ["feat/done"]

    # Queryable through the MCP tool function — the same call path any
    # harness reaches over stdio.
    out = mcp_server.actions_search(agent="tidy", op_class="git.branch.delete")
    assert out["count"] == 1
    row = out["actions"][0]
    assert row["candidate"] == "feat/done"
    assert row["outcome"] == "proceeded"
    assert row["name"] == "prune_merged"
    # Undo handle: the tidy-archive ref written before deletion.
    assert row["undo_ref"] == "refs/tidy-archive/local/feat/done"
    # And the archive ref actually exists (the handle is honest).
    sha = _git(repo, "rev-parse", "--verify", "refs/tidy-archive/local/feat/done")
    assert sha


def test_dry_run_tick_is_journaled_with_flag(repo, state_dir):
    from axiom.extensions.builtins.hygiene.branch_prune import prune
    from axiom.extensions.builtins.memory import mcp_server

    result = prune(repo, state_dir=state_dir, dry_run=True)
    assert result.would_prune == ["feat/done"]

    out = mcp_server.actions_search(agent="tidy")
    assert out["count"] == 1
    assert out["actions"][0]["dry_run"] is True
    # Nothing was deleted.
    assert "feat/done" in _git(repo, "branch", "--list", "feat/done")
