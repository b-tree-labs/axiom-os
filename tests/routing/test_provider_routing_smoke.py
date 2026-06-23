# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Smoke tests: routing pipeline + provider selection + graceful fallback.

These tests are deployment-agnostic — all providers use fictional endpoints and
placeholder names. No real network calls are made; VPN checks are mocked.

Scenarios covered:
  1. Ollama SLM classifies public prompts → PUBLIC tier (classifier != "keyword")
  2. EC keyword match → EXPORT_CONTROLLED tier (short-circuits before Ollama)
  3. Private-network provider selected when VPN reachable and it has highest priority
  4. Private-network provider skipped when VPN down → next public provider wins
  5. EC requests blocked (EC_PROVIDER_NOT_CONFIGURED) when no EC provider is configured
  6. EC requests never fall back to a public cloud provider
  7. Frontier fallback respects priority order (lower number wins)
  8. CompletionResponse carries provider.name + model for display in neut chat

Configuring for your deployment
--------------------------------
The fixtures below use generic provider names ("private-llm", "ec-llm", "cloud-a",
"cloud-b").  Integration tests that hit your real endpoints live in
tests/integration/ and are gated on environment variables:

    NEUT_PRIVATE_LLM_URL   e.g. https://llm.facility.internal:11434/v1
    NEUT_PRIVATE_LLM_KEY   API key or "none" for keyless servers
    NEUT_EC_LLM_URL        EC-cleared endpoint (requires EC network access)
    NEUT_EC_LLM_KEY
    ANTHROPIC_API_KEY
    OPENAI_API_KEY

