# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Read a file under the repository root."""

from __future__ import annotations

from axiom import REPO_ROOT as _REPO_ROOT
from axiom.infra.orchestrator.actions import ActionCategory

from ..tools import ToolDef

TOOLS = [
    ToolDef(
        name="read_file",
        description="Read the contents of a file under the repository root.",
        category=ActionCategory.READ,
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path from repo root (e.g., 'docs/prds/prd_foo.md').",
                },
            },
            "required": ["path"],
        },
    ),
]


def execute(name: str, params: dict) -> dict:
    """Read a file and return its content (truncated to 8000 chars)."""
    if name != "read_file":
        return {"error": f"Unknown tool: {name}"}

    rel_path = params.get("path", "")
    if not rel_path:
        return {"error": "path parameter is required"}

    target = (_REPO_ROOT / rel_path).resolve()

    # Safety: prevent path traversal above repo root
    try:
        target.relative_to(_REPO_ROOT)
    except ValueError:
        return {"error": "Path is outside the repository."}

    if not target.exists():
        return {"error": f"File not found: {rel_path}"}
    if not target.is_file():
        return {"error": f"Not a file: {rel_path}"}

    try:
        content = target.read_text(encoding="utf-8")
        truncated = len(content) > 8000
        if truncated:
            content = content[:8000] + "\n... (truncated)"
        return {
            "path": rel_path,
            "content": content,
            "size": target.stat().st_size,
            "truncated": truncated,
        }
    except Exception as e:
        return {"error": f"Could not read {rel_path}: {e}"}
