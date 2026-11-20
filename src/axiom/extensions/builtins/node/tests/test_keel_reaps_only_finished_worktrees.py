# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""KEEL removes a finished worktree, and nothing else, with real git repos."""

from __future__ import annotations

import subprocess
import tarfile
import time
from pathlib import Path

import pytest

from axiom.extensions.builtins.node import reap
from axiom.extensions.builtins.node.skills import vitals

LATER = 10 * 86400  # "now" far enough ahead that the age floor is met


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=T", "-c", "user.email=t@example.invalid",
         "-c", "commit.gpgsign=false", *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    repo = root / "repo"
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    (repo / ".gitignore").write_text(".env\nlocal-data/\n.venv/\n__pycache__/\n")
    (repo / "a.txt").write_text("a\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    return root


def add_wt(ws: Path, name: str) -> Path:
    wt = ws / name
    git(ws / "repo", "worktree", "add", "-q", "-b", f"feat/{name}", str(wt), "main")
    return wt


def run(ws, tmp_path, *, apply=True, cwds=(), **cfg):
    state = tmp_path / "state"
    c = reap.ReapConfig(**cfg)
    return reap.reap(
        ws, state, c, apply=apply, cwds=list(cwds), venvs=[tmp_path / "no-venv"],
        now=time.time() + LATER,
    ), state


def reasons(out):
    return {Path(s["path"]).name: s["reason"] for s in out["skipped"]}


def test_a_finished_clean_worktree_goes_and_its_branch_stays(ws, tmp_path):
    wt = add_wt(ws, "done")
    out, _ = run(ws, tmp_path)
    assert [Path(r["path"]).name for r in out["removed"]] == ["done"]
    assert not wt.exists()
    assert "feat/done" in git(ws / "repo", "branch", "--list", "feat/done")
    assert (ws / "repo").exists()  # the base checkout is never a candidate


def test_plan_changes_nothing_and_names_what_it_would_do(ws, tmp_path):
    wt = add_wt(ws, "done")
    out, _ = run(ws, tmp_path, apply=False)
    assert out["mode"] == "plan" and wt.exists()
    assert [Path(w["path"]).name for w in out["would_remove"]] == ["done"]
    assert reasons(out)["repo"] == "base checkout"


def test_uncommitted_and_untracked_work_survives(ws, tmp_path):
    a, b = add_wt(ws, "edited"), add_wt(ws, "untracked")
    (a / "a.txt").write_text("changed\n")
    (b / "new.txt").write_text("x\n")
    out, _ = run(ws, tmp_path)
    assert out["removed"] == [] and a.exists() and b.exists()
    assert "uncommitted" in reasons(out)["edited"] and "uncommitted" in reasons(out)["untracked"]


def test_unpushed_commits_survive(ws, tmp_path):
    wt = add_wt(ws, "ahead")
    (wt / "b.txt").write_text("b\n")
    git(wt, "add", "-A")
    git(wt, "commit", "-q", "-m", "unmerged work")
    out, _ = run(ws, tmp_path)
    assert wt.exists() and "not on the default branch" in reasons(out)["ahead"]


def test_a_process_inside_keeps_the_worktree(ws, tmp_path):
    wt = add_wt(ws, "busy")
    p = subprocess.Popen(["sleep", "60"], cwd=wt)
    try:
        time.sleep(0.3)
        state = tmp_path / "state"
        out = reap.reap(ws, state, reap.ReapConfig(), apply=True, venvs=[tmp_path / "no-venv"],
                        now=time.time() + LATER)  # real process table
    finally:
        p.kill()
        p.wait()
    assert wt.exists() and "process" in reasons(out)["busy"]


def test_not_knowing_who_is_inside_removes_nothing(ws, tmp_path):
    wt = add_wt(ws, "unknown")
    out = reap.reap(
        ws, tmp_path / "state", reap.ReapConfig(), apply=True, cwds=None,
        venvs=[tmp_path / "no-venv"], now=time.time() + LATER,
    )
    assert wt.exists() and "fail closed" in reasons(out)["unknown"]


def test_locked_and_recent_worktrees_survive(ws, tmp_path):
    locked, fresh = add_wt(ws, "locked"), add_wt(ws, "fresh")
    git(ws / "repo", "worktree", "lock", str(locked))
    state = tmp_path / "state"
    out = reap.reap(ws, state, reap.ReapConfig(), apply=True, cwds=[],
                    venvs=[tmp_path / "no-venv"], now=time.time())  # nothing is 24h old
    assert locked.exists() and fresh.exists()
    assert reasons(out)["locked"] == "locked" and "active" in reasons(out)["fresh"]


def test_an_editable_install_floor_holds(ws, tmp_path, monkeypatch):
    wt = add_wt(ws, "bound")
    monkeypatch.setattr(reap.wtmod, "editable_bindings", lambda p, v=None: [("venv", "pkg")])
    out, _ = run(ws, tmp_path)
    assert wt.exists() and "editable install" in reasons(out)["bound"]


def test_ignored_state_is_archived_recoverable_and_caches_are_not(ws, tmp_path):
    wt = add_wt(ws, "stateful")
    (wt / ".env").write_text("SECRET=1\n")
    (wt / "local-data").mkdir()
    (wt / "local-data" / "x.csv").write_text("1,2\n")
    (wt / ".venv").mkdir()
    (wt / ".venv" / "big").write_text("cache")
    (wt / "__pycache__").mkdir()
    (wt / "__pycache__" / "m.pyc").write_text("c")
    out, state = run(ws, tmp_path)
    (r,) = out["removed"]
    assert not wt.exists()
    assert sorted(r["archived_paths"]) == [".env", "local-data"]
    manifest = (state / reap.ARCHIVE_DIR / reap.MANIFEST).read_text().splitlines()
    assert len(manifest) == 1 and r["archive"] in manifest[0]
    dest = tmp_path / "restore"
    with tarfile.open(r["archive"]) as tf:
        tf.extractall(dest)  # noqa: S202 - our own archive in a temp dir
        assert not any(".venv" in n or "pycache" in n for n in tf.getnames())
    assert (dest / ".env").read_text() == "SECRET=1\n"
    assert (dest / "local-data" / "x.csv").read_text() == "1,2\n"


def test_a_failed_archive_blocks_removal_and_keeps_the_state(ws, tmp_path, monkeypatch):
    wt = add_wt(ws, "stateful")
    (wt / ".env").write_text("SECRET=1\n")

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(reap.tarfile, "open", boom)
    out, _ = run(ws, tmp_path)
    assert out["removed"] == [] and "archive failed" in out["failed"][0]["reason"]
    assert wt.exists() and (wt / ".env").read_text() == "SECRET=1\n"


def test_the_batch_limits_a_run(ws, tmp_path):
    for n in ("one", "two", "three"):
        add_wt(ws, n)
    out, _ = run(ws, tmp_path, batch=2)
    assert len(out["removed"]) == 2 and out["deferred"] == 1


def test_archives_expire_by_retention(ws, tmp_path):
    wt = add_wt(ws, "stateful")
    (wt / ".env").write_text("S\n")
    out, state = run(ws, tmp_path, retention_days=1)
    archive = Path(out["removed"][0]["archive"])
    assert archive.exists()
    assert reap.expire_archives(state, now=time.time() + 20 * 86400) == [str(archive)]
    assert not archive.exists()
    assert (state / reap.ARCHIVE_DIR / reap.MANIFEST).read_text() == ""


def test_heartbeat_reaps_only_on_an_even_keel(monkeypatch):
    calls = []

    class R:
        value = {"removed": []}
        actions_taken = ["removed 0 finished worktree(s); branches kept"]

    from axiom.extensions.builtins.node.skills import reap_worktrees

    monkeypatch.setattr(reap_worktrees, "run", lambda p, c=None: calls.append(p) or R())
    disks = [{"mount": "/", "state": "ok"}]
    for verdict, ds in (("pressure", disks), ("critical", disks), ("even keel", [])):
        v = {"verdict": verdict, "disks": ds}
        vitals._reap_on_heartbeat(None, v, [])
        assert v["reap"].startswith("skipped")
    assert calls == []
    v = {"verdict": "even keel", "disks": disks}
    vitals._reap_on_heartbeat(None, v, [])
    assert calls == [{"apply": True, "heartbeat": True}] and v["reap"] == {"removed": []}
