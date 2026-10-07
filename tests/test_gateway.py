# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the LLM gateway provider selection logic.

Tests the Gateway._select_provider routing algorithm, fallback behavior,
configuration loading, provider identity, and observability — all without
making real HTTP calls.
"""

from __future__ import annotations

import textwrap
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# We need to mock heavy imports before importing gateway.  The gateway module
# imports axiom (which resolves REPO_ROOT) and provider_base at module level.
# We patch minimally so imports succeed in a test environment.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch, tmp_path):
    """Ensure tests don't leak env vars or touch real config."""
    monkeypatch.delenv("FAKE_KEY", raising=False)
    monkeypatch.delenv("MISSING_KEY", raising=False)


# ---------------------------------------------------------------------------
# Helpers to build a Gateway with fake providers (no TOML, no filesystem)
# ---------------------------------------------------------------------------

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
        use_for=use_for or ["fallback"],
        routing_tier=routing_tier,
        routing_tags=routing_tags or [],
        requires_vpn=requires_vpn,
        verify_ssl=verify_ssl,
        max_tokens_default=max_tokens_default,
    )


def _gateway_with_providers(providers, monkeypatch):
    """Build a Gateway without reading any config file."""
    monkeypatch.setenv("FAKE_KEY", "sk-test-fake-key")

    from axiom.infra.gateway import Gateway

    with patch.object(Gateway, "_load_config", lambda self: None):
        gw = Gateway()
    gw.providers = list(providers)
    gw.providers.sort(key=lambda p: p.priority)
    return gw


# ===========================================================================
# Provider Selection
# ===========================================================================


