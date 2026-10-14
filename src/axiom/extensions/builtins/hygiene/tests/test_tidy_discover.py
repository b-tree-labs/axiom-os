# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for TIDY's local-RAG steward discovery.

TIDY proactively discovers local git repos under the workspace and
honors a caller-supplied exclusion list. Each discovered repo is a
candidate for graph + vector ingest.
"""

from __future__ import annotations

import subprocess

from axiom.extensions.builtins.hygiene._git_isolation import (
    assert_test_tmp_path,
    git_isolated_env,
)


def _git_init(path):
    path.mkdir(parents=True, exist_ok=True)
    assert_test_tmp_path(path)
    subprocess.run(
        ["git", "init", "--quiet"],
        cwd=path,
        check=True,
        capture_output=True,
        env=git_isolated_env(),
    )


class TestDiscoverLocalRepos:
    def test_finds_immediate_git_repo(self, tmp_path):
        from axiom.extensions.builtins.hygiene.agents.tidy.discover import (
            discover_local_repos,
        )

        repo = tmp_path / "alpha"
        _git_init(repo)

        found = discover_local_repos(tmp_path)
        assert [r.path for r in found] == [repo]

    def test_finds_multiple_sibling_repos(self, tmp_path):
        from axiom.extensions.builtins.hygiene.agents.tidy.discover import (
            discover_local_repos,
        )

        for name in ("alpha", "beta", "gamma"):
            _git_init(tmp_path / name)

        found = sorted(r.path.name for r in discover_local_repos(tmp_path))
        assert found == ["alpha", "beta", "gamma"]

    def test_does_not_recurse_into_repo(self, tmp_path):
        from axiom.extensions.builtins.hygiene.agents.tidy.discover import (
            discover_local_repos,
        )

        _git_init(tmp_path / "outer")
        _git_init(tmp_path / "outer" / "inner")  # nested submodule-like

        found = [r.path.name for r in discover_local_repos(tmp_path)]
        assert found == ["outer"]

    def test_default_excludes_nothing(self, tmp_path):
        from axiom.extensions.builtins.hygiene.agents.tidy.discover import (
            discover_local_repos,
        )

        for name in ("alpha", "beta", "private_repo"):
            _git_init(tmp_path / name)

        found = sorted(r.path.name for r in discover_local_repos(tmp_path))
        assert found == ["alpha", "beta", "private_repo"]

    def test_explicit_exclude_paths(self, tmp_path):
        from axiom.extensions.builtins.hygiene.agents.tidy.discover import (
            discover_local_repos,
        )

        _git_init(tmp_path / "alpha")
        _git_init(tmp_path / "beta")
        _git_init(tmp_path / "private_repo")

        found = sorted(
            r.path.name
            for r in discover_local_repos(
                tmp_path, exclude_names={"private_repo", "scratch"}
            )
        )
        assert found == ["alpha", "beta"]

    def test_skips_dot_directories(self, tmp_path):
        from axiom.extensions.builtins.hygiene.agents.tidy.discover import (
            discover_local_repos,
        )

        _git_init(tmp_path / "axiom")
        _git_init(tmp_path / ".venv")

        found = [r.path.name for r in discover_local_repos(tmp_path)]
        assert found == ["axiom"]

    def test_returns_empty_when_no_repos(self, tmp_path):
        from axiom.extensions.builtins.hygiene.agents.tidy.discover import (
            discover_local_repos,
        )

        (tmp_path / "regular_dir").mkdir()
        assert discover_local_repos(tmp_path) == []

    def test_records_head_sha_for_dedup(self, tmp_path):
        from axiom.extensions.builtins.hygiene.agents.tidy.discover import (
            discover_local_repos,
        )

        repo = tmp_path / "alpha"
        _git_init(repo)
        (repo / "README.md").write_text("hello")
        assert_test_tmp_path(repo)
        env = git_isolated_env()
        subprocess.run(
            ["git", "add", "README.md"],
            cwd=repo, check=True, capture_output=True, env=env,
        )
        subprocess.run(
            ["git", "-c", "user.email=t@e.com", "-c", "user.name=t",
             "commit", "-m", "initial"],
            cwd=repo, check=True, capture_output=True, env=env,
        )

        found = discover_local_repos(tmp_path)
        assert len(found) == 1
        assert found[0].head_sha is not None
        assert len(found[0].head_sha) == 40  # full SHA-1
