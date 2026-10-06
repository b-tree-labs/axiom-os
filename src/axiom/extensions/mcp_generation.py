# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Auto-MCP config generation from extension manifests (#29).

Each extension declares its MCP server(s) in ``axiom-extension.toml``:

    [mcp_servers.axiom-classroom]
    command = "python"
    args = ["-m", "axiom.extensions.builtins.classroom.mcp_server"]

Rather than having the user hand-edit their MCP client config,
``axi mcp generate`` collates every enabled extension's declared
servers into a target client config (Claude Code ``.mcp.json`` today).
User-added entries are preserved; extension-declared entries always
win on name collision (the manifest is the source of truth).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from enum import Enum
from pathlib import Path
from typing import Any

from axiom.extensions.contracts import Extension

log = logging.getLogger(__name__)


class MCPTarget(str, Enum):
    """Supported MCP client config formats."""

    CLAUDE_CODE = "claude_code"   # .mcp.json at project root
    CURSOR = "cursor"             # .cursor/mcp.json
    CLAUDE_DESKTOP = "claude_desktop"  # ~/Library/.../claude_desktop_config.json


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------


def _server_to_claude_code(server) -> dict[str, Any]:
    """Render one MCPServerDef in Claude Code's expected shape."""
    entry: dict[str, Any] = {
        "command": server.command,
        "args": list(server.args or []),
    }
    if server.env:
        entry["env"] = dict(server.env)
    return entry


def build_mcp_config(
    extensions: Iterable[Extension],
    target: MCPTarget = MCPTarget.CLAUDE_CODE,
) -> dict[str, Any]:
    """Collate every enabled extension's MCP servers into a client config.

    Disabled extensions are skipped. Extensions without any declared MCP
    servers contribute nothing. The output is always a well-formed
    config for ``target``, even when the input is empty.
    """
    servers: dict[str, dict[str, Any]] = {}
    for ext in extensions:
        if not getattr(ext, "enabled", True):
            continue
        if not getattr(ext, "mcp_servers", None):
            continue
        for key, server_def in ext.mcp_servers.items():
            if target in (MCPTarget.CLAUDE_CODE, MCPTarget.CURSOR, MCPTarget.CLAUDE_DESKTOP):
                servers[key] = _server_to_claude_code(server_def)
            else:  # pragma: no cover — defensive
                raise ValueError(f"unsupported MCP target: {target}")

    # Claude Code, Cursor, and Claude Desktop all use the ``mcpServers`` key.
    return {"mcpServers": servers}


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------


def merge_mcp_config(
    existing: dict[str, Any], generated: dict[str, Any],
) -> dict[str, Any]:
    """Merge a generated MCP config into an existing one.

    Preserves entries the user added manually; extension-generated
    entries win on same-name collision so an outdated ``command`` in
    the user's file can't mask a fresh manifest.
    """
    out_servers: dict[str, Any] = dict(existing.get("mcpServers", {}))
    out_servers.update(generated.get("mcpServers", {}))
    merged = dict(existing)
    merged["mcpServers"] = out_servers
    return merged


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------


def write_mcp_config(
    path: Path,
    extensions: Iterable[Extension],
    target: MCPTarget = MCPTarget.CLAUDE_CODE,
) -> dict[str, Any]:
    """Generate the MCP config for ``extensions`` and write it to ``path``.

    If ``path`` exists, merge with the existing content so user-added
    entries are preserved. Returns the final merged config that was
    written. Never removes user-added server entries.
    """
    existing: dict[str, Any] = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(existing, dict):
                log.warning("existing MCP config at %s is not an object; replacing", path)
                existing = {}
        except json.JSONDecodeError as exc:
            log.warning("existing MCP config at %s is invalid JSON (%s); replacing", path, exc)
            existing = {}

    generated = build_mcp_config(extensions, target=target)
    merged = merge_mcp_config(existing, generated)

    # Atomic write via tmp file + rename so a partial write can't corrupt the config.
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    return merged
