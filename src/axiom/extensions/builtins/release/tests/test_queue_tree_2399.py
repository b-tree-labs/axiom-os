# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The tree-already-tested decision, against real git repositories (#2399, slice 3)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from axiom.extensions.builtins.release import queue_tree
from axiom.extensions.builtins.release.queue_tree import FULL_PATH_JOBS, judge, tree_of


def _git(cwd: Path, *args: str) -> str:
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
        "HOME": str(cwd),
        "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
    }
    return subprocess.run(
        ["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit(repo: Path, name: str, text: str, msg: str) -> str:
    (repo / name).write_text(text)
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", msg)
    return _git(repo, "rev-parse", "HEAD")


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _commit(repo, "a.txt", "a\n", "base")
    return repo


def _jobs(**overrides: str) -> list[dict]:
    return [
        {
            "name": f"{n} (3.13)" if n == "Unit Tests" else n,
            "conclusion": overrides.get(n, "success"),
        }
        for n in FULL_PATH_JOBS
    ]


def _run(
    tree: str,
    *,
    branch="rivet-queue/7",
    event="push",
    conclusion="success",
    status="completed",
    run_id=11,
) -> dict:
    return {
        "id": run_id,
        "event": event,
        "head_branch": branch,
        "status": status,
        "conclusion": conclusion,
        "head_commit": {"tree_id": tree},
    }


def test_fast_forward_keeps_the_tree_and_is_judged_tested(tmp_path):
    repo = _repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "rivet-queue/7")
    queue_sha = _commit(repo, "b.txt", "b\n", "member")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "--ff-only", "rivet-queue/7")
    main_sha = _git(repo, "rev-parse", "HEAD")
    assert main_sha == queue_sha
    verdict = judge(
        tree_of(main_sha, cwd=str(repo)),
        [_run(tree_of(queue_sha, cwd=str(repo)))],
        lambda _id: _jobs(),
    )
    assert verdict.skip and verdict.run_id == 11


def test_a_merge_commit_has_a_new_sha_and_the_same_tree(tmp_path):
    repo = _repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "rivet-queue/7")
    queue_sha = _commit(repo, "b.txt", "b\n", "member")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "--no-ff", "-m", "merge", "rivet-queue/7")
    main_sha = _git(repo, "rev-parse", "HEAD")
    assert main_sha != queue_sha
    tree = tree_of(main_sha, cwd=str(repo))
    assert tree == tree_of(queue_sha, cwd=str(repo))
    assert judge(tree, [_run(tree)], lambda _id: _jobs()).skip


def test_a_different_tree_is_not_skipped(tmp_path):
    repo = _repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "rivet-queue/7")
    queue_sha = _commit(repo, "b.txt", "b\n", "member")
    _git(repo, "checkout", "-q", "main")
    main_sha = _commit(repo, "c.txt", "c\n", "something else landed first")
    verdict = judge(
        tree_of(main_sha, cwd=str(repo)),
        [_run(tree_of(queue_sha, cwd=str(repo)))],
        lambda _id: _jobs(),
    )
    assert not verdict.skip and verdict.reason == "no_queue_run_for_tree"


def test_a_queue_run_that_was_not_the_full_path_is_not_trusted():
    for skipped in FULL_PATH_JOBS:
        verdict = judge("t1", [_run("t1")], lambda _id, s=skipped: _jobs(**{s: "skipped"}))
        assert not verdict.skip and skipped in verdict.reason
    missing = [j for j in _jobs() if not j["name"].startswith("Migration")]
    assert not judge("t1", [_run("t1")], lambda _id: missing).skip


def test_a_failed_or_unfinished_queue_run_is_not_trusted():
    assert not judge("t1", [_run("t1", conclusion="failure")], lambda _id: _jobs()).skip
    assert not judge(
        "t1", [_run("t1", status="in_progress", conclusion=None)], lambda _id: _jobs()
    ).skip


def test_only_a_push_to_a_queue_branch_counts():
    assert not judge("t1", [_run("t1", branch="main")], lambda _id: _jobs()).skip
    assert not judge("t1", [_run("t1", event="merge_group")], lambda _id: _jobs()).skip
    assert not judge("t1", [_run("t1", branch="feature/rivet-queue/7")], lambda _id: _jobs()).skip


def test_every_doubt_fails_closed():
    assert not judge("", [_run("t1")], lambda _id: _jobs()).skip
    assert not judge("t1", None, lambda _id: _jobs()).skip
    assert not judge("t1", [_run("t1")], lambda _id: None).skip

    def boom(_id):
        raise OSError("network")

    assert not judge("t1", [_run("t1")], boom).skip
    assert not judge("t1", ["junk", {}], lambda _id: _jobs()).skip


def test_a_later_green_run_is_found_after_a_red_one_for_the_same_tree():
    runs = [_run("t1", conclusion="failure", run_id=1), _run("t1", run_id=2)]
    assert judge("t1", runs, lambda _id: _jobs()).run_id == 2


def test_the_command_prints_its_decision_and_fails_closed_without_the_forge(capsys):
    def get(path):
        if "/jobs" in path:
            return {"jobs": _jobs()}
        return {"workflow_runs": [_run("t1")]}

    assert queue_tree.main(["--repo", "o/r", "--sha", "x", "--tree", "t1"], get=get) == 0
    out = capsys.readouterr().out
    assert "skip=true" in out and "queue_run=11" in out

    def down(path):
        raise OSError("offline")

    assert queue_tree.main(["--repo", "o/r", "--sha", "x", "--tree", "t1"], get=down) == 0
    assert "skip=false" in capsys.readouterr().out


def test_the_full_path_jobs_are_real_job_names_in_ci_yml():
    import yaml

    root = Path(__file__).resolve().parents[6]
    jobs = yaml.safe_load((root / ".github/workflows/ci.yml").read_text())["jobs"]
    names = [str(j.get("name", "")) for j in jobs.values()]
    for needed in FULL_PATH_JOBS:
        assert any(n.startswith(needed) for n in names), needed
