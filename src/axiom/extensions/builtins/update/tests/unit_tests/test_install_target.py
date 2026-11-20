# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""`axi update` resolves its install source from builder-declared branding, so a
product can switch distribution channels without touching installer code.

Concretely: a consumer can flip public↔private (PyPI↔git) by setting/clearing
`update_repo_url` in its BrandingConfig — no change to `_update_deps`.
"""

from __future__ import annotations

from pathlib import Path

from axiom.extensions.builtins.update.cli import _resolve_install_target


class TestResolveInstallTarget:
    REPO = Path("/work/repo")

    def test_editable_dev_checkout(self):
        args, cwd, label = _resolve_install_target(
            is_editable=True, package_name="example-consumer",
            update_repo_url=None, repo_root=self.REPO,
        )
        assert label == "editable"
        assert args[:2] == ["install", "-e"] and ".[all]" in args
        assert cwd == self.REPO

    def test_pypi_is_the_default(self):
        # No update_repo_url -> public PyPI upgrade (covers axiom-os-lm, and
        # a consumer once it returns to PyPI).
        args, cwd, label = _resolve_install_target(
            is_editable=False, package_name="axiom-os-lm",
            update_repo_url=None, repo_root=self.REPO,
        )
        assert label == "pypi"
        assert args == ["install", "--upgrade", "axiom-os-lm", "-q"]
        assert "git+" not in " ".join(args)
        assert cwd is None

    def test_git_channel_when_builder_sets_repo_url(self):
        # Private/source distribution (a consumer today) -> git install.
        url = "https://github.com/example-org/example-consumer.git"
        args, cwd, label = _resolve_install_target(
            is_editable=False, package_name="example-consumer",
            update_repo_url=url, repo_root=self.REPO,
        )
        assert label == "git"
        assert "--upgrade" in args
        assert f"example-consumer @ git+{url}" in args
        assert cwd is None

    def test_git_channel_honors_explicit_ref(self):
        url = "https://github.com/example-org/example-consumer.git"
        args, _, label = _resolve_install_target(
            is_editable=False, package_name="example-consumer",
            update_repo_url=url, repo_root=self.REPO, ref="v1.2.0",
        )
        assert label == "git"
        assert f"example-consumer @ git+{url}@v1.2.0" in args

    def test_editable_takes_precedence_over_repo_url(self):
        # A dev checkout updates from local source regardless of channel config.
        args, cwd, label = _resolve_install_target(
            is_editable=True, package_name="example-consumer",
            update_repo_url="https://example.com/x.git", repo_root=self.REPO,
        )
        assert label == "editable"
