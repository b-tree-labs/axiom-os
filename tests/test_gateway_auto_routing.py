# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for `auto` routing mode and tier-hint plumbing.

Per spec-model-routing.md §14:
- §14.2 Configuration: [gateway] block with default_routing + auto_strategy
- §14.3 Tier hints: complete(tier_hint=...) threads into ModelContext
- §14.5 Per-prompt override: provider_override short-circuits auto
- §14.6 Failure semantics: ModelStrategyUnsatisfiable → legacy fallback
- §14.7 Migration: default is "pinned" when [gateway] block absent
"""

from __future__ import annotations

import textwrap
from unittest.mock import MagicMock, patch

# Reuse the test_gateway helpers — minimal duplication; the patterns
# already established there (LLMProvider construction + _gateway_with_providers)
# are the canonical entry point.

def _make_provider(
    name="p1",
    endpoint="https://api.example.com/v1",
    model="test-model",
    uid="",
    api_key_env="FAKE_KEY",
    priority=10,
    use_for=None,
    routing_tier="any",
    routing_tags=None,
    requires_vpn=False,
    verify_ssl=True,
    max_tokens_default=0,
):
    from axiom.infra.gateway import LLMProvider

    return LLMProvider(
        name=name,
        endpoint=endpoint,
        model=model,
        uid=uid or f"uid-{name}",
        api_key_env=api_key_env,
        priority=priority,
        use_for=use_for or ["fallback", "chat"],
        routing_tier=routing_tier,
        routing_tags=routing_tags or [],
        requires_vpn=requires_vpn,
        verify_ssl=verify_ssl,
        max_tokens_default=max_tokens_default,
    )


def _gateway_with_providers(providers, monkeypatch, gateway_block=""):
    """Build a Gateway from an inline llm-providers.toml with [gateway]."""
    monkeypatch.setenv("FAKE_KEY", "sk-test-fake-key")
    from axiom.infra.gateway import Gateway

    with patch.object(Gateway, "_load_config", lambda self: None):
        gw = Gateway()
    gw.providers = list(providers)
    gw.providers.sort(key=lambda p: p.priority)
    if gateway_block:
        # Hand-set the resolved gateway config so we can test the dispatch
        # branches without exercising the TOML parser in every test.
        import tomllib
        gw._gateway_config = tomllib.loads(gateway_block).get("gateway", {})
    return gw


# ---------------------------------------------------------------------------
# §14.2 — Configuration loading
# ---------------------------------------------------------------------------


class TestGatewayConfigBlock:
    def test_default_routing_auto_read_from_toml(self, monkeypatch, tmp_path):
        from axiom.infra.gateway import Gateway

        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "llm-providers.toml").write_text(textwrap.dedent('''
            [gateway]
            default_routing = "auto"
            auto_strategy = "cost-conservative"

            [[providers]]
            name = "p1"
            endpoint = "https://api.example.com/v1"
            model = "m1"
            api_key_env = "FAKE_KEY"
            priority = 10
        '''))
        monkeypatch.setenv("FAKE_KEY", "k")
        gw = Gateway(config_dir=config_dir)

        assert gw._gateway_config["default_routing"] == "auto"
        assert gw._gateway_config["auto_strategy"] == "cost-conservative"

    def test_default_routing_defaults_to_pinned_when_block_missing(
        self, monkeypatch, tmp_path
    ):
        from axiom.infra.gateway import Gateway

        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "llm-providers.toml").write_text(textwrap.dedent('''
            [[providers]]
            name = "p1"
            endpoint = "https://api.example.com/v1"
            model = "m1"
            api_key_env = "FAKE_KEY"
            priority = 10
        '''))
        monkeypatch.setenv("FAKE_KEY", "k")
        gw = Gateway(config_dir=config_dir)

        assert gw._gateway_config.get("default_routing", "pinned") == "pinned"


# ---------------------------------------------------------------------------
# §14.3 — Tier-hint plumbing
# ---------------------------------------------------------------------------


class TestTierHintOnModelContext:
    def test_model_context_has_tier_hint_field(self):
        from axiom.agents.strategy.types import (
            CohortModelPolicy,
            ModelContext,
            UserModelPolicy,
        )
        from axiom.vega.federation.policy import ClassificationStamp

        ctx = ModelContext(
            classification=ClassificationStamp.unclassified(),
            budget_remaining_usd=1.0,
            network_reachable=frozenset({"public"}),
            user_policy=UserModelPolicy(),
            cohort_policy=CohortModelPolicy(),
            available_providers={},
            tier_hint="simple",
        )
        assert ctx.tier_hint == "simple"

    def test_model_context_tier_hint_defaults_to_none(self):
        from axiom.agents.strategy.types import (
            CohortModelPolicy,
            ModelContext,
            UserModelPolicy,
        )
        from axiom.vega.federation.policy import ClassificationStamp

        ctx = ModelContext(
            classification=ClassificationStamp.unclassified(),
            budget_remaining_usd=1.0,
            network_reachable=frozenset({"public"}),
            user_policy=UserModelPolicy(),
            cohort_policy=CohortModelPolicy(),
            available_providers={},
        )
        assert ctx.tier_hint is None


# ---------------------------------------------------------------------------
# §14 — Routing dispatch
# ---------------------------------------------------------------------------


class TestAutoModeDispatch:
    def test_auto_mode_invokes_strategy_resolver(self, monkeypatch):
        """When default_routing=auto, Gateway resolves via ModelStrategy."""
        p = _make_provider(name="strategy-pick", priority=10)
        gw = _gateway_with_providers(
            [p], monkeypatch,
            gateway_block='[gateway]\ndefault_routing = "auto"\nauto_strategy = "cost-conservative"\n',
        )

        resolve_mock = MagicMock()
        with patch.object(gw, "_resolve_via_strategy", resolve_mock) as m:
            m.return_value = p
            with patch.object(gw, "_call_provider") as call_mock:
                call_mock.return_value = MagicMock(
                    text="ok", provider="strategy-pick", success=True
                )
                gw.complete(prompt="hello")

        resolve_mock.assert_called_once()

    def test_pinned_mode_skips_strategy(self, monkeypatch):
        """When default_routing=pinned (or missing), legacy path runs."""
        p = _make_provider(name="legacy-pick", priority=10)
        gw = _gateway_with_providers([p], monkeypatch)

        with patch.object(gw, "_resolve_via_strategy") as resolve_mock:
            with patch.object(gw, "_call_provider") as call_mock:
                call_mock.return_value = MagicMock(
                    text="ok", provider="legacy-pick", success=True
                )
                with patch("axiom.infra.gateway.Gateway._check_vpn", return_value=False):
                    gw.complete(prompt="hello")

        resolve_mock.assert_not_called()

    def test_provider_override_short_circuits_auto(self, monkeypatch):
        """§14.5: provider_override always wins, even in auto mode."""
        p_pin = _make_provider(name="pinned-by-user", priority=10)
        p_other = _make_provider(name="other", priority=20)
        gw = _gateway_with_providers(
            [p_pin, p_other], monkeypatch,
            gateway_block='[gateway]\ndefault_routing = "auto"\n',
        )
        gw.set_provider_override("pinned-by-user")

        with patch.object(gw, "_resolve_via_strategy") as resolve_mock:
            with patch.object(gw, "_call_provider") as call_mock:
                call_mock.return_value = MagicMock(
                    text="ok", provider="pinned-by-user", success=True
                )
                gw.complete(prompt="hello")

        # Override path wins, strategy resolver never consulted
        resolve_mock.assert_not_called()


class TestTierHintThreading:
    def test_complete_passes_tier_hint_to_strategy_path(self, monkeypatch):
        """tier_hint kwarg threads through to _resolve_via_strategy."""
        p = _make_provider(name="p", priority=10)
        gw = _gateway_with_providers(
            [p], monkeypatch,
            gateway_block='[gateway]\ndefault_routing = "auto"\n',
        )

        with patch.object(gw, "_resolve_via_strategy") as resolve_mock:
            resolve_mock.return_value = p
            with patch.object(gw, "_call_provider") as call_mock:
                call_mock.return_value = MagicMock(
                    text="ok", provider="p", success=True
                )
                gw.complete(prompt="x", tier_hint="simple")

        kwargs = resolve_mock.call_args.kwargs
        assert kwargs.get("tier_hint") == "simple"


# ---------------------------------------------------------------------------
# §14.6 — Failure semantics
# ---------------------------------------------------------------------------


class TestStrategyFailureFallback:
    def test_unsatisfiable_falls_back_to_legacy_select(self, monkeypatch):
        """If strategy raises ModelStrategyUnsatisfiable, gateway falls back
        to _select_provider so the prompt isn't dropped."""
        from axiom.agents.strategy.strategy import ModelStrategyUnsatisfiable
        from axiom.agents.strategy.types import ModelRole

        p = _make_provider(name="legacy-pick", priority=10)
        gw = _gateway_with_providers(
            [p], monkeypatch,
            gateway_block='[gateway]\ndefault_routing = "auto"\n',
        )

        def _explode(*a, **kw):
            raise ModelStrategyUnsatisfiable(ModelRole.EXECUTOR, ["all dead"])

        with patch.object(gw, "_resolve_via_strategy", side_effect=_explode):
            with patch.object(gw, "_call_provider") as call_mock:
                call_mock.return_value = MagicMock(
                    text="legacy fallback", provider="legacy-pick", success=True
                )
                with patch("axiom.infra.gateway.Gateway._check_vpn", return_value=False):
                    result = gw.complete(prompt="hi")

        # The call still happened via legacy path
        assert call_mock.call_count == 1
        called_provider = call_mock.call_args.args[0]
        assert called_provider.name == "legacy-pick"
        assert result.success