class TestProviderSelection:
    """Tests for Gateway._select_provider."""

    def test_selects_highest_priority(self, monkeypatch):
        p_low = _make_provider(name="low", priority=50)
        p_high = _make_provider(name="high", priority=1)
        gw = _gateway_with_providers([p_low, p_high], monkeypatch)

        with patch("axiom.infra.gateway.Gateway._check_vpn", return_value=False):
            selected = gw._select_provider("fallback")
        assert selected is not None
        assert selected.name == "high"

    def test_routing_tier_public_skips_ec(self, monkeypatch):
        p_ec = _make_provider(name="ec", routing_tier="export_controlled", priority=1)
        p_pub = _make_provider(name="pub", routing_tier="public", priority=10)
        gw = _gateway_with_providers([p_ec, p_pub], monkeypatch)

        selected = gw._select_provider("fallback", routing_tier="public")
        assert selected is not None
        assert selected.name == "pub"

    def test_ec_tier_only_selects_ec_provider(self, monkeypatch):
        p_any = _make_provider(name="any-tier", routing_tier="any", priority=1)
        p_ec = _make_provider(name="ec", routing_tier="export_controlled", priority=10)
        gw = _gateway_with_providers([p_any, p_ec], monkeypatch)

        selected = gw._select_provider("fallback", routing_tier="export_controlled")
        assert selected is not None
        assert selected.name == "ec"

    def test_ec_never_falls_back_to_public(self, monkeypatch):
        """EC request with no EC provider returns None (compliance)."""
        p_pub = _make_provider(name="pub", routing_tier="public", priority=1)
        gw = _gateway_with_providers([p_pub], monkeypatch)

        selected = gw._select_provider("fallback", routing_tier="export_controlled")
        assert selected is None

    def test_fallback_to_next_provider_when_first_unavailable(self, monkeypatch):
        """Provider without API key is skipped."""
        p1 = _make_provider(name="no-key", priority=1, api_key_env="MISSING_KEY")
        p2 = _make_provider(name="has-key", priority=10)
        gw = _gateway_with_providers([p1, p2], monkeypatch)

        selected = gw._select_provider("fallback")
        assert selected is not None
        assert selected.name == "has-key"

    def test_cli_provider_override(self, monkeypatch):
        p1 = _make_provider(name="default", priority=1)
        p2 = _make_provider(name="forced", priority=99)
        gw = _gateway_with_providers([p1, p2], monkeypatch)
        gw.set_provider_override("forced")

        selected = gw._select_provider("fallback")
        assert selected is not None
        assert selected.name == "forced"

    def test_cli_model_override(self, monkeypatch):
        p = _make_provider(name="p1", model="original-model", priority=1)
        gw = _gateway_with_providers([p], monkeypatch)
        gw.set_model_override("override-model")

        selected = gw._select_provider("fallback")
        assert selected is not None
        assert selected.model == "override-model"

    def test_vpn_provider_skipped_when_unreachable(self, monkeypatch):
        p_vpn = _make_provider(name="vpn", requires_vpn=True, priority=1)
        p_pub = _make_provider(name="pub", priority=10)
        gw = _gateway_with_providers([p_vpn, p_pub], monkeypatch)

        # The VPN provider is still in the candidates list — _select_provider
        # doesn't filter on VPN for the standard candidate path, only for
        # prefer_provider chain. So both are candidates, vpn wins on priority.
        # The VPN check happens at call-time (in generate()), not selection.
        # Let's verify the prefer_provider chain correctly skips VPN.
        with patch(
            "axiom.extensions.builtins.settings.store.SettingsStore"
        ) as MockSettings:
            mock_store = MagicMock()
            mock_store.get.side_effect = lambda key, default=None: {
                "routing.prefer_provider": ["vpn", "pub"],
                "routing.prefer_when": "reachable",
            }.get(key, default)
            MockSettings.return_value = mock_store

            with patch.object(gw, "_check_vpn", return_value=False):
                selected = gw._select_provider("fallback")

        assert selected is not None
        assert selected.name == "pub"

    def test_vpn_provider_used_when_reachable(self, monkeypatch):
        p_vpn = _make_provider(name="vpn", requires_vpn=True, priority=1)
        p_pub = _make_provider(name="pub", priority=10)
        gw = _gateway_with_providers([p_vpn, p_pub], monkeypatch)

        with patch(
            "axiom.extensions.builtins.settings.store.SettingsStore"
        ) as MockSettings:
            mock_store = MagicMock()
            mock_store.get.side_effect = lambda key, default=None: {
                "routing.prefer_provider": ["vpn"],
                "routing.prefer_when": "reachable",
            }.get(key, default)
            MockSettings.return_value = mock_store

            with patch.object(gw, "_check_vpn", return_value=True):
                selected = gw._select_provider("fallback")

        assert selected is not None
        assert selected.name == "vpn"

    def test_required_tags_filter(self, monkeypatch):
        p1 = _make_provider(name="tagged", routing_tags=["restricted"], priority=1)
        p2 = _make_provider(name="untagged", priority=5)
        gw = _gateway_with_providers([p1, p2], monkeypatch)

        selected = gw._select_provider("fallback", required_tags={"restricted"})
        assert selected is not None
        assert selected.name == "tagged"

    def test_relaxes_tags_keeps_tier(self, monkeypatch):
        """If no provider matches tags, relax tags but keep tier."""
        p = _make_provider(name="pub", routing_tier="public", priority=1)
        gw = _gateway_with_providers([p], monkeypatch)

        # Request tag that no provider has — should relax and still return pub
        selected = gw._select_provider(
            "fallback", routing_tier="public", required_tags={"nonexistent"}
        )
        assert selected is not None
        assert selected.name == "pub"


# ===========================================================================
# Fallback Behavior
# ===========================================================================


