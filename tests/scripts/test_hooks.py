"""Smoke tests for git hooks under scripts/hooks/.

Covers the override-reason gate added 2026-06-01:

  1. commit-msg stamps a Bypass-Reason trailer when AXI_OVERRIDE_REASON is set
     and leaves the message untouched otherwise (and when a trailer already
     exists).
  2. pre-push refuses when main is red + no trailer + no override env var.
  3. pre-push accepts (and amends a trailer onto HEAD) when AXI_OVERRIDE_REASON
     is set at push time.

And the suite invocation, which is what keeps a push from running serially:
workers on both the full run and a large selection, and the full-suite
fallback whenever selection cannot decide.

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


def _isolated_env() -> dict:
    """git-isolated env with the hooks' own control variable stripped.

    ``git_isolated_env()`` strips inherited ``GIT_*`` but otherwise copies the
    developer's environment — including ``AXI_OVERRIDE_REASON`` if the shell
    running pytest has it exported (e.g. left over from a legitimate red-main
    push earlier in the day). These tests exercise the very hooks that
    variable controls, so inheriting it INVERTS the refusal tests: commit-msg
    stamps the trailer nobody asked for, pre-push accepts what the test
    expects it to refuse. Every spawned git gets this env; a test that wants
    the variable set passes it explicitly via ``_env(..., extra=...)``.
    """
    e = git_isolated_env()
    e.pop("AXI_OVERRIDE_REASON", None)
    return e


def _git(cwd: Path, *args: str, env: dict | None = None, input_: str | None = None):
    # Default to a git-isolated env (strips inherited GIT_* and pins config to
    # /dev/null). Without this, running under the pre-push hook inherits GIT_DIR
    # etc. from `git push`, which overrides cwd= and lands these fixture commits
    # on the real repo (the recurring "seed"/Test pollution).
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=env if env is not None else _isolated_env(),
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
    # via inherited GIT_* either (see _git). AXI_OVERRIDE_REASON is stripped
    # there too — a test that wants it set says so through `extra`.
    e = _isolated_env()
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


def test_refusal_survives_override_reason_exported_in_the_runners_shell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Negative control: the isolation itself, proven able to fail.

    Export AXI_OVERRIDE_REASON in the process running the tests — exactly the
    developer-shell leak — and assert the red-main refusal STILL happens in
    the isolated subprocess. Before ``_isolated_env`` stripped the variable,
    this scenario inverted three tests in this file: the commit-msg hook
    stamped the trailer nobody asked for and pre-push happily accepted.
    """
    monkeypatch.setenv("AXI_OVERRIDE_REASON", "leaked from the developer's shell")

    repo = _init_repo(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _make_gh_shim(bin_dir, "failure")  # ← red main
    env = _env(tmp_path / "home", bin_dir)
    assert "AXI_OVERRIDE_REASON" not in env
    _make_remote(tmp_path, repo)
    _install_hooks(repo)

    _git(repo, "checkout", "-q", "-b", "feature", env=env)
    (repo / "b.txt").write_text("b\n")
    _git(repo, "add", "b.txt", env=env)
    _git(repo, "commit", "-m", "no trailer here", env=env)

    msg = _git(repo, "log", "-1", "--format=%B", env=env).stdout
    assert "Bypass-Reason" not in msg, (
        "the exported variable leaked into the commit-msg hook"
    )

    r = _git(repo, "push", "origin", "feature", env=env)
    assert r.returncode != 0, (
        "expected refusal: the exported variable leaked into the pre-push hook"
    )
    assert "Bypass-Reason" in r.stdout + r.stderr


# ──────────────────────────────────────────────────────────────────────
# pre-push: the unit-suite invocation
# ──────────────────────────────────────────────────────────────────────

_RECORDING_PYTHON = """\
#!/usr/bin/env bash
# Stands in for the venv's python: records each call, runs nothing. A real
# python here would run the real suite. Importing anything succeeds, so the
# hook sees pytest-testmon as installed, which is what every laptop has.
# AXI_TEST_MAP_COMMIT and AXI_TEST_DECISION play scripts/testmon_blast_radius.py.
printf '%s\\n' "$*" >> "$AXI_TEST_PY_LOG"
case "$*" in
  *throwaway_postgres.py\\ create*) echo "postgresql://hook.invalid/axiom_hook_test" ;;
  *--docs-only*) exit 1 ;;
  *--map-commit*) echo "${AXI_TEST_MAP_COMMIT:-}" ;;
  *--decide*) printf '%b\\n' "${AXI_TEST_DECISION:-}" ;;
esac
exit 0
"""


def _push_recording_python(
    tmp_path: Path, *, conclusion: str = "success", extra: dict | None = None, testmon_map: bool = False
) -> tuple[subprocess.CompletedProcess, list[list[str]]]:
    """Push a feature branch through the real hook; return the suite commands.

    The cheap-guard and collect-only pytest calls carry no ``--timeout``, so
    the suite is the pytest call that does.
    """
    repo = _init_repo(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _make_gh_shim(bin_dir, conclusion)
    _make_remote(tmp_path, repo)
    _install_hooks(repo)
    if testmon_map:
        (repo / ".testmondata").write_text("map")

    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    py = venv / "bin" / "python"
    py.write_text(_RECORDING_PYTHON)
    py.chmod(0o755)
    py_log = tmp_path / "python-calls.log"
    py_log.touch()

    env = _env(
        tmp_path / "home",
        bin_dir,
        {
            "AXI_PRE_PUSH_SKIP_CHECKS": "0",
            "VIRTUAL_ENV": str(venv),
            "AXI_TEST_PY_LOG": str(py_log),
        },
    )
    env.pop("AXI_HOOK_WORKERS", None)
    env.pop("AXI_HOOK_FULL", None)
    env.update(extra or {})

    _git(repo, "checkout", "-q", "-b", "feature", env=env)
    (repo / "b.txt").write_text("b\n")
    _git(repo, "add", "b.txt", env=env)
    _git(repo, "commit", "-m", "feature work", env=env)
    result = _git(repo, "push", "origin", "feature", env=env)

    calls = [line.split() for line in py_log.read_text().splitlines()]
    return result, [c for c in calls if c[:2] == ["-m", "pytest"] and "--timeout=90" in c]


SUITE_ARGS = ["-m", "pytest", "--tb=short", "-q", "--no-header", "--timeout=90"]
FULL_RUN = [*SUITE_ARGS, "-n", "4", "--testmon-noselect"]


def test_pre_push_falls_back_to_the_full_suite_in_parallel_with_no_map(tmp_path: Path):
    """No testmon map means no selection: every test, on workers, map refreshed."""
    result, suites = _push_recording_python(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert suites == [FULL_RUN]


def test_pre_push_axi_hook_full_runs_every_test_in_parallel(tmp_path: Path):
    _, suites = _push_recording_python(tmp_path, extra={"AXI_HOOK_FULL": "1"}, testmon_map=True)
    assert suites == [FULL_RUN]


def test_pre_push_falls_back_when_the_decision_is_not_a_selection(tmp_path: Path):
    """An empty or failed decision must not narrow the run."""
    _, suites = _push_recording_python(tmp_path, extra={"AXI_TEST_MAP_COMMIT": "abc"}, testmon_map=True)
    assert suites == [FULL_RUN]


@pytest.mark.parametrize("workers", ["2", "0"])
def test_pre_push_honours_the_worker_count(tmp_path: Path, workers: str):
    """AGENTS.md documents AXI_HOOK_WORKERS; 0 is how to force a serial run."""
    _, suites = _push_recording_python(tmp_path, extra={"AXI_HOOK_WORKERS": workers})
    assert suites == [[*SUITE_ARGS, "-n", workers, "--testmon-noselect"]]


def test_pre_push_runs_a_large_selection_on_workers(tmp_path: Path):
    _, suites = _push_recording_python(
        tmp_path,
        extra={"AXI_TEST_MAP_COMMIT": "abc", "AXI_TEST_DECISION": "mode=blast\\nn=40"},
        testmon_map=True,
    )
    assert suites == [[*SUITE_ARGS, "-n", "4", "--testmon", "--testmon-forceselect"]]


def test_pre_push_runs_a_small_selection_in_process(tmp_path: Path):
    _, suites = _push_recording_python(
        tmp_path,
        extra={"AXI_TEST_MAP_COMMIT": "abc", "AXI_TEST_DECISION": "mode=blast\\nn=5"},
        testmon_map=True,
    )
    assert suites == [[*SUITE_ARGS, "-n", "0", "--testmon", "--testmon-forceselect"]]


def test_pre_push_refuses_red_main_before_running_any_test(tmp_path: Path):
    """The red-main gate stays in front of the suite."""
    result, suites = _push_recording_python(tmp_path, conclusion="failure")
    assert result.returncode != 0, "expected push to be refused"
    assert "Bypass-Reason" in result.stdout + result.stderr
    assert suites == []


@pytest.mark.parametrize("hook", [PRE_PUSH, COMMIT_MSG])
def test_hook_files_are_executable(hook: Path):
    assert hook.exists(), f"{hook} missing"
    assert os.access(hook, os.X_OK), f"{hook} not executable"
