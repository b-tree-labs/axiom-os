# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""CI creates no new directory at the repository root (#2441, the revert of #2457).

#2457 restored a rolling test history into ``.ci-history`` at the checkout root. The
pull-request run narrowed to two minutes and never ran ``tests/test_repo_hygiene.py``;
main's full run did, and it failed: "Unexpected root directories: ['.ci-history']". The
guards-first step runs in Smoke, before the unit job restores anything, so nothing saw it.

Two checks close that class:

1. Every path a workflow restores, downloads or writes at the root (cache and download
   paths, ``mkdir``, ``--out``, redirects, ``--junitxml``, copies) is either not a
   directory at the root or a directory the hygiene guard already allows.
2. The workflow's own history path is exercised the way the workflow does it, inside a
   copy of the checkout's root layout, and the hygiene guard is run against the result.
"""

from __future__ import annotations

import importlib.util
import os
import shlex
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))

#: Root-level names that are files, not directories (a bare cache path cannot say which).
ROOT_FILES = {".testmondata"}

_spec = importlib.util.spec_from_file_location(
    "repo_hygiene", ROOT / "tests" / "test_repo_hygiene.py"
)
assert _spec and _spec.loader
_hygiene = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_hygiene)
ALLOWED = set(_hygiene.TestRootDirPolicy.ALLOWED_ROOT_DIRS)


def _relative(path: str) -> str | None:
    """A path under the checkout, or None for an absolute, home, parent or variable path."""
    path = path.strip().strip("\"'")
    if not path or path[0] in "/~$" or path.startswith(("..", "${{", "-")):
        return None
    return path[2:] if path.startswith("./") else path


def _top(path: str) -> tuple[str, bool]:
    """(first component, whether it is a directory given the path's own shape)."""
    first, slash, _rest = path.partition("/")
    return first, bool(slash)


_VALUE_FLAGS = {
    "-n",
    "-m",
    "-k",
    "-p",
    "--tb",
    "--junitxml",
    "--timeout",
    "--dist",
    "--maxfail",
    "-o",
}


def runs_the_whole_suite(job: dict) -> bool:
    """Does a step run a bare ``pytest``: no path, so every testpath, the root-layout guard included?"""
    for step in job.get("steps") or []:
        for line in (step.get("run") or "").splitlines():
            try:
                tokens = shlex.split(line, comments=True)
            except ValueError:
                continue
            for at, token in enumerate(tokens):
                if token.endswith("pytest") and "working-directory" not in step:
                    rest, skip, paths = tokens[at + 1 :], False, []
                    for arg in rest:
                        if skip:
                            skip = False
                        elif arg in _VALUE_FLAGS:
                            skip = True
                        elif arg in {";", "&&", "||", "|"}:
                            break
                        elif not arg.startswith("-") and not arg.startswith("$"):
                            paths.append(arg)
                    if not paths:
                        return True
    return False


def root_dirs_written(workflow: dict, only_suite_jobs: bool = False) -> set[tuple[str, str]]:
    """``(job.step, root directory)`` for every workflow step that puts a directory at the root.

    ``only_suite_jobs`` keeps the jobs that run the whole suite in their checkout: those are
    the ones where ``tests/test_repo_hygiene.py`` sees what the job wrote.
    """
    found: set[tuple[str, str]] = set()
    for job_id, job in (workflow.get("jobs") or {}).items():
        if only_suite_jobs and not runs_the_whole_suite(job):
            continue
        for index, step in enumerate(job.get("steps") or []):
            where = f"{job_id}.{step.get('name') or index}"
            uses, with_ = str(step.get("uses", "")), step.get("with") or {}
            paths: list[tuple[str, bool]] = []  # (path, a bare name is a directory)
            if uses.startswith("actions/cache") or uses.startswith("actions/download-artifact"):
                raw = with_.get("path", "")
                paths += [(line, True) for line in str(raw).splitlines() if line.strip()]
            run = step.get("run") or ""
            for line in run.splitlines():
                if line.lstrip().startswith("#"):
                    continue
                try:
                    tokens = shlex.split(line, comments=True, posix=True)
                except ValueError:
                    continue
                for at, token in enumerate(tokens):
                    nxt = tokens[at + 1] if at + 1 < len(tokens) else ""
                    if token == "mkdir":
                        paths += [(t, True) for t in tokens[at + 1 :] if not t.startswith("-")]
                    elif token in {"--out", "-o", "--output"}:
                        paths.append((nxt, False))
                    elif token.startswith("--out=") or token.startswith("--junitxml="):
                        paths.append((token.split("=", 1)[1], False))
                    elif token in {">", ">>"}:
                        paths.append((nxt, False))
                    elif token == "cp" and len(tokens) > at + 2:
                        paths.append((tokens[-1], False))
            for raw_path, bare_is_dir in paths:
                rel = _relative(raw_path)
                if rel is None:
                    continue
                first, nested = _top(rel)
                if nested or (bare_is_dir and first not in ROOT_FILES):
                    found.add((where, first))
    return found


def _violations(workflow: dict) -> list[str]:
    return sorted(
        f"{where}: {name}"
        for where, name in root_dirs_written(workflow, only_suite_jobs=True)
        if name not in ALLOWED
    )


# --- the check can fail ------------------------------------------------------------------------


def test_the_check_sees_a_cache_path_a_download_path_and_the_run_step_writes():
    workflow = yaml.safe_load(
        """
jobs:
  j:
    steps:
      - name: restore
        uses: actions/cache/restore@abc
        with: {path: ".ci-history", key: k}
      - name: download
        uses: actions/download-artifact@abc
        with: {path: shards}
      - name: run
        run: |
          mkdir -p made/dir
          tool --out written/file.json
          echo hi > redirected/x.txt
          pytest --junitxml=reports/junit.xml
          cp a.txt copied/a.txt
"""
    )
    names = {name for _where, name in root_dirs_written(workflow)}
    assert names == {".ci-history", "shards", "made", "written", "redirected", "reports", "copied"}


def test_what_lives_outside_the_checkout_or_is_a_root_file_is_not_a_root_directory():
    workflow = yaml.safe_load(
        """
jobs:
  j:
    steps:
      - uses: actions/cache/restore@abc
        with: {path: "${{ runner.temp }}/ci-history", key: k}
      - uses: actions/cache/restore@abc
        with: {path: ".testmondata", key: k}
      - run: |
          mkdir -p "$RUNNER_TEMP/x"
          tool --out "$RUNNER_TEMP/y.json"
          echo a >> "$GITHUB_ENV"
          pytest --junitxml=junit.xml
"""
    )
    assert root_dirs_written(workflow) == set()


def test_an_allowed_root_directory_is_fine():
    assert "dist" in ALLOWED and ".venv" in ALLOWED


def test_only_jobs_that_run_the_whole_suite_are_held_to_it():
    suite = yaml.safe_load(
        "jobs: {a: {steps: [{run: 'pytest -n 4 --junitxml=j.xml'}]}, b: {steps: [{run: 'pytest tests/x.py'}]}}"
    )
    assert runs_the_whole_suite(suite["jobs"]["a"]) and not runs_the_whole_suite(suite["jobs"]["b"])
    real = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())["jobs"]
    assert runs_the_whole_suite(real["unit-tests"]), "the unit job is the one that meets the guard"


