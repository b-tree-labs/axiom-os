"""Smoke tests for git hooks under scripts/hooks/.

Covers the override-reason gate added 2026-06-01:

  1. commit-msg stamps a Bypass-Reason trailer when AXI_OVERRIDE_REASON is set
     and leaves the message untouched otherwise (and when a trailer already
     exists).
  2. pre-push refuses when main is red + no trailer + no override env var.
  3. pre-push accepts (and amends a trailer onto HEAD) when AXI_OVERRIDE_REASON
     is set at push time.

The tests stub out the `gh` CLI by prepending a tempdir with a fake `gh` shim
to PATH so the hook sees a configurable origin/main status without hitting the
network. AXI_PRE_PUSH_SKIP_CHECKS=1 short-circuits the ruff/pytest sections so
these stay tight smoke tests.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import textwrap
from pathlib import Path

import pytest

from axiom.extensions.builtins.hygiene._git_isolation import git_isolated_env

REPO_ROOT = Path(__file__).resolve().parents[2]
PRE_PUSH = REPO_ROOT / "scripts" / "hooks" / "pre-push"
COMMIT_MSG = REPO_ROOT / "scripts" / "hooks" / "commit-msg"


def _git(cwd: Path, *args: str, env: dict | None = None, input_: str | None = None):
    # Default to a git-isolated env (strips inherited GIT_* and pins config to
    # /dev/null). Without this, running under the pre-push hook inherits GIT_DIR
    # etc. from `git push`, which overrides cwd= and lands these fixture commits
    # on the real repo (the recurring "seed"/Test pollution).
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=env if env is not None else git_isolated_env(),
        input=input_,
        capture_output=True,
        text=True,
        check=False,
    )


def _make_gh_shim(dir_: Path, conclusion: str) -> None:
    """Create a fake `gh` CLI on PATH that echoes a configurable conclusion."""
    shim = dir_ / "gh"
    shim.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env bash
            # Fake gh shim for hook tests. Always reports conclusion={conclusion!r}.
            if [ "$1" = "run" ] && [ "$2" = "list" ]; then
              echo {conclusion!r}
              exit 0
            fi
            exit 0
            """
        )
    )
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("seed\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "seed")
    return repo


def _install_hooks(repo: Path) -> None:
    hooks_dir = repo / ".git" / "hooks"
    hooks_dir.mkdir(exist_ok=True)
    shutil.copy(PRE_PUSH, hooks_dir / "pre-push")
    shutil.copy(COMMIT_MSG, hooks_dir / "commit-msg")
    (hooks_dir / "pre-push").chmod(0o755)
    (hooks_dir / "commit-msg").chmod(0o755)


def _env(tmp_home: Path, bin_dir: Path, extra: dict | None = None) -> dict:
    # Start from a git-isolated env so the hook-test bodies can't escape tmp_path
    # via inherited GIT_* either (see _git).
    e = git_isolated_env()
    e["HOME"] = str(tmp_home)
    e["PATH"] = f"{bin_dir}:{e['PATH']}"
    # Short-circuit ruff/pytest from the pre-push hook — we're testing the gate.
    e["AXI_PRE_PUSH_SKIP_CHECKS"] = "1"
    # Don't inherit a venv reference from the test runner.
    e.pop("VIRTUAL_ENV", None)
    if extra:
        e.update(extra)
    return e


# ──────────────────────────────────────────────────────────────────────
# commit-msg hook
# ──────────────────────────────────────────────────────────────────────

def test_commit_msg_stamps_trailer_when_env_set(tmp_path: Path):
    repo = _init_repo(tmp_path)
    _install_hooks(repo)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _make_gh_shim(bin_dir, "success")
    env = _env(tmp_path / "home", bin_dir, {"AXI_OVERRIDE_REASON": "fixing CI"})

    (repo / "a.txt").write_text("a\n")
    _git(repo, "add", "a.txt", env=env)
    r = _git(repo, "commit", "-m", "add a", env=env)
    assert r.returncode == 0, r.stderr

    msg = _git(repo, "log", "-1", "--format=%B", env=env).stdout
    assert "Bypass-Reason: fixing CI" in msg, msg


def test_commit_msg_noop_when_env_unset(tmp_path: Path):
    repo = _init_repo(tmp_path)
    _install_hooks(repo)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _make_gh_shim(bin_dir, "success")
    env = _env(tmp_path / "home", bin_dir)

    (repo / "a.txt").write_text("a\n")
    _git(repo, "add", "a.txt", env=env)
    _git(repo, "commit", "-m", "add a", env=env)
    msg = _git(repo, "log", "-1", "--format=%B", env=env).stdout
    assert "Bypass-Reason" not in msg


def test_commit_msg_idempotent_when_trailer_already_present(tmp_path: Path):
    repo = _init_repo(tmp_path)
    _install_hooks(repo)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _make_gh_shim(bin_dir, "success")
    env = _env(tmp_path / "home", bin_dir, {"AXI_OVERRIDE_REASON": "second reason"})

    (repo / "a.txt").write_text("a\n")
    _git(repo, "add", "a.txt", env=env)
    _git(
        repo,
        "commit",
        "-m",
        "add a\n\nBypass-Reason: original reason\n",
        env=env,
    )
    msg = _git(repo, "log", "-1", "--format=%B", env=env).stdout
    # Existing trailer preserved; no second trailer injected.
    assert msg.count("Bypass-Reason:") == 1
    assert "original reason" in msg


# ──────────────────────────────────────────────────────────────────────
# pre-push hook
# ──────────────────────────────────────────────────────────────────────

def _make_remote(tmp_path: Path, source_repo: Path) -> Path:
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", "-q", "-b", "main", str(remote)],
        check=True,
    )
    _git(source_repo, "remote", "add", "origin", str(remote))
    _git(source_repo, "push", "-q", "origin", "main")
    return remote


