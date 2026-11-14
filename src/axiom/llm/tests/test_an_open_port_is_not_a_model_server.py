"""Only a server that lists a model is auto-discovered, and an EC-cleared
provider serves public content too.

Measured on a node, 2026-11-10: the k3d load balancer held localhost:8080 with
nothing behind it. The gateway found no provider file, registered the open port
as "axiom-local", and every web app chat failed with "Remote end closed
connection without response". The same node is cleared for export-controlled
work, and a provider declared export_controlled was never chosen for public
content, against the one rule in tier_policy.
"""

from __future__ import annotations

import socket

import pytest

from axiom.llm import gateway as gw


class _OpenPort:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def bare_gateway(tmp_path, monkeypatch):
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: _OpenPort())
    return tmp_path


def test_an_open_port_that_lists_no_model_is_not_registered(bare_gateway, monkeypatch):
    def no_models(url, timeout):
        raise ConnectionError("Remote end closed connection without response")

    monkeypatch.setattr(gw, "_fetch_json", no_models)
    g = gw.Gateway(config_dir=bare_gateway)
    assert [p.name for p in g.providers] == []


def test_a_server_that_lists_a_model_is_registered_under_that_name(bare_gateway, monkeypatch):
    monkeypatch.setattr(gw, "_fetch_json", lambda url, timeout: {"data": [{"id": "bonsai-1.7b.gguf"}]})
    g = gw.Gateway(config_dir=bare_gateway)
    assert [(p.name, p.model) for p in g.providers] == [("axiom-local", "bonsai-1.7b.gguf")]


def _write(config_dir, tier):
    (config_dir / "llm-providers.toml").write_text(
        "[[gateway.providers]]\n"
        'name = "site-model"\nuid = "u-1"\n'
        'endpoint = "http://127.0.0.1:9/v1"\nmodel = "m"\napi_key_env = ""\n'
        f'priority = 1\nrouting_tier = "{tier}"\n'
    )


def test_an_ec_cleared_provider_serves_public_and_controlled_content(tmp_path, monkeypatch):
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    _write(tmp_path, "export_controlled")
    g = gw.Gateway(config_dir=tmp_path)
    assert g._select_provider_impl("chat", "public", None, None).name == "site-model"
    assert g._select_provider_impl("chat", "export_controlled", None, None).name == "site-model"


def test_a_public_provider_never_receives_controlled_content(tmp_path, monkeypatch):
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    _write(tmp_path, "public")
    g = gw.Gateway(config_dir=tmp_path)
    assert g._select_provider_impl("chat", "export_controlled", None, None) is None
