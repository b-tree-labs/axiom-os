# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the unified MCP-server installer (axi mcp install)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from types import SimpleNamespace

from axiom.extensions.builtins.mcp import install as inst


def _point_paths_at(tmp_path, monkeypatch):
    """Redirect every client's config path into tmp_path."""
    paths = {
        "AXIOM_CLAUDE_CONFIG": tmp_path / ".claude.json",
        "AXIOM_CLAUDE_DESKTOP_CONFIG": tmp_path / "claude_desktop_config.json",
        "AXIOM_CURSOR_CONFIG": tmp_path / "cursor-mcp.json",
        "AXIOM_WINDSURF_CONFIG": tmp_path / "windsurf.json",
        "AXIOM_GEMINI_CONFIG": tmp_path / "gemini-settings.json",
        "AXIOM_OPENCODE_CONFIG": tmp_path / "opencode.json",
        "AXIOM_VSCODE_CONFIG": tmp_path / "vscode-mcp.json",
        "AXIOM_CODEX_CONFIG": tmp_path / "codex.toml",
    }
    for k, v in paths.items():
        monkeypatch.setenv(k, str(v))
    # Isolate side effects: don't touch real OS services or the real ~/.claude.
    monkeypatch.setenv("AXIOM_CLAUDE_SETTINGS", str(tmp_path / "claude-settings.json"))
    monkeypatch.setattr(
        inst, "ensure_ingress_service",
        lambda *, dry_run=False: {"action": "running", "service": "com.axiom.gateway-ingress", "provider": "test"},
    )
    return paths


def test_resolve_server_defaults_to_axiom(monkeypatch):
    monkeypatch.delenv("AXIOM_MCP_SERVER_MODULE", raising=False)
    monkeypatch.delenv("AXIOM_MCP_SERVER_NAME", raising=False)
    name, command, args = inst.resolve_server()
    assert name == "axiom"
    assert args == ["-m", "axiom.extensions.builtins.mcp.server"]
    assert command == sys.executable


def test_resolve_server_consumer_override(monkeypatch):
    monkeypatch.setenv("AXIOM_MCP_SERVER_MODULE", "consumer_pkg.mcp_server")
    monkeypatch.delenv("AXIOM_MCP_SERVER_NAME", raising=False)
    name, _, args = inst.resolve_server()
    assert args == ["-m", "consumer_pkg.mcp_server"]
    assert name == "consumer-pkg"  # derived from top-level module, _ -> -


def test_resolve_server_explicit_name_override(monkeypatch):
    monkeypatch.setenv("AXIOM_MCP_SERVER_MODULE", "consumer_pkg.mcp_server")
    monkeypatch.setenv("AXIOM_MCP_SERVER_NAME", "branded")
    name, _, args = inst.resolve_server()
    assert name == "branded"


def test_install_claude_json_adds_then_unchanged(tmp_path, monkeypatch):
    _point_paths_at(tmp_path, monkeypatch)
    r1 = inst.install(tools=["claude-code"])
    assert r1["results"]["claude-code"]["action"] == "added"
    cfg = json.loads((tmp_path / ".claude.json").read_text())
    entry = cfg["mcpServers"][r1["server"]]
    assert entry["type"] == "stdio"  # claude carries the type field
    assert entry["args"] == r1["args"]
    r2 = inst.install(tools=["claude-code"])
    assert r2["results"]["claude-code"]["action"] == "unchanged"


def test_cursor_entry_has_no_type(tmp_path, monkeypatch):
    _point_paths_at(tmp_path, monkeypatch)
    inst.install(tools=["cursor"])
    cfg = json.loads((tmp_path / "cursor-mcp.json").read_text())
    entry = next(iter(cfg["mcpServers"].values()))
    assert "type" not in entry
    assert entry["command"] and entry["args"]


def test_vscode_uses_servers_key(tmp_path, monkeypatch):
    _point_paths_at(tmp_path, monkeypatch)
    inst.install(tools=["vscode"])
    cfg = json.loads((tmp_path / "vscode-mcp.json").read_text())
    assert "servers" in cfg and "mcpServers" not in cfg
    assert next(iter(cfg["servers"].values()))["type"] == "stdio"


