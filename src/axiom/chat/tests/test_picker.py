# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for per-prompt provider override parsing.

Per spec-chat-model-picker.md §3.1 (@provider prefix) + §3.2 (/m short
slash). Test plan derived from the spec rules:

- `@` must be first non-whitespace character of the line
- Name matches `[a-zA-Z0-9_-]+` greedily; ends at first whitespace
- Provider name match is case-insensitive against gateway provider list
- Unknown names produce an inline error before the turn fires
- `/m <name> <prompt>` is equivalent to `@<name> <prompt>`
- The override prefix is stripped before the prompt is sent
"""

from __future__ import annotations


class TestParsePerPromptOverride:
    """Pure parser — no gateway, no agent, no chat REPL."""

    def test_plain_prompt_returns_none_override(self):
        from axiom.chat.picker import parse_per_prompt_override

        result = parse_per_prompt_override("explain BWR primary loops")
        assert result.override_name is None
        assert result.stripped_prompt == "explain BWR primary loops"
        assert result.syntax is None

    def test_at_prefix_extracts_provider_name(self):
        from axiom.chat.picker import parse_per_prompt_override

        result = parse_per_prompt_override("@anthropic explain BWR primary loops")
        assert result.override_name == "anthropic"
        assert result.stripped_prompt == "explain BWR primary loops"
        assert result.syntax == "at"

    def test_at_prefix_with_leading_whitespace(self):
        """Spec §3.1: '@ must be first non-whitespace character'."""
        from axiom.chat.picker import parse_per_prompt_override

        result = parse_per_prompt_override("   @anthropic hello")
        assert result.override_name == "anthropic"
        assert result.stripped_prompt == "hello"

    def test_at_in_middle_of_prompt_is_not_override(self):
        """email@example.com inside a prompt is not an override."""
        from axiom.chat.picker import parse_per_prompt_override

        result = parse_per_prompt_override("contact me at user@example.com")
        assert result.override_name is None
        assert result.stripped_prompt == "contact me at user@example.com"

    def test_provider_name_accepts_kebab_and_underscores(self):
        from axiom.chat.picker import parse_per_prompt_override

        result = parse_per_prompt_override("@local-qwen-rag explain X")
        assert result.override_name == "local-qwen-rag"
        assert result.stripped_prompt == "explain X"

        result2 = parse_per_prompt_override("@my_local_llm hello")
        assert result2.override_name == "my_local_llm"

    def test_slash_m_short_form(self):
        """Spec §3.2: '/m local-qwen explain X' is equivalent to @local-qwen."""
        from axiom.chat.picker import parse_per_prompt_override

        result = parse_per_prompt_override("/m local-qwen explain X")
        assert result.override_name == "local-qwen"
        assert result.stripped_prompt == "explain X"
        assert result.syntax == "slash_m"

    def test_slash_m_without_prompt_is_an_error(self):
        """`/m anthropic` with no follow-up text is malformed."""
        from axiom.chat.picker import parse_per_prompt_override

        result = parse_per_prompt_override("/m anthropic")
        assert result.override_name == "anthropic"
        assert result.stripped_prompt == ""

    def test_slash_m_requires_space_after(self):
        """`/model` is the session-default command, NOT picker override."""
        from axiom.chat.picker import parse_per_prompt_override

        result = parse_per_prompt_override("/model anthropic")
        assert result.override_name is None
        assert result.stripped_prompt == "/model anthropic"


class TestResolveProviderName:
    """Provider-name validation against the gateway provider list."""

    def test_known_name_returns_canonical(self):
        from axiom.chat.picker import resolve_provider_name

        assert resolve_provider_name("anthropic", ["anthropic", "openai"]) == "anthropic"

    def test_case_insensitive_match(self):
        """Spec §3.1: 'matched against … name field (case-insensitive)'."""
        from axiom.chat.picker import resolve_provider_name

        assert resolve_provider_name("ANTHROPIC", ["anthropic"]) == "anthropic"
        assert resolve_provider_name("Anthropic", ["anthropic"]) == "anthropic"

    def test_unknown_name_returns_none(self):
        from axiom.chat.picker import resolve_provider_name

        assert resolve_provider_name("nonsense", ["anthropic", "openai"]) is None


class TestUnknownProviderError:
    """Spec §3.1: 'Unknown names produce an inline error before the turn fires.'"""

    def test_error_message_lists_known_providers(self):
        from axiom.chat.picker import format_unknown_provider_error

        msg = format_unknown_provider_error("foo", ["anthropic", "local-qwen", "openai"])
        assert "Unknown provider 'foo'" in msg
        assert "anthropic" in msg
        assert "local-qwen" in msg
        assert "openai" in msg

    def test_error_message_points_to_model_command(self):
        from axiom.chat.picker import format_unknown_provider_error

        msg = format_unknown_provider_error("foo", ["anthropic"])
        assert "/model" in msg

    def test_empty_provider_list_still_renders(self):
        from axiom.chat.picker import format_unknown_provider_error

        msg = format_unknown_provider_error("foo", [])
        assert "Unknown provider 'foo'" in msg


# ---------------------------------------------------------------------------
# §3 one-turn override applier — the context-manager that agent.turn() uses
# ---------------------------------------------------------------------------


class _FakeProvider:
    def __init__(self, name):
        self.name = name


class _FakeGateway:
    """Minimum surface required by apply_per_prompt_override."""

    def __init__(self, provider_names, prior_override=None):
        self.providers = [_FakeProvider(n) for n in provider_names]
        self._provider_override = prior_override
        self.set_calls: list[str | None] = []

    def set_provider_override(self, name):
        self.set_calls.append(name)
        self._provider_override = name


class TestApplyPerPromptOverride:
    def test_no_override_passes_prompt_through(self):
        from axiom.chat.picker import apply_per_prompt_override

        gw = _FakeGateway(["anthropic", "openai"])
        with apply_per_prompt_override("hello world", gw) as outcome:
            assert outcome.error_message is None
            assert outcome.stripped_prompt == "hello world"
            assert outcome.override_applied is None
        assert gw.set_calls == []

    def test_valid_override_sets_then_restores(self):
        from axiom.chat.picker import apply_per_prompt_override

        gw = _FakeGateway(["anthropic", "openai"], prior_override="openai")
        with apply_per_prompt_override("@anthropic explain X", gw) as outcome:
            assert outcome.error_message is None
            assert outcome.stripped_prompt == "explain X"
            assert outcome.override_applied == "anthropic"
            # During the block: override is in effect
            assert gw._provider_override == "anthropic"
        # After exit: prior override is restored
        assert gw._provider_override == "openai"
        assert gw.set_calls == ["anthropic", "openai"]

    def test_unknown_override_returns_error_and_does_not_set(self):
        from axiom.chat.picker import apply_per_prompt_override

        gw = _FakeGateway(["anthropic", "openai"], prior_override="openai")
        with apply_per_prompt_override("@nonsense explain X", gw) as outcome:
            assert outcome.error_message is not None
            assert "Unknown provider 'nonsense'" in outcome.error_message
            assert outcome.override_applied is None
            # Original override unchanged
            assert gw._provider_override == "openai"
        assert gw.set_calls == []

    def test_case_insensitive_resolution_uses_canonical_name(self):
        from axiom.chat.picker import apply_per_prompt_override

        gw = _FakeGateway(["anthropic"])
        with apply_per_prompt_override("@ANTHROPIC hi", gw) as outcome:
            assert outcome.override_applied == "anthropic"  # canonical form

    def test_slash_m_form_works_the_same(self):
        from axiom.chat.picker import apply_per_prompt_override

        gw = _FakeGateway(["local-qwen"])
        with apply_per_prompt_override("/m local-qwen explain X", gw) as outcome:
            assert outcome.error_message is None
            assert outcome.stripped_prompt == "explain X"
            assert outcome.override_applied == "local-qwen"

    def test_restore_runs_even_when_block_raises(self):
        from axiom.chat.picker import apply_per_prompt_override

        gw = _FakeGateway(["anthropic"], prior_override=None)
        try:
            with apply_per_prompt_override("@anthropic boom", gw):
                raise RuntimeError("simulated turn failure")
        except RuntimeError:
            pass
        assert gw._provider_override is None  # restored to prior