# --- the real workflows ----------------------------------------------------------------------------


def test_no_workflow_creates_a_root_directory_the_hygiene_guard_forbids():
    bad = []
    for path in WORKFLOWS:
        bad += [f"{path.name} {v}" for v in _violations(yaml.safe_load(path.read_text()))]
    assert not bad, (
        "a workflow step puts a new directory at the repository root, and a full run's "
        "tests/test_repo_hygiene.py will fail on it. Keep it under ${{ runner.temp }} or an "
        f"allowed directory, and do not widen the allowlist: {bad}"
    )


# --- the history path, exercised the way the workflow does it -----------------------------------------


def _workflow_history(checkout: Path, runner_temp: Path) -> tuple[Path, Path]:
    """The history directory a cache restore creates and the file the tests append to, as the
    workflow names them, with the expressions the runner would fill in."""
    ci = yaml.safe_load((WORKFLOWS[0].parent / "ci.yml").read_text())
    steps = {s.get("name"): s for s in ci["jobs"]["unit-tests"]["steps"] if isinstance(s, dict)}

    def fill(text: str) -> str:
        return text.replace("${{ runner.temp }}", str(runner_temp)).replace(
            "${{ github.workspace }}", str(checkout)
        )

    directory = Path(fill(steps["Test history from earlier runs"]["with"]["path"]))
    env = steps["Run unit tests"]["env"]["AXIOM_TEST_HISTORY"]
    file = Path(fill(env))
    return (directory if directory.is_absolute() else checkout / directory), (
        file if file.is_absolute() else checkout / file
    )


def test_running_the_history_code_the_way_the_workflow_does_leaves_the_hygiene_guard_green(
    tmp_path,
):
    checkout = tmp_path / "checkout"
    runner_temp = tmp_path / "runner_temp"
    runner_temp.mkdir()
    # the checkout's root layout, empty: only what lands beside these directories matters
    (checkout / "tests").mkdir(parents=True)
    for item in ROOT.iterdir():
        if item.is_dir() and item.name in ALLOWED and item.name != "tests":
            (checkout / item.name).mkdir()
    (checkout / "tests" / "test_repo_hygiene.py").write_text(
        (ROOT / "tests" / "test_repo_hygiene.py").read_text()
    )
    (checkout / "test_sample.py").write_text("def test_ok(): pass\n")

    directory, history = _workflow_history(checkout, runner_temp)
    directory.mkdir(parents=True, exist_ok=True)  # what actions/cache/restore does for its path
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "PYTHONPATH": str(ROOT / "src"),
        "PYTEST_ADDOPTS": "-p axiom.extensions.builtins.release.order_plugin",
        "AXIOM_TEST_HISTORY": str(history),
        "AXIOM_TEST_ORDER": "failed-first",
        "GITHUB_RUN_ID": "1",
        "GITHUB_SHA": "a" * 40,
        "GITHUB_EVENT_NAME": "push",
    }
    ran = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "-o",
            "addopts=",
            "test_sample.py",
        ],
        cwd=checkout,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert ran.returncode == 0, ran.stdout + ran.stderr
    assert history.is_file(), "the workflow's history path is where the plugin wrote"

    hygiene = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "-o",
            "addopts=",
            "tests/test_repo_hygiene.py::TestRootDirPolicy",
        ],
        cwd=checkout,
        env={**env, "PYTEST_ADDOPTS": ""},
        capture_output=True,
        text=True,
        check=False,
    )
    assert hygiene.returncode == 0, hygiene.stdout[-800:]
    assert not [p.name for p in checkout.iterdir() if p.is_dir() and p.name not in ALLOWED]
