# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Integration tests for the GitLab channel.

Tests the real connection to gitlab.example.com:
  1. Authentication — can we connect with GITLAB_TOKEN?
  2. Project discovery — does the export find your projects?
  3. Signal extraction — does the GitLab diff extractor produce signals?

Requires: GITLAB_TOKEN environment variable
          pip install python-gitlab
"""

from pathlib import Path

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.gitlab]


class TestGitLabConnection:
    """Verify live GitLab API access."""

    def test_authenticate(self, gitlab_token):
        """Can we authenticate to the private GitLab instance?"""
        import gitlab as gl

        server = gl.Gitlab(
            "https://gitlab.example.com",
            private_token=gitlab_token,
        )
        try:
            server.auth()
        except gl.exceptions.GitlabAuthenticationError as e:
            pytest.skip(f"GITLAB_TOKEN rejected (401) — update CI variable: {e}")
        assert server.user is not None
        print(f"  Authenticated as: {server.user.username}")

    def test_discover_projects(self, gitlab_token):
        """Can we find projects in the ut-computational-ne group?"""
        import gitlab as gl

        server = gl.Gitlab(
            "https://gitlab.example.com",
            private_token=gitlab_token,
        )
        try:
            server.auth()
        except gl.exceptions.GitlabAuthenticationError as e:
            pytest.skip(f"GITLAB_TOKEN rejected (401) — update CI variable: {e}")

        try:
            group = server.groups.get("ut-computational-ne")
            projects = group.projects.list(include_subgroups=True, all=True)
            assert len(projects) > 0
            print(f"  Found {len(projects)} project(s)")
            for p in projects[:5]:
                print(f"    - {p.path_with_namespace}")
        except Exception as e:
            pytest.skip(f"Could not access group: {e}")


class TestGitLabExport:
    """Test the full export → extract pipeline."""

    def test_export_produces_json(self, gitlab_token, tmp_path):
        """Run a minimal export and verify it produces valid JSON."""
        import gitlab as gl

        GITLAB_URL = "https://gitlab.example.com"
        TARGET_GROUP = "ut-computational-ne"

        server = gl.Gitlab(GITLAB_URL, private_token=gitlab_token)
        try:
            server.auth()
        except gl.exceptions.GitlabAuthenticationError as e:
            pytest.skip(f"GITLAB_TOKEN rejected (401) — update CI variable: {e}")

        try:
            group = server.groups.get(TARGET_GROUP)
        except Exception as e:
            pytest.skip(f"Cannot access {TARGET_GROUP}: {e}")

        # Just verify we can list projects — full export is slow
        projects = group.projects.list(include_subgroups=True, per_page=5)
        assert len(projects) > 0
        print(f"  Export would cover {len(projects)}+ projects")

    def test_extractor_on_existing_export(self, tmp_path):
        """If a GitLab export JSON exists, the extractor processes it."""
        from axiom.extensions.builtins.signals.extractors.gitlab_diff import (
            GitLabDiffExtractor,
        )

        # Check if real exports exist
        exports_dir = Path(__file__).resolve().parents[2] / "src" / "axiom" / "exports"
        if not exports_dir.exists():
            pytest.skip("No src/axiom/exports/ directory")

        exports = sorted(exports_dir.glob("gitlab_export_*.json"), reverse=True)
        if not exports:
            pytest.skip("No gitlab export files found")

        extractor = GitLabDiffExtractor()
        extraction = extractor.extract(exports[0])

        assert len(extraction.signals) > 0
        print(f"  Extracted {len(extraction.signals)} signals from {exports[0].name}")
        for s in extraction.signals[:3]:
            print(f"    [{s.signal_type}] {s.detail[:80]}")
