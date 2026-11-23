# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The tier-1 fixer resolves a merge conflict on a pull request that is not protected.

A conflict is not a failing test: nothing a model can run says it is right. So the
resolution is bounded three ways. It never touches a protected path or a tier-2 pull
request, every marker must be gone from every conflicted file, and the result is a
merge commit on a side branch that the actor applies without a force-push and lands
only when fresh CI is green. About eight pull requests sat for weeks on conflicts
only a person could fix (2026-11-22).
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest

from axiom.extensions.builtins.release.fixes import Diagnosis
from axiom.extensions.builtins.release.lander.model import OpenPull
from axiom.extensions.builtins.release.llm_fixer import LLMFixProvider, load_settings


@dataclass
class Call:
    tool_id: str
    name: str
    input: dict = field(default_factory=dict)


class Scripted:
    def __init__(self, turns):
        self.turns = turns

    def complete_with_tools(self, messages, **kwargs):
        calls = self.turns.pop(0) if self.turns else []
        return SimpleNamespace(success=True, text="", tool_use=calls, error=None)


def _g(cwd: Path, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return done.stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A bare origin, a PR branch ``feat`` and a ``main`` that edit the same line."""
    origin = tmp_path / "origin.git"
    _g(tmp_path, "init", "--bare", "-b", "main", str(origin))
    work = tmp_path / "work"
    _g(tmp_path, "clone", "-q", str(origin), str(work))
    for key, val in (("user.name", "t"), ("user.email", "t@example.com")):
        _g(work, "config", key, val)
    (work / "a.txt").write_text("one\ntwo\nthree\n")
    (work / "src").mkdir()
    (work / "src" / "keep.py").write_text("X = 1\n")
    _g(work, "add", "-A")
    _g(work, "commit", "-qm", "base")
    _g(work, "push", "-q", "origin", "main")
    _g(work, "checkout", "-q", "-b", "feat")
    (work / "a.txt").write_text("one\nfeature two\nthree\n")
    _g(work, "commit", "-qam", "feature")
    _g(work, "push", "-q", "origin", "feat")
    head = _g(work, "rev-parse", "HEAD")
    _g(work, "checkout", "-q", "main")
    (work / "a.txt").write_text("one\nmain two\nthree\n")
    _g(work, "commit", "-qam", "main moves")
    _g(work, "push", "-q", "origin", "main")
    monkeypatch.chdir(work)
    return SimpleNamespace(work=work, origin=origin, head=head)


def _pull(repo, **kw) -> OpenPull:
    base = dict(number=7, title="t", head_sha=repo.head, head_ref="feat", base_ref="main",
                files=("a.txt",), checks="failing", mergeable=False)
    base.update(kw)
    return OpenPull(**base)


def _provider(tmp_path: Path, turns, *, resolve: bool = True, enabled: bool = True) -> LLMFixProvider:
    cfg = tmp_path / "lander.toml"
    cfg.write_text(
        f"[fixer]\nenabled = {str(enabled).lower()}\nresolve_conflicts = {str(resolve).lower()}\n",
        encoding="utf-8",
    )
    provider = LLMFixProvider(completer=Scripted(turns))
    provider.bind(
        config=SimpleNamespace(source=str(cfg), extra_path_rules=None, replaces_human_paths=False, repos=())
    )
    return provider


RESOLVE = [
    [Call("1", "write_file", {"path": "a.txt", "content": "one\nfeature and main two\nthree\n"})],
    [Call("2", "finish", {"fixed": True, "summary": "keep both edits"})],
]
DIAG = Diagnosis("merge_conflict", "not mergeable", ("a.txt",))


def test_cf1_the_setting_defaults_on_and_can_be_turned_off(tmp_path):
    assert load_settings(None).resolve_conflicts is True
    cfg = tmp_path / "c.toml"
    cfg.write_text("[fixer]\nenabled = true\nresolve_conflicts = false\n")
    assert load_settings(cfg).resolve_conflicts is False


def test_cf2_a_conflict_is_resolved_into_a_merge_commit_on_a_side_branch(repo, tmp_path):
    fix = _provider(tmp_path, [list(t) for t in RESOLVE]).draft(DIAG, _pull(repo))
    assert not fix.needs_human, fix.reason
    assert fix.kind == "merge_conflict" and fix.writer == "rivet-llm"
    assert fix.branch.startswith("rivet-fix/7-") and fix.target_ref == "feat"
    side = _g(repo.origin, "rev-parse", fix.branch)
    parents = _g(repo.origin, "rev-list", "--parents", "-n", "1", side).split()[1:]
    assert repo.head in parents and len(parents) == 2  # a merge of main into the head
    assert _g(repo.origin, "show", f"{side}:a.txt") == "one\nfeature and main two\nthree"
    # fast-forward from the pull request's head: no force-push is ever needed
    _g(repo.origin, "merge-base", "--is-ancestor", repo.head, side)


def test_cf3_a_marker_left_behind_is_refused(repo, tmp_path):
    bad = [
        [Call("1", "write_file", {"path": "a.txt", "content": "<<<<<<< HEAD\nx\n=======\ny\n>>>>>>> m\n"})],
        [Call("2", "finish", {"fixed": True, "summary": "done"})],
    ]
    fix = _provider(tmp_path, bad).draft(DIAG, _pull(repo))
    assert fix.needs_human and "marker" in fix.reason


def test_cf4_a_conflicted_file_the_model_never_touched_is_refused(repo, tmp_path):
    nothing = [[Call("1", "finish", {"fixed": True, "summary": "looks fine"})]]
    fix = _provider(tmp_path, nothing).draft(DIAG, _pull(repo))
    assert fix.needs_human


def test_cf5_off_means_the_old_behaviour(repo, tmp_path):
    fix = _provider(tmp_path, [list(t) for t in RESOLVE], resolve=False).draft(DIAG, _pull(repo))
    assert fix.needs_human and fix.reason == "merge_conflict"
    fix = _provider(tmp_path, [list(t) for t in RESOLVE], enabled=False).draft(DIAG, _pull(repo))
    assert fix.needs_human and fix.reason == "merge_conflict"


def test_cf6_a_protected_pull_request_stays_with_a_person(repo, tmp_path):
    fix = _provider(tmp_path, [list(t) for t in RESOLVE]).draft(
        Diagnosis("merge_conflict", "x", ("migrations/001.sql",)),
        _pull(repo, files=("migrations/001.sql",)),
    )
    assert fix.needs_human


def test_cf7_a_tier_two_pull_request_stays_with_a_person(repo, tmp_path, monkeypatch):
    from axiom.extensions.builtins.release import llm_fixer

    class _Map:
        def classify(self, paths):
            return SimpleNamespace(tier=2)

    monkeypatch.setattr(llm_fixer, "_load_tier_map", lambda: _Map())
    fix = _provider(tmp_path, [list(t) for t in RESOLVE]).draft(DIAG, _pull(repo))
    assert fix.needs_human and fix.reason == "merge_conflict"


def test_cf8_a_conflict_in_a_protected_file_is_refused_and_the_merge_aborted(repo, tmp_path):
    # the pull request's own files look harmless, but main's change collides in a workflow
    work = repo.work
    _g(work, "checkout", "-q", "main")
    (work / ".github" / "workflows").mkdir(parents=True)
    (work / ".github" / "workflows" / "ci.yml").write_text("a: 1\n")
    _g(work, "add", "-A")
    _g(work, "commit", "-qm", "workflow")
    _g(work, "checkout", "-q", "feat")
    (work / ".github" / "workflows").mkdir(parents=True, exist_ok=True)
    (work / ".github" / "workflows" / "ci.yml").write_text("a: 2\n")
    _g(work, "add", "-A")
    _g(work, "commit", "-qm", "feat workflow")
    _g(work, "push", "-q", "origin", "feat")
    head = _g(work, "rev-parse", "HEAD")
    _g(work, "checkout", "-q", "main")
    (work / ".github" / "workflows" / "ci.yml").write_text("a: 3\n")
    _g(work, "commit", "-qam", "main workflow")
    _g(work, "push", "-q", "origin", "main")
    fix = _provider(tmp_path, [list(t) for t in RESOLVE]).draft(
        DIAG, _pull(repo, head_sha=head, files=("a.txt",))
    )
    assert fix.needs_human


def test_cf9_a_shallow_checkout_is_deepened_before_the_merge(tmp_path):
    """The lander's checkout is depth 1, so the base and the head share no visible history.
    The fixer fetches full history only when it is about to resolve a conflict."""
    calls: list[list[str]] = []

    def git(args, cwd=None):
        calls.append(list(args))
        return "true\n" if args[:2] == ["rev-parse", "--is-shallow-repository"] else ""

    provider = LLMFixProvider(completer=Scripted([]), git=git)
    provider._ensure_history()
    assert ["fetch", "--quiet", "--unshallow", "origin"] in calls

    calls.clear()

    def git_full(args, cwd=None):
        calls.append(list(args))
        return "false\n"

    LLMFixProvider(completer=Scripted([]), git=git_full)._ensure_history()
    assert not any("--unshallow" in c for c in calls)
