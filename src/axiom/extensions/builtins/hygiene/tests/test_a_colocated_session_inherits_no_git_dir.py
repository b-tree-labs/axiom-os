# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A session that collects only colocated tests carries no inherited git context (#532).

``tests/test_git_env_neutralised.py`` runs this file in its own session with
``GIT_DIR`` set, the way ``git push`` sets it for the pre-push hook. With it in
scope, a test that commits in a scratch repo commits in the developer's
checkout instead."""

import os
import subprocess


def test_git_discovery_resolves_a_scratch_repo_to_itself(tmp_path):
    for var in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"):
        assert var not in os.environ, f"{var}={os.environ[var]} is in scope"
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, capture_output=True)
    top = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=tmp_path,
                         capture_output=True, text=True, check=True).stdout.strip()
    assert os.path.realpath(top) == os.path.realpath(tmp_path)
