# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for chat friendly error formatting and secret redaction."""

from __future__ import annotations


class TestFriendlyErrors:
    """Tests for errors.friendly() — maps exceptions to helpful messages."""

    def test_provider_not_found(self):
        from axiom.extensions.builtins.chat.errors import friendly

        exc = ValueError("Provider not found: bogus")
        result = friendly(exc, providers=["anthropic", "ollama"])
        assert "/model" in result
        assert "anthropic" in result or "ollama" in result

    def test_session_not_found(self):
        from axiom.extensions.builtins.chat.errors import friendly

        exc = KeyError("session-abc123 not found")
        result = friendly(exc, providers=None)
        assert "/sessions" in result

    def test_rate_limit_error(self):
        from axiom.extensions.builtins.chat.errors import friendly

        exc = Exception("429 Too Many Requests: rate limit exceeded")
        result = friendly(exc)
        assert "wait" in result.lower() or "switch" in result.lower() or "rate" in result.lower()

    def test_network_error(self):
        from axiom.extensions.builtins.chat.errors import friendly

        exc = ConnectionError("Failed to establish a connection to api.anthropic.com")
        result = friendly(exc, provider="anthropic")
        assert "anthropic" in result.lower() or "reach" in result.lower()

    def test_auth_error(self):
        from axiom.extensions.builtins.chat.errors import friendly

        exc = Exception("401 Unauthorized: invalid API key")
        result = friendly(exc)
        assert "/model" in result or "key" in result.lower()

    def test_fallback_shows_first_line(self):
        from axiom.extensions.builtins.chat.errors import friendly

        exc = RuntimeError("Something unexpected happened\nWith details on line 2")
        result = friendly(exc)
        assert "Something unexpected happened" in result


class TestSecretRedaction:
    """Tests that secret tokens are redacted in friendly() fallback output."""

    def test_redacts_sk_key(self):
        from axiom.extensions.builtins.chat.errors import friendly

        exc = Exception("Error with key sk-test-abc123DEF456ghi789")
        result = friendly(exc)
        assert "sk-test-abc123DEF456ghi789" not in result
        assert "[redacted]" in result

    def test_redacts_bearer_token(self):
        from axiom.extensions.builtins.chat.errors import friendly

        exc = Exception("Authorization: Bearer eyJhbGciOiJSUzI1NiJ9.payload.sig")
        result = friendly(exc)
        assert "eyJhbGciOiJSUzI1NiJ9.payload.sig" not in result
        assert "[redacted]" in result

    def test_redacts_org_id(self):
        from axiom.extensions.builtins.chat.errors import friendly

        exc = Exception("org-abcXYZ123 is not authorized")
        result = friendly(exc)
        assert "org-abcXYZ123" not in result
        assert "[redacted]" in result

    def test_redacts_env_key_value(self):
        from axiom.extensions.builtins.chat.errors import friendly

        exc = Exception("ANTHROPIC_API_KEY=sk-ant-super-secret leaked in config")
        result = friendly(exc)
        assert "sk-ant-super-secret" not in result
        assert "[redacted]" in result
