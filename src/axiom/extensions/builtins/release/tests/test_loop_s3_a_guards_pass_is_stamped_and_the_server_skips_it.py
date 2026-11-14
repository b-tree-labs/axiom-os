# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""LOOP-S3, CI cost: the server does not repeat guards the pre-push hook already passed.

The hook stamps a pass as a git note on the pushed commit, ``Guards-Passed: <tree>
<guards-version>``, only when every guard passed. ``loop-guards.yml`` skips the guards when
the pushed commit's tree carries a stamp for the current guards version, and runs them for
any other push: no stamp (the hook was skipped, or GitHub made the commit with a tree
nobody stamped) or a stamp for other guards. A forged stamp gains nothing: the automatic
revert still judges main's real CI. Real git repositories; the guard runner is a stand-in.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from axiom.extensions.builtins.hygiene._git_isolation import git_isolated_env
from axiom.extensions.builtins.release import guards

pytestmark = pytest.mark.acceptance("LOOP-S3")

REPO_ROOT = Path(__file__).resolve().parents[6]


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, env=git_isolated_env(), capture_output=True, text=True,
                          check=True).stdout.strip()


def _repo(tmp_path: Path, name: str = "repo") -> Path:
    root = tmp_path / name
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "commit.gpgsign", "false")
    (root / "README.md").write_text("seed\n")
    _git(root, "add", "README.md")
    _git(root, "commit", "-q", "-m", "seed")
    return root


def _runner(rc: int):
    def run(args, **_kw):
        return subprocess.CompletedProcess(args, rc, stdout="" if rc == 0 else "FAILED tests/x.py", stderr="")
    return run


def test_the_hook_stamps_the_tree_only_when_every_guard_passed(tmp_path):
    root = _repo(tmp_path)
    failed = guards.run_guards(root, base="HEAD", head="HEAD", runner=_runner(1), stamp=True)
    assert not failed.ok and guards.find_stamp(root, "HEAD") is None, "a failed run writes no stamp"
    passed = guards.run_guards(root, base="HEAD", head="HEAD", runner=_runner(0), stamp=True)
    assert passed.ok
    tree = _git(root, "rev-parse", "HEAD^{tree}")
    assert guards.find_stamp(root, "HEAD") == f"Guards-Passed: {tree} {guards.guards_version(root)}"


def test_the_server_skips_a_stamped_tree_and_runs_every_other_push(tmp_path):
    root = _repo(tmp_path)
    guards.run_guards(root, base="HEAD", head="HEAD", runner=_runner(0), stamp=True)
    # A commit GitHub makes with the same tree (a squash of an up-to-date branch) is covered.
    same_tree = _git(root, "commit-tree", "HEAD^{tree}", "-p", "HEAD", "-m", "squash")
    assert guards.find_stamp(root, same_tree) is not None
    # A tree nobody stamped (the hook was skipped) is not.
    (root / "x.txt").write_text("unstamped\n")
    _git(root, "add", "x.txt")
    _git(root, "commit", "-q", "-m", "pushed with the hook skipped")
    assert guards.find_stamp(root, "HEAD") is None
    # A stamp made by other guards is not either.
    _git(root, "notes", f"--ref={guards.NOTES_REF}", "add", "-f", "-m",
         f"Guards-Passed: {_git(root, 'rev-parse', 'HEAD^{tree}')} not-these-guards", "HEAD")
    assert guards.find_stamp(root, "HEAD") is None
    assert guards.main(["--stamped", "HEAD", "--root", str(root)]) == 1, "the workflow's check: run the guards"
    assert guards.main(["--stamped", same_tree, "--root", str(root)]) == 0, "the workflow's check: skip"


def test_the_hook_pushes_the_stamp_and_the_server_reads_it_before_installing_anything():
    hook = (REPO_ROOT / "scripts" / "hooks" / "pre-push").read_text()
    assert "guards --stamp" in hook and guards.NOTES_REF in hook
    assert "refs/notes/" in hook.split("PUSH_INPUT=", 1)[1].split("CURRENT_BRANCH", 1)[0], \
        "a push of the notes ref alone returns before the suite"
    wf = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "loop-guards.yml").read_text())
    steps = wf["jobs"]["guards"]["steps"]
    names = [s.get("id") or s.get("name") for s in steps]
    stamp_at = names.index("stamp")
    assert guards.NOTES_REF in steps[stamp_at]["run"] and "--stamped" in steps[stamp_at]["run"]
    for step in steps[stamp_at + 1:]:
        assert step.get("if") == "steps.stamp.outputs.skip != 'true'", step.get("name")
