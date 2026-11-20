# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Bundled prompt-template commands, in the same file format users write (ADR-168).

A bundled command is an ordinary ``.md`` prompt template shipped as package data, parsed by the same
``user_commands`` code that reads ``~/.axi/commands/*.md``. So a user copies it, edits it, and drops it in
their commands directory under the same name: that file wins over the bundled one (``resolve``).
"""

from __future__ import annotations

from pathlib import Path

from ..user_commands import UserCommand, _parse_frontmatter, load_user_commands

TEMPLATE_DIR = Path(__file__).parent / "templates"


def bundled_path(name: str) -> Path:
    return TEMPLATE_DIR / f"{name}.md"


def bundled(name: str) -> UserCommand:
    path = bundled_path(name)
    meta, body = _parse_frontmatter(path.read_text(encoding="utf-8"))
    return UserCommand(
        name=name,
        description=meta.get("description") or f"bundled command: {name}",
        body=body,
        source=path,
        scope="user",
        argument_hint=meta.get("argument-hint") or "",
    )


def resolve(name: str) -> tuple[UserCommand, bool]:
    """The command to run for ``name`` and whether a user/project file overrode the bundled one."""
    override = load_user_commands().get(name)
    if override is not None:
        return override, True
    return bundled(name), False
