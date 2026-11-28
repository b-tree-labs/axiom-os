# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Routing build plan, Phase 0 acceptance, P0.3 a private tier (RS-E5, issue #545).

docs/working/routing-build-plan-2026-11-12.md, section 3:

- P0.3-A1: ``routing_tier = "restricted"`` normalises to ``private``, not ``export_controlled``.
- P0.3-A2: a private provider is not blocked by the missing audit key.

``restricted`` was documented in the example providers file as a private-network model (a VPN-gated
server on the site network), and the gateway silently promoted it to ``export_controlled``. That
cleared a server nobody had attested for controlled content, and on a node with no audit key it
warned that every request would be blocked. ``private`` is its own tier: it takes any content the
label admits, which is everything but controlled content.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from axiom.llm import gateway as gw
from axiom.llm import tier_policy

_PRIVATE_FILE = """
[gateway]

[[gateway.providers]]
name = "site-llm"
uid = "uid-site-llm"
endpoint = "https://llm.site.example:41883/v1"
model = "qwen"
api_key_env = "P03_FAKE_KEY"
priority = 1
use_for = ["chat", "fallback"]
routing_tier = "{tier}"
"""


@pytest.fixture
def node(monkeypatch, tmp_path: Path):
    """A node with no audit key. Returns ``(config_dir, config_loads, sent)``.

    The audit log behaves as the real one does without ``AXIOM_AUDIT_HMAC_KEY``: it refuses ``ec``
    mode. The provider call is a recorder; nothing reaches the network.
    """
    monkeypatch.delenv("AXIOM_AUDIT_HMAC_KEY", raising=False)
    monkeypatch.setenv("P03_FAKE_KEY", "sk-test-not-real")
    monkeypatch.setenv("AXIOM_ACTOR", "@casey:site")
    loads: list[dict] = []
    sent: list[str] = []

    class _Audit:
        def set_mode(self, mode):
            if mode == "ec":
                raise ValueError("EC audit log requires AXIOM_AUDIT_HMAC_KEY.")

        def write_config_load(self, **kw):
            loads.append(kw)

        def write_routing(self, **kw):
            pass

        def write_vpn(self, **kw):
            pass

    from axiom.infra import audit_log

    monkeypatch.setattr(audit_log.AuditLog, "get", classmethod(lambda cls: _Audit()))
    monkeypatch.setattr(gw.Gateway, "_discover_local_llm", lambda self: None)

    def _call(self, provider, messages, system, tools, max_tokens):
        sent.append(provider.name)
        return gw.CompletionResponse(
            text="ok", provider=provider.name, model=provider.model, success=True
        )

    monkeypatch.setattr(gw.Gateway, "_call_provider_with_tools", _call)
    tier_policy._providers_cache.clear()
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    return config_dir, loads, sent


def _load(config_dir: Path, tier: str) -> gw.Gateway:
    (config_dir / "llm-providers.toml").write_text(
        _PRIVATE_FILE.format(tier=tier), encoding="utf-8"
    )
    return gw.Gateway(config_dir=config_dir)


# ---------------------------------------------------------------------------
# P0.3-A1
# ---------------------------------------------------------------------------


def test_p0_3_a1_restricted_normalises_to_private():
    """P0.3-A1: the deprecated alias names the private tier."""
    p = gw.LLMProvider(
        name="site-llm",
        endpoint="https://llm.site.example/v1",
        model="m",
        routing_tier="restricted",
    )

    assert p.routing_tier == "private"


def test_p0_3_a1_a_restricted_provider_in_the_providers_file_loads_as_private(node):
    """P0.3-A1: the same through the providers file the gateway reads."""
    config_dir, _loads, _sent = node

    g = _load(config_dir, "restricted")

    assert [p.routing_tier for p in g.providers] == ["private"]


def test_p0_3_a1_private_is_accepted_as_written(node, caplog):
    """P0.3-A1: ``private`` is a tier of its own, kept as written and not reported as deprecated."""
    config_dir, _loads, _sent = node

    with caplog.at_level(logging.WARNING, logger="axiom.llm.gateway"):
        g = _load(config_dir, "private")

    assert [p.routing_tier for p in g.providers] == ["private"]
    assert "deprecated routing_tier" not in caplog.text


def test_p0_3_a1_a_private_provider_is_not_cleared_for_controlled_content(node):
    """P0.3-A1: private is not controlled. A controlled request with only a private provider is refused."""
    config_dir, _loads, sent = node
    g = _load(config_dir, "restricted")

    out = g.complete_with_tools(
        [{"role": "user", "content": "q"}], routing_tier="export_controlled"
    )

    assert sent == [], "controlled content reached a private provider"
    assert out.error == "EC_PROVIDER_NOT_CONFIGURED"
    assert not tier_policy.admits("private", tier_policy.CONTROLLED)
    assert (
        tier_policy.endpoint_clearance("https://llm.site.example:41883/v1")
        != tier_policy.CONTROLLED
    )


# ---------------------------------------------------------------------------
# P0.3-A2
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tier", ["restricted", "private"])
def test_p0_3_a2_a_private_provider_is_not_blocked_by_the_missing_audit_key(node, caplog, tier):
    """P0.3-A2: no audit key, a private provider: no controlled-audit mode, no "will be blocked" warning,
    and a request it may take is answered by it."""
    config_dir, loads, sent = node

    with caplog.at_level(logging.WARNING, logger="axiom.llm.gateway"):
        g = _load(config_dir, tier)

    assert "AXIOM_AUDIT_HMAC_KEY" not in caplog.text, caplog.text
    assert loads and loads[-1]["ec_providers_count"] == 0
    assert g._ec_audit_enabled is False

    out = g.complete_with_tools([{"role": "user", "content": "q"}], routing_tier="any")

    assert out.success and sent == ["site-llm"]


