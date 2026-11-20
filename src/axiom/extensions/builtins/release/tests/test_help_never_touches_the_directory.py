# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``release --help`` prints help and does nothing else (#709).

The repo guard ran ahead of argument parsing, so asking for help in a folder
that is not a repository went through the "initialize one here?" path: once it
created a repository unasked, and today it still answers a help request with
"not a git repository" and exit 1, or, on a terminal, offers to run
``git init``. Help must never consult the guard.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.release import cli


@pytest.fixture
def not_a_repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    consulted: list[tuple] = []

    def guard(*args, **kwargs):
        consulted.append(args)
        return False

    monkeypatch.setattr(cli, "git_available", lambda: True)
    monkeypatch.setattr(cli, "ensure_repo_or_offer_init", guard)
    return tmp_path, consulted


@pytest.mark.parametrize("argv", [
    ["--help"], ["-h"], ["cut", "--help"], ["status", "-h"],
])
def test_help_prints_and_never_runs_the_repo_guard(not_a_repo, capsys, argv):
    folder, consulted = not_a_repo

    with pytest.raises(SystemExit) as exit_info:
        cli.main(argv)

    assert exit_info.value.code == 0
    assert "usage:" in capsys.readouterr().out
    assert consulted == [], "help consulted the repo guard"
    assert list(folder.iterdir()) == []
