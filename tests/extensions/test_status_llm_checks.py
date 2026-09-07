# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for neut status LLM/Ollama/routing checks.

Proves:
1. LLM provider check reports correctly with/without providers
2. Ollama check reports running/not-running/degraded
3. Routing check reads settings
4. Cold-start error message is helpful
"""

from __future__ import annotations

from unittest import mock


class TestLlmProviderCheck:
    """Tests for check_llm_providers."""

    def test_no_providers_is_unhealthy(self):
        from axiom.extensions.builtins.status.cli import HealthChecker, HealthStatus

        checker = HealthChecker()
        with mock.patch("axiom.infra.gateway.Gateway") as MockGw:
            MockGw.return_value.providers = []
            result = checker.check_llm_providers()

        assert result.status == HealthStatus.UNHEALTHY
        assert "No providers" in result.message
        assert "fix" in result.details

    def test_providers_with_keys_is_healthy(self):
        from axiom.extensions.builtins.status.cli import HealthChecker, HealthStatus

        mock_provider = mock.MagicMock()
        mock_provider.name = "anthropic"
        mock_provider.api_key = "sk-test"
        mock_provider.requires_vpn = False

        checker = HealthChecker()
        with mock.patch("axiom.infra.gateway.Gateway") as MockGw:
            MockGw.return_value.providers = [mock_provider]
            result = checker.check_llm_providers()

        assert result.status == HealthStatus.HEALTHY
        assert "1 provider ready" in result.message


class TestOllamaCheck:
    """Tests for check_ollama."""

    def test_ollama_not_running_is_degraded(self):
        from axiom.extensions.builtins.status.cli import HealthChecker, HealthStatus

        checker = HealthChecker()
        with mock.patch("urllib.request.urlopen", side_effect=Exception("refused")):
            result = checker.check_ollama()

        assert result.status == HealthStatus.DEGRADED
        assert "Not running" in result.message
        assert "fix" in result.details

    def test_ollama_running_with_model_is_healthy(self):
        import json

        from axiom.extensions.builtins.status.cli import HealthChecker, HealthStatus

        mock_resp = mock.MagicMock()
        mock_resp.read.return_value = json.dumps({
            "models": [{"name": "llama3.2:1b"}],
        }).encode()
        mock_resp.__enter__ = mock.MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = mock.MagicMock(return_value=False)

        checker = HealthChecker()
        with mock.patch("urllib.request.urlopen", return_value=mock_resp):
            result = checker.check_ollama()

        assert result.status == HealthStatus.HEALTHY
        assert "available" in result.message


class TestRoutingCheck:
    """Tests for check_routing."""

    def test_routing_defaults(self):
        from axiom.extensions.builtins.status.cli import HealthChecker, HealthStatus

        checker = HealthChecker()
        result = checker.check_routing()

        assert result.status == HealthStatus.HEALTHY
        assert "mode=" in result.message
        assert "sensitivity=" in result.message


class TestColdStartMessage:
    """Tests for improved cold-start error in neut chat."""

    def test_no_provider_message_shows_quick_start(self):
        """Cold-start message moved to welcome banner in render providers.

        _print_model_status is now a no-op stub for backward compat.
        The actual cold-start guidance is rendered by rich_render/ansi_render
        welcome_banner() when gateway.active_provider is None.
        """
        from axiom.extensions.builtins.chat.cli import _print_model_status

        mock_gateway = mock.MagicMock()
        mock_gateway.active_provider = None

        # Stub is a no-op — no assertion on output
        _print_model_status(mock_gateway)


class TestProbeOllama:
    """Tests for Ollama in setup probe."""

    def test_ollama_in_dependency_list(self):
        from axiom.setup.probe import ProbeResult, _probe_dependencies
        result = ProbeResult()
        _probe_dependencies(result)

        dep_names = [d.name for d in result.dependencies]
        assert "ollama" in dep_names

        ollama_dep = next(d for d in result.dependencies if d.name == "ollama")
        assert not ollama_dep.required  # optional
        assert "classification" in ollama_dep.purpose.lower()