class TestFallbackBehavior:
    def test_all_providers_fail_returns_none(self, monkeypatch):
        """No usable providers → None."""
        p = _make_provider(name="no-key", api_key_env="MISSING_KEY")
        gw = _gateway_with_providers([p], monkeypatch)

        selected = gw._select_provider("fallback")
        assert selected is None

    def test_falls_through_chain(self, monkeypatch):
        """Multiple providers: first has no key, second works."""
        p1 = _make_provider(name="dead", api_key_env="MISSING_KEY", priority=1)
        p2 = _make_provider(name="dead2", api_key_env="MISSING_KEY", priority=2)
        p3 = _make_provider(name="alive", priority=3)
        gw = _gateway_with_providers([p1, p2, p3], monkeypatch)

        selected = gw._select_provider("fallback")
        assert selected is not None
        assert selected.name == "alive"

    def test_task_filter(self, monkeypatch):
        """Provider is selected only if its use_for includes the task."""
        p_signal = _make_provider(name="signal-only", use_for=["signal"], priority=1)
        p_fallback = _make_provider(name="fallback-ok", use_for=["fallback"], priority=10)
        gw = _gateway_with_providers([p_signal, p_fallback], monkeypatch)

        # Requesting "publish" — signal-only doesn't match, fallback does
        selected = gw._select_provider("publish")
        assert selected is not None
        assert selected.name == "fallback-ok"


# ===========================================================================
# Configuration
# ===========================================================================


class TestConfiguration:
    def test_load_from_toml(self, monkeypatch, tmp_path):
        """Gateway loads providers from a TOML config file."""
        monkeypatch.setenv("TEST_API_KEY", "sk-test")

        config_dir = tmp_path / "config"
        config_dir.mkdir()
        toml_content = textwrap.dedent("""\
            [gateway]
            [[gateway.providers]]
            name = "test-provider"
            uid = "uid-test-001"
            endpoint = "https://api.test.com/v1"
            model = "test-model"
            api_key_env = "TEST_API_KEY"
            priority = 5
            use_for = ["fallback"]
            routing_tier = "public"
        """)
        (config_dir / "llm-providers.toml").write_text(toml_content)

        from axiom.infra.gateway import Gateway

        with patch("axiom.infra.provider_base.ensure_provider_uids"):
            gw = Gateway(config_dir=config_dir)

        assert len(gw.providers) >= 1
        names = [p.name for p in gw.providers]
        assert "test-provider" in names

    def test_provider_without_api_key_skipped_in_selection(self, monkeypatch):
        p = _make_provider(name="no-key", api_key_env="NONEXISTENT_ENV_VAR")
        gw = _gateway_with_providers([p], monkeypatch)

        selected = gw._select_provider("fallback")
        assert selected is None

    def test_api_key_env_resolves(self, monkeypatch):
        monkeypatch.setenv("MY_SECRET_KEY", "sk-resolved")
        p = _make_provider(name="p1", api_key_env="MY_SECRET_KEY")
        assert p.api_key == "sk-resolved"

    def test_empty_provider_list(self, monkeypatch):
        gw = _gateway_with_providers([], monkeypatch)
        selected = gw._select_provider("fallback")
        assert selected is None


# ===========================================================================
# API key resolution — env var, then vault (store_credential)
# ===========================================================================


class TestApiKeyResolution:
    """LLMProvider.api_key resolves env var first, then the vault by name."""

    def test_api_key_env_takes_precedence_over_vault(self, monkeypatch):
        monkeypatch.setenv("FAKE_KEY", "sk-from-env")
        monkeypatch.setattr(
            "axiom.infra.connections.get_credential", lambda name: "sk-from-vault"
        )
        p = _make_provider(name="tejas", api_key_env="FAKE_KEY")
        assert p.api_key == "sk-from-env"

    def test_api_key_falls_back_to_vault_when_env_unset(self, monkeypatch):
        monkeypatch.delenv("FAKE_KEY", raising=False)
        monkeypatch.setattr(
            "axiom.infra.connections.get_credential", lambda name: f"sk-vault-{name}"
        )
        p = _make_provider(name="tejas", api_key_env="FAKE_KEY")
        assert p.api_key == "sk-vault-tejas"

    def test_api_key_resolves_from_vault_when_no_env_var_configured(self, monkeypatch):
        monkeypatch.setattr(
            "axiom.infra.connections.get_credential", lambda name: "sk-vault-only"
        )
        p = _make_provider(name="tejas", api_key_env="")
        assert p.api_key == "sk-vault-only"

    def test_api_key_none_when_neither_env_nor_vault(self, monkeypatch):
        monkeypatch.delenv("FAKE_KEY", raising=False)
        monkeypatch.setattr("axiom.infra.connections.get_credential", lambda name: None)
        p = _make_provider(name="tejas", api_key_env="FAKE_KEY")
        assert p.api_key is None


