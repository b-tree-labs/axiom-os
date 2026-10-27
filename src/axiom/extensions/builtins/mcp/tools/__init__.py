# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Toolkit for the root MCP server.

This extension does not contribute tools back to itself
(``[extension.mcp] enabled = false``). Callers that need to inspect a
surface already built by the library use these functions instead of
reading ``MCPSurface`` fields. The library is the package public API
(``AggregationRegistry``, ``MCPSurface``, and the other names in
``__all__`` of the extension).
"""

from __future__ import annotations

from typing import Any

__all__ = ["describe_surface", "tool_owner"]


def describe_surface(surface: Any) -> dict[str, Any]:
    """Plain description of a built surface, plus a tool count.

    ``surface`` is an ``MCPSurface`` (or anything with ``to_dict()``).
    """
    data = dict(surface.to_dict())
    tools = data.get("tools") or []
    data["tool_count"] = len(tools)
    return data


def tool_owner(surface: Any, tool_name: str) -> str | None:
    """Which contribution owns ``tool_name``, or None when the surface has no such tool.

    Delegates to ``MCPSurface.handler_source``.
    """
    return surface.handler_source(tool_name)
