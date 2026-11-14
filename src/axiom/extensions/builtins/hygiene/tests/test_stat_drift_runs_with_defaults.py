# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""`hygiene stat drift` runs when its optional flags are left out (#682).

The CLI drops every argument whose value is None before handing the rest to
the skill, and the skill rebuilt an ``argparse.Namespace`` from what was left.
A legacy handler then read ``args.workspace`` (default None, so dropped) and
died with AttributeError: once with no output at all and exit 1, later with
only the exception's one line. Any legacy verb reading an optional flag it was
not given failed the same way.
"""
from __future__ import annotations

import subprocess

from axiom.extensions.builtins.hygiene import cli


def _repo(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    return tmp_path


def test_stat_drift_with_no_flags_runs(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(_repo(tmp_path))
    rc = cli.main(["stat", "drift"])
    out = capsys.readouterr().out
    assert "AttributeError" not in out
    assert rc == 0, out
    assert "No drifting worktrees found." in out


def test_stat_drift_with_only_workspace_runs(tmp_path, capsys):
    _repo(tmp_path / "one")
    rc = cli.main(["stat", "drift", "--workspace", str(tmp_path)])
    out = capsys.readouterr().out
    assert "AttributeError" not in out
    assert rc == 0, out
