# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for TIDY's RACI-approval CLI surface.

`axi hygiene discover` lists discovered local repos.
`axi hygiene propose <action>` reports the current RACI decision.
`axi hygiene approve <action>` records yes; future proposals return AUTO.
`axi hygiene deny <action>` records no; future proposals return SKIP.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from axiom.extensions.builtins.hygiene._git_isolation import (
    assert_test_tmp_path,
    git_isolated_env,
)


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    """Redirect ~/.axi/agents/tidy/ to a tmp dir."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setenv("AXI_STATE_DIR", str(state_dir))
    return state_dir


def _git_init(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    assert_test_tmp_path(path)
    subprocess.run(
        ["git", "init", "--quiet"], cwd=path, check=True, capture_output=True,
        env=git_isolated_env(),
    )


class TestDiscoverCli:
    def test_discover_lists_repos(self, tmp_path, isolated_state, capsys, monkeypatch):
        from axiom.extensions.builtins.hygiene.cli import main

        ws = tmp_path / "ws"
        _git_init(ws / "alpha")
        _git_init(ws / "beta")
        monkeypatch.setenv("AXI_WORKSPACE_ROOT", str(ws))

        rc = main(["discover"])
        out = capsys.readouterr().out

        assert rc == 0
        assert "alpha" in out
        assert "beta" in out
        assert "2 repo(s) discovered" in out

    def test_discover_honors_runtime_exclude_list(
        self, tmp_path, isolated_state, capsys, monkeypatch
    ):
        from axiom.extensions.builtins.hygiene.cli import main

        ws = tmp_path / "ws"
        _git_init(ws / "alpha")
        _git_init(ws / "private_repo")
        monkeypatch.setenv("AXI_WORKSPACE_ROOT", str(ws))

        exclude_path = isolated_state / "agents" / "tidy" / "exclude_repos.json"
        exclude_path.parent.mkdir(parents=True, exist_ok=True)
        exclude_path.write_text('["private_repo"]')

        rc = main(["discover"])
        out = capsys.readouterr().out

        assert rc == 0
        repo_lines = [
            line for line in out.splitlines() if line.startswith("  ") and "@" in line
        ]
        assert any("alpha" in line for line in repo_lines)
        assert all("private_repo" not in line for line in repo_lines)
        assert "1 repo(s) discovered" in out
        assert "1 excluded by config" in out


class TestProposeApproveDenyCli:
    def test_propose_first_time_returns_ask(self, isolated_state, capsys):
        from axiom.extensions.builtins.hygiene.cli import main

        rc = main(["propose", "local-rag-steward"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "ASK" in out

    def test_approve_then_propose_returns_auto(self, isolated_state, capsys):
        from axiom.extensions.builtins.hygiene.cli import main

        assert main(["approve", "local-rag-steward"]) == 0
        capsys.readouterr()  # drop approve output

        rc = main(["propose", "local-rag-steward"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "AUTO" in out

    def test_deny_then_propose_returns_skip(self, isolated_state, capsys):
        from axiom.extensions.builtins.hygiene.cli import main

        assert main(["deny", "local-rag-steward"]) == 0
        capsys.readouterr()

        rc = main(["propose", "local-rag-steward"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "SKIP" in out

    def test_state_persists_to_disk(self, isolated_state):
        from axiom.extensions.builtins.hygiene.cli import main

        main(["approve", "local-rag-steward"])
        ledger_path = isolated_state / "agents" / "tidy" / "raci_state.json"
        assert ledger_path.exists()
        data = json.loads(ledger_path.read_text())
        assert data["actions"]["local-rag-steward"]["state"] == "approved"
