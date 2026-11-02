# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""The suite must not inherit a git context from whoever launched it.

``git`` exports ``GIT_DIR``, ``GIT_WORK_TREE``, ``GIT_INDEX_FILE`` and friends
into every hook it runs, so a suite launched from ``pre-push`` inherits them.
``GIT_DIR`` short-circuits *all* repo discovery: it beats ``cwd=``, ``-C`` and
``GIT_CEILING_DIRECTORIES``. A test that builds a scratch repo in ``tmp_path``
then runs ``git remote add origin`` does not touch its scratch repo at all —
it reaches into the real one. That is how "run the suite from a worktree" got
a reputation for corrupting the branch it ran on.

So the session neutralises them once, at startup, before collection.
"""

from __future__ import annotations

import os
import subprocess


from tests.conftest import GIT_DISCOVERY_VARS


def test_no_git_discovery_context_leaks_into_the_session():
    """Only the vars that redirect discovery. GIT_EDITOR and friends are the
    developer's preference and none of our business."""
    leaked = sorted(k for k in GIT_DISCOVERY_VARS if k in os.environ)
    assert not leaked, f"git discovery context inherited by the session: {leaked}"


def test_scratch_repo_discovery_is_not_hijacked(tmp_path):
    """A scratch repo must resolve to itself, not to whatever launched us."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    top = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=tmp_path, capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert os.path.realpath(top) == os.path.realpath(tmp_path), (
        "git discovery resolved outside the scratch repo — a GIT_DIR is in scope"
    )
