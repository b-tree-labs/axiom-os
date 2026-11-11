"""A service finds its LLM providers in the node config directory, whatever its
working directory.

Measured on a node, 2026-11-10: the serving app ran from $HOME, the project
root followed the working directory, no llm-providers.toml was found there,
and the chat had no provider but a dead port.
"""

from __future__ import annotations

from axiom.llm import gateway as gw


def test_the_node_config_dir_wins_when_it_holds_the_file(tmp_path, monkeypatch):
    node = tmp_path / "node-config"
    node.mkdir()
    (node / "llm-providers.toml").write_text("")
    monkeypatch.setenv("AXIOM_CONFIG_DIR", str(node))
    assert gw.default_providers_dir() == node


def test_without_it_the_project_layout_is_used(tmp_path, monkeypatch):
    monkeypatch.setenv("AXIOM_CONFIG_DIR", str(tmp_path / "empty"))
    assert gw.default_providers_dir() in (gw.CONFIG_DIR, gw.CONFIG_EXAMPLE_DIR)


def test_the_gateway_reads_providers_from_the_node_config_dir(tmp_path, monkeypatch):
    node = tmp_path / "node-config"
    node.mkdir()
    (node / "llm-providers.toml").write_text(
        "[[gateway.providers]]\n"
        'name = "site-model"\nuid = "u-1"\nendpoint = "http://127.0.0.1:9/v1"\n'
        'model = "m"\napi_key_env = ""\npriority = 1\nrouting_tier = "export_controlled"\n'
    )
    monkeypatch.setenv("AXIOM_CONFIG_DIR", str(node))
    monkeypatch.setattr(gw, "_listed_model_id", lambda endpoint: None)
    g = gw.Gateway()
    assert [p.name for p in g.providers] == ["site-model"]
