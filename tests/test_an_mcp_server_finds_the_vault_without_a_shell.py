# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""An MCP server a desktop app starts, with no shell behind it, still finds the vault.

Found in a colleague's first hour: the vault was configured only by three variables a shell profile
exports. A desktop AI app or an editor starts the MCP server without that profile, so the server
found no vault, sent no key, and the bound node answered 403 "does not hold the medallion" to a
person who had done every step right.

The settings now live in the platform's own config (``~/.axi/config/secrets.toml``: backend, URL,
and the token as the path of an owner-only file). This starts the real MCP server over stdio with an
environment holding nothing but ``HOME``, a bare ``PATH`` and the interpreter's path, against a fake
vault and a fake node on loopback, and makes the call a harness would make.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from axiom.extensions.builtins.mcp import clean_env_doctor
from axiom.extensions.builtins.secrets import vault_settings

SRC = Path(__file__).resolve().parents[1] / "src"
BAO_TOKEN = "bao-token-for-the-test"  # noqa: S105 - a fake vault's fake token
NODE_KEY = "axk_test_reader_key"
SITE = "alpha-site"

pytestmark = pytest.mark.timeout(240)


def _serve(handler: type[BaseHTTPRequestHandler]) -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


class _Vault(BaseHTTPRequestHandler):
    """OpenBao kv/v2, holding one key, for one token."""

    def do_GET(self):  # noqa: N802
        if self.headers.get("X-Vault-Token") != BAO_TOKEN:
            return self._send(403, {"errors": ["permission denied"]})
        if self.path.split("?")[0] != "/v1/kv/data/alpha-reader":
            return self._send(404, {"errors": []})
        return self._send(200, {"data": {"data": {"value": NODE_KEY}, "metadata": {"version": 1}}})

    def _send(self, code: int, body: dict) -> None:
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *_):
        pass


class _Node(_Vault):
    """The bound node's data read: answers only the key the binding names."""

    asked: list = []

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("content-length") or 0)
        self.asked.append(json.loads(self.rfile.read(length) or b"{}"))
        if self.headers.get("authorization") != f"Bearer {NODE_KEY}":
            return self._send(403, {"detail": "this credential does not hold the medallion"})
        return self._send(200, {"ok": True, "value": {"data": {"answered_by": "node"}}})


@pytest.fixture
def machine(tmp_path, monkeypatch):
    """A laptop's home: bound to the node, the vault named in the platform's own config."""
    vault, vault_url = _serve(_Vault)
    node, node_url = _serve(_Node)
    home = tmp_path / "home"
    state = home / ".axi"
    state.mkdir(parents=True)
    (state / "site-bindings.json").write_text(json.dumps({
        "current": SITE,
        "bindings": {SITE: {"site": SITE, "face_url": node_url, "token_ref": "openbao://kv/data/alpha-reader"}},
    }), encoding="utf-8")
    token_file = state / "openbao" / "axi-token"
    token_file.parent.mkdir()
    token_file.write_text(BAO_TOKEN, encoding="utf-8")
    token_file.chmod(0o600)

    def write_settings() -> Path:
        monkeypatch.setenv("AXI_STATE_DIR", str(state))
        try:
            return vault_settings.write(backend="openbao", url=vault_url, token_file=str(token_file))
        finally:
            monkeypatch.delenv("AXI_STATE_DIR")

    env = {"HOME": str(home), "PATH": "/usr/bin:/bin", "PYTHONPATH": str(SRC)}
    yield type("Machine", (), {"home": home, "env": env, "write_settings": staticmethod(write_settings),
                               "token_file": token_file, "node": _Node})
    vault.shutdown()
    node.shutdown()


async def _call_tool(env: dict[str, str], tool: str, args: dict) -> dict:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=sys.executable, args=["-m", "axiom.extensions.builtins.mcp.server"], env=env
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool, args)
            return json.loads(result.content[0].text)


def test_the_settings_file_holds_no_secret(machine):
    path = machine.write_settings()
    text = path.read_text(encoding="utf-8")
    assert BAO_TOKEN not in text and str(machine.token_file) in text
    assert oct(path.stat().st_mode & 0o777) == "0o600"


def test_a_token_file_others_can_read_is_refused(machine):
    machine.token_file.chmod(0o644)
    with pytest.raises(vault_settings.VaultSettingsError):
        machine.write_settings()


def test_an_mcp_server_with_an_empty_environment_reads_through_the_vault(machine):
    machine.write_settings()
    assert not any(k.startswith("AXIOM_") for k in machine.env)

    result = asyncio.run(_call_tool(machine.env, "axiom_data__roles", {}))

    assert result.get("ok"), result
    assert result["value"]["data"]["answered_by"] == "node"


def test_mcp_doctor_reports_the_vault_and_the_binding_from_a_clean_environment(machine):
    before = clean_env_doctor.rows(clean_env_doctor.probe(env=machine.env))
    assert not all(r.ok for r in before), before
    assert any(r.name == "Binding key" and not r.ok for r in before)

    machine.write_settings()
    after = clean_env_doctor.rows(clean_env_doctor.probe(env=machine.env))
    assert all(r.ok for r in after), after
    named = {r.name: r.status for r in after}
    assert named["Vault token"] == "from config"
    assert named["Binding key"] == "resolves"
    assert BAO_TOKEN not in json.dumps([r.__dict__ for r in after])
    assert NODE_KEY not in json.dumps([r.__dict__ for r in after])


def test_the_doctor_starts_from_what_a_desktop_app_passes(monkeypatch):
    monkeypatch.setenv("AXIOM_OPENBAO_TOKEN", "from-a-shell")
    env = clean_env_doctor.clean_env()
    assert "AXIOM_OPENBAO_TOKEN" not in env
    assert env["PATH"] == "/usr/bin:/bin"
    assert os.environ.get("HOME") == env.get("HOME")
