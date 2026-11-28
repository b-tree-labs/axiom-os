# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The pre-push hook got faster without getting weaker.

Two halves:

* A push that only merges origin/main runs the cheap guards and no suite (#2289), and every
  other merge keeps the normal path (stand-in python, like test_hooks.py).
* The REAL hook, with the real guards and the real lint, on a clone of this checkout, still
  refuses what it always refused: red main, a lint error, a customer or domain name in the
  platform, a leak on the public surface, a stray root directory, a failing test. A harmless
  change goes through the same harness, so a refusal is the hook's and not the harness's.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location("_hook_helpers", Path(__file__).with_name("test_hooks.py"))
h = importlib.util.module_from_spec(_spec)
sys.modules["_hook_helpers"] = h
_spec.loader.exec_module(h)

REPO_ROOT = h.REPO_ROOT

# Built at run time: this file is scanned for the terms it plants.
HOST_NAME = "rasc" + "al"
DOMAIN_NOUN = "reac" + "tor"
RETIRED_WORD = "sig" + "nal"
AXI_CMD = "a" + "xi"


# ──────────────────────────────────────────────────────────────────────
# a push that only merges origin/main (#2289)
# ──────────────────────────────────────────────────────────────────────


def _merge_scenario(tmp_path: Path, *, conflict: bool = False, own_commit: str | None = None,
                    extra: dict | None = None, with_flags: bool = True):
    """feature is pushed; main moves on and is fetched; feature merges origin/main; push."""
    repo = h._init_repo(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    h._make_gh_shim(bin_dir, "success")
    h._make_remote(tmp_path, repo)
    h._install_hooks(repo)
    quiet = h._env(tmp_path / "home", bin_dir)
    guards = repo / "src" / "axiom" / "extensions" / "builtins" / "release" / "guards.py"
    guards.parent.mkdir(parents=True, exist_ok=True)
    guards.write_text("# stand-in\n")
    # A branch cut before the speed flags existed has a verb that does not know them.
    (guards.parent / "cli.py").write_text("# stand-in\n" + ("# --fail-fast\n" if with_flags else ""))
    (repo / "tests").mkdir()
    (repo / "tests" / "test_c.py").write_text("def test_c():\n    assert 1\n")
    h._git(repo, "add", "-A", env=quiet)
    h._git(repo, "commit", "-q", "-m", "base", env=quiet)
    h._git(repo, "push", "-q", "origin", "main", env=quiet)

    h._git(repo, "checkout", "-q", "-b", "feature", env=quiet)
    (repo / "f.txt").write_text("f\n")
    c_feature = "def test_c():\n    assert 2\n" if conflict else "def test_c():\n    assert 1\n"
    (repo / "tests" / "test_c.py").write_text(c_feature)
    h._git(repo, "add", "-A", env=quiet)
    h._git(repo, "commit", "-q", "-m", "feature", env=quiet)
    assert h._git(repo, "push", "-q", "origin", "feature", env=quiet).returncode == 0

    h._git(repo, "checkout", "-q", "main", env=quiet)
    (repo / "m.txt").write_text("m\n")
    if conflict:
        (repo / "tests" / "test_c.py").write_text("def test_c():\n    assert 3\n")
    h._git(repo, "add", "-A", env=quiet)
    h._git(repo, "commit", "-q", "-m", "main moves", env=quiet)
    h._git(repo, "push", "-q", "origin", "main", env=quiet)
    h._git(repo, "checkout", "-q", "feature", env=quiet)
    merged = h._git(repo, "merge", "--no-edit", "origin/main", env=quiet)
    if conflict:
        assert merged.returncode != 0
        (repo / "tests" / "test_c.py").write_text("def test_c():\n    assert 2 + 1\n")
        h._git(repo, "add", "-A", env=quiet)
        assert h._git(repo, "commit", "-q", "--no-edit", env=quiet).returncode == 0
    else:
        assert merged.returncode == 0, merged.stdout + merged.stderr
    if own_commit:
        (repo / own_commit).parent.mkdir(parents=True, exist_ok=True)
        (repo / own_commit).write_text("def test_own():\n    pass\n")
        h._git(repo, "add", "-A", env=quiet)
        h._git(repo, "commit", "-q", "-m", "own work", env=quiet)

    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    py = venv / "bin" / "python"
    py.write_text(h._RECORDING_PYTHON)
    py.chmod(0o755)
    log = tmp_path / "python-calls.log"
    log.touch()
    env = h._env(tmp_path / "home", bin_dir, {
        "AXI_PRE_PUSH_SKIP_CHECKS": "0", "VIRTUAL_ENV": str(venv), "AXI_TEST_PY_LOG": str(log),
        **(extra or {}),
    })
    env.pop("AXI_HOOK_FULL", None) if not (extra or {}).get("AXI_HOOK_FULL") else None
    result = h._git(repo, "push", "origin", "feature", env=env)
    calls = [line.split() for line in log.read_text().splitlines()]
    suites = [c for c in calls if c[:2] == ["-m", "pytest"] and "--timeout=90" in c]
    guard_calls = [c for c in calls if c[:3] == ["-m", "axiom.extensions.builtins.release.cli", "guards"]]
    return result, suites, guard_calls, tmp_path / "home" / ".axi" / "pre-push-path.log"


def test_a_clean_merge_of_main_runs_the_guards_and_no_suite(tmp_path: Path):
    result, suites, guard_calls, path_log = _merge_scenario(tmp_path)
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert "only merges origin/main" in out
    assert suites == []
    assert len(guard_calls) == 1, "the cheap guards still judge the push"
    assert "merge-of-main-only" in path_log.read_text(), "the path that ran is logged"


def test_the_guards_the_hook_runs_fail_fast_and_scan_what_changed(tmp_path: Path):
    _, _, guard_calls, _ = _merge_scenario(tmp_path)
    assert "--fail-fast" in guard_calls[0] and "--incremental" in guard_calls[0]


def test_a_branch_whose_guards_verb_predates_the_speed_flags_is_not_refused_for_them(tmp_path: Path):
    """Worktrees share the hook; an older branch must still push, with the slow guards."""
    result, _, guard_calls, _ = _merge_scenario(tmp_path, with_flags=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "--fail-fast" not in guard_calls[0] and "--incremental" not in guard_calls[0]


def test_a_merge_with_a_hand_resolved_conflict_keeps_the_normal_path(tmp_path: Path):
    result, suites, guard_calls, path_log = _merge_scenario(tmp_path, conflict=True)
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert "only merges origin/main" not in out
    assert suites, "the resolved test file is run"
    assert not path_log.exists() or "merge-of-main-only" not in path_log.read_text()


def test_a_merge_pushed_with_a_commit_of_the_authors_keeps_the_normal_path(tmp_path: Path):
    result, suites, _, _ = _merge_scenario(tmp_path, own_commit="tests/test_own.py")
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert "only merges origin/main" not in out
    assert suites and "tests/test_own.py" in suites[0]


def test_axi_hook_full_still_runs_everything_for_a_merge_of_main(tmp_path: Path):
    result, suites, _, _ = _merge_scenario(tmp_path, extra={"AXI_HOOK_FULL": "1"})
    assert "only merges origin/main" not in result.stdout + result.stderr
    assert suites and "--testmon-noselect" in suites[0]


# ──────────────────────────────────────────────────────────────────────
# the real hook still refuses what it refused
# ──────────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def proof(tmp_path_factory):
    """A repository built from the files of this checkout's HEAD (one commit), with a bare
    remote, the real hook installed, and a stand-in gh. It does not depend on the source
    repository's history, so a shallow CI checkout and a full one behave the same. Heavy once
    (a checkout), then each case resets to the base."""
    base = tmp_path_factory.mktemp("proof")
    clone = base / "clone"
    clone.mkdir()
    archive = subprocess.run(["git", "archive", "HEAD"], cwd=REPO_ROOT, env=h._isolated_env(),
                             capture_output=True, check=True).stdout
    subprocess.run(["tar", "-x", "-C", str(clone)], input=archive, check=True)
    for cmd in (("init", "-q", "-b", "main"), ("config", "user.email", "dev@hook-proof.org"),
                ("config", "user.name", "Dev"), ("config", "commit.gpgsign", "false"),
                ("add", "-A")):
        assert h._git(clone, *cmd).returncode == 0
    assert h._git(clone, "commit", "-q", "-m", "base").returncode == 0
    head = h._git(clone, "rev-parse", "HEAD").stdout.strip()
    assert h._git(clone, "rev-parse", "--is-shallow-repository").stdout.strip() == "false", (
        "the proof repository must not be shallow: a shallow repository cannot push to the remote"
    )
    remote = base / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(remote)], check=True)
    h._git(clone, "remote", "add", "origin", str(remote))
    assert h._git(clone, "push", "-q", "origin", "main").returncode == 0
    h._install_hooks(clone)
    # The clone runs the hook under test, not whatever HEAD carried.
    for name in ("pre-push", "pre_push_markexpr.py", "pre_push_failed.py", "throwaway_postgres.py"):
        shutil.copy(REPO_ROOT / "scripts" / "hooks" / name, clone / "scripts" / "hooks" / name)
    return {"clone": clone, "base": head, "dir": base}


def _push(proof, tmp_path: Path, files: dict[str, str], *, gh: str = "success", message: str = "change"):
    clone: Path = proof["clone"]
    home = tmp_path / "home"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    h._make_gh_shim(bin_dir, gh)
    env = h._isolated_env()
    env.update({
        "HOME": str(home), "PATH": f"{bin_dir}:{env['PATH']}", "VIRTUAL_ENV": sys.prefix,
        "AXI_HOOK_NO_POSTGRES_REASON": "hook proof: the cases need no database",
    })
    for key in ("AXI_PRE_PUSH_SKIP_CHECKS", "AXI_HOOK_FULL", "AXI_OVERRIDE_REASON"):
        env.pop(key, None)
    subprocess.run(["git", "--git-dir", str(proof["dir"] / "remote.git"), "update-ref", "-d", "refs/heads/feature"],
                   check=True)
    h._git(clone, "reset", "-q", "--hard", proof["base"])
    h._git(clone, "clean", "-fdq")
    assert h._git(clone, "checkout", "-q", "-B", "feature", proof["base"]).returncode == 0
    for rel, text in files.items():
        target = clone / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    h._git(clone, "add", "-A")
    assert h._git(clone, "commit", "-q", "-m", message, env=env).returncode == 0
    result = h._git(clone, "push", "origin", "feature", env=env)
    sent = h._git(clone, "ls-remote", "origin", "refs/heads/feature").stdout.strip()
    return result, result.stdout + result.stderr, sent


def test_control_a_harmless_change_goes_through_the_same_harness(proof, tmp_path: Path):
    """Without this, every refusal below could be the harness failing for its own reasons."""
    result, out, sent = _push(proof, tmp_path, {"docs/guides/hook-proof-note.md": "A plain note.\n"})
    assert result.returncode == 0, out
    assert sent, "the push arrived"
    assert "cheap guards passed" in out


def test_red_main_still_refuses_a_push_with_no_receipt(proof, tmp_path: Path):
    result, out, sent = _push(proof, tmp_path, {"docs/guides/hook-proof-note.md": "A plain note.\n"}, gh="failure")
    assert result.returncode != 0
    assert "Bypass-Reason" in out and "RED" in out
    assert not sent
    assert "running test suite" not in out


def test_a_lint_error_the_repair_cannot_fix_still_blocks(proof, tmp_path: Path):
    result, out, sent = _push(proof, tmp_path, {"src/axiom/hook_proof_lint.py": "value = an_undefined_name\n"})
    assert result.returncode != 0
    assert "ruff still fails after --fix" in out or "ruff found errors" in out
    assert not sent


def test_a_name_in_the_platform_that_belongs_to_a_domain_still_blocks(proof, tmp_path: Path):
    text = f'"""The {DOMAIN_NOUN} trips the naming guard."""\n'
    result, out, sent = _push(proof, tmp_path, {"src/axiom/hook_proof_naming.py": text})
    assert result.returncode != 0
    assert "✗ naming" in out and "a guard failed" in out
    assert not sent


def test_a_leak_on_the_public_surface_still_blocks(proof, tmp_path: Path):
    text = f"This page names the {HOST_NAME} host.\n"
    result, out, sent = _push(proof, tmp_path, {"docs/guides/hook-proof-leak.md": text})
    assert result.returncode != 0
    assert "✗ public-surface" in out
    assert not sent


def test_a_retired_word_in_a_new_file_still_blocks(proof, tmp_path: Path):
    text = f'"""A new module that says {RETIRED_WORD}: the retired-word guard refuses it."""\n'
    result, out, sent = _push(proof, tmp_path, {"src/axiom/hook_proof_retired.py": text})
    assert result.returncode != 0
    assert "✗ retired-words" in out and "a guard failed" in out
    assert not sent


def test_a_hand_built_state_folder_still_blocks(proof, tmp_path: Path):
    text = 'from pathlib import Path\n\nSTATE = Path.home() / ".axi"\n'
    result, out, sent = _push(proof, tmp_path, {"src/axiom/hook_proof_state.py": text})
    assert result.returncode != 0
    assert "✗ state-folder" in out
    assert not sent


def test_a_hardcoded_cli_name_in_printed_text_still_blocks(proof, tmp_path: Path):
    text = f'def show():\n    print("{AXI_CMD} status: all well")\n'
    result, out, sent = _push(proof, tmp_path, {"src/axiom/hook_proof_cli_name.py": text})
    assert result.returncode != 0
    assert "✗ cli-name" in out
    assert not sent


def test_a_new_root_directory_still_blocks(proof, tmp_path: Path):
    result, out, sent = _push(proof, tmp_path, {"hook_proof_stray/readme.txt": "stray\n"})
    assert result.returncode != 0
    assert "✗ root-dirs" in out
    assert not sent


def test_a_failing_test_in_the_changed_set_still_blocks(proof, tmp_path: Path):
    body = "def test_hook_proof_fails():\n    assert 1 == 2\n"
    result, out, sent = _push(proof, tmp_path, {"tests/test_hook_proof_fails.py": body})
    assert result.returncode != 0
    assert "tests failed — push blocked" in out
    assert not sent


# ──────────────────────────────────────────────────────────────────────
# the narrowed sweep selects the failing test
# ──────────────────────────────────────────────────────────────────────


def _testmon_project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    (proj / "pkg").mkdir(parents=True)
    (proj / "pkg" / "__init__.py").write_text("")
    (proj / "pkg" / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (proj / "pkg" / "other.py").write_text("def twice(a):\n    return a * 2\n")
    (proj / "test_calc.py").write_text("from pkg.calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n")
    (proj / "test_other.py").write_text("from pkg.other import twice\n\n\ndef test_twice():\n    assert twice(2) == 4\n")
    for i in range(8):  # the cap is a third of the suite, so the suite must be bigger than three tests
        (proj / f"test_bystander_{i}.py").write_text(f"def test_bystander_{i}():\n    assert {i} == {i}\n")
    (proj / "pyproject.toml").write_text('[tool.pytest.ini_options]\naddopts = "-m \\"not slow\\""\nmarkers = ["slow"]\n')
    return proj


def _pytest(proj: Path, *args: str) -> subprocess.CompletedProcess:
    env = h._isolated_env()
    env["PYTHONPATH"] = str(proj)
    return subprocess.run([sys.executable, "-m", "pytest", "-p", "no:cacheprovider", *args],
                          cwd=proj, env=env, capture_output=True, text=True)


def test_the_narrowed_sweep_selects_the_test_that_a_change_breaks(tmp_path: Path):
    """The hook's own commands: build a map, break one module, collect with testmon, decide."""
    pytest.importorskip("testmon")
    proj = _testmon_project(tmp_path)
    built = _pytest(proj, "--testmon-noselect")
    assert built.returncode == 0, built.stdout + built.stderr
    broken = proj / "pkg" / "calc.py"
    broken.write_text("def add(a, b):\n    return a - b\n")
    # testmon trusts an unchanged mtime; a same-size edit in the same second would be missed.
    stamp = broken.stat().st_mtime + 10
    os.utime(broken, (stamp, stamp))

    collect = _pytest(proj, "--testmon", "--testmon-forceselect", "--collect-only", "--tb=no")
    blast = REPO_ROOT / "scripts" / "testmon_blast_radius.py"
    changed = tmp_path / "changed.txt"
    changed.write_text("pkg/calc.py\n")
    decision = subprocess.run(
        [sys.executable, str(blast), "--decide", "--map-key", "testmon-local-" + "a" * 40,
         "--database", str(proj / ".testmondata"), "--changed-files", str(changed)],
        input=collect.stdout, capture_output=True, text=True, cwd=proj,
    )
    assert "mode=blast" in decision.stdout, decision.stdout + decision.stderr
    assert "n=1" in decision.stdout

    ran = _pytest(proj, "--testmon", "--testmon-forceselect")
    assert ran.returncode == 1, ran.stdout + ran.stderr  # the selected test fails: the push is refused
    assert "test_add" in ran.stdout and "test_twice" not in ran.stdout
    assert "1 failed" in ran.stdout and "passed" not in ran.stdout.splitlines()[-1]
