# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for setup friction reduction.

Validates that:
  - No credentials are marked required (all optional, skip-friendly)
  - Facility config auto-generates without prompts
  - Community pack auto-installs without prompts
  - PG password is generated and stored securely
  - Git hosting is flexible (GitHub first, GitLab optional)
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest


class TestCredentialGuides:
    def test_no_required_credentials(self):
        """No credential should block setup — all must be optional."""
        from axiom.setup.guides import CREDENTIAL_GUIDES

        required = [g for g in CREDENTIAL_GUIDES if g.required]
        assert required == [], (
            f"These credentials are marked required and will block setup: "
            f"{[g.env_var for g in required]}. "
            "All credentials should be optional to minimize setup friction."
        )

    def test_github_before_gitlab(self):
        """GitHub should appear before GitLab (more common)."""
        from axiom.setup.guides import CREDENTIAL_GUIDES

        git_guides = [g for g in CREDENTIAL_GUIDES if g.env_var in ("GITHUB_TOKEN", "GITLAB_TOKEN")]
        assert len(git_guides) == 2
        env_vars = [g.env_var for g in CREDENTIAL_GUIDES]
        gh_idx = env_vars.index("GITHUB_TOKEN")
        gl_idx = env_vars.index("GITLAB_TOKEN")
        assert gh_idx < gl_idx, "GITHUB_TOKEN should appear before GITLAB_TOKEN"

    def test_llm_keys_first(self):
        """LLM keys should be first to enable chat-assisted mode early."""
        from axiom.setup.guides import CREDENTIAL_GUIDES

        first_two = [g.env_var for g in CREDENTIAL_GUIDES[:2]]
        assert "ANTHROPIC_API_KEY" in first_two or "OPENAI_API_KEY" in first_two


class TestSecretsModule:
    def test_generate_password_length(self):
        from axiom.setup.secrets import generate_password

        pw = generate_password()
        assert len(pw) >= 16, "Generated password too short"

    def test_generate_password_unique(self):
        from axiom.setup.secrets import generate_password

        passwords = {generate_password() for _ in range(10)}
        assert len(passwords) == 10, "Generated passwords not unique"

    def test_env_file_roundtrip(self):
        from axiom.setup.secrets import _get_env_file, _store_env_file

        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            _store_env_file("TEST_KEY", "test_value_123", env_path)

            # Verify file permissions
            import stat
            mode = stat.S_IMODE(env_path.stat().st_mode)
            assert mode == 0o600, f"Expected chmod 600, got {oct(mode)}"

            # Read back
            value = _get_env_file("TEST_KEY", env_path)
            assert value == "test_value_123"

    def test_env_file_update_existing(self):
        from axiom.setup.secrets import _get_env_file, _store_env_file

        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            _store_env_file("KEY", "old", env_path)
            _store_env_file("KEY", "new", env_path)
            assert _get_env_file("KEY", env_path) == "new"
            # Should not duplicate
            lines = env_path.read_text().strip().splitlines()
            key_lines = [line for line in lines if line.startswith("KEY=")]
            assert len(key_lines) == 1

    def test_env_file_multiple_keys(self):
        from axiom.setup.secrets import _get_env_file, _store_env_file

        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            _store_env_file("A", "1", env_path)
            _store_env_file("B", "2", env_path)
            assert _get_env_file("A", env_path) == "1"
            assert _get_env_file("B", env_path) == "2"

    def test_store_and_get_secret_env_fallback(self):
        """store_secret + get_secret roundtrip via .env fallback."""
        from unittest.mock import patch

        from axiom.setup.secrets import get_secret, store_secret

        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            # Disable keychain so we test the .env fallback path
            with patch("axiom.setup.secrets._store_keychain", return_value=False), \
                 patch("axiom.setup.secrets._get_keychain", return_value=None):
                store_secret("TEST_ROUNDTRIP", "secret_value", env_path)
                value = get_secret("TEST_ROUNDTRIP", env_path)
                assert value == "secret_value"


class TestCommunityPackAutoInstall:
    def test_offer_community_pack_no_prompt(self):
        """offer_community_pack should not call input()."""
        import ast
        import inspect

        from axiom.setup.community_pack import offer_community_pack

        source = inspect.getsource(offer_community_pack)
        tree = ast.parse(source)

        # Check that input() is not called anywhere in the function
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name) and func.id == "input":
                    pytest.fail(
                        "offer_community_pack() calls input() — "
                        "community pack should auto-install without prompting"
                    )


class TestFacilityConfig:
    def test_no_facility_type_prompt(self):
        """Wizard should not prompt for facility type."""
        import inspect

        from axiom.setup.wizard import SetupWizard

        source = inspect.getsource(SetupWizard._phase_config)
        assert "_ask_facility_type" not in source, (
            "_phase_config still calls _ask_facility_type — "
            "facility type should auto-default to 'research'"
        )
        assert "prompt_text" not in source, (
            "_phase_config still prompts for facility name — "
            "should auto-generate from $USER"
        )


class TestDockerCompose:
    def test_pg_password_not_hardcoded(self):
        """docker-compose.yml must not have hardcoded PG password."""
        compose = Path(__file__).resolve().parents[1] / "src" / "axiom" / "setup" / "docker-compose.yml"
        if not compose.exists():
            pytest.skip("docker-compose.yml not found")
        content = compose.read_text()
        assert "POSTGRES_PASSWORD: axiom" not in content, (
            "docker-compose.yml has hardcoded password 'axiom'. "
            "Must use ${AXIOM_PG_PASSWORD:-axiom} with runtime generation."
        )
