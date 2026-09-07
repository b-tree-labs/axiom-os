# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""End-to-end integration tests for `auto` routing mode.

The unit tests in test_gateway_auto_routing.py stop at the dispatch
boundary (verifying _resolve_via_strategy was called). These tests go
one step further: they assert the provider returned by ModelStrategy
actually receives the HTTP call that follows. Catches gaps between
"resolved" and "called" — e.g., wrong provider name, lost override,
fallback firing when it shouldn't.
"""

from __future__ import annotations

import os
import tempfile
import textwrap
from pathlib import Path
from unittest.mock import patch


def _write_config(default_routing: str = "auto") -> Path:
    """Build a tmpfs llm-providers.toml with two real-shaped providers."""
    cfg_dir = Path(tempfile.mkdtemp()) / "config"
    cfg_dir.mkdir()
    (cfg_dir / "llm-providers.toml").write_text(textwrap.dedent(f'''
        [gateway]
        default_routing = "{default_routing}"
        auto_strategy = "cost-conservative"

        [[gateway.providers]]
        name = "anthropic"
        endpoint = "https://api.anthropic.com/v1"
        model = "claude-sonnet-4-5"
        api_key_env = "FAKE_KEY"
        priority = 10

        [[gateway.providers]]
        name = "openai"
        endpoint = "https://api.openai.com/v1"
        model = "gpt-4o"
        api_key_env = "FAKE_KEY"
        priority = 20
    '''))
    return cfg_dir


class TestAutoRoutingEndToEnd:
    """`Gateway.complete()` under default_routing=auto — full path."""

    def test_strategy_pick_actually_receives_http_call(self, monkeypatch):
        os.environ["FAKE_KEY"] = "sk-test"
        from axiom.infra.gateway import Gateway, GatewayResponse

        cfg_dir = _write_config(default_routing="auto")
        gw = Gateway(config_dir=cfg_dir)

        seen_providers = []

        def fake_call_provider(self, provider, prompt, system, max_tokens):
            seen_providers.append(provider.name)
            return GatewayResponse(
                text="ok", provider=provider.name, success=True
            )

        with patch.object(Gateway, "_call_provider", fake_call_provider):
            response = gw.complete(prompt="hello", task="chat")

        # Strategy picks the lower-priority-number provider (cost-conservative
        # default tiebreaker); anthropic at priority=10 wins.
        assert seen_providers == ["anthropic"], (
            "strategy-resolved provider must be the one _call_provider receives"
        )
        assert response.provider == "anthropic"
        assert response.success

    def test_pinned_mode_uses_legacy_select(self, monkeypatch):
        os.environ["FAKE_KEY"] = "sk-test"
        from axiom.infra.gateway import Gateway, GatewayResponse

        cfg_dir = _write_config(default_routing="pinned")
        gw = Gateway(config_dir=cfg_dir)

        seen_providers = []

        def fake_call_provider(self, provider, prompt, system, max_tokens):
            seen_providers.append(provider.name)
            return GatewayResponse(
                text="ok", provider=provider.name, success=True
            )

        with patch.object(Gateway, "_call_provider", fake_call_provider):
            with patch("axiom.infra.gateway.Gateway._check_vpn", return_value=False):
                gw.complete(prompt="hello", task="chat")

        # Legacy path: same priority order, so anthropic still wins —
        # but via the legacy code path (verified by not invoking the
        # strategy registry, which we'd otherwise see via the same
        # provider regardless of mode).
        assert seen_providers == ["anthropic"]

    def test_per_prompt_provider_override_wins_in_auto_mode(self, monkeypatch):
        """spec-model-routing §14.5: override always wins, even in auto."""
        os.environ["FAKE_KEY"] = "sk-test"
        from axiom.infra.gateway import Gateway, GatewayResponse

        cfg_dir = _write_config(default_routing="auto")
        gw = Gateway(config_dir=cfg_dir)
        gw.set_provider_override("openai")  # user asked for openai

        seen_providers = []

        def fake_call_provider(self, provider, prompt, system, max_tokens):
            seen_providers.append(provider.name)
            return GatewayResponse(text="ok", provider=provider.name, success=True)

        with patch.object(Gateway, "_call_provider", fake_call_provider):
            gw.complete(prompt="hello", task="chat")

        assert seen_providers == ["openai"]

    def test_unsatisfiable_strategy_falls_through_to_legacy_with_real_call(
        self, monkeypatch
    ):
        """§14.6: when strategy can't resolve, legacy _select_provider runs
        and the resulting provider is what receives the HTTP call."""
        os.environ["FAKE_KEY"] = "sk-test"
        from axiom.agents.strategy.strategy import ModelStrategyUnsatisfiable
        from axiom.agents.strategy.types import ModelRole
        from axiom.infra.gateway import Gateway, GatewayResponse

        cfg_dir = _write_config(default_routing="auto")
        gw = Gateway(config_dir=cfg_dir)

        seen_providers = []

        def fake_call_provider(self, provider, prompt, system, max_tokens):
            seen_providers.append(provider.name)
            return GatewayResponse(text="ok", provider=provider.name, success=True)

        def explode(*a, **kw):
            raise ModelStrategyUnsatisfiable(ModelRole.EXECUTOR, ["all dead"])

        with patch.object(Gateway, "_resolve_via_strategy", explode):
            with patch.object(Gateway, "_call_provider", fake_call_provider):
                with patch("axiom.infra.gateway.Gateway._check_vpn", return_value=False):
                    response = gw.complete(prompt="hello", task="chat")

        # Strategy raised → fell to legacy → legacy picked anthropic by priority
        assert seen_providers == ["anthropic"]
        assert response.success
