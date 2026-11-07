# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A site that declares ``agents = "none"`` runs no agent and makes no LLM call.

A data acquisition node at a partner site is reviewed by that site's
information security office. "No agent runs here, and nothing on this machine
calls a language model" has to be a property the node enforces, not a habit of
whoever installed it. The policy is the site's declaration in the node file;
turning features on cannot widen it, and changing it is a deliberate act with
its own command.

The LLM check is real: a gateway pointed at a local HTTP server that counts
every request it receives.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from axiom.infra import node_functions as nf


class _CountingLLM(BaseHTTPRequestHandler):
    hits: list[str] = []

    def do_POST(self):  # noqa: N802
        type(self).hits.append(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        body = json.dumps({
            "id": "x", "object": "chat.completion", "model": "m",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # quiet
        pass


@pytest.fixture
def llm_server():
    _CountingLLM.hits = []
    srv = HTTPServer(("127.0.0.1", 0), _CountingLLM)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv, _CountingLLM.hits
    srv.shutdown()


@pytest.fixture
def node(tmp_path, monkeypatch):
    path = tmp_path / "node.toml"
    monkeypatch.setenv(nf.CONFIG_ENV, str(path))
    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path / "state"))
    return path


def _gateway(tmp_path: Path, port: int):
    from axiom.llm.gateway import Gateway

    cfg = tmp_path / "llmcfg"
    cfg.mkdir()
    (cfg / "llm-providers.toml").write_text(
        "[gateway]\n\n[[gateway.providers]]\n"
        'name = "counting-test-llm"\n'
        f'endpoint = "http://127.0.0.1:{port}/v1"\n'
        'model = "m"\n'
        'use_for = ["extraction", "fallback"]\n'
        "priority = 1\n"
    )
    return Gateway(config_dir=cfg)


def _collector(node_path: Path, **extra) -> nf.NodeConfig:
    cfg = nf.apply(role="collector", functions=["acquire", "transmit"], path=node_path)
    for k, v in extra.items():
        cfg = nf.save(nf.NodeConfig(**{**cfg.__dict__, k: v}), node_path)
    return cfg


# -- the policy itself ----------------------------------------------------------


def test_a_role_node_defaults_to_no_agents_and_a_general_install_keeps_them(node):
    assert nf.load(node).agent_policy == "local"  # no declaration: today's behavior
    _collector(node)
    assert nf.load(node).agent_policy == "none"


def test_the_declared_policy_is_read_and_written(node):
    _collector(node)
    nf.set_agent_policy("assist", path=node)
    assert nf.load(node).agent_policy == "assist"
    assert 'agents = "assist"' in node.read_text()
    with pytest.raises(nf.UnknownFeature):
        nf.set_agent_policy("lots", path=node)


def test_turning_agents_on_as_a_feature_is_refused_until_the_site_allows_them(node):
    _collector(node)
    with pytest.raises(nf.AgentPolicyRefused) as e:
        nf.enable(nf.AGENTS_FEATURE, known={}, path=node)
    msg = str(e.value)
    assert 'agents = "none"' in msg and "features agents local" in msg
    nf.set_agent_policy("local", path=node)
    assert nf.AGENTS_FEATURE in nf.enable(nf.AGENTS_FEATURE, known={}, path=node).features


def test_narrowing_the_policy_turns_agents_back_off(node):
    _collector(node)
    nf.set_agent_policy("local", path=node)
    nf.enable(nf.AGENTS_FEATURE, known={}, path=node)
    cfg = nf.set_agent_policy("none", path=node)
    assert nf.AGENTS_FEATURE not in cfg.features
    assert not nf.background_services_allowed(cfg)


def test_the_autonomy_switch_cannot_override_the_site(node, tmp_path):
    from axiom.extensions.builtins.settings.store import SettingsStore, autonomy_enabled

    _collector(node)
    SettingsStore().set("autonomy.enabled", True)
    assert autonomy_enabled() is False


def test_no_background_service_is_registered_on_a_none_node(node):
    from axiom.extensions.builtins.agents.cli import register_all_daemon_agents
    from axiom.extensions.builtins.settings.store import SettingsStore

    _collector(node)
    SettingsStore().set("autonomy.enabled", True)
    results = register_all_daemon_agents()
    assert results and not any(r.registered or r.started for r in results)
    assert any("agents" in (r.error or "") and "none" in (r.error or "") for r in results)


# -- the LLM guard (real HTTP) --------------------------------------------------


def test_a_general_install_still_calls_its_llm(node, tmp_path, llm_server):
    srv, hits = llm_server
    resp = _gateway(tmp_path, srv.server_port).complete("hello", task="extraction")
    assert resp.success and hits  # the control: the counter does see calls


def test_a_none_node_makes_no_llm_request_even_after_turning_features_on(node, tmp_path, llm_server):
    srv, hits = llm_server
    _collector(node)
    nf.enable("data_platform", known={}, path=node)  # a non-agent feature
    resp = _gateway(tmp_path, srv.server_port).complete("hello", task="extraction")
    assert not resp.success
    assert hits == []
    assert "agents" in (resp.error or "").lower()


def test_assist_keeps_the_local_machine_free_of_llm_calls_too(node, tmp_path, llm_server):
    srv, hits = llm_server
    _collector(node)
    nf.set_agent_policy("assist", path=node)
    _gateway(tmp_path, srv.server_port).complete("hello", task="extraction")
    assert hits == []


def test_a_running_node_stays_silent_across_ordinary_commands(node, tmp_path, llm_server):
    """Ordinary use of a none node, as separate processes, sends nothing to an LLM.

    The processes find the counting server through the normal default config
    (``AXIOM_ROOT/runtime/config``), and the last one asks the gateway for a
    completion the way any feature would.
    """
    srv, hits = llm_server
    _collector(node)
    root = tmp_path / "proj"
    (root / "runtime" / "config").mkdir(parents=True)
    (root / "runtime" / "config" / "llm-providers.toml").write_text(
        "[gateway]\n\n[[gateway.providers]]\n"
        'name = "counting-test-llm"\n'
        f'endpoint = "http://127.0.0.1:{srv.server_port}/v1"\n'
        'model = "m"\nuse_for = ["extraction", "fallback"]\npriority = 1\n'
    )
    env = {**os.environ, "AXIOM_ROOT": str(root), "AXI_DIAGNOSES_QUIET": "1"}
    cli = [sys.executable, "-c", "import sys; from axiom.axiom_cli import main; sys.exit(main())"]
    for argv in (["status"], ["features", "list"], ["whoami"], ["features", "enable", "data_platform"]):
        subprocess.run([*cli, *argv], env=env, capture_output=True, text=True, timeout=180, cwd=root)
    out = subprocess.run(
        [sys.executable, "-c",
         "from axiom.llm.gateway import Gateway; r = Gateway().complete('hello', task='extraction'); print(r.success, r.error)"],
        env=env, capture_output=True, text=True, timeout=180, cwd=root,
    )
    assert out.stdout.startswith("False"), out.stdout + out.stderr
    assert hits == []
    assert nf.load(node).agent_policy == "none"


# -- what people see and use ------------------------------------------------------


def test_status_says_no_agents_are_running_and_why(node):
    from axiom.extensions.builtins.status import role_status

    cfg = _collector(node)
    text = role_status.render(cfg, role_status.collect(cfg))
    assert "Agents" in text and "none running" in text and 'policy: none' in text


def test_the_policy_is_changed_with_its_own_command_and_listed(node, capsys):
    from axiom.extensions.builtins.features import cli

    _collector(node)
    assert cli.main(["list"]) == 0
    assert 'Agents: none (site policy)' in capsys.readouterr().out
    assert cli.main(["enable", nf.AGENTS_FEATURE]) != 0
    assert "features agents local" in capsys.readouterr().err
    assert cli.main(["agents", "local"]) == 0
    assert nf.load(node).agent_policy == "local"


def test_an_ai_client_cannot_widen_the_policy(node):
    """Changing the policy is offered on the command line only, never over MCP."""
    from axiom.extensions.builtins.features.skills import _SPECS

    spec = {s.name: s for s in _SPECS}["features.agents"]
    assert "mcp" not in spec.surfaces and "agent_tool" not in spec.surfaces
