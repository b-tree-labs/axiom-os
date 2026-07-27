# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Workspace context detection for neut chat.

Detects model.yaml in the current working directory (or a parent) and
builds a context string that is injected into the system prompt and
shown in the welcome banner.
"""

from __future__ import annotations

from pathlib import Path


def detect_workspace_context() -> str:
    """Detect model.yaml in cwd and build context string for system prompt."""
    model_yaml = Path.cwd() / "model.yaml"
    if not model_yaml.exists():
        # Check parent dirs (user might be in a subdirectory)
        for parent in Path.cwd().parents:
            if (parent / "model.yaml").exists():
                model_yaml = parent / "model.yaml"
                break
        else:
            return ""

    try:
        import yaml

        data = yaml.safe_load(model_yaml.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return ""

        model_id = data.get("model_id", "unknown")
        reactor = data.get("reactor_type", "")
        code = data.get("physics_code", "")
        version = data.get("version", "")
        materials = data.get("materials", [])
        input_files = data.get("input_files", [])
        description = data.get("description", "")

        ctx_parts = [f"Working on model: {model_id} v{version} ({reactor} {code})"]
        if description and not description.startswith("TODO"):
            ctx_parts.append(f"Description: {description}")
        if materials:
            mat_names = [
                m.get("name", f"m{m.get('number', '?')}") if isinstance(m, dict) else str(m)
                for m in materials
            ]
            ctx_parts.append(f"Materials: {', '.join(mat_names)}")
        if input_files:
            file_names = [f.get("path", "") if isinstance(f, dict) else str(f) for f in input_files]
            ctx_parts.append(f"Input files: {', '.join(file_names)}")

        return "\n".join(ctx_parts)
    except Exception:
        return ""


def workspace_summary_line(ctx: str) -> str:
    """Extract a one-line summary from workspace context for the banner.

    Returns empty string if *ctx* is empty.
    """
    if not ctx:
        return ""
    # First line is always the "Working on model: ..." line
    return ctx.splitlines()[0]
