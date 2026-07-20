# Copyright (c) 2026 B-Tree Ventures, LLC
# SPDX-License-Identifier: Apache-2.0

"""Per-tool permission state for the chat agent.

Three modes per tool name:

  ``allow``  — execute without prompting
  ``ask``    — fall through to the existing ApprovalGate prompt (default)
  ``deny``   — refuse without prompting

Set during a chat session by the user choosing ``A`` (Always allow) or
``D`` (Always deny) at the approval prompt, or via ``/permissions``.
Lifecycle is per-session; cross-session persistence is a future enhancement
(would land in ``$AXI_STATE_DIR/tool_permissions.json``).
"""

from __future__ import annotations

from typing import Literal

PermissionMode = Literal["allow", "ask", "deny"]
_VALID_MODES: tuple[str, ...] = ("allow", "ask", "deny")


class ToolPermissions:
    """In-memory per-tool permission map. Default mode is ``ask``."""

    def __init__(self) -> None:
        self._mode: dict[str, PermissionMode] = {}

    def get(self, tool: str) -> PermissionMode:
        return self._mode.get(tool, "ask")

    def set(self, tool: str, mode: PermissionMode) -> None:
        if mode not in _VALID_MODES:
            raise ValueError(
                f"unknown permission mode {mode!r} (expected one of {_VALID_MODES})"
            )
        self._mode[tool] = mode

    def reset(self, tool: str | None = None) -> None:
        """Clear one tool's setting, or all if ``tool`` is None."""
        if tool is None:
            self._mode.clear()
        else:
            self._mode.pop(tool, None)

    def all(self) -> dict[str, PermissionMode]:
        """Return only tools with explicit settings."""
        return dict(self._mode)


def format_permissions(perms: ToolPermissions) -> str:
    """Render the permission table for ``/permissions``."""
    items = perms.all()
    if not items:
        return (
            "\n  No tool overrides set.\n"
            "  All tools default to: ask (write tools prompt; read tools auto-approve).\n"
        )
    lines = ["", "  Tool permissions:"]
    for name, mode in sorted(items.items()):
        lines.append(f"    {name:30s}  {mode}")
    lines.append("")
    return "\n".join(lines)


__all__ = ["PermissionMode", "ToolPermissions", "format_permissions"]
