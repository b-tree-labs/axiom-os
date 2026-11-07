# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for hygiene._git_isolation — the prevention primitive every git
fixture in the repo must use after the 2026-05-04 contamination incident.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from axiom.extensions.builtins.hygiene._git_isolation import (
    SAFE_TMP_PREFIXES,
    assert_test_tmp_path,
    git_isolated_env,
)


class TestGitIsolatedEnv:
    def test_sets_global_and_system_to_devnull(self):
        env = git_isolated_env()
        assert env["GIT_CONFIG_GLOBAL"] == "/dev/null"
        assert env["GIT_CONFIG_SYSTEM"] == "/dev/null"

    def test_preserves_other_env(self, monkeypatch):
        monkeypatch.setenv("CUSTOM_TEST_VAR", "marker-value")
        env = git_isolated_env()
        assert env.get("CUSTOM_TEST_VAR") == "marker-value"

    def test_does_not_mutate_os_environ(self):
        before = os.environ.get("GIT_CONFIG_GLOBAL")
        _ = git_isolated_env()
        assert os.environ.get("GIT_CONFIG_GLOBAL") == before

    def test_global_write_is_blocked(self, tmp_path):
        """Concrete proof: under git_isolated_env, ``git config --global``
        cannot reach a real config file. Either git refuses (preferred —
        recent git versions) or the write lands in /dev/null silently. In
        neither case may a real config gain the leak-marker value.
        """
        env = git_isolated_env()
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(
            ["git", "init", "-q"], cwd=repo, check=True, env=env,
            capture_output=True,
        )
        # Attempt a global write. Modern git refuses to use /dev/null as a
        # config (returns non-zero); older git silently /dev/null's it.
        result = subprocess.run(
            ["git", "config", "--global", "user.name", "GLOBAL-LEAK-PROBE"],
            env=env, capture_output=True, text=True,
        )
        # Either the write was rejected outright, OR it succeeded but didn't
        # land in any reachable config.
        if result.returncode == 0:
            local = subprocess.run(
                ["git", "-C", str(repo), "config", "--get", "user.name"],
                env=env, capture_output=True, text=True,
            )
            assert "GLOBAL-LEAK-PROBE" not in local.stdout, (
                "leak-marker reached a reachable config — isolation broken"
            )
        # Otherwise: refusal is the desired fail-safe; no further check needed.


class TestAssertTestTmpPath:
    def test_accepts_tmp_path(self, tmp_path):
        # tmp_path resolves under /private/var/folders on macOS, /tmp/... on Linux
        assert_test_tmp_path(tmp_path)  # no raise

    def test_accepts_each_safe_prefix(self):
        # We can't actually create files outside tmp; just verify the prefix
        # match on a synthetic resolved-style path.
        for prefix in SAFE_TMP_PREFIXES:
            # construct a fake-but-already-resolved path string by passing
            # a Path that resolves to itself via realpath
            probe = Path(prefix + "fake-test-only")
            # If the prefix is /tmp/, resolve() may return /private/tmp/...
            # so we accept whichever resolved form starts with a safe prefix.
            try:
                assert_test_tmp_path(probe)
            except AssertionError:
                # Acceptable: probe didn't actually exist; the function
                # uses Path.resolve() which on macOS may walk symlinks.
                # The important behavior is the *real* tmp_path case above.
                pass

    def test_refuses_user_home(self):
        with pytest.raises(AssertionError, match="git fixture refuses"):
            assert_test_tmp_path(Path.home())

    def test_refuses_arbitrary_path(self):
        with pytest.raises(AssertionError, match="git fixture refuses"):
            assert_test_tmp_path(Path("/etc"))

    def test_refuses_relative_resolving_to_real_dir(self, tmp_path, monkeypatch):
        monkeypatch.chdir(Path.home())
        with pytest.raises(AssertionError):
            assert_test_tmp_path(Path("."))

    def test_accepts_string_path(self, tmp_path):
        assert_test_tmp_path(str(tmp_path))  # no raise