# ===========================================================================
# Export-controlled tier — fail-closed (no relaxation to non-EC providers)
# ===========================================================================


class TestExportControlledFailClosed:
    """An export_controlled request must NEVER be served by a non-EC provider,
    even when that means returning no provider at all. This is the compliance
    invariant: controlled content cannot egress to a public/cloud endpoint."""

    def test_ec_request_returns_none_when_no_ec_provider(self, monkeypatch):
        pub = _make_provider(name="pub", routing_tier="public", priority=1)
        anyp = _make_provider(name="anyp", routing_tier="any", priority=2)
        gw = _gateway_with_providers([pub, anyp], monkeypatch)

        with patch("axiom.infra.gateway.Gateway._check_vpn", return_value=False):
            selected = gw._select_provider("fallback", routing_tier="export_controlled")
        assert selected is None

    def test_ec_request_does_not_relax_to_public_as_last_resort(self, monkeypatch):
        # Only a public provider exists; EC must still refuse it.
        pub = _make_provider(name="pub", routing_tier="public", priority=1)
        gw = _gateway_with_providers([pub], monkeypatch)

        with patch("axiom.infra.gateway.Gateway._check_vpn", return_value=False):
            selected = gw._select_provider("synthesis", routing_tier="export_controlled")
        assert selected is None

    def test_ec_request_selects_ec_provider_when_present(self, monkeypatch):
        pub = _make_provider(name="pub", routing_tier="public", priority=1)
        ec = _make_provider(name="ec", routing_tier="export_controlled", priority=2)
        gw = _gateway_with_providers([pub, ec], monkeypatch)

        with patch("axiom.infra.gateway.Gateway._check_vpn", return_value=False):
            selected = gw._select_provider("fallback", routing_tier="export_controlled")
        assert selected is not None
        assert selected.name == "ec"

    def test_prefer_picks_named_provider_within_tier(self, monkeypatch):
        a = _make_provider(name="a", priority=1)
        b = _make_provider(name="b", priority=50)
        gw = _gateway_with_providers([a, b], monkeypatch)
        with patch("axiom.infra.gateway.Gateway._check_vpn", return_value=False):
            sel = gw._select_provider("fallback", prefer="b")
        assert sel is not None and sel.name == "b"  # beats higher-priority 'a'

    def test_prefer_cannot_escape_ec_tier(self, monkeypatch):
        pub = _make_provider(name="pub", routing_tier="public", priority=1)
        ec = _make_provider(name="ec", routing_tier="export_controlled", priority=2)
        gw = _gateway_with_providers([pub, ec], monkeypatch)
        with patch("axiom.infra.gateway.Gateway._check_vpn", return_value=False):
            sel = gw._select_provider("fallback", routing_tier="export_controlled", prefer="pub")
        assert sel is not None and sel.name == "ec"  # prefer ignored; tier wins


# ===========================================================================
# Provider Identity
# ===========================================================================


