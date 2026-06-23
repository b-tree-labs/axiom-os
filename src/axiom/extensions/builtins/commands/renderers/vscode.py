# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""VS Code renderer.

VS Code added native MCP support in 2025. We emit:

- `.vscode/mcp.json` — registers the Axiom MCP server (Copilot Chat + any
  other MCP-aware extension picks it up automatically).
- `.vscode/tasks.json` — one task per CLI verb, so users can run any
  `axi <noun> <verb>` from the Command Palette via *Tasks: Run Task*.
  The palette filter (`> task`) gives a quick-search experience that's
  the closest VS Code analog to a slash command.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..discovery import CommandTree


def _task_entry(noun: str, verb_name: str, help_text: str, args: tuple[str, ...]) -> dict:
    arg_inputs: list[dict] = []
    for a in args:
        arg_inputs.append({"id": a, "type": "promptString", "description": a})
    return {
        "label": f"axi {noun} {verb_name}".strip(),
        "type": "shell",
        "command": "axi",
        "args": [noun, *([verb_name] if verb_name else []), *[f"${{input:{a}}}" for a in args]],
        "presentation": {"reveal": "always", "panel": "dedicated"},
        "detail": help_text or f"axi {noun} {verb_name}",
        "_axi_inputs": [a["id"] for a in arg_inputs],  # surfaces inputs for merge
    }


def render(tree: CommandTree, out_dir: Path) -> list[Path]:
    base = out_dir / ".vscode"
    base.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    # mcp.json
    mcp_path = base / "mcp.json"
    mcp_config = {
        "servers": {
            "axiom": {
                "type": "stdio",
                "command": "axi",
                "args": ["mcp", "serve"],
            }
        }
    }
    mcp_path.write_text(json.dumps(mcp_config, indent=2) + "\n", encoding="utf-8")
    written.append(mcp_path)

    # tasks.json
    tasks: list[dict] = []
    inputs_seen: dict[str, dict] = {}
    for noun, cli_noun in sorted(tree.nouns.items()):
        if not cli_noun.verbs:
            tasks.append(_task_entry(noun, "", cli_noun.description, ()))
            continue
        for verb in cli_noun.verbs:
            entry = _task_entry(noun, verb.name, verb.help, verb.args)
            for arg in entry.pop("_axi_inputs"):
                inputs_seen.setdefault(
                    arg, {"id": arg, "type": "promptString", "description": arg}
                )
            tasks.append(entry)

    tasks_path = base / "tasks.json"
    tasks_payload = {
        "version": "2.0.0",
        "tasks": tasks,
        "inputs": list(inputs_seen.values()),
    }
    tasks_path.write_text(
        json.dumps(tasks_payload, indent=2) + "\n", encoding="utf-8"
    )
    written.append(tasks_path)

    return written
