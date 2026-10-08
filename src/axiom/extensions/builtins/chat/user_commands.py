# Copyright (c) 2026 B-Tree Ventures, LLC
# SPDX-License-Identifier: Apache-2.0

"""User-defined slash commands for the chat surface.

Mirrors Claude Code's ``.claude/commands/*.md`` pattern. Each ``.md``
file in either of two locations becomes a ``/<filename>`` command whose
body is a prompt template injected into the chat agent:

  - User scope:    ``$AXI_STATE_DIR/commands/*.md``  (e.g. ``~/.axi/commands/``)
  - Project scope: ``<project_root>/.<cli_name>/commands/*.md``

On name collision, project scope wins (matches Claude Code's behavior
and lets a repo customize commands without editing user globals).

File format::

    ---
    description: One-line description shown in /help
    argument-hint: <hint>
    ---
    Body of the prompt. Use $ARGUMENTS for whatever the user typed
    after the command name.

Frontmatter is optional; if absent or unparseable the body is still
loaded and a fallback description ("user command: <name>") is used.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

log = logging.getLogger(__name__)

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)


@dataclass(frozen=True)
class UserCommand:
    name: str
    description: str
    body: str
    source: Path
    scope: Literal["user", "project"]
    argument_hint: str = ""


@dataclass(frozen=True)
class UserCommandPrompt:
    """Sentinel returned by ``try_dispatch_user_command`` when a match is
    found. The caller is expected to route ``prompt`` through the chat
    agent's normal turn pipeline rather than printing it as a static
    response."""

    command_name: str
    prompt: str


def _user_commands_dir() -> Path | None:
    try:
        from axiom.infra.paths import get_user_state_dir

        return get_user_state_dir() / "commands"
    except Exception:
        return None


def _project_commands_dir() -> Path | None:
    try:
        from axiom.infra.branding import get_branding
        from axiom.infra.paths import get_project_root

        return get_project_root() / f".{get_branding().cli_name}" / "commands"
    except Exception:
        return None


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Return (metadata, body). Metadata is empty on absent/invalid frontmatter."""
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}, text
    raw_meta, body = match.group(1), match.group(2)
    meta: dict[str, str] = {}
    for line in raw_meta.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        # Only accept simple scalar key:value lines. Skip anything that
        # looks like nested YAML — we don't need a full YAML dependency
        # for this surface and a malformed file should still expose its body.
        if key in {"description", "argument-hint", "argument_hint"}:
            meta[key] = value
    return meta, body.lstrip("\n")


def _load_dir(directory: Path | None, scope: Literal["user", "project"]) -> dict[str, UserCommand]:
    if directory is None or not directory.is_dir():
        return {}
    out: dict[str, UserCommand] = {}
    for path in sorted(directory.glob("*.md")):
        try:
            text = path.read_text()
        except OSError as exc:
            log.warning("user_commands: cannot read %s: %s", path, exc)
            continue
        meta, body = _parse_frontmatter(text)
        name = path.stem
        out[name] = UserCommand(
            name=name,
            description=meta.get("description") or f"user command: {name}",
            body=body,
            source=path,
            scope=scope,
            argument_hint=meta.get("argument-hint") or meta.get("argument_hint") or "",
        )
    return out


def load_user_commands() -> dict[str, UserCommand]:
    """Return ``{name: UserCommand}`` merged from user + project scopes.

    Project scope wins on name collision.
    """
    merged: dict[str, UserCommand] = {}
    merged.update(_load_dir(_user_commands_dir(), "user"))
    merged.update(_load_dir(_project_commands_dir(), "project"))
    return merged


def render_command(cmd: UserCommand, args: str) -> str:
    """Substitute ``$ARGUMENTS`` with the user's args. Bodies without the
    placeholder are returned unchanged (matches Claude Code)."""
    if "$ARGUMENTS" not in cmd.body:
        return cmd.body
    return cmd.body.replace("$ARGUMENTS", args)


def try_dispatch_user_command(command_line: str) -> UserCommandPrompt | None:
    """Look up ``/<name>`` in user-defined commands. Returns a prompt to
    inject into the agent when matched, ``None`` otherwise.

    Argument splitting: anything after the first whitespace becomes
    ``$ARGUMENTS`` verbatim (no shell-style parsing — users pass natural
    language)."""
    if not command_line.startswith("/"):
        return None
    stripped = command_line[1:]
    if not stripped:
        return None
    if " " in stripped:
        name, args = stripped.split(" ", 1)
    else:
        name, args = stripped, ""
    cmds = load_user_commands()
    cmd = cmds.get(name)
    if cmd is None:
        return None
    return UserCommandPrompt(
        command_name=name,
        prompt=render_command(cmd, args),
    )


__all__ = [
    "UserCommand",
    "UserCommandPrompt",
    "load_user_commands",
    "render_command",
    "try_dispatch_user_command",
]
