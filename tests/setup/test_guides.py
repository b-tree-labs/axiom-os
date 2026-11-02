# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for axiom.setup.guides."""


from axiom.setup.guides import (
    CREDENTIAL_GUIDES,
    get_guide,
    get_llm_guides,
    get_optional_guides,
    get_required_guides,
)


class TestCredentialGuide:
    def test_all_guides_have_required_fields(self):
        for guide in CREDENTIAL_GUIDES:
            assert guide.env_var, f"Missing env_var on {guide}"
            assert guide.display_name, f"Missing display_name on {guide.env_var}"
            assert guide.description, f"Missing description on {guide.env_var}"
            assert guide.steps, f"Missing steps on {guide.env_var}"
            assert guide.url, f"Missing url on {guide.env_var}"

    def test_no_jargon_in_display_names(self):
        forbidden = ["API key", "token", "OAuth", "environment variable", "env var"]
        for guide in CREDENTIAL_GUIDES:
            for term in forbidden:
                assert term not in guide.display_name, (
                    f"Jargon '{term}' found in display_name of {guide.env_var}"
                )

    def test_no_jargon_in_descriptions(self):
        forbidden = ["API key", "environment variable", "env var"]
        for guide in CREDENTIAL_GUIDES:
            for term in forbidden:
                assert term not in guide.description, (
                    f"Jargon '{term}' found in description of {guide.env_var}"
                )


class TestValidators:
    def test_gitlab_token_valid(self):
        guide = get_guide("GITLAB_TOKEN")
        assert guide is not None
        assert guide.validate("glpat-abcdefghij1234") is True

    def test_gitlab_token_invalid(self):
        guide = get_guide("GITLAB_TOKEN")
        assert guide is not None
        assert guide.validate("not-a-token") is False

    def test_ms_uuid_valid(self):
        guide = get_guide("MS_GRAPH_CLIENT_ID")
        assert guide is not None
        assert guide.validate("12345678-1234-1234-1234-123456789abc") is True

    def test_ms_uuid_invalid(self):
        guide = get_guide("MS_GRAPH_CLIENT_ID")
        assert guide is not None
        assert guide.validate("not-a-uuid") is False

    def test_ms_secret_valid(self):
        guide = get_guide("MS_GRAPH_CLIENT_SECRET")
        assert guide is not None
        assert guide.validate("a_long_enough_secret") is True

    def test_ms_secret_too_short(self):
        guide = get_guide("MS_GRAPH_CLIENT_SECRET")
        assert guide is not None
        assert guide.validate("short") is False

    def test_anthropic_key_valid(self):
        guide = get_guide("ANTHROPIC_API_KEY")
        assert guide is not None
        assert guide.validate("sk-ant-abcdefghij1234567890") is True

    def test_anthropic_key_invalid(self):
        guide = get_guide("ANTHROPIC_API_KEY")
        assert guide is not None
        assert guide.validate("sk-not-anthropic") is False

    def test_openai_key_valid(self):
        guide = get_guide("OPENAI_API_KEY")
        assert guide is not None
        assert guide.validate("sk-1234567890abcdefgh") is True

    def test_openai_key_invalid(self):
        guide = get_guide("OPENAI_API_KEY")
        assert guide is not None
        assert guide.validate("bad") is False

    def test_linear_key_valid(self):
        guide = get_guide("LINEAR_API_KEY")
        assert guide is not None
        assert guide.validate("lin_api_1234567890abc") is True

    def test_linear_key_invalid(self):
        guide = get_guide("LINEAR_API_KEY")
        assert guide is not None
        assert guide.validate("lin_not_right") is False


class TestLookups:
    def test_get_guide_found(self):
        guide = get_guide("GITLAB_TOKEN")
        assert guide is not None
        assert guide.env_var == "GITLAB_TOKEN"

    def test_get_guide_not_found(self):
        assert get_guide("NONEXISTENT") is None

    def test_required_guides(self):
        required = get_required_guides()
        assert all(g.required for g in required)
        # All credentials are now optional to minimize setup friction

    def test_optional_guides(self):
        optional = get_optional_guides()
        assert all(not g.required for g in optional)

    def test_llm_guides(self):
        llm = get_llm_guides()
        envs = {g.env_var for g in llm}
        assert "ANTHROPIC_API_KEY" in envs
        assert "OPENAI_API_KEY" in envs

    def test_llm_guides_first_in_order(self):
        """LLM keys should come before other credentials."""
        llm_envs = {"ANTHROPIC_API_KEY", "OPENAI_API_KEY"}
        first_non_llm_idx = None
        last_llm_idx = None
        for i, guide in enumerate(CREDENTIAL_GUIDES):
            if guide.env_var in llm_envs:
                last_llm_idx = i
            elif first_non_llm_idx is None:
                first_non_llm_idx = i
        if last_llm_idx is not None and first_non_llm_idx is not None:
            assert last_llm_idx < first_non_llm_idx, (
                "LLM credential guides should come before other credentials"
            )
