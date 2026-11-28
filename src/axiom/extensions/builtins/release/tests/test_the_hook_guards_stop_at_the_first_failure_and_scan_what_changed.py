# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The pre-push hook asks the guards to fail fast and to scan only what the push changed.

The server runs the same guards without either flag, so it reports every failure at once and
rescans the whole public surface. These tests pin both halves: the hook's run is faster, and
it still refuses everything the whole run refuses.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from axiom.extensions.builtins.hygiene._git_isolation import git_isolated_env
from axiom.extensions.builtins.release import guards

REPO_ROOT = Path(__file__).resolve().parents[6]

#: Built at run time: this file is itself scanned for the term it plants.
LEAK = "the " + "rasc" + "al host"


def _git(root: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", *args], cwd=root, env=git_isolated_env(), capture_output=True, text=True, check=True
    )
    return done.stdout.strip()


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "commit.gpgsign", "false")
    (root / "scripts").mkdir()
    shutil.copy(REPO_ROOT / "scripts" / "build_public_mirror.py", root / "scripts")
    shutil.copytree(REPO_ROOT / "mirror", root / "mirror")
    (root / "README.md").write_text("seed\n")
    (root / "tests").mkdir()
    (root / "tests" / "test_demo_is_retired.py").write_text("# stand-in for a retired-word guard\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "seed")
    return root


def _commit(root: Path, path: str, text: str) -> str:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    _git(root, "add", path)
    _git(root, "commit", "-q", "-m", "change")
    return _git(root, "rev-parse", "HEAD")


class Calls:
    """A runner that records each command and answers per command."""

    def __init__(self, pytest_rc: int = 0, pytest_out: str = "", adr_rc: int = 0):
        self.pytest_rc, self.pytest_out, self.adr_rc = pytest_rc, pytest_out, adr_rc
        self.pytest: list[list[str]] = []
        self.scripts: list[list[str]] = []

    def __call__(self, args, **_kwargs):
        if "pytest" in args:
            self.pytest.append(list(args))
            return subprocess.CompletedProcess(args, self.pytest_rc, stdout=self.pytest_out, stderr="")
        self.scripts.append(list(args))
        return subprocess.CompletedProcess(args, self.adr_rc, stdout="ADR collision\n", stderr="")

    def ran(self, target: str) -> bool:
        return any(target in arg for call in self.pytest for arg in call)


def _run(root: Path, calls: Calls, base: str, head: str, **kwargs):
    return guards.run_guards(root, base=base, head=head, runner=calls, python="/usr/bin/python3", **kwargs)


def test_the_instant_guards_run_first_and_a_failure_there_runs_no_pytest(tmp_path: Path):
    root = _repo(tmp_path)
    base = _git(root, "rev-parse", "HEAD")
    head = _commit(root, "docs/note.md", "a note\n")
    calls = Calls(adr_rc=1)
    report = _run(root, calls, base, head, fail_fast=True)
    assert not report.ok
    assert [r.id for r in report.results if not r.ok] == ["adr-numbers"]
    assert calls.pytest == []
    assert "stopped at the first failure" in report.render()


def test_a_failure_in_the_quick_pytest_guards_never_starts_the_slow_scan(tmp_path: Path):
    root = _repo(tmp_path)
    base = _git(root, "rev-parse", "HEAD")
    head = _commit(root, "docs/note.md", "a note\n")
    out = "FAILED tests/test_repo_hygiene.py::TestRootDirPolicy::test_no_unexpected_root_dirs - AssertionError\n"
    calls = Calls(pytest_rc=1, pytest_out=out)
    report = _run(root, calls, base, head, fail_fast=True)
    assert not report.ok
    assert [r.id for r in report.results if not r.ok] == ["root-dirs"]
    assert len(calls.pytest) == 1 and not calls.ran("test_mirror.py")
    assert "public-surface" not in [r.id for r in report.results]


def test_without_fail_fast_every_guard_still_runs_and_reports(tmp_path: Path):
    """The server's run: one pytest call, every failure reported together."""
    root = _repo(tmp_path)
    base = _git(root, "rev-parse", "HEAD")
    head = _commit(root, "docs/note.md", "a note\n")
    out = (
        "FAILED tests/test_repo_hygiene.py::TestRootDirPolicy::test_no_unexpected_root_dirs - x\n"
        "FAILED tests/test_mirror.py::test_public_surface_has_no_forbidden_terms - x\n"
    )
    calls = Calls(pytest_rc=1, pytest_out=out, adr_rc=1)
    report = _run(root, calls, base, head)
    assert [r.id for r in report.results if not r.ok] == ["root-dirs", "public-surface", "adr-numbers"]
    assert len(calls.pytest) == 1


def test_fail_fast_runs_the_slow_scan_last_and_alone_when_nothing_failed_before_it(tmp_path: Path):
    root = _repo(tmp_path)
    base = _git(root, "rev-parse", "HEAD")
    head = _commit(root, "docs/note.md", "a note\n")
    calls = Calls()
    report = _run(root, calls, base, head, fail_fast=True)
    assert report.ok
    assert len(calls.pytest) == 2
    assert calls.ran("test_mirror.py") and not any("test_mirror.py" in a for a in calls.pytest[0])
    assert "test_mirror.py" in " ".join(calls.pytest[1])
    assert {r.id for r in report.results} == {g.id for g in guards.GUARDS}


def test_incremental_scans_only_the_changed_public_files_and_still_catches_a_leak(tmp_path: Path):
    root = _repo(tmp_path)
    base = _git(root, "rev-parse", "HEAD")
    head = _commit(root, "docs/guides/probe.md", f"We run on {LEAK}.\n")
    calls = Calls()
    report = _run(root, calls, base, head, fail_fast=True, incremental=True)
    assert not report.ok
    assert [r.id for r in report.results if not r.ok] == ["public-surface"]
    assert not calls.ran("test_mirror.py"), "the narrowed scan replaces the whole-surface test"


def test_incremental_passes_a_clean_change_without_the_whole_scan(tmp_path: Path):
    root = _repo(tmp_path)
    base = _git(root, "rev-parse", "HEAD")
    head = _commit(root, "docs/guides/probe.md", "A clean page.\n")
    calls = Calls()
    report = _run(root, calls, base, head, fail_fast=True, incremental=True)
    assert report.ok
    assert not calls.ran("test_mirror.py")


def test_incremental_scans_everything_when_the_scan_itself_changed(tmp_path: Path):
    """A change to what the scan means (its terms, its allowances, the exclusions) is never
    judged by the lines it changed."""
    root = _repo(tmp_path)
    for path in ("scripts/build_public_mirror.py", "mirror/exclude.txt", "tests/test_mirror.py"):
        base = _git(root, "rev-parse", "HEAD")
        target = root / path
        head = _commit(root, path, (target.read_text() if target.exists() else "") + "\n# edit\n")
        calls = Calls()
        report = _run(root, calls, base, head, fail_fast=True, incremental=True)
        assert report.ok
        assert calls.ran("test_mirror.py"), f"{path} changed, so the whole surface is scanned"


def test_incremental_falls_back_to_the_whole_scan_when_it_cannot_load_the_scan(tmp_path: Path):
    root = _repo(tmp_path)
    (root / "scripts" / "build_public_mirror.py").write_text("raise RuntimeError('broken')\n")
    base = _git(root, "rev-parse", "HEAD")
    head = _commit(root, "docs/guides/probe.md", f"We run on {LEAK}.\n")
    calls = Calls()
    _run(root, calls, base, head, fail_fast=True, incremental=True)
    assert calls.ran("test_mirror.py")


def test_incremental_without_a_base_scans_the_whole_surface(tmp_path: Path):
    root = _repo(tmp_path)
    calls = Calls()
    _run(root, calls, "", "HEAD", fail_fast=True, incremental=True)
    # no origin/main and no parent: the base is the seed's parent, which does not exist
    assert calls.ran("test_mirror.py")