class TestProviderIdentity:
    def test_provider_has_stable_uid(self, monkeypatch):
        monkeypatch.setenv("FAKE_KEY", "sk-test")
        p = _make_provider(name="p1", uid="my-stable-uid")
        assert p.uid == "my-stable-uid"

    def test_uid_auto_generated_when_blank(self, monkeypatch):
        monkeypatch.setenv("FAKE_KEY", "sk-test")
        p = _make_provider(name="p1", uid="")
        # uid should have been auto-generated (non-empty)
        assert p.uid != ""

    def test_config_hash_changes_on_config_change(self, monkeypatch):
        monkeypatch.setenv("FAKE_KEY", "sk-test")
        p1 = _make_provider(name="p1", endpoint="https://a.com", model="m1")
        p2 = _make_provider(name="p1", endpoint="https://b.com", model="m1")
        assert p1.config_hash != p2.config_hash

    def test_instance_id_changes_on_reload(self, monkeypatch):
        monkeypatch.setenv("FAKE_KEY", "sk-test")
        p1 = _make_provider(name="p1", uid="same-uid")
        p2 = _make_provider(name="p1", uid="same-uid")
        # instance_id is a fresh UUID4 each time
        assert p1.instance_id != p2.instance_id

    def test_config_hash_stable_for_same_config(self, monkeypatch):
        monkeypatch.setenv("FAKE_KEY", "sk-test")
        p1 = _make_provider(name="p1", uid="u1", endpoint="https://a.com", model="m1")
        p2 = _make_provider(name="p1", uid="u1", endpoint="https://a.com", model="m1")
        assert p1.config_hash == p2.config_hash


# ===========================================================================
# VPN Check
# ===========================================================================


class TestVPNCheck:
    def test_check_vpn_returns_true_on_success(self, monkeypatch):
        monkeypatch.setenv("FAKE_KEY", "sk-test")
        gw = _gateway_with_providers([], monkeypatch)
        p = _make_provider(name="vpn", endpoint="https://vpn.internal:443/v1", requires_vpn=True)

        with patch("socket.create_connection") as mock_conn:
            mock_conn.return_value.__enter__ = MagicMock()
            mock_conn.return_value.__exit__ = MagicMock(return_value=False)
            assert gw._check_vpn(p) is True

    def test_check_vpn_returns_false_on_timeout(self, monkeypatch):
        monkeypatch.setenv("FAKE_KEY", "sk-test")
        gw = _gateway_with_providers([], monkeypatch)
        p = _make_provider(name="vpn", endpoint="https://vpn.internal:443/v1", requires_vpn=True)

        with patch("socket.create_connection", side_effect=OSError("timeout")):
            assert gw._check_vpn(p) is False


# ===========================================================================
# Observability
# ===========================================================================


class TestObservability:
    def test_provider_identity_dict(self, monkeypatch):
        """Provider exposes an identity dict for structured logging."""
        monkeypatch.setenv("FAKE_KEY", "sk-test")
        p = _make_provider(name="observable", uid="obs-uid-123")
        ident = p.identity
        assert isinstance(ident, dict)
        assert "obs-uid-123" in str(ident.values())

    def test_providers_logged_on_load(self, monkeypatch, tmp_path, caplog):
        """Config load logs each provider's identity."""
        monkeypatch.setenv("TEST_KEY", "sk-test")
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        toml_content = textwrap.dedent("""\
            [gateway]
            [[gateway.providers]]
            name = "log-test"
            uid = "uid-log-001"
            endpoint = "https://api.test.com/v1"
            model = "test-model"
            api_key_env = "TEST_KEY"
            priority = 5
        """)
        (config_dir / "llm-providers.toml").write_text(toml_content)

        import logging

        from axiom.infra.gateway import Gateway

        with caplog.at_level(logging.INFO, logger="axiom.llm.gateway"):
            with patch("axiom.infra.provider_base.ensure_provider_uids"):
                Gateway(config_dir=config_dir)

        assert any("Provider loaded" in r.message for r in caplog.records)


# ===========================================================================
# GatewayResponse / stub behavior
# ===========================================================================


class TestGatewayResponse:
    def test_stub_response_structure(self):
        from axiom.infra.gateway import GatewayResponse

        resp = GatewayResponse(text="raw", provider="stub", success=False, error="all failed")
        assert resp.provider == "stub"
        assert resp.success is False
        assert resp.error == "all failed"


# ===========================================================================
# Real upstream error surfacing (no generic "LLM unavailable")
# ===========================================================================


class _FakeResponse:
    """Minimal stand-in for a requests.Response on a raised HTTPError."""

    def __init__(self, status_code, text):
        self.status_code = status_code
        self.text = text


