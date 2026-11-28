# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A tag of a version-only commit on a green parent is not retested (#2441 item 10, ADR-289).

The strict content check is tested in ``release/tests/test_version_bump.py``. This runs the
real shell of the "Tag or main already tested" job in a real git repository with a stand-in
``gh``, because the property that matters is the chain: own run green, else a version-only
diff on a green main parent, else the normal path, and any failure along the way means the
normal path.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
CI = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())
SCRIPT = next(s["run"] for s in CI["jobs"]["tag-already-green"]["steps"] if s.get("id") == "check")

PYPROJECT = '[project]\nname = "axiom-os-lm"\nversion = "{v}"\n'


def _git(cwd, *args):
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _repo(tmp_path, *, with_code_change=False):
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q")
    (work / "pyproject.toml").write_text(PYPROJECT.format(v="0.70.47"))
    (work / "src.py").write_text("x = 1\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "parent")
    parent = _git(work, "rev-parse", "HEAD")
    (work / "pyproject.toml").write_text(PYPROJECT.format(v="0.70.48"))
    if with_code_change:
        (work / "src.py").write_text("x = 2\n")
    _git(work, "commit", "-qam", "release")
    head = _git(work, "rev-parse", "HEAD")
    os.symlink(ROOT / "src", work / "src")
    _git(work, "remote", "add", "origin", str(work))  # the job fetches the parent from origin
    return work, parent, head


def _run(tmp_path, work, head, green, *, gh_fails=False, ref="refs/tags/v0.70.48"):
    """Run the job's script. ``green`` is the set of shas that have a green main run."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(
        "#!/bin/bash\n"
        + (
            "exit 1\n"
            if gh_fails
            else 'url="$2"\nfor sha in ' + " ".join(green) + "; do\n"
            '  if [[ "$url" == *"head_sha=$sha&"* && "$url" == *"branch=main"* && "$url" == *"status=success"* ]]; then echo 1; exit 0; fi\n'
            "done\necho 0\n"
        )
    )
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
    py = bin_dir / "python3"
    py.symlink_to(sys.executable)
    out = tmp_path / "out"
    out.write_text("")
    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "GITHUB_REF": ref,
        "GITHUB_REPOSITORY": "o/r",
        "GITHUB_OUTPUT": str(out),
        "GITHUB_SHA": head,
        "GH_TOKEN": "x",
    }
    done = subprocess.run(
        ["bash", "-c", SCRIPT], cwd=work, env=env, capture_output=True, text=True, check=False
    )
    return done, out.read_text()


@pytest.fixture
def release(tmp_path):
    return _repo(tmp_path)


def test_a_tag_whose_own_commit_is_green_skips_as_before(tmp_path, release):
    work, _parent, head = release
    done, out = _run(tmp_path, work, head, green=[head])
    assert "skip=true" in out, done.stdout + done.stderr


def test_a_version_only_commit_on_a_green_parent_skips_the_suite(tmp_path, release):
    work, parent, head = release
    done, out = _run(tmp_path, work, head, green=[parent])
    assert "skip=true" in out, done.stdout + done.stderr
    assert "version" in done.stdout.lower()


def test_a_version_only_commit_whose_parent_is_not_green_runs_the_suite(tmp_path, release):
    work, _parent, head = release
    done, out = _run(tmp_path, work, head, green=[])
    assert "skip=false" in out, done.stdout + done.stderr


def test_a_commit_with_code_in_it_runs_the_suite_even_on_a_green_parent(tmp_path):
    work, parent, head = _repo(tmp_path, with_code_change=True)
    done, out = _run(tmp_path, work, head, green=[parent])
    assert "skip=false" in out, done.stdout + done.stderr
    assert "src.py" in done.stdout


def test_when_github_cannot_be_asked_the_suite_runs(tmp_path, release):
    work, _parent, head = release
    done, out = _run(tmp_path, work, head, green=[], gh_fails=True)
    assert "skip=false" in out, done.stdout + done.stderr


def test_the_fast_path_is_for_tags_only_a_push_to_main_keeps_its_own_rule(tmp_path, release):
    work, parent, head = release
    done, out = _run(tmp_path, work, head, green=[parent], ref="refs/heads/main")
    assert "skip=false" in out, done.stdout + done.stderr


def test_the_job_names_the_check_the_parent_run_and_the_decision(tmp_path, release):
    work, parent, head = release
    done, _out = _run(tmp_path, work, head, green=[parent])
    assert parent in done.stdout
