# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""OpenCode renderer (sst/opencode TUI).

OpenCode supports MCP servers via its config file. We register the Axiom
MCP server so any OpenCode session sees Axiom's tools natively. OpenCode's
slash-command surface is built-in (no user-defined slash commands as of
this writing); per-verb shims aren't applicable, similar to Codex.

Config path: project-local `.opencode/opencode.json` is preferred for
project-scoped registration; users can copy the resulting block into
`~/.config/opencode/opencode.json` for global use.

Note: OpenCode's MCP config schema may evolve. Verify against current docs
if integration regresses.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..discovery import CommandTree


def render(tree: CommandTree, out_dir: Path) -> list[Path]:
    del tree  # OpenCode has no per-verb shim surface
    base = out_dir / ".opencode"
    base.mkdir(parents=True, exist_ok=True)
    config_path = base / "opencode.json"

    existing: dict = {}
    if config_path.exists():
        try:
            existing = json.loads(config_path.read_text(encoding="utf-8") or "{}")
        except json.JSONDecodeError:
            existing = {}

    mcp = existing.setdefault("mcp", {})
    servers = mcp.setdefault("servers", {})
    servers["axiom"] = {
        "command": "axi",
        "args": ["mcp", "serve"],
    }

    config_path.write_text(
        json.dumps(existing, indent=2) + "\n", encoding="utf-8"
    )
    return [config_path]
