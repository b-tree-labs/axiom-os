# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for axiom.setup.federation_probe (spec-federation §6.6 MVP)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from axiom.infra.connections import (
    Connection,
    ConnectionHealth,
    ConnectionRegistry,
    HealthStatus,
)
from axiom.setup import federation_probe as fp


def _make_conn(
    name: str = "private-llm",
    category: str = "llm",
    endpoint: str = "https://internal-gpu.example:8443/v1",
    routing_tier: str = "restricted",
    vpn_name: str | None = "private network VPN",
    credential_env_var: str = "",
    model: str = "example-model",
):
    """Build a Connection mock (health is stubbed via patching check_health).

    Test fixtures use generic / placeholder names per
    feedback_axiom_domain_agnostic — no org, site, host, or model names.
    """
    c = MagicMock(spec=Connection)
    c.name = name
    c.display_name = name.replace("-", " ").title()
    c.kind = "api"
    c.category = category
    c.endpoint = endpoint
    c.routing_tier = routing_tier
    c.vpn_name = vpn_name
    c.requires_vpn = bool(vpn_name)
    c.credential_env_var = credential_env_var
    c.model = model
    c.extension = "chat"
    return c


def _registry(conns):
    r = ConnectionRegistry()
    r._connections = {c.name: c for c in conns}  # type: ignore[attr-defined]
    return r


def _healthy(latency_ms: float = 12.0) -> ConnectionHealth:
    return ConnectionHealth(status=HealthStatus.HEALTHY, latency_ms=latency_ms)


def _unhealthy(reason: str = "unreachable") -> ConnectionHealth:
    return ConnectionHealth(status=HealthStatus.UNHEALTHY, message=reason)


def test_probe_endpoint_reachable():
    conn = _make_conn()
    reg = _registry([conn])
    with patch.object(fp, "check_health", return_value=_healthy(latency_ms=12.0)):
        result = fp.probe_endpoint(conn, reg)
    assert result.reachable is True
    assert result.latency_ms == 12


def test_probe_endpoint_unreachable():
    conn = _make_conn()
    reg = _registry([conn])
    with patch.object(fp, "check_health", return_value=_unhealthy()):
        result = fp.probe_endpoint(conn, reg)
    assert result.reachable is False
    assert result.latency_ms is None


def test_probe_endpoint_handles_check_health_exception():
    conn = _make_conn()
    reg = _registry([conn])
    with patch.object(fp, "check_health", side_effect=RuntimeError("DNS lookup failed")):
        result = fp.probe_endpoint(conn, reg)
    assert result.reachable is False
    assert result.latency_ms is None


def test_discover_only_returns_reachable_llm_connections():
    reachable = _make_conn(name="reachable-llm")
    unreachable = _make_conn(name="dead-llm")
    non_llm = _make_conn(name="github", category="code")
    reg = _registry([reachable, unreachable, non_llm])

    def fake_check(name, registry=None):
        if name == "dead-llm":
            return _unhealthy()
        return _healthy()

    with patch.object(fp, "check_health", side_effect=fake_check):
        results = fp.discover_llm_endpoints(reg)

    names = sorted(r.connection.name for r in results)
    assert names == ["reachable-llm"], (
        "Only the reachable LLM should be returned; non-LLM categories + "
        "unreachable LLMs are filtered"
    )


def test_discover_attaches_rag_companion_when_same_host():
    llm = _make_conn(
        name="private-llm-with-rag",
        endpoint="http://internal-gateway.example:8443/v1",
    )
    rag = _make_conn(
        name="domain-corpus",
        category="rag",
        endpoint="http://internal-gateway.example:8443/rag",
    )
    rag.display_name = "Domain RAG corpus"
    reg = _registry([llm, rag])

    with patch.object(fp, "check_health", return_value=_healthy()):
        results = fp.discover_llm_endpoints(reg)

    assert len(results) == 1
    assert results[0].rag_corpus == "Domain RAG corpus"


def test_render_prompt_includes_all_required_fields():
    result = fp.ProbeResult(
        connection=_make_conn(),
        reachable=True,
        latency_ms=12,
        rag_corpus="Domain corpus",
    )
    text = fp.render_prompt(result)
    assert "Endpoint:" in text
    assert "Latency:" in text
    assert "12ms" in text
    assert "Cost:" in text
    assert "EC posture:" in text
    assert "NOT EC-safe" in text, "non-export_controlled tier should label as NOT EC-safe"
    assert "RAG corpus:" in text
    assert "Domain corpus" in text
    assert "Federated:" in text