def test_pre_push_refuses_when_main_red_and_no_trailer(tmp_path: Path):
    repo = _init_repo(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _make_gh_shim(bin_dir, "failure")  # ← red
    env = _env(tmp_path / "home", bin_dir)
    _make_remote(tmp_path, repo)
    _install_hooks(repo)

    _git(repo, "checkout", "-q", "-b", "feature", env=env)
    (repo / "b.txt").write_text("b\n")
    _git(repo, "add", "b.txt", env=env)
    _git(repo, "commit", "-m", "no trailer here", env=env)

    r = _git(repo, "push", "origin", "feature", env=env)
    assert r.returncode != 0, "expected push to be refused"
    combined = r.stdout + r.stderr
    assert "Bypass-Reason" in combined
    # Audit log written.
    log = tmp_path / "home" / ".axi" / "pre-push-bypass.log"
    assert log.exists(), "audit log should have been written"
    assert "refused-no-trailer" in log.read_text()


def test_pre_push_accepts_when_override_env_set_stamps_trailer(tmp_path: Path):
    repo = _init_repo(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _make_gh_shim(bin_dir, "failure")
    _make_remote(tmp_path, repo)
    _install_hooks(repo)

    base_env = _env(tmp_path / "home", bin_dir)
    _git(repo, "checkout", "-q", "-b", "feature", env=base_env)
    (repo / "b.txt").write_text("b\n")
    _git(repo, "add", "b.txt", env=base_env)
    # Commit WITHOUT the env var first — no trailer on the commit.
    _git(repo, "commit", "-m", "feature work", env=base_env)

    # Now push WITH the env var — pre-push amends the trailer onto HEAD.
    push_env = _env(
        tmp_path / "home",
        bin_dir,
        {"AXI_OVERRIDE_REASON": "hardening the very hook"},
    )
    r = _git(repo, "push", "origin", "feature", env=push_env)
    assert r.returncode == 0, r.stdout + r.stderr
    msg = _git(repo, "log", "-1", "--format=%B", env=push_env).stdout
    assert "Bypass-Reason: hardening the very hook" in msg
    log = tmp_path / "home" / ".axi" / "pre-push-bypass.log"
    assert log.exists()
    assert "hardening the very hook" in log.read_text()


def test_pre_push_passes_when_main_green(tmp_path: Path):
    repo = _init_repo(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _make_gh_shim(bin_dir, "success")
    _make_remote(tmp_path, repo)
    _install_hooks(repo)

    env = _env(tmp_path / "home", bin_dir)
    _git(repo, "checkout", "-q", "-b", "feature", env=env)
    (repo / "b.txt").write_text("b\n")
    _git(repo, "add", "b.txt", env=env)
    _git(repo, "commit", "-m", "feature work", env=env)
    r = _git(repo, "push", "origin", "feature", env=env)
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.parametrize("hook", [PRE_PUSH, COMMIT_MSG])
def test_hook_files_are_executable(hook: Path):
    assert hook.exists(), f"{hook} missing"
    assert os.access(hook, os.X_OK), f"{hook} not executable"