def test_p0_3_a1_the_auto_strategy_does_not_clear_a_private_provider_for_controlled_content(node):
    """P0.3-A1: with ``default_routing = "auto"`` the strategy path picks no private provider for a
    controlled request either (the strategy clears its own ``private`` network tier for controlled work)."""
    config_dir, _loads, _sent = node
    g = _load(config_dir, "restricted")
    g._gateway_config = {"default_routing": "auto"}

    picked = g._select_with_routing("chat", routing_tier="export_controlled")

    assert picked is None, (
        f"a private provider was chosen for controlled content: {picked and picked.name}"
    )


# ---------------------------------------------------------------------------
# P0.3, the auto strategy: clearance for controlled content comes only from the declared tier
# ---------------------------------------------------------------------------


def _auto_gateway(monkeypatch, *providers: gw.LLMProvider) -> gw.Gateway:
    from unittest.mock import patch

    monkeypatch.setenv("P03_FAKE_KEY", "sk-test-not-real")
    with patch.object(gw.Gateway, "_load_config", lambda self: None):
        g = gw.Gateway()
    g.providers = list(providers)
    g._gateway_config = {"default_routing": "auto"}
    return g


def _site(name: str, tier: str, *, vpn: bool) -> gw.LLMProvider:
    return gw.LLMProvider(
        name=name,
        uid=f"uid-{name}",
        endpoint=f"https://{name}.site.example/v1",
        model="m",
        api_key_env="P03_FAKE_KEY",
        priority=1,
        use_for=["chat", "fallback"],
        routing_tier=tier,
        requires_vpn=vpn,
    )


@pytest.mark.parametrize("tier", ["public", "private"])
def test_p0_3_auto_a_vpn_provider_is_not_cleared_for_controlled_content_by_the_vpn_alone(
    monkeypatch, tier
):
    """P0.3 auto gap: under ``default_routing = "auto"``, ``requires_vpn`` does not clear a provider."""
    g = _auto_gateway(monkeypatch, _site("vpn-llm", tier, vpn=True))
    monkeypatch.setattr(g, "_check_vpn", lambda p: True)

    picked = g._select_with_routing("chat", routing_tier="export_controlled")

    assert picked is None, f"a {tier} provider behind the VPN was chosen for controlled content"


def test_p0_3_auto_a_declared_export_controlled_provider_is_chosen_for_controlled_content(
    monkeypatch,
):
    """P0.3 auto gap: the declared tier is what clears, with or without the VPN."""
    g = _auto_gateway(
        monkeypatch,
        _site("vpn-llm", "private", vpn=True),
        _site("ec-llm", "export_controlled", vpn=False),
    )
    monkeypatch.setattr(g, "_check_vpn", lambda p: True)

    picked = g._select_with_routing("chat", routing_tier="export_controlled")

    assert picked is not None and picked.name == "ec-llm"


# ---------------------------------------------------------------------------
# P0.3, setup writes the canonical name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("declared", ["", "restricted", "private"])
def test_p0_3_private_network_llm_setup_writes_private(monkeypatch, tmp_path, declared):
    """P0.3: ``chat/connections.py`` writes ``routing_tier = "private"``, never the deprecated ``restricted``."""
    from types import SimpleNamespace

    import axiom
    import axiom.infra.connections as conns
    import axiom.extensions.builtins.settings.store as store
    from axiom.extensions.builtins.chat import connections as setup

    (tmp_path / "runtime" / "config").mkdir(parents=True)
    providers = tmp_path / "runtime" / "config" / "llm-providers.toml"
    providers.write_text("[gateway]\n", encoding="utf-8")
    monkeypatch.setattr(axiom, "REPO_ROOT", tmp_path)
    conn = SimpleNamespace(
        endpoint="https://llm.site.example/v1",
        credential_env_var="SITE_KEY",
        display_name="site model",
        vpn_name="VPN",
        vpn_connect_guide="",
        health_endpoint="",
        model="qwen",
        routing_tier=declared,
        verify_ssl=True,
    )
    monkeypatch.setattr(conns, "get_registry", lambda: SimpleNamespace(get=lambda name: conn))

    class _Settings:
        def get(self, key, default=None):
            return default

        def set(self, key, value):
            pass

    monkeypatch.setattr(store, "SettingsStore", _Settings)

    assert setup._setup_private_network_llm("site-llm") == 0

    written = providers.read_text(encoding="utf-8")
    assert 'routing_tier = "private"' in written, written
    assert "restricted" not in written


def test_p0_3_the_shipped_self_hosted_connection_declares_private():
    """P0.3: the chat extension's self-hosted connection declares the canonical tier."""
    from axiom.infra.toml_compat import tomllib

    manifest = (
        Path(gw.__file__).parents[1] / "extensions" / "builtins" / "chat" / "axiom-extension.toml"
    )
    data = tomllib.loads(manifest.read_text(encoding="utf-8"))
    tiers = {
        c.get("name"): c.get("routing_tier")
        for c in data.get("connections", [])
        if c.get("routing_tier")
    }

    assert tiers.get("qwen-selfhosted") == "private", tiers
