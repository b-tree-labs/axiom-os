# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""MCP toolkit and the library it is built on.

The root server does not contribute tools to itself (``[extension.mcp]
enabled = false``), so the toolkit is not a ``kind = "tool"`` block. It is
``tools/``: ``describe_surface`` and ``tool_owner``, which call the public
library (``MCPSurface.to_dict`` and ``handler_source``) instead of letting
callers read the fields. The library remains the package ``__all__``.

Cmd, hook and service entries live in their AEOS kind directories. The old
module paths stay importable.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

EXT = Path(__file__).resolve().parents[1]


def test_kind_entries_point_at_kind_directories() -> None:
    manifest = tomllib.loads((EXT / "axiom-extension.toml").read_text(encoding="utf-8"))
    expected = {
        "cmd": "axiom.extensions.builtins.mcp.commands.cli:main",
        "hook": "axiom.extensions.builtins.mcp.hooks.subscriber:on_extension_changed",
        "service": "axiom.extensions.builtins.mcp.services.http_mount:mcp_mount_spec",
    }
    found = {}
    for block in manifest["extension"]["provides"]:
        kind = block.get("kind")
        if kind in expected:
            found[kind] = block["entry"]
    assert found == expected
    assert (EXT / "commands" / "cli.py").is_file()
    assert (EXT / "hooks" / "subscriber.py").is_file()
    assert (EXT / "services" / "http_mount.py").is_file()


def test_compatibility_paths_resolve() -> None:
    from axiom.extensions.builtins.mcp import cli, http_mount, subscriber
    from axiom.extensions.builtins.mcp.commands import cli as cli_impl
    from axiom.extensions.builtins.mcp.hooks import subscriber as hook_impl
    from axiom.extensions.builtins.mcp.services import http_mount as service_impl

    assert cli.main is cli_impl.main
    assert subscriber.on_extension_changed is hook_impl.on_extension_changed
    assert http_mount.mcp_mount_spec is service_impl.mcp_mount_spec


def test_toolkit_describes_a_surface_through_the_library() -> None:
    from axiom.extensions.builtins.mcp.tools import describe_surface, tool_owner

    surface = SimpleNamespace(
        to_dict=lambda: {
            "tools": [{"name": "axiom_demo__ping", "description": "ping"}],
            "resources": [],
            "prompts": [],
            "sources": [{"kind": "extension", "name": "demo", "tool_names": ["axiom_demo__ping"]}],
        },
        handler_source=lambda name: "demo" if name == "axiom_demo__ping" else None,
    )
    described = describe_surface(surface)
    assert described["tool_count"] == 1
    assert described["tools"][0]["name"] == "axiom_demo__ping"
    assert tool_owner(surface, "axiom_demo__ping") == "demo"
    assert tool_owner(surface, "missing") is None


def test_library_all_includes_the_toolkit() -> None:
    pytest.importorskip("mcp")
    from axiom.extensions.builtins.mcp import __all__ as names

    assert "describe_surface" in names
    assert "tool_owner" in names
    assert "AggregationRegistry" in names
