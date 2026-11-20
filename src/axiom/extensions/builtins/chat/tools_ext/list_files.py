# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""List files in a directory under the repository root."""

from __future__ import annotations

from axiom import REPO_ROOT as _REPO_ROOT
from axiom.infra.orchestrator.actions import ActionCategory

from ..tools import ToolDef

TOOLS = [
    ToolDef(
        name="list_files",
        description="List files in a directory under the repository root.",
        category=ActionCategory.READ,
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative directory path from repo root (default: root).",
                },
                "pattern": {
                    "type": "string",
                    "description": "Glob pattern to filter files (default: '*').",
                },
            },
        },
    ),
]


def execute(name: str, params: dict) -> dict:
    """List files in a directory with optional glob filtering."""
    if name != "list_files":
        return {"error": f"Unknown tool: {name}"}

    rel_path = params.get("path", ".")
    pattern = params.get("pattern", "*")

    target = (_REPO_ROOT / rel_path).resolve()

    # Safety: prevent path traversal
    try:
        target.relative_to(_REPO_ROOT)
    except ValueError:
        return {"error": "Path is outside the repository."}

    if not target.exists():
        return {"error": f"Directory not found: {rel_path}"}
    if not target.is_dir():
        return {"error": f"Not a directory: {rel_path}"}

    try:
        entries = sorted(target.glob(pattern))
        files = []
        dirs = []
        for e in entries[:100]:  # Cap at 100 entries
            rel = e.relative_to(_REPO_ROOT)
            if e.is_dir():
                dirs.append(str(rel) + "/")
            elif e.is_file():
                files.append(str(rel))

        return {
            "path": rel_path,
            "directories": dirs,
            "files": files,
            "total": len(dirs) + len(files),
        }
    except Exception as e:
        return {"error": f"Could not list {rel_path}: {e}"}
