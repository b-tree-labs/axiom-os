# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""End-to-end stdio round-trip against the real MCP SDK client.

Spec: ``docs/specs/spec-builtin-mcp-server.md`` §12.6.

Spawns ``python -m axiom.extensions.builtins.mcp.server`` as a
subprocess, drives it with the upstream ``mcp.client.stdio.ClientSession``,
and verifies the surface a real peer harness would see. This is the
load-bearing contract test: anything Claude Code / Cursor / Goose
exercises starts here.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[7]


pytestmark = [
    pytest.mark.timeout(30),
    pytest.mark.skip(
        reason=(
            "Depends on the 29 axiom-extension.toml manifests having "
            "[extension.mcp] blocks declared, and on the hygiene/signals "
            "mcp_handlers shipped in Branch D "
            "(feat/hygiene-signals-mcp-handlers). Will re-enable when those "
            "follow-on extractions land."
        )
    ),
]


def _spawn_env(tmp_axiom_home: Path) -> dict[str, str]:
    """Build a child-process environment that points axiom at the sandbox."""
    env = os.environ.copy()
    env["AXIOM_HOME"] = str(tmp_axiom_home)
    # Ensure the worktree's src/ is importable inside the subprocess —
    # the venv resolves `axiom` against the main checkout otherwise and
    # the MCP module wouldn't be visible to the child interpreter.
    src_dir = REPO_ROOT / "src"
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        f"{src_dir}{os.pathsep}{existing}" if existing else str(src_dir)
    )
    return env


async def _list_tools_via_stdio(env: dict[str, str]) -> list[str]:
    """Spawn the server, run ``initialize`` + ``list_tools``, return tool names."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "axiom.extensions.builtins.mcp.server"],
        env=env,
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            response = await session.list_tools()
            return [tool.name for tool in response.tools]


def test_stdio_roundtrip_lists_platform_tools(tmp_axiom_home):
    """A real mcp client over stdio sees all 7 platform tools."""
    env = _spawn_env(tmp_axiom_home)
    names = asyncio.run(_list_tools_via_stdio(env))

    # Every spec-§8 platform tool must surface to a real client.
    expected = {
        "axiom_memory__compose",
        "axiom_memory__retrieve",
        "axiom_memory__list",
        "axiom_federation__node_status",
        "axiom_rag__retrieve",
        "axiom_signals__brief",
        "axiom_node__hooks_list",
    }
    missing = expected - set(names)
    assert not missing, f"missing platform tools over stdio: {missing}"


def test_stdio_roundtrip_includes_block_b_extension_tools(tmp_axiom_home):
    """Memory + signals + hygiene contributions show up over the wire."""
    env = _spawn_env(tmp_axiom_home)
    names = asyncio.run(_list_tools_via_stdio(env))

    # The three Block-B extensions should advertise their `_ext` tools.
    assert "axiom_memory_ext__show" in names, names
    assert "axiom_signals_ext__status" in names, names
    assert "axiom_hygiene_ext__status" in names, names


async def _call_tool_via_stdio(
    env: dict[str, str], name: str, args: dict
) -> str:
    """Spawn the server, call one tool, return the first text content."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "axiom.extensions.builtins.mcp.server"],
        env=env,
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(name, args)
            # Return the first text-shaped chunk so the caller can json.loads.
            for content in result.content:
                if hasattr(content, "text"):
                    return content.text
            return ""


def test_stdio_roundtrip_call_tool_returns_payload(tmp_axiom_home):
    """A round-trip ``call_tool`` against ``axiom_node__hooks_list`` works."""
    import json

    env = _spawn_env(tmp_axiom_home)
    text = asyncio.run(
        _call_tool_via_stdio(env, "axiom_node__hooks_list", {})
    )
    assert text, "expected a non-empty TextContent payload"
    payload = json.loads(text)
    # Real wiring: the response carries the hook list (may be empty on a
    # bare tmp_axiom_home, but the structure must be present).
    assert "hooks" in payload, payload