def test_render_prompt_marks_export_controlled_as_ec_safe():
    result = fp.ProbeResult(
        connection=_make_conn(routing_tier="export_controlled"),
        reachable=True,
        latency_ms=5,
        rag_corpus=None,
    )
    text = fp.render_prompt(result)
    assert "EC posture:  EC-safe" in text
    assert "RAG corpus:" not in text


def test_decline_memoization_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(fp, "_decline_store_path", lambda: tmp_path / "declined.json")
    assert not fp.has_declined("foo")
    fp.record_decline("foo")
    assert fp.has_declined("foo")
    assert not fp.has_declined("bar")


def test_write_provider_entry_first_provider_marked_default(tmp_path):
    target = tmp_path / "llm-providers.toml"
    result = fp.ProbeResult(
        connection=_make_conn(),
        reachable=True,
        latency_ms=12,
        rag_corpus=None,
    )
    fp.write_provider_entry(result, target)
    text = target.read_text(encoding="utf-8")
    assert "[[gateway.providers]]" in text
    assert 'name         = "private-llm"' in text
    assert "default      = true" in text, "first provider should be marked default"


def test_write_provider_entry_not_default_when_others_exist(tmp_path):
    target = tmp_path / "llm-providers.toml"
    target.write_text(
        "[[gateway.providers]]\n"
        'name = "anthropic"\n'
        'endpoint = "https://api.anthropic.com/v1"\n',
        encoding="utf-8",
    )
    result = fp.ProbeResult(
        connection=_make_conn(),
        reachable=True,
        latency_ms=12,
        rag_corpus=None,
    )
    fp.write_provider_entry(result, target)
    text = target.read_text(encoding="utf-8")
    assert text.count("[[gateway.providers]]") == 2
    new_block = text.split("# Auto-added by federation probe")[-1]
    assert "default      = true" not in new_block


def test_run_install_probe_non_tty_skips_prompt(tmp_path, capsys):
    conn = _make_conn()
    reg = _registry([conn])
    target = tmp_path / "llm-providers.toml"

    with patch.object(fp, "check_health", return_value=_healthy()):
        adopted = fp.run_install_probe(
            registry=reg,
            llm_providers_path=target,
            stdin_is_tty=False,
        )

    assert adopted == 0
    assert not target.exists(), "non-TTY context must NOT auto-adopt"
    out = capsys.readouterr().out
    assert "Federated LLM service(s) detected" in out
    assert "federation adopt" in out


def test_run_install_probe_skips_previously_declined(tmp_path, monkeypatch):
    conn = _make_conn()
    reg = _registry([conn])
    monkeypatch.setattr(fp, "_decline_store_path", lambda: tmp_path / "declined.json")
    fp.record_decline(conn.name)
    target = tmp_path / "llm-providers.toml"

    with patch.object(fp, "check_health", return_value=_healthy()):
        adopted = fp.run_install_probe(
            registry=reg,
            llm_providers_path=target,
            stdin_is_tty=True,
        )

    assert adopted == 0
    assert not target.exists()


def test_run_install_probe_accept_writes_provider(tmp_path, monkeypatch, capsys):
    conn = _make_conn()
    reg = _registry([conn])
    monkeypatch.setattr(fp, "_decline_store_path", lambda: tmp_path / "declined.json")
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")
    target = tmp_path / "llm-providers.toml"

    with patch.object(fp, "check_health", return_value=_healthy()):
        adopted = fp.run_install_probe(
            registry=reg,
            llm_providers_path=target,
            stdin_is_tty=True,
        )

    assert adopted == 1
    assert target.exists()
    text = target.read_text(encoding="utf-8")
    assert 'name         = "private-llm"' in text


def test_run_install_probe_decline_records_and_does_not_write(tmp_path, monkeypatch):
    conn = _make_conn()
    reg = _registry([conn])
    monkeypatch.setattr(fp, "_decline_store_path", lambda: tmp_path / "declined.json")
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")
    target = tmp_path / "llm-providers.toml"

    with patch.object(fp, "check_health", return_value=_healthy()):
        adopted = fp.run_install_probe(
            registry=reg,
            llm_providers_path=target,
            stdin_is_tty=True,
        )

    assert adopted == 0
    assert not target.exists()
    assert fp.has_declined(conn.name)