def test_codex_toml(tmp_path, monkeypatch):
    _point_paths_at(tmp_path, monkeypatch)
    r = inst.install(tools=["codex"])
    assert r["results"]["codex"]["action"] == "added"
    body = (tmp_path / "codex.toml").read_text()
    assert "[mcp_servers." in body and "command" in body
    assert inst.install(tools=["codex"])["results"]["codex"]["action"] == "unchanged"


def test_dry_run_writes_nothing(tmp_path, monkeypatch):
    _point_paths_at(tmp_path, monkeypatch)
    r = inst.install(tools=["claude-code", "codex"], dry_run=True)
    assert r["results"]["claude-code"]["action"] == "would-add"
    assert not (tmp_path / ".claude.json").exists()
    assert not (tmp_path / "codex.toml").exists()


def test_all_tools_writes_every_supported(tmp_path, monkeypatch):
    _point_paths_at(tmp_path, monkeypatch)
    r = inst.install(all_tools=True)
    assert set(r["results"]) == set(inst.supported_tools())
    assert all(v["action"] == "added" for v in r["results"].values())


def test_cli_install_dry_run_subprocess(tmp_path, monkeypatch):
    env = {**os.environ, **{k: str(v) for k, v in _point_paths_at(tmp_path, monkeypatch).items()}}
    proc = subprocess.run(
        [sys.executable, "-m", "axiom.extensions.builtins.mcp.cli", "install", "--all", "--dry-run"],
        capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 0, proc.stderr
    assert "Would install Axiom MCP server" in proc.stdout
    assert "would-add" in proc.stdout


def test_uninstall_removes_only_our_entry(tmp_path, monkeypatch):
    _point_paths_at(tmp_path, monkeypatch)
    r = inst.install(tools=["cursor"])
    our_name = r["server"]
    cfg_path = tmp_path / "cursor-mcp.json"
    cfg = json.loads(cfg_path.read_text())
    cfg["mcpServers"]["someone-else"] = {"command": "x", "args": []}
    cfg_path.write_text(json.dumps(cfg))

    res = inst.uninstall(tools=["cursor"])
    assert res["results"]["cursor"]["action"] == "removed"
    after = json.loads(cfg_path.read_text())["mcpServers"]
    assert "someone-else" in after  # untouched
    assert our_name not in after  # ours gone
    assert inst.uninstall(tools=["cursor"])["results"]["cursor"]["action"] == "absent"


def test_uninstall_absent_when_no_config(tmp_path, monkeypatch):
    _point_paths_at(tmp_path, monkeypatch)
    assert inst.uninstall(tools=["codex"])["results"]["codex"]["action"] == "absent"


def test_uninstall_codex(tmp_path, monkeypatch):
    _point_paths_at(tmp_path, monkeypatch)
    our_name = inst.install(tools=["codex"])["server"]
    assert inst.uninstall(tools=["codex"])["results"]["codex"]["action"] == "removed"
    import tomlkit

    doc = tomlkit.loads((tmp_path / "codex.toml").read_text())
    assert our_name not in (doc.get("mcp_servers") or {})


def test_uninstall_dry_run_writes_nothing(tmp_path, monkeypatch):
    _point_paths_at(tmp_path, monkeypatch)
    our_name = inst.install(tools=["claude-code"])["server"]
    r = inst.uninstall(tools=["claude-code"], dry_run=True)
    assert r["results"]["claude-code"]["action"] == "would-remove"
    cfg = json.loads((tmp_path / ".claude.json").read_text())["mcpServers"]
    assert our_name in cfg


# --- consumer install: ingress service + claude base-url, idempotent --------


def test_wire_claude_base_url_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("AXIOM_CLAUDE_SETTINGS", str(tmp_path / "settings.json"))
    r1 = inst.wire_claude_base_url("http://127.0.0.1:8788")
    assert r1["action"] == "added"
    data = json.loads((tmp_path / "settings.json").read_text())
    assert data["env"]["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:8788"
    assert inst.wire_claude_base_url("http://127.0.0.1:8788")["action"] == "unchanged"


def test_wire_claude_base_url_preserves_other_settings(tmp_path, monkeypatch):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"env": {"FOO": "bar"}, "other": 1}))
    monkeypatch.setenv("AXIOM_CLAUDE_SETTINGS", str(p))
    inst.wire_claude_base_url("http://127.0.0.1:8788")
    data = json.loads(p.read_text())
    assert data["env"]["FOO"] == "bar" and data["other"] == 1
    assert data["env"]["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:8788"


def test_ensure_ingress_service_idempotent_when_running(monkeypatch):
    import axiom.infra.services as services

    calls = {"install": 0, "start": 0}

    class _FakeMgr:
        def __init__(self, *a, **k):
            self._svc = SimpleNamespace(service_id="com.axiom.gateway-ingress")
            self.provider_name = "fake"
        def status(self):
            return SimpleNamespace(status=services.ServiceStatus.RUNNING)
        def install(self):
            calls["install"] += 1
        def start(self):
            calls["start"] += 1

    monkeypatch.setattr(services, "ServiceManager", _FakeMgr)
    r = inst.ensure_ingress_service()
    assert r["action"] == "running"
    assert calls == {"install": 0, "start": 0}  # no-op when already up


def test_ensure_ingress_service_starts_when_down(monkeypatch):
    import axiom.infra.services as services

    calls = {"install": 0, "start": 0}

    class _FakeMgr:
        def __init__(self, *a, **k):
            self._svc = SimpleNamespace(service_id="com.axiom.gateway-ingress")
            self.provider_name = "fake"
        def status(self):
            return SimpleNamespace(status=services.ServiceStatus.STOPPED)
        def install(self):
            calls["install"] += 1
        def start(self):
            calls["start"] += 1

    monkeypatch.setattr(services, "ServiceManager", _FakeMgr)
    assert inst.ensure_ingress_service()["action"] == "started"
    assert calls == {"install": 1, "start": 1}


def test_install_default_is_mcp_only_no_model_redirect(tmp_path, monkeypatch):
    """Plain install must NOT start the ingress or repoint the IDE's model —
    safe to run in the IDE you develop with."""
    _point_paths_at(tmp_path, monkeypatch)
    # Fail loudly if the service is touched without --route-model.
    monkeypatch.setattr(inst, "ensure_ingress_service",
                        lambda *, dry_run=False: (_ for _ in ()).throw(AssertionError("must not run")))
    r = inst.install(tools=["claude-code"])
    assert r["results"]["claude-code"]["action"] == "added"  # MCP registered
    assert "base_url" not in r["results"]["claude-code"]      # model NOT redirected
    assert r["ingress"]["action"] == "skipped"
    assert not (tmp_path / "claude-settings.json").exists()   # settings untouched


def test_install_route_model_full_path_idempotent(tmp_path, monkeypatch):
    _point_paths_at(tmp_path, monkeypatch)  # ingress mocked to 'running'
    r1 = inst.install(tools=["claude-code"], route_model=True)
    assert r1["results"]["claude-code"]["action"] == "added"
    assert r1["results"]["claude-code"]["base_url"] == "added"
    assert r1["ingress"]["action"] == "running"
    # second run: everything a no-op
    r2 = inst.install(tools=["claude-code"], route_model=True)
    assert r2["results"]["claude-code"]["action"] == "unchanged"
    assert r2["results"]["claude-code"]["base_url"] == "unchanged"


# --- EC client-capability matrix + env stamping -----------------------------


def test_ec_capable_matrix(monkeypatch):
    # Cursor is never EC-capable (model not routable), even with --route-model.
    cursor = inst._SPEC_BY_NAME["cursor"]
    assert inst._ec_capable(cursor, route_model=True) is False
    assert inst._ec_capable(cursor, route_model=False) is False
    # Claude Code: EC-capable ONLY when routed.
    cc = inst._SPEC_BY_NAME["claude-code"]
    assert inst._ec_capable(cc, route_model=True) is True
    assert inst._ec_capable(cc, route_model=False) is False


def test_client_env_stamps_identity_and_ec_capability():
    cc = inst._SPEC_BY_NAME["claude-code"]
    env_routed = inst._client_env(cc, route_model=True)
    assert env_routed["AXIOM_MCP_CLIENT"] == "claude-code"
    assert env_routed["AXIOM_MCP_CLIENT_EC_CAPABLE"] == "true"
    env_unrouted = inst._client_env(cc, route_model=False)
    assert env_unrouted["AXIOM_MCP_CLIENT_EC_CAPABLE"] == "false"
    cursor = inst._SPEC_BY_NAME["cursor"]
    assert inst._client_env(cursor, route_model=True)["AXIOM_MCP_CLIENT_EC_CAPABLE"] == "false"


def test_install_writes_ec_capability_into_client_env(tmp_path, monkeypatch):
    _point_paths_at(tmp_path, monkeypatch)
    inst.install(tools=["cursor"])
    cfg = json.loads((tmp_path / "cursor-mcp.json").read_text())
    env = next(iter(cfg["mcpServers"].values()))["env"]
    assert env["AXIOM_MCP_CLIENT"] == "cursor"
    assert env["AXIOM_MCP_CLIENT_EC_CAPABLE"] == "false"  # cursor never EC-capable


# --- VS Code Copilot BYOK endpoint wiring -----------------------------------


def test_wire_vscode_byok_writes_customendpoint_array(tmp_path, monkeypatch):
    p = tmp_path / "chatLanguageModels.json"
    monkeypatch.setenv("AXIOM_VSCODE_CHATMODELS", str(p))
    r = inst.wire_vscode_byok("http://127.0.0.1:8788")
    assert r["action"] == "added"
    data = json.loads(p.read_text())
    assert isinstance(data, list)  # top-level ARRAY (VS Code BYOK schema)
    prov = data[0]
    assert prov["vendor"] == "customendpoint"
    assert prov["apiType"] == "chat-completions"
    m = prov["models"][0]
    assert m["url"] == "http://127.0.0.1:8788/v1/chat/completions"  # per-model url, full path
    assert m["toolCalling"] is True  # else hidden from agent picker
    # idempotent
    assert inst.wire_vscode_byok("http://127.0.0.1:8788")["action"] == "unchanged"


def test_wire_vscode_byok_preserves_other_providers(tmp_path, monkeypatch):
    p = tmp_path / "chatLanguageModels.json"
    p.write_text(json.dumps([{"name": "My Other", "vendor": "anthropic", "models": []}]))
    monkeypatch.setenv("AXIOM_VSCODE_CHATMODELS", str(p))
    inst.wire_vscode_byok("http://127.0.0.1:8788")
    names = [x["name"] for x in json.loads(p.read_text())]
    assert "My Other" in names and "Axiom Gateway" in names


def test_install_vscode_route_model_wires_byok(tmp_path, monkeypatch):
    _point_paths_at(tmp_path, monkeypatch)
    monkeypatch.setenv("AXIOM_VSCODE_CHATMODELS", str(tmp_path / "chatLanguageModels.json"))
    r = inst.install(tools=["vscode"], route_model=True)
    assert r["results"]["vscode"]["chat_models"] == "added"
    assert (tmp_path / "chatLanguageModels.json").exists()


def test_install_vscode_default_does_not_wire_byok(tmp_path, monkeypatch):
    _point_paths_at(tmp_path, monkeypatch)
    monkeypatch.setenv("AXIOM_VSCODE_CHATMODELS", str(tmp_path / "chatLanguageModels.json"))
    r = inst.install(tools=["vscode"])  # no route_model
    assert "chat_models" not in r["results"]["vscode"]
    assert not (tmp_path / "chatLanguageModels.json").exists()


# --- EC stamp requires endpoint wiring actually implemented -------------------


def test_ec_capable_requires_endpoint_wiring_implemented():
    # codex is model_routable but has NO endpoint wiring yet -> NOT EC-capable
    # even with --route-model (would falsely disable the withhold gate otherwise).
    codex = inst._SPEC_BY_NAME["codex"]
    assert codex.model_routable is True
    assert inst._ec_capable(codex, route_model=True) is False
    # claude-code + vscode ARE wired -> EC-capable when routed.
    assert inst._ec_capable(inst._SPEC_BY_NAME["claude-code"], route_model=True) is True
    assert inst._ec_capable(inst._SPEC_BY_NAME["vscode"], route_model=True) is True


def test_install_codex_route_model_not_stamped_ec_capable(tmp_path, monkeypatch):
    _point_paths_at(tmp_path, monkeypatch)
    r = inst.install(tools=["codex"], route_model=True)
    # endpoint not wired -> stamped non-EC (fail-closed), gate stays active.
    assert r["results"]["codex"]["ec_capable"] == "false"