def _http_error(status, body):
    """Build a requests.HTTPError carrying a response (status + body)."""
    import requests

    exc = requests.HTTPError(f"{status} error")
    exc.response = _FakeResponse(status, body)
    return exc


class TestErrorClassification:
    """_classify_http_error maps real upstream signals to transient/persistent."""

    def test_4xx_is_persistent_with_status_and_body(self):
        from axiom.infra.gateway import PersistentLLMError, _classify_http_error

        err = _classify_http_error(_http_error(401, "invalid api key"), provider="p1")
        assert isinstance(err, PersistentLLMError)
        assert err.status == 401
        assert err.body == "invalid api key"
        assert err.provider == "p1"
        assert "401" in str(err) and "invalid api key" in str(err) and "p1" in str(err)

    def test_5xx_is_transient(self):
        from axiom.infra.gateway import TransientLLMError, _classify_http_error

        err = _classify_http_error(_http_error(503, "upstream down"), provider="p1")
        assert isinstance(err, TransientLLMError)
        assert err.status == 503

    def test_429_is_transient(self):
        from axiom.infra.gateway import TransientLLMError, _classify_http_error

        err = _classify_http_error(_http_error(429, "slow down"), provider="p1")
        assert isinstance(err, TransientLLMError)
        assert err.status == 429

    def test_connection_error_is_transient_no_status(self):
        import requests

        from axiom.infra.gateway import TransientLLMError, _classify_http_error

        err = _classify_http_error(
            requests.ConnectionError("name resolution failed"), provider="p1"
        )
        assert isinstance(err, TransientLLMError)
        assert err.status is None
        assert "name resolution failed" in str(err)

    def test_client_summary_redacts_upstream_body(self):
        """The client-safe summary keeps provider + status but DROPS the raw
        upstream body, which can echo internal hostnames / DSNs / auth hints
        (SRV-033). str(err) still carries the body for server-side logs."""
        from axiom.infra.gateway import _classify_http_error

        leaky = "DSN postgres://secret@db.internal:5432 invalid token sk-abc123"
        err = _classify_http_error(_http_error(401, leaky), provider="p1")
        summary = err.client_summary()
        # provider + status survive; the leaky body does NOT.
        assert "p1" in summary and "401" in summary
        assert "secret" not in summary
        assert "db.internal" not in summary
        assert "sk-abc123" not in summary
        # full detail is still available server-side (logs / str()).
        assert "secret" in str(err)