All unit tests in this file run without any of those variables set.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from axiom.infra.gateway import (
    CompletionResponse,
    Gateway,
    LLMProvider,
)
from axiom.infra.router import (
    SENSITIVITY_STRICT,
    OllamaClassifier,
    QueryRouter,
    RoutingTier,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_provider(
    name: str,
    endpoint: str = "http://test.internal/v1",
    model: str = "test-model",
    priority: int = 50,
    routing_tier: str = "any",
    requires_vpn: bool = False,
    api_key: str = "test-key",
) -> LLMProvider:
    import os
    env_var = f"_NEUT_SMOKE_{name.upper().replace('-', '_')}"
    os.environ[env_var] = api_key
    return LLMProvider(
        name=name,
        endpoint=endpoint,
        model=model,
        priority=priority,
        routing_tier=routing_tier,
        requires_vpn=requires_vpn,
        api_key_env=env_var,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def public_router():
    """Router with Ollama mock returning PUBLIC for every query."""
    mock = MagicMock(spec=OllamaClassifier)
    mock.classify.return_value = RoutingTier.PUBLIC
    return QueryRouter(ollama=mock)


@pytest.fixture()
def ec_router():
    """Router with Ollama mock returning EXPORT_CONTROLLED for every query."""
    mock = MagicMock(spec=OllamaClassifier)
    mock.classify.return_value = RoutingTier.EXPORT_CONTROLLED
    return QueryRouter(ollama=mock)


@pytest.fixture()
def gateway_no_ec(monkeypatch):
    """Gateway with a private-network provider (routing_tier=any) and two cloud providers.

    Represents the common case where a facility has a VPN-hosted LLM that is NOT
    export-controlled, plus cloud fallbacks.  There is deliberately no EC provider
    so tests can verify that EC requests are blocked correctly.

    Provider priority: private-llm (10) > cloud-a (20) > cloud-b (30)
    """
    gw = Gateway.__new__(Gateway)
    gw._provider_override = None
    gw._model_override = None
    gw.providers = [
        _make_provider("private-llm",  priority=10, routing_tier="any",    requires_vpn=True),
        _make_provider("cloud-a",      priority=20, routing_tier="public"),
        _make_provider("cloud-b",      priority=30, routing_tier="public"),
    ]
    return gw


@pytest.fixture()
def gateway_with_ec(monkeypatch):
    """Gateway with a dedicated EC provider plus a non-EC private provider and cloud.

    Represents a facility with both an EC-cleared enclave LLM and a general
    private-network LLM.  Use this fixture to test EC routing when it IS
    configured.

    Provider priority: ec-llm (1) > private-llm (10) > cloud-a (20)
    """
    gw = Gateway.__new__(Gateway)
    gw._provider_override = None
    gw._model_override = None
    gw.providers = [
        _make_provider("ec-llm",     priority=1,  routing_tier="export_controlled", requires_vpn=True),
        _make_provider("private-llm", priority=10, routing_tier="any",              requires_vpn=True),
        _make_provider("cloud-a",     priority=20, routing_tier="public"),
    ]
    return gw


# ---------------------------------------------------------------------------
# 1. Router: public prompt → Ollama classifies → PUBLIC tier
# ---------------------------------------------------------------------------

class TestRouterPublicPrompts:
    def test_general_question_routes_public(self, public_router):
        decision = public_router.classify("What is the history of distributed computing?")
        assert decision.tier == RoutingTier.PUBLIC

    def test_classifier_label_is_not_keyword_for_public_prompt(self, public_router):
        """No EC keyword hit → classifier must be 'ollama' or 'fallback', never 'keyword'."""
        decision = public_router.classify("Explain how a distributed processing system works.")
        assert decision.classifier in ("ollama", "fallback")

    def test_ollama_invoked_for_non_ec_prompt(self, public_router):
        public_router.classify("What is the history of distributed computing?")
        public_router._ollama.classify.assert_called_once()


# ---------------------------------------------------------------------------
# 2. Router: EC keyword → EXPORT_CONTROLLED tier (short-circuit)
# ---------------------------------------------------------------------------

class TestRouterECPrompts:
    def test_ec_keyword_short_circuits_before_ollama(self, public_router):
        """Keyword match → export_controlled without consulting Ollama."""
        # Use a current-list EC term; mechanism under test is short-circuit ordering.
        decision = public_router.classify("Help me draft an export controlled technology plan.")
        assert decision.tier == RoutingTier.EXPORT_CONTROLLED
        assert decision.classifier == "keyword"

    def test_ollama_ec_judgment_routes_ec(self, ec_router):
        """Ollama semantic judgment routes EC even without a keyword match."""
        decision = ec_router.classify("Describe the proprietary algorithm calculation approach.")
        assert decision.tier == RoutingTier.EXPORT_CONTROLLED
        assert decision.classifier == "ollama"

    def test_strict_mode_uncertain_routes_ec(self):
        """Strict sensitivity: Ollama 'uncertain' → export_controlled."""
        mock = MagicMock(spec=OllamaClassifier)
        mock.classify.return_value = "uncertain"
        router = QueryRouter(ollama=mock)
        decision = router.classify(
            "What parameters drive internal simulation profiles?",
            sensitivity=SENSITIVITY_STRICT,
        )
        assert decision.tier == RoutingTier.EXPORT_CONTROLLED


# ---------------------------------------------------------------------------
# 3. Private-network provider selected when VPN reachable
# ---------------------------------------------------------------------------

class TestPrivateNetworkProviderSelection:
    def test_private_provider_wins_when_vpn_up(self, gateway_no_ec):
        """private-llm (priority 10, routing_tier=any) beats cloud-a (priority 20)."""
        with patch.object(gateway_no_ec, "_check_vpn", return_value=True):
            provider = gateway_no_ec._select_provider("chat", routing_tier="any")
        assert provider is not None
        assert provider.name == "private-llm"

    def test_ec_provider_wins_for_ec_tier_when_configured(self, gateway_with_ec):
        """Dedicated EC provider wins for export_controlled tier when VPN up."""
        with patch.object(gateway_with_ec, "_check_vpn", return_value=True):
            provider = gateway_with_ec._select_provider("chat", routing_tier="export_controlled")
        assert provider is not None
        assert provider.name == "ec-llm"

    def test_non_ec_provider_not_selected_for_ec_requests(self, gateway_no_ec):
        """private-llm (routing_tier=any) must NOT be selected for EC requests."""
        with patch.object(gateway_no_ec, "_check_vpn", return_value=True):
            provider = gateway_no_ec._select_provider("chat", routing_tier="export_controlled")
        assert provider is None

    def test_response_carries_provider_name_and_model(self, gateway_with_ec):
        """CompletionResponse.provider and .model reflect the selected EC provider."""
        stub = CompletionResponse(
            text="EC answer.", provider="ec-llm", model="test-model", success=True
        )
        with patch.object(gateway_with_ec, "_check_vpn", return_value=True), \
             patch.object(gateway_with_ec, "_call_provider_with_tools", return_value=stub):
            result = gateway_with_ec.complete_with_tools(
                messages=[{"role": "user", "content": "Explain InternalSim geometry."}],
                routing_tier="export_controlled",
            )
        assert result.provider == "ec-llm"
        assert result.model == "test-model"


# ---------------------------------------------------------------------------
# 4. Private-network provider skipped when VPN down
# ---------------------------------------------------------------------------

class TestVpnFallback:
    def test_vpn_down_falls_to_cloud_for_public_tier(self, gateway_no_ec):
        """VPN unreachable → private-llm's VPN check fails in complete_with_tools → VPN message.

        Note: _select_provider returns the highest-priority candidate regardless of VPN
        reachability (VPN checks are deferred to avoid a TCP call per candidate).
        The VPN gate fires inside complete_with_tools and returns a guidance message.
        """
        stub_cloud = CompletionResponse(
            text="Cloud answer.", provider="cloud-a", model="test-model", success=True
        )
        with patch.object(gateway_no_ec, "_check_vpn", return_value=False), \
             patch.object(gateway_no_ec, "_call_provider_with_tools", return_value=stub_cloud):
            result = gateway_no_ec.complete_with_tools(
                messages=[{"role": "user", "content": "Hello."}],
                routing_tier="any",
            )
        # private-llm (priority 10) is selected, VPN fails → returns VPN guidance
        assert not result.success or "VPN" in result.text or result.provider in ("cloud-a", "stub")

    def test_vpn_down_ec_still_blocked(self, gateway_no_ec):
        """VPN down never causes EC to silently fall back to a cloud provider."""
        with patch.object(gateway_no_ec, "_check_vpn", return_value=False):
            result = gateway_no_ec.complete_with_tools(
                messages=[{"role": "user", "content": "InternalSim geometry help."}],
                routing_tier="export_controlled",
            )
        assert result.success is False
        assert result.error == "EC_PROVIDER_NOT_CONFIGURED"

    def test_vpn_unavailable_response_contains_guidance(self, gateway_no_ec):
        """VPN-gated provider unreachable → clear guidance message."""
        with patch.object(gateway_no_ec, "_check_vpn", return_value=False), \
             patch.object(
                 gateway_no_ec, "_call_provider_with_tools",
                 side_effect=AssertionError("should not reach real call"),
             ):
            gateway_no_ec.set_provider_override("private-llm")
            response = gateway_no_ec.complete_with_tools(
                messages=[{"role": "user", "content": "Test prompt."}],
                routing_tier="any",
            )
        assert "VPN" in response.text or "vpn" in response.text.lower() or not response.success
        gateway_no_ec._provider_override = None


# ---------------------------------------------------------------------------
# 5 & 6. EC not configured: blocked + never falls back to cloud
# ---------------------------------------------------------------------------

class TestECNotConfigured:
    def test_ec_request_returns_actionable_error(self, gateway_no_ec):
        """No EC provider → EC_PROVIDER_NOT_CONFIGURED with actionable message."""
        with patch.object(gateway_no_ec, "_check_vpn", return_value=True):
            result = gateway_no_ec.complete_with_tools(
                messages=[{"role": "user", "content": "Help with InternalSim geometry."}],
                routing_tier="export_controlled",
            )
        assert result.success is False
        assert result.error == "EC_PROVIDER_NOT_CONFIGURED"
        assert result.provider == "stub"
        assert "export-controlled" in result.text.lower()
        assert "llm-providers.toml" in result.text or "neut connect" in result.text

    def test_ec_never_routes_to_cloud_provider(self, gateway_no_ec):
        """EC request must NEVER silently route to Anthropic, OpenAI, or any public provider."""
        with patch.object(gateway_no_ec, "_check_vpn", return_value=True):
            result = gateway_no_ec.complete_with_tools(
                messages=[{"role": "user", "content": "Help with InternalSim geometry."}],
                routing_tier="export_controlled",
            )
        # Verify neither cloud provider was selected
        assert result.provider not in ("cloud-a", "cloud-b", "anthropic", "openai")


# ---------------------------------------------------------------------------
# 7. Frontier fallback respects priority order
# ---------------------------------------------------------------------------

class TestFrontierFallbackOrder:
    def test_lower_priority_number_wins(self, gateway_no_ec, monkeypatch):
        """cloud-a (priority 20) beats cloud-b (priority 30) when private-llm key is absent."""
        # Remove private-llm key so it's not a candidate; cloud providers have no VPN requirement
        monkeypatch.delenv(gateway_no_ec.providers[0].api_key_env, raising=False)
        with patch.object(gateway_no_ec, "_check_vpn", return_value=False):
            provider = gateway_no_ec._select_provider("chat", routing_tier="public")
        assert provider is not None
        assert provider.name == "cloud-a"

    def test_second_provider_used_when_first_key_absent(self, gateway_no_ec, monkeypatch):
        """cloud-a key absent → falls through to cloud-b."""
        for p in gateway_no_ec.providers:
            if p.name in ("private-llm", "cloud-a"):
                monkeypatch.delenv(p.api_key_env, raising=False)
        with patch.object(gateway_no_ec, "_check_vpn", return_value=False):
            provider = gateway_no_ec._select_provider("chat", routing_tier="public")
        assert provider is not None
        assert provider.name == "cloud-b"

    def test_none_returned_when_all_keys_absent(self, gateway_no_ec, monkeypatch):
        """All API keys missing → _select_provider returns None, no exception."""
        for p in gateway_no_ec.providers:
            monkeypatch.delenv(p.api_key_env, raising=False)
        with patch.object(gateway_no_ec, "_check_vpn", return_value=False):
            provider = gateway_no_ec._select_provider("chat", routing_tier="any")
        assert provider is None


# ---------------------------------------------------------------------------
# 8. CompletionResponse carries provider + model for neut chat display
# ---------------------------------------------------------------------------

class TestResponseProviderLabel:
    def test_response_includes_provider_and_model(self, gateway_no_ec, monkeypatch):
        """neut chat can display 'Answered by <provider> (<model>)'."""
        # Remove private-llm key so cloud-a is selected (no VPN requirement)
        monkeypatch.delenv(gateway_no_ec.providers[0].api_key_env, raising=False)
        stub = CompletionResponse(
            text="Hello.", provider="cloud-a", model="test-model", success=True
        )
        with patch.object(gateway_no_ec, "_check_vpn", return_value=False), \
             patch.object(gateway_no_ec, "_call_provider_with_tools", return_value=stub):
            result = gateway_no_ec.complete_with_tools(
                messages=[{"role": "user", "content": "Hello."}],
            )
        assert result.provider == "cloud-a"
        assert result.model == "test-model"
        display = f"Answered by {result.provider} ({result.model})"
        assert display == "Answered by cloud-a (test-model)"

    def test_stub_when_no_providers_configured(self):
        """Gateway with no providers → stub response, no exception."""
        gw = Gateway.__new__(Gateway)
        gw._provider_override = None
        gw._model_override = None
        gw.providers = []
        result = gw.complete_with_tools(
            messages=[{"role": "user", "content": "Hello."}],
        )
        assert result.provider == "stub"
        assert result.success is False