class TestCompleteSurfacesRealErrors:
    """Gateway.complete() propagates the real reason into GatewayResponse.error."""

    def test_persistent_error_surfaced_not_masked(self, monkeypatch):
        from axiom.infra.gateway import PersistentLLMError

        p = _make_provider(name="bad-key", priority=1)
        gw = _gateway_with_providers([p], monkeypatch)

        def _boom(provider, *a, **k):
            raise PersistentLLMError(
                "provider rejected request", status=401, body="bad key", provider="bad-key"
            )

        with patch.object(gw, "_call_provider", side_effect=_boom):
            resp = gw.complete("hi")

        assert resp.success is False
        assert resp.provider == "stub"
        assert "401" in resp.error
        assert "bad key" in resp.error
        assert "bad-key" in resp.error

    def test_transient_falls_back_to_next_provider(self, monkeypatch):
        from axiom.infra.gateway import GatewayResponse

        p1 = _make_provider(name="flaky", priority=1)
        p2 = _make_provider(name="healthy", priority=2)
        gw = _gateway_with_providers([p1, p2], monkeypatch)

        def _call(provider, *a, **k):
            if provider.name == "flaky":
                raise _http_error(503, "down")
            return GatewayResponse(text="ok", provider=provider.name, success=True)

        with patch.object(gw, "_call_provider", side_effect=_call):
            resp = gw.complete("hi")

        assert resp.success is True
        assert resp.provider == "healthy"

    def test_all_fail_names_providers_and_reasons(self, monkeypatch):
        p1 = _make_provider(name="alpha", priority=1)
        p2 = _make_provider(name="beta", priority=2)
        gw = _gateway_with_providers([p1, p2], monkeypatch)

        def _call(provider, *a, **k):
            if provider.name == "alpha":
                raise _http_error(503, "alpha-down")
            raise _http_error(502, "beta-down")

        with patch.object(gw, "_call_provider", side_effect=_call):
            resp = gw.complete("hi")

        assert resp.success is False
        assert "alpha" in resp.error and "beta" in resp.error
        assert "503" in resp.error and "502" in resp.error
        assert "alpha-down" in resp.error and "beta-down" in resp.error

    def test_persistent_does_not_fall_back(self, monkeypatch):
        """A 4xx on the first provider must NOT silently try the second."""
        p1 = _make_provider(name="first", priority=1)
        p2 = _make_provider(name="second", priority=2)
        gw = _gateway_with_providers([p1, p2], monkeypatch)

        calls = []

        def _call(provider, *a, **k):
            calls.append(provider.name)
            raise _http_error(400, "malformed")

        with patch.object(gw, "_call_provider", side_effect=_call):
            resp = gw.complete("hi")

        assert resp.success is False
        assert calls == ["first"]  # never tried "second"
        assert "400" in resp.error and "malformed" in resp.error

    def test_no_provider_error_names_task(self, monkeypatch):
        p = _make_provider(name="no-key", api_key_env="MISSING_KEY")
        gw = _gateway_with_providers([p], monkeypatch)

        resp = gw.complete("hi", task="extraction")
        assert resp.success is False
        assert "extraction" in resp.error


class TestCompleteWithToolsSurfacesRealErrors:
    def test_real_status_and_body_in_error(self, monkeypatch):
        p = _make_provider(name="solo", priority=1)
        gw = _gateway_with_providers([p], monkeypatch)

        with patch.object(
            gw, "_call_provider_with_tools", side_effect=_http_error(403, "forbidden model")
        ):
            resp = gw.complete_with_tools([{"role": "user", "content": "hi"}])

        assert resp.success is False
        assert "403" in resp.error
        assert "forbidden model" in resp.error
        assert "solo" in resp.error


# ===========================================================================
# Local LLM auto-discovery
# ===========================================================================


class TestLocalLLMAutoDiscovery:
    """The fallback provider registered by _discover_local_llm reads its
    model identifier from setup.llamafile.DEFAULT_LOCAL_MODEL_ID — single
    source of truth, no hardcoded "bonsai-1.7b".
    """

    def test_uses_default_local_model_id(self, monkeypatch):
        from axiom.infra.gateway import Gateway
        from axiom.setup.llamafile import DEFAULT_LOCAL_MODEL_ID

        with patch.object(Gateway, "_load_config", lambda self: None):
            gw = Gateway()
        gw.providers = []  # clear

        # Patch socket so the discovery thinks the local server is reachable
        mock_sock = MagicMock()
        mock_sock.__enter__ = MagicMock()
        mock_sock.__exit__ = MagicMock(return_value=False)
        with patch("socket.create_connection", return_value=mock_sock):
            gw._discover_local_llm()

        local = [p for p in gw.providers if "localhost:8080" in (p.endpoint or "")]
        assert local, "expected a local provider to be registered"
        # Single source of truth — must equal the constant from setup.llamafile
        assert local[0].model == DEFAULT_LOCAL_MODEL_ID
        # Sanity: that constant is the qwen profile's id, not "bonsai-1.7b"
        assert DEFAULT_LOCAL_MODEL_ID == "qwen2.5-7b-instruct"

    def test_skips_when_already_have_local_provider(self, monkeypatch):
        from axiom.infra.gateway import Gateway, LLMProvider

        with patch.object(Gateway, "_load_config", lambda self: None):
            gw = Gateway()
        gw.providers = [
            LLMProvider(
                name="manual",
                uid="manual",
                endpoint="http://localhost:8080",
                model="custom-model",
                api_key_env="",
                priority=1,
            )
        ]
        before = len(gw.providers)
        with patch("socket.create_connection") as mock_conn:
            gw._discover_local_llm()
            mock_conn.assert_not_called()
        assert len(gw.providers) == before


# --- reasoning headroom: max_tokens_default as a FLOOR ------------------------


def test_floor_max_tokens_raises_to_floor():
    from types import SimpleNamespace

    from axiom.llm.gateway import _floor_max_tokens
    p = SimpleNamespace(max_tokens_default=4096)
    assert _floor_max_tokens(p, 24) == 4096      # small caller bumped to floor
    assert _floor_max_tokens(p, 32000) == 32000  # large caller NEVER reduced


def test_floor_max_tokens_no_floor_passthrough():
    from types import SimpleNamespace

    from axiom.llm.gateway import _floor_max_tokens
    assert _floor_max_tokens(SimpleNamespace(max_tokens_default=0), 100) == 100
    assert _floor_max_tokens(SimpleNamespace(), 100) == 100  # missing attr -> passthrough


# --- LiteLLM as within-tier transport (RATIONALIZE-4) ------------------------
# A provider with transport="litellm" represents a whole per-tier LiteLLM
# router group (the individual vLLM/Tejas backends live inside LiteLLM, invisible
# to Axiom). Axiom keeps the policy seam — classify -> tier/tag filter ->
# EC-never-relaxes — but hands WITHIN-tier mechanics (fallback / retry /
# load-balance) to LiteLLM. So Axiom must NOT fan out across its own provider
# list within a LiteLLM-backed tier.


def _litellm_provider(name="grp", routing_tier="public", priority=1):
    p = _make_provider(name=name, routing_tier=routing_tier, priority=priority,
                       endpoint="http://localhost:41883/v1", api_key_env="")
    p.transport = "litellm"
    return p


class TestLiteLLMTransport:
    def test_transport_defaults_to_direct(self, monkeypatch):
        assert _make_provider().transport == "direct"

    def test_litellm_group_neuters_axiom_in_tier_fanout(self, monkeypatch):
        """With a LiteLLM-group primary, _ordered_candidates returns ONLY the
        group — LiteLLM owns within-tier fallback, not Axiom."""
        grp = _litellm_provider(name="public-group", routing_tier="public", priority=1)
        # Other usable same-tier providers exist, but must be ignored: in the
        # LiteLLM architecture they would be group MEMBERS, not Axiom-level peers.
        other = _make_provider(name="extra-pub", routing_tier="public", priority=5)
        gw = _gateway_with_providers([grp, other], monkeypatch)

        ordered = gw._ordered_candidates(grp, routing_tier="public")
        assert [p.name for p in ordered] == ["public-group"]

    def test_direct_primary_keeps_axiom_fanout(self, monkeypatch):
        """Regression: a direct provider still fans out across peers (legacy)."""
        p1 = _make_provider(name="d1", routing_tier="public", priority=1)
        p2 = _make_provider(name="d2", routing_tier="public", priority=5)
        gw = _gateway_with_providers([p1, p2], monkeypatch)

        ordered = gw._ordered_candidates(p1, routing_tier="public")
        assert {p.name for p in ordered} == {"d1", "d2"}

    def test_litellm_ec_group_is_tier_isolated(self, monkeypatch):
        """EC isolation is structural: the EC group is selected for EC requests
        and the public group is never folded in."""
        ec_grp = _litellm_provider(name="ec-group", routing_tier="export_controlled",
                                   priority=1)
        pub_grp = _litellm_provider(name="pub-group", routing_tier="public", priority=2)
        gw = _gateway_with_providers([ec_grp, pub_grp], monkeypatch)

        selected = gw._select_provider("fallback", routing_tier="export_controlled")
        assert selected is not None and selected.name == "ec-group"
        ordered = gw._ordered_candidates(selected, routing_tier="export_controlled")
        assert [p.name for p in ordered] == ["ec-group"]  # no public fold-in
